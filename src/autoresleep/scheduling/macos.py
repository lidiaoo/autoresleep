"""macOS scheduling: launchd LaunchAgents in ~/Library/LaunchAgents.

Per-user agents (not /Library/LaunchDaemons): the judge has to read the interactive session's
input idle, exactly like the Windows task and the Linux user unit. No sudo, nothing outside
the home directory.

`StartInterval` is the portable equivalent of a repeating timer. macOS has no "just resumed"
trigger for a user agent; the listener daemon detects the resume itself (autoresleep.resume)
and the judge's interval picks up the decision within one tick.

ProgramArguments are built by the SHARED launcher helper, so a checkout on a Mac runs
`<python> <workdir>/autoresleep.py judge` and an installed package falls back to
`<python> -m autoresleep judge` - one argv entry per token, XML-escaped.
"""
from __future__ import annotations

import shlex
from typing import List, Optional
from xml.sax.saxutils import escape as xml_escape

from .artifact import Artifact, launcher_invocation

PREFIX = "com.autoresleep"
#: the LaunchAgent must ACT (see the Linux note): the CLI default is decide-only
JUDGE_MODE = "judge --apply"
AGENT_DIR = "~/Library/LaunchAgents"

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
{extra}
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""

JUDGE_EXTRA = """  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>Nice</key><integer>5</integer>"""

LISTENER_EXTRA = """  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>Nice</key><integer>5</integer>"""


def program_arguments(python_exe: str, workdir: str, mode: str,
                      config_path: Optional[str]) -> List[str]:
    """One argv entry per token - launchd does not go through a shell either."""
    return shlex.split(launcher_invocation(python_exe, workdir, mode, config_path))


def macos_artifacts(python_exe: str, workdir: str, interval_minutes: int,
                    config_path: Optional[str] = None,
                    log_path: Optional[str] = None) -> List[Artifact]:
    log = log_path or "~/Library/Logs/AutoReSleep.log"
    plan = [
        (PREFIX + ".judge", JUDGE_MODE, JUDGE_EXTRA.format(interval=int(interval_minutes) * 60)),
        (PREFIX + ".listener", "listen", LISTENER_EXTRA),
    ]
    artifacts: List[Artifact] = []
    for label, mode, extra in plan:
        argv = program_arguments(python_exe, workdir, mode, config_path)
        args = "".join("\n    <string>%s</string>" % xml_escape(tok) for tok in argv)
        content = PLIST.format(label=label, args=args, extra=extra, log=log)
        short = "autoresleep-" + ("judge" if mode.split()[0] == "judge" else "listener")
        artifacts.append(Artifact(
            name=short,
            path="%s/%s.plist" % (AGENT_DIR, label),
            content=content,
            kind="plist",
            apply_cmd=["launchctl", "bootstrap", "gui/$(id -u)", "%s/%s.plist" % (AGENT_DIR, label)],
            remove_cmd=["launchctl", "bootout", "gui/$(id -u)/%s" % label],
            note="LaunchAgent in %s, no sudo needed" % AGENT_DIR,
        ))
    return artifacts


def apply_hint() -> str:
    return ("launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/%s.judge.plist\n"
            "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/%s.listener.plist\n"
            "# check: launchctl print gui/$(id -u)/%s.judge" % (PREFIX, PREFIX, PREFIX))