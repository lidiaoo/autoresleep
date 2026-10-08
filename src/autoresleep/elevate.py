"""Getting the privileged half of an install done without sending the operator for a terminal.

Three platforms, three mechanisms, one contract. `docs/install-pitfalls.md` is the acceptance test
for this module, and these are the rules it lays down:

  * **the elevated child never asks anything.** Every answer travels as an internal argument
    (`--elevated`, `--config`, `--no-firewall`), because an environment variable does not survive
    UAC, and a `Read-Host`/`input()` inside a window nobody can see is a hang, not a question.
  * **the child leaves a line-based, timestamped progress file**, and the parent prints the new
    lines in the operator's own console while it waits. "Silence, while the UAC box may be hiding
    behind this window" is the failure that cost the most time on the real box.
  * **success or failure is decided by READING that file**, never by "I saw no output".
  * Windows: `ShellExecuteW(..., "runas", ...)` with SW_HIDE - no PowerShell quoting to get wrong,
    and no window to click on. Linux: `pkexec` (graphical prompt) then `sudo` (a tty) then print
    the exact command. macOS: `osascript ... with administrator privileges`.

The child is the same program with `--elevated`: the privileged steps are a subset of `install` /
`uninstall`, so there is no second implementation to drift out of sync with the first.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

#: how often the parent re-reads the progress file
PROGRESS_POLL_S = 0.5
#: no new line for this long -> say something useful (a UAC box can be behind the window)
PROGRESS_SILENT_S = 20.0
#: give up entirely after this
PROGRESS_TIMEOUT_S = 900.0


def is_elevated() -> bool:
    """Are we already running with the privileges we need?"""
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except Exception:
        return False


class Progress:
    """The child's record and the parent's only source of truth.

    Line-based and timestamped on purpose: the parent must be able to read it WHILE the child runs,
    which `Start-Transcript` cannot do (and on the real box it hung all by itself).
    """

    def __init__(self, path):
        self.path = Path(path)

    def say(self, message: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
                fh.flush()
        except Exception:
            pass

    def start(self, what: str) -> None:
        self.say("start %s as=%s" % (what, "elevated" if is_elevated() else "user"))

    def step(self, what: str) -> None:
        self.say("step  %s" % what)

    def done(self, rc: int) -> None:
        self.say("done  rc=%d" % int(rc))

    # ------------------------------------------------------------------ parent side
    def lines(self) -> List[str]:
        try:
            return [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:
            return []

    def clear(self) -> None:
        try:
            self.path.unlink()
        except Exception:
            pass


def _rc_from(lines: Sequence[str]) -> Optional[int]:
    for line in reversed(list(lines)):
        body = line.split("  ", 1)[-1].strip()
        if body.startswith("done") and "rc=" in body:
            try:
                return int(body.split("rc=", 1)[1].split()[0])
            except Exception:
                return None
    return None


def child_argv(argv: Sequence[str], progress_path, config_path: Optional[str] = None) -> List[str]:
    """The command the elevated child runs: the same work, marked internal.

    `--elevated` means "you are the privileged half: do the privileged steps only, ask nothing" and
    `--progress` is where it says what it is doing. Everything else is forwarded, so the child sees
    the same configuration the operator chose.
    """
    out = list(argv) + ["--elevated", "--progress", str(progress_path)]
    if config_path and "--config" not in out:
        out += ["--config", str(config_path)]
    return out


def launch_argv(argv: Sequence[str]) -> List[str]:
    """`autoresleep <child argv>` as a real command line, using the checkout's launcher."""
    return [sys.executable, str(_launcher())] + list(argv)


def _launcher() -> Path:
    # src/autoresleep/elevate.py -> the checkout root holds the launcher script
    return Path(__file__).resolve().parents[2] / "autoresleep.py"


def _start(cmd: Sequence[str], progress_path, out: Callable[[str], None]) -> Tuple[bool, str]:
    """Start the child WITH PRIVILEGES, returning (started, mechanism). Never asks anything."""
    if os.name == "nt":
        import ctypes
        # ShellExecuteW + "runas": the UAC prompt, then the process runs hidden (SW_HIDE). Using
        # PowerShell's Start-Process here would mean quoting an argument list inside a command
        # inside a string - three chances to get it wrong, all of them silent.
        params = subprocess.list2cmdline(list(cmd[1:]))
        try:
            rc = int(ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd[0], params, None, 0))
        except Exception as exc:
            out("  [error] could not ask for elevation: %s" % exc)
            return False, "failed"
        if rc <= 32:
            # 5 = access denied (the operator said no to UAC); anything <= 32 is a failure
            out("  [error] elevation was refused or failed (ShellExecuteW rc=%d)" % rc)
            return False, "refused"
        return True, "ShellExecuteW(runas)"
    if sys.platform == "darwin":
        import json
        quoted = " ".join(_shell_quote(part) for part in cmd)
        script = ("do shell script %s with administrator privileges" % json.dumps(quoted))
        try:
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            out("  [error] could not ask for elevation: %s" % exc)
            return False, "failed"
        return True, "osascript(with administrator privileges)"
    # POSIX: pkexec first (a desktop session gets a graphical prompt and the child inherits
    # nothing, so everything must be on the command line), then sudo, then print the command.
    # stdout/stderr go to the progress file and nothing else, so the only thing this terminal shows
    # is sudo's own password prompt on /dev/tty.
    if shutil.which("pkexec"):
        try:
            subprocess.Popen(["pkexec"] + list(cmd), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            return True, "pkexec"
        except Exception as exc:
            out("  [error] pkexec failed: %s" % exc)
            return False, "failed"
    if shutil.which("sudo"):
        try:
            subprocess.Popen(["sudo"] + list(cmd), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True, "sudo"
        except Exception as exc:
            out("  [error] sudo failed: %s" % exc)
            return False, "failed"
    out("  could not elevate automatically: neither pkexec nor sudo is installed.")
    out("  run this as root, or install pkexec:")
    out("    " + " ".join(_shell_quote(p) for p in cmd))
    return False, "no-mechanism"


def _shell_quote(text: str) -> str:
    if not text or any(ch in text for ch in " \t\"'\\$`&|;<>()*?[]{}!"):
        return "'" + text.replace("'", "'\\''") + "'"
    return text


def delegate(argv: Sequence[str], progress_path, out: Callable[[str], None] = print,
             config_path: Optional[str] = None, label: str = "privileged steps",
             allow: bool = True) -> Optional[int]:
    """Run the privileged half elevated, reading its progress out loud. Returns its rc, or None.

    None means "it never produced a verdict" - the caller must treat that as a failure and NOT as
    success, and it must say what to do about it.

    `AUTORESLEEP_NO_ELEVATE=1` refuses here as a last line of defence: a test suite, a CI job or a
    sandbox must never make a UAC / pkexec prompt appear on somebody's desktop, and that guard
    belongs at the choke point rather than in every caller.
    """
    if not allow or os.environ.get("AUTORESLEEP_NO_ELEVATE", "").strip().lower() in (
            "1", "true", "yes", "on"):
        out("  elevation is switched off (AUTORESLEEP_NO_ELEVATE); nothing was started.")
        return None
    progress = Progress(progress_path)
    progress.clear()
    cmd = launch_argv(child_argv(argv, progress_path, config_path))
    out("")
    out("  requesting administrator privileges for: %s" % label)
    out("  (a UAC / authentication prompt may appear - if it is hidden, check the taskbar)")
    started, how = _start(cmd, progress_path, out)
    if not started:
        return None
    out("  elevated via %s; reading its progress:" % how)
    return _wait(progress, out)


def _wait(progress: Progress, out: Callable[[str], None], silent_s: float = PROGRESS_SILENT_S,
          timeout_s: float = PROGRESS_TIMEOUT_S) -> Optional[int]:
    """Print new progress lines as they appear; return the child's rc, or None if it never said."""
    seen = 0
    started = time.time()
    last_new = started
    warned = False
    while True:
        lines = progress.lines()
        for line in lines[seen:]:
            seen += 1
            last_new = time.time()
            warned = False
            out("  " + line)
        rc = _rc_from(lines)
        if rc is not None:
            return rc
        now = time.time()
        if now - started > timeout_s:
            out("  [error] the elevated step produced no verdict within %.0fs" % timeout_s)
            out("          last progress line: %s" % (lines[-1] if lines else "(none)"))
            return None
        if not warned and now - last_new > silent_s:
            warned = True
            if seen == 0:
                out("  waiting: nothing has started yet. If a UAC prompt is open, answer it;")
                out("           if the prompt was dismissed, re-run and choose 'Yes'.")
            else:
                out("  waiting: no new progress for %.0fs (it may be waiting on the OS)" % silent_s)
        time.sleep(PROGRESS_POLL_S)