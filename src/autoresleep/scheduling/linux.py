"""Linux scheduling: systemd *user* units.

User units, not system ones, because the judge has to read the interactive session's input
idle - the same reason v1 had to register its task as the logged-on user. Everything lands in
~/.config/systemd/user and is applied with `systemctl --user`, so no root, no sudo, and no
files outside the user's home.

Resume handling on Linux: no hook needed (that is the point of autoresleep.resume). logind's
PrepareForSleep signal would be available for a user session, but the wall/monotonic skew is
visible from inside the daemon on every distro without D-Bus policy questions, so the tool does
not depend on it. A `systemd-sleep` hook would need root, so it is deliberately not used.
"""
from __future__ import annotations

from typing import List, Optional

from .artifact import Artifact, launcher_invocation

PREFIX = "autoresleep"
#: the timer must ACT: a bare `judge` only decides (the CLI default) and would report success
#: forever without ever suspending anything
JUDGE_MODE = "judge --apply"
USER_UNIT_DIR = "~/.config/systemd/user"

JUDGE_SERVICE = """[Unit]
Description=AutoReSleep judge - decide whether to go back to sleep
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=oneshot
ExecStart={cmd}
# a judge run must never be killed halfway through a suspend call
TimeoutStartSec=120
"""

JUDGE_TIMER = """[Unit]
Description=AutoReSleep judge timer - every {interval} min, plus right after boot/resume

[Timer]
OnBootSec=1min
OnUnitActiveSec={interval}min
AccuracySec=15s
Persistent=false

[Install]
WantedBy=timers.target
"""

LISTENER_SERVICE = """[Unit]
Description=AutoReSleep listener - magic packet / marker detection, stays resident
After=network-online.target graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={cmd}
Restart=always
RestartSec=5
# the daemon sleeps between polls; it must not be throttled into uselessness
Nice=5
OOMPolicy=continue

[Install]
WantedBy=default.target
"""


def _home_units(name: str) -> str:
    return "%s/%s" % (USER_UNIT_DIR, name)


def linux_artifacts(python_exe: str, workdir: str, interval_minutes: int,
                    config_path: Optional[str] = None) -> List[Artifact]:
    cmd_judge = launcher_invocation(python_exe, workdir, JUDGE_MODE, config_path)
    cmd_listen = launcher_invocation(python_exe, workdir, "listen", config_path)

    services = [
        (PREFIX + "-judge.service", JUDGE_SERVICE.format(cmd=cmd_judge)),
        (PREFIX + "-judge.timer", JUDGE_TIMER.format(interval=interval_minutes)),
        (PREFIX + "-listener.service", LISTENER_SERVICE.format(cmd=cmd_listen)),
    ]

    artifacts: List[Artifact] = []
    for name, content in services:
        kind = "timer" if name.endswith(".timer") else "unit"
        enable = ("systemctl --user enable --now " + name) if kind == "timer" else \
                 ("systemctl --user enable --now " + name)
        artifacts.append(Artifact(
            name=name,
            path=_home_units(name),
            content=content,
            kind=kind,
            apply_cmd=["systemctl", "--user", "daemon-reload", "&&", "systemctl", "--user",
                       "enable", "--now", name],
            start_cmd=["systemctl", "--user", "start", name],
            remove_cmd=["systemctl", "--user", "disable", "--now", name],
            note="user unit, no root needed (%s)" % USER_UNIT_DIR,
        ))
    return artifacts


def apply_hint() -> str:
    return ("systemctl --user daemon-reload && systemctl --user enable --now "
            "autoresleep-judge.timer autoresleep-listener.service\n"
            "# keep it alive without an active login (optional, needs root once):\n"
            "#   sudo loginctl enable-linger $USER")