"""Linux: the same three jobs, done with the tools a normal desktop already has.

Idle, honestly: there is no one API. Order of preference, best first -
  1. Wayland/GNOME: the compositor knows the seat's idle time (gdbus -> org.gnome.Mutter.IdleMonitor)
  2. X11: XScreenSaver's idle query (libXss via ctypes)
  3. `/dev/input/*` mtime: the kernel's own "when did the last input event arrive" - crude but
     it cannot be reset by power requests, which is the property the whole tool depends on.

Suspend: logind via `systemctl suspend`. Inhibitors are checked first: if something holds a
sleep inhibitor, refusing to suspend and logging why is the correct behaviour, not a bug.

Wake source: the kernel names its wakeup source in dmesg/journalctl ("Wakeup source: ..."), and
`/sys/power/pm_wakeup_irq` says which IRQ did it. There is no per-frame reason code on any
platform, so the classification here is deliberately coarse and always keeps the raw line.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..decide import Guards
from ..pkt import Adapter, adapters
from .base import FirewallStatus, IdleInfo, System, WakeSource

WIRELESS_DRIVERS = ("iwlwifi", "iwlmvm", "ath10k", "ath11k", "ath9k", "rtw88", "rtw89",
                    "mt76", "brcmfmac", "rtl8xxxu", "rtl8188", "wl", "rtl8821")
TIMER_SOURCES = ("rtc", "rtc0", "hpet")


def _run(cmd: Sequence[str], timeout: float = 15.0) -> str:
    """Hidden execution + this-host decoding, same contract as everywhere else."""
    try:
        from .base import run_hidden
        return run_hidden(cmd, timeout)
    except Exception:
        return ""


class LinuxSystem(System):
    name = "linux"
    display_name = "Linux (systemd/X11/Wayland)"
    firewall_supported = True

    # ------------------------------------------------------------------ idle
    def idle(self) -> IdleInfo:
        for probe in (self._idle_wayland, self._idle_x11, self._idle_devinput):
            try:
                info = probe()
            except Exception as exc:
                info = IdleInfo(-1.0, method=probe.__name__, error=str(exc))
            if info.seconds >= 0:
                return info
        return IdleInfo(-1.0, method="none", error="no idle source available")

    def _idle_wayland(self) -> IdleInfo:
        if not os.environ.get("WAYLAND_DISPLAY"):
            return IdleInfo(-1.0, method="wayland", error="not a Wayland session")
        out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                    "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                    "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
        m = re.search(r"\(?(?:uint64\s+)?(\d+)", out)
        if not m:
            return IdleInfo(-1.0, method="wayland", error="gdbus returned nothing usable")
        return IdleInfo(seconds=int(m.group(1)) / 1000.0, method="Mutter.IdleMonitor (ms -> s)")

    def _idle_x11(self) -> IdleInfo:
        if not os.environ.get("DISPLAY"):
            return IdleInfo(-1.0, method="x11", error="no DISPLAY")
        libname = ctypes.util.find_library("Xss") or "libXss.so.1"
        libx = ctypes.util.find_library("X11") or "libX11.so.6"
        try:
            x11 = ctypes.CDLL(libx)
            xss = ctypes.CDLL(libname)
        except OSError as exc:
            return IdleInfo(-1.0, method="x11", error=str(exc))

        class XSCREENSAVERINFO(ctypes.Structure):
            _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int),
                        ("kind", ctypes.c_int), ("til_or_since", ctypes.c_ulong),
                        ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]

        x11.XOpenDisplay.restype = ctypes.c_void_p
        display = x11.XOpenDisplay(None)
        if not display:
            return IdleInfo(-1.0, method="x11", error="XOpenDisplay failed (another user's session?)")
        try:
            root = x11.XDefaultRootWindow(ctypes.c_void_p(display))
            info = XSCREENSAVERINFO()
            if not xss.XScreenSaverQueryInfo(ctypes.c_void_p(display), root, ctypes.byref(info)):
                return IdleInfo(-1.0, method="x11", error="XScreenSaverQueryInfo failed")
            return IdleInfo(seconds=info.idle / 1000.0, method="XScreenSaverQueryInfo (X11)")
        finally:
            try:
                x11.XCloseDisplay(ctypes.c_void_p(display))
            except Exception:
                pass

    def _idle_devinput(self) -> IdleInfo:
        """Newest mtime among /dev/input/event*: crude, but unresetable by power requests.

        Needs read permission; if the device is unreadable the mtime is still readable, which
        is exactly why this works from an unprivileged process.
        """
        newest = 0.0
        candidates = glob.glob("/dev/input/event*") + glob.glob("/dev/input/mouse*")
        for dev in candidates:
            try:
                newest = max(newest, os.stat(dev).st_mtime)
            except OSError:
                continue
        if newest <= 0:
            return IdleInfo(-1.0, method="devinput", error="no readable /dev/input device")
        return IdleInfo(seconds=max(0.0, time.time() - newest), tick=str(int(newest)),
                        method="/dev/input/* mtime (kernel input, no privileges)")

    # ------------------------------------------------------------------ suspend
    def suspend(self) -> str:
        blockers = self._inhibitors()
        if blockers:
            raise RuntimeError("sleep inhibitors held by: " + "; ".join(blockers))
        if shutil.which("systemctl"):
            _run(["systemctl", "suspend"])
            return "systemctl suspend (logind)"
        raise RuntimeError("no suspend mechanism found (systemd/logind required)")

    def _inhibitors(self) -> List[str]:
        out = _run(["systemd-inhibit", "--list", "--no-pager", "--mode=block"])
        found: List[str] = []
        for line in out.splitlines()[1:]:
            if "sleep" in line.lower() and "block" in line.lower():
                found.append(" ".join(line.split()[:3]))
        return found

    # ------------------------------------------------------------------ wake source
    def wake_source(self) -> WakeSource:
        raw = ""
        if shutil.which("journalctl"):
            out = _run(["journalctl", "-b", "-k", "--no-pager", "-o", "short-iso", "-n", "400"])
            for line in out.splitlines():
                if re.search(r"(?i)wakeup source|pm: suspend exit|wakeup reason", line):
                    raw = line.strip()
        if not raw and shutil.which("dmesg"):
            out = _run(["dmesg"])
            for line in out.splitlines():
                if re.search(r"(?i)wakeup source|pm: suspend exit", line):
                    raw = line.strip()
        irq = ""
        try:
            irq = Path("/sys/power/pm_wakeup_irq").read_text().strip()
        except Exception:
            pass

        if not raw and not irq:
            return WakeSource(kind="unknown", method="journalctl/dmesg + pm_wakeup_irq")

        low = raw.lower()
        who = ""
        kind = "unknown"
        if any(d in low for d in WIRELESS_DRIVERS):
            kind = "wifi-nic"
            who = "driver:" + next(d for d in WIRELESS_DRIVERS if d in low)
        elif any(t in low for t in TIMER_SOURCES):
            kind = "timer"
            who = "rtc"
        elif "usb" in low or "hid" in low:
            kind = "other-device"
            who = "usb/hid"
        elif irq:
            kind = "other-device"
            who = "irq:" + irq
        return WakeSource(kind=kind, who=who, raw=(raw + (" | pm_wakeup_irq=" + irq if irq else "")),
                          timer=kind == "timer", method="journalctl/dmesg + /sys/power/pm_wakeup_irq")

    # ------------------------------------------------------------------ guards
    def guards(self) -> Guards:
        return Guards(remote_in_use=self._remote_sessions(),
                      display_in_use=False,     # fullscreen detection is compositor-specific
                      media_playing=self._media_playing(),
                      update_running=self._update_running())

    def _media_playing(self) -> bool:
        for status in glob.glob("/proc/asound/card*/pcm*/sub*/status"):
            try:
                if "RUNNING" in Path(status).read_text():
                    return True
            except Exception:
                continue
        return False

    def _remote_sessions(self) -> bool:
        out = _run(["who"])
        for line in out.splitlines():
            parts = line.split()
            # `who` appends the source host for remote logins and leaves it out locally
            if len(parts) >= 5 and "(" in line:
                return True
        out = _run(["ss", "-tn", "state", "established"])
        if re.search(r":22\s+", out):
            return True
        return False

    def _update_running(self) -> bool:
        for path in glob.glob("/proc/*/comm"):
            try:
                comm = Path(path).read_text().strip()
            except Exception:
                continue
            if comm in ("packagekitd", "unattended-upgr", "apt", "dnf", "pacman"):
                return True
        return False

    # ------------------------------------------------------------------ firewall
    def firewall(self) -> FirewallStatus:
        if shutil.which("ufw"):
            out = _run(["ufw", "status"])
            state = "enabled" if "active" in out.lower() else "disabled"
            rules = [l for l in out.splitlines() if "udp" in l.lower()]
            return FirewallStatus(
                state=state, detail="ufw: " + ("active" if state == "enabled" else "inactive"),
                rule_names=rules,
                hints=["sudo ufw allow from 192.168.0.0/16 to any port %d proto udp" % p
                       for p in (9, 7, 45678)] if state == "enabled" else [])
        for tool in ("nft", "iptables"):
            if shutil.which(tool):
                out = _run([tool, "list", "rules"] if tool == "nft" else [tool, "-S"])
                return FirewallStatus(state="enabled" if out.strip() else "unknown",
                                      detail="%s present; no user-level API to open a port"
                                             % tool,
                                      hints=["add the listener ports with an explicit rule for "
                                             "this host's subnet (see README, firewall section)"])
        return FirewallStatus(state="unknown", detail="no ufw/nft/iptables found")

    def firewall_apply(self, ports: List[int], marker_port: int, program: str) -> List[str]:
        """Only ufw has a safe one-liner; anything else is printed for the user to apply."""
        if not shutil.which("ufw"):
            return []
        subnet = self._local_subnet()
        done: List[str] = []
        for port in list(ports) + [marker_port]:
            rule = ["ufw", "allow", "from", subnet, "to", "any", "port", str(port), "proto", "udp"]
            _run(rule)
            done.append(" ".join(rule))
        return done

    def _local_subnet(self) -> str:
        out = _run(["ip", "-o", "-4", "addr", "show"])
        m = re.search(r"inet\s+(\d+\.\d+\.\d+)\.\d+/(\d+)", out)
        if m:
            return "%s.0/%s" % (m.group(1), m.group(2))
        return "192.168.0.0/16"

    def firewall_remove(self) -> List[str]:
        if not shutil.which("ufw"):
            return []
        removed = []
        out = _run(["ufw", "status", "numbered"])
        for line in out.splitlines():
            if "udp" in line.lower() and re.search(r"\b(9|7|67|68|45678)\b", line):
                m = re.match(r"\s*\[\s*(\d+)\]", line)
                if m:
                    _run(["ufw", "delete", m.group(1)])
                    removed.append("ufw rule #" + m.group(1))
        return removed