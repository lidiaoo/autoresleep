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

import os

from typing import List, Optional, Tuple

from .artifact import Artifact, launcher_invocation

PREFIX = "autoresleep"
#: the timer must ACT: a bare `judge` only decides (the CLI default) and would report success
#: forever without ever suspending anything
JUDGE_MODE = "judge --apply"
USER_UNIT_DIR = "~/.config/systemd/user"
SYSTEM_UNIT_DIR = "/etc/systemd/system"
SENSOR_UNIT_NAME = PREFIX + "-wol-sensor.service"

WOL_SENSOR_SERVICE = """[Unit]
Description=AutoReSleep WoL sensor - magic packets (UDP 9/7 + ethertype 0x0842) and the hardware input clock
After=network.target

[Service]
Type=simple
# Root-owned unit, NOT a root process. Binding UDP 9/7 needs a privilege a `--user` unit cannot
# have, so the operator keeps their identity and gets exactly one capability. This half only
# records what arrived; deciding and suspending stay in the user listener, because logind refuses
# `suspend` to a process with no session (polkit: allow_inactive = auth_admin_keep), which would
# make a system-unit listener need a polkit rule before it could ever sleep the machine.
User={user}
Group={group}
# `input` is what lets the sensor read /dev/input/event*: the desktop idle counter is reset by a
# resume, so a real hardware event is the only way to tell a keypress wake from a wifi wake.
SupplementaryGroups=input
Environment=HOME={home}
# CAP_NET_BIND_SERVICE: UDP 9/7 are privileged ports.
# CAP_NET_RAW: the bare ethertype 0x0842 magic frames that `etherwake`-style senders use - a
# UDP-only listener never sees them, and it is the frame NICs are most often woken by.
AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_RAW
NoNewPrivileges=no
ExecStart={cmd}
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
"""

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
Description=AutoReSleep judge timer - every {interval} min, plus right after login

[Timer]
# OnActiveSec, not OnBootSec: a --user manager starts at login, so OnBootSec only fires for the
# (rare) case of lingering users whose manager survived a reboot. OnActiveSec counts from the
# moment the timer itself is enabled - the judge runs once, 1 min after install/logon.
OnActiveSec=1min
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


def _is_root() -> bool:
    """Are we root? `os.geteuid` does not exist on Windows, and these Linux artifacts are
    deliberately generated on ANY host - that is what makes them assertable from a Windows box
    (`tests/test_scheduling_artifacts.py` runs the Linux and macOS cases there)."""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def _operator_uid():
    """The human who ran install - which is NOT root when the privileged half is executing.

    The elevated child runs as root, but the sensor unit must run as the OPERATOR (see the unit
    header: "the operator keeps their identity"). `os.getuid()` alone would answer 0 there and the
    unit would come out `User=root` with `Environment=HOME=/root`: the sensor's state would land
    in /root, away from the operator's, and - because CapabilityBoundingSet drops
    CAP_DAC_OVERRIDE - a root process that owns nothing under /root cannot even write it. Hence
    the crash loop. pkexec leaves PKEXEC_UID and sudo leaves SUDO_UID for exactly this reason.
    None when there is no real uid (Windows), so the caller falls back to $USER/$HOME.
    """
    getuid = getattr(os, "getuid", None)
    if _is_root():
        for var in ("PKEXEC_UID", "SUDO_UID"):
            value = os.environ.get(var, "")
            if value.isdigit():
                return int(value)
    return getuid() if getuid is not None else None


def operator_identity() -> Tuple[str, str, str]:
    """(user, group, home) of the operator - the privileged sensor runs as them, not as root."""
    import getpass
    uid = _operator_uid()
    if uid is not None:
        try:
            import grp
            import pwd
            entry = pwd.getpwuid(uid)
            try:
                group = grp.getgrgid(entry.pw_gid).gr_name
            except Exception:
                group = entry.pw_name
            return entry.pw_name, group, entry.pw_dir or os.path.expanduser("~")
        except Exception:
            pass
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
    return user, user, os.environ.get("HOME") or os.path.expanduser("~")


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
        if kind == "timer":
            # the timer carries the judge's [Install] - enabling it is what schedules the judge
            apply_cmd = ["systemctl", "--user", "enable", "--now", name]
        elif name.endswith(".service") and "listener" in name:
            # the listener has [Install]/WantedBy=default.target, so it is enabled directly
            apply_cmd = ["systemctl", "--user", "enable", "--now", name]
        else:
            # judge.service has NO [Install] section - it is a static unit started by its timer
            # (`systemctl enable` on it fails with "no installation config"). It is listed FIRST,
            # so its daemon-reload is what makes systemd see all three freshly written units.
            apply_cmd = ["systemctl", "--user", "daemon-reload"]
        artifacts.append(Artifact(
            name=name,
            path=_home_units(name),
            content=content,
            kind=kind,
            apply_cmd=apply_cmd,
            start_cmd=["systemctl", "--user", "start", name],
            # the static judge.service cannot be disabled ("no installation config") - stop is
            # the only reverse action it accepts; its file is unlinked right after anyway
            remove_cmd=(["systemctl", "--user", "stop", name] if name == PREFIX + "-judge.service"
                        else ["systemctl", "--user", "disable", "--now", name]),
            note="user unit, no root needed (%s)" % USER_UNIT_DIR,
        ))

    # The one root-owned artifact: a system unit that binds the privileged WoL ports as the
    # operator, with a single capability. Without it the packet chain watches only the marker
    # port, and a magic-packet wake gets re-slept because nobody saw the packet arrive.
    user, group, home = operator_identity()
    sensor_path = "%s/%s" % (SYSTEM_UNIT_DIR, SENSOR_UNIT_NAME)
    sudo = [] if _is_root() else ["sudo"]
    artifacts.append(Artifact(
        name=SENSOR_UNIT_NAME,
        path=sensor_path,
        content=WOL_SENSOR_SERVICE.format(
            user=user, group=group, home=home,
            cmd=launcher_invocation(python_exe, workdir, "wol-sensor", config_path)),
        kind="unit",
        # A rebuild, not an overwrite: `systemctl enable --now` on a unit that is already running
        # does NOT restart it, so a re-install with a changed unit body (a new capability, a new
        # ExecStart) would silently keep running the old process. Stop and disable first, then
        # write, then enable - best-effort, because on a first install there is nothing to stop.
        prepare_cmd=[sudo + ["systemctl", "disable", "--now", SENSOR_UNIT_NAME]],
        apply_cmd=sudo + ["systemctl", "daemon-reload"],
        apply_more=[sudo + ["systemctl", "enable", "--now", SENSOR_UNIT_NAME]],
        start_cmd=sudo + ["systemctl", "restart", SENSOR_UNIT_NAME],
        remove_cmd=sudo + ["systemctl", "disable", "--now", SENSOR_UNIT_NAME],
        remove_more=(sudo + ["rm", "-f", sensor_path],
                     sudo + ["systemctl", "daemon-reload"]),
        note="system unit, root-owned; runs as %s with CAP_NET_BIND_SERVICE only" % user,
        needs_root=True,
    ))
    return artifacts


def apply_hint() -> str:
    return ("systemctl --user daemon-reload && systemctl --user enable --now "
            "autoresleep-judge.timer autoresleep-listener.service\n"
            "# keep it alive without an active login (optional, needs root once):\n"
            "#   sudo loginctl enable-linger $USER")

def linger_enabled(user: Optional[str] = None) -> Optional[bool]:
    """Will this user's `--user` units outlive the login session?

    `Linger=no` means the units are torn down at logout, and after a reboot there is nothing
    running until somebody logs in - the machine looks protected while nothing is watching it.
    """
    who = user or os.environ.get("USER") or os.environ.get("LOGNAME")
    if not who:
        return None
    try:
        from ..system.linux import _run
        out = _run(["loginctl", "show-user", who, "-p", "Linger", "--value"], timeout=5.0)
    except Exception:
        return None
    text = (out or "").strip().lower()
    return {"yes": True, "no": False}.get(text)
