"""The one data structure every platform module returns.

Kept in its own module so the platform modules can import it without importing the package
`__init__` that dispatches to them (which is a circular import).
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple


def launcher_parts(python_exe: str, workdir: str, mode: str, config_path: Optional[str] = None,
                   windowless: bool = False) -> Tuple[str, str]:
    """(program, arguments) for an action that is launched directly, with no shell.

    Built structurally rather than by splitting a command line: `shlex.split` in POSIX mode eats
    backslashes, so re-parsing `E:\\ENV\\Python\\pythonw.exe` yields the unusable
    `E:ENVPythonpythonw.exe`. Windows paths must never travel through a shell-quoting round trip.
    """
    exe = python_exe
    if windowless and os.name == "nt":
        candidate = python_exe.replace("python.exe", "pythonw.exe")
        if candidate != python_exe and os.path.exists(candidate):
            exe = candidate
    cfg = ('--config "%s"' % config_path) if config_path else ""
    launcher = os.path.join(workdir, "autoresleep.py")
    if os.path.exists(launcher):
        return exe, ('"%s" %s %s' % (launcher, mode, cfg)).strip()
    return exe, ("-m autoresleep %s %s" % (mode, cfg)).strip()


def launcher_invocation(python_exe: str, workdir: str, mode: str,
                        config_path: Optional[str] = None, windowless: bool = False) -> str:
    """The command line a scheduled job should run - checkout or installed package.

    `-m autoresleep` only resolves when the package is installed, so a job generated from a
    checkout on a machine that never ran `pip install -e .` would fail on every trigger with
    "No module named autoresleep". The launcher script in the repo root fixes sys.path itself,
    so it is preferred whenever it exists. `windowless` picks pythonw.exe (Windows) when that
    interpreter actually exists next to python.exe - this is v1's run-hidden.vbs role.
    """
    exe = python_exe
    if windowless and os.name == "nt":
        candidate = python_exe.replace("python.exe", "pythonw.exe")
        if candidate != python_exe and os.path.exists(candidate):
            exe = candidate
    cfg = ('--config "%s"' % config_path) if config_path else ""
    launcher = os.path.join(workdir, "autoresleep.py")
    if os.path.exists(launcher):
        return ('%s "%s" %s %s' % (exe, launcher, mode, cfg)).strip()
    return ("%s -m autoresleep %s %s" % (exe, mode, cfg)).strip()


def split_invocation(text: str) -> Tuple[str, str]:
    """Split a command line into (program, arguments) - Task Scheduler wants them separately.

    Paths with spaces stay quoted so they survive as a single argument.
    """
    parts = shlex.split(text)
    if not parts:
        return "", ""
    rest = ['"%s"' % p if " " in p else p for p in parts[1:]]
    return parts[0], " ".join(rest)


@dataclass
class Artifact:
    name: str
    path: str                      # absolute target path, or a task name on Windows
    content: str                   # the text to write (empty for Windows: schtasks owns it)
    kind: str                      # 'task' | 'unit' | 'timer' | 'plist'
    apply_cmd: Sequence[str] = field(default_factory=list)
    remove_cmd: Sequence[str] = field(default_factory=list)
    #: further argv's run right after remove_cmd (stop, delete the file, reload the manager)
    remove_more: Sequence[Sequence[str]] = field(default_factory=list)
    #: how to start it NOW, for jobs that only trigger at logon/boot. v1 started the listener as
    #: part of install; without that, nothing runs until the next logon and every chain looks dead.
    start_cmd: Sequence[str] = field(default_factory=list)
    #: further argv's run right after apply_cmd - a *system* manager needs its own daemon-reload
    #: before `enable --now` can see a freshly written unit, and one Artifact carries one command
    apply_more: Sequence[Sequence[str]] = field(default_factory=list)
    #: further argv's run BEFORE apply_cmd, best-effort: "remove whatever is there now, so this is a
    #: REBUILD and not an overwrite". Not politeness - measured behaviour: overwriting a Task
    #: Scheduler definition in place keeps the old principal/triggers/ACL, `launchctl bootstrap` on
    #: an already-bootstrapped label fails outright, and `systemctl enable --now` on a unit that is
    #: already running does NOT restart it - so a changed body would never take effect. Failure here
    #: is reported as a note, not as an error: on a first install there is nothing to remove.
    prepare_cmd: Sequence[Sequence[str]] = field(default_factory=list)
    #: the target is root-owned, so install hands this artifact to the elevated child (`--elevated`),
    #: which writes it directly and applies it - see elevate.py
    needs_root: bool = False
    note: str = ""

    def apply_line(self) -> str:
        return " ".join(self.apply_cmd)

    def remove_line(self) -> str:
        return " ".join(self.remove_cmd)