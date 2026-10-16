"""macOS: ioreg for idle, pmset for sleeping and for saying why it woke up.

Idle: `ioreg -c IOHIDSystem` publishes HIDIdleTime in nanoseconds - the same number the
system's own idle timer uses, and readable without root or a window server connection.

Suspend: `pmset sleepnow`. `pmset -g assertions` is checked first, because a Mac holding an
idle-sleep assertion (a film playing, a backup running) is doing what it was told.

Wake source: `pmset -g log` records "Wake from ..." together with the reason, including whether
Wi-Fi was the trigger. It is a big log, so only the tail is read, and the matching line is kept
verbatim in `raw` - the classification on top of it is a convenience, never the evidence.

What macOS can and cannot say (the reason this file has no WoWLAN reader like Linux's): `pmset`
prints `due to <CODE>/<CLASS>`, where the CODE names the HARDWARE that fired the wake - `ARPT`
(Apple's wifi node), `GLAN` (the gigabit LAN node = wired Ethernet), `EHC`/`OHC`/`XHC` (USB), `RTC`,
`LidOpen`, `PowerButton` - and the CLASS is a coarse category (`Network`, `USB`, `Alarm`). There is
no packet-level cause anywhere in it: macOS does not report "this was a magic packet" the way an
Intel wifi driver reports it through nl80211, so a network wake here is judged exactly like Linux's
minus the firmware report (packets heard by the listener, then hands, then the link). The
OS-side equivalent of "arm magic packets only" is `sudo pmset -a womp 1` (plus turning off
`tcpkeepalive`/`powernap`, which are the reason a Mac wakes on its own every couple of hours);
`pmset -g assertions` printing `MAGICWAKE` is how you check it is armed.
"""
from __future__ import annotations

import os
from typing import Optional

import re
import shutil
import subprocess
from typing import List, Sequence

from ..decide import Guards
from ..pkt import adapters
from .base import FirewallStatus, IdleInfo, System, WakeSource

SOCKS = ("ARPT", "en0", "en1", "WiFi", "Wi-Fi", "Airport", "wl0")

#: The wired-side node names, checked AFTER the wireless ones: on a Mac `GLAN` is the on-board
#: gigabit LAN node (`en0` on a Mac mini is the same wire, but on a MacBook `en0` is the Wi-Fi
#: interface - so the wireless set keeps the ambiguous names and this one only adds what is
#: unambiguously a cable). Without this, a wired wake-on-LAN wake came out as `other-device`, and
#: the engine leaves other-device wakes alone by design - i.e. a wired WoL woke the machine and it
#: never re-slept.
WIRED = ("GLAN", "Ethernet", "以太网")


def _run(cmd: Sequence[str], timeout: float = 25.0) -> str:
    """Hidden execution + this-host decoding, same contract as everywhere else."""
    try:
        from .base import run_hidden
        return run_hidden(cmd, timeout)
    except Exception:
        return ""


class MacSystem(System):
    name = "darwin"
    display_name = "macOS"
    firewall_supported = True

    # ------------------------------------------------------------------ idle
    def idle(self) -> IdleInfo:
        out = _run(["ioreg", "-c", "IOHIDSystem"])
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if not m:
            return IdleInfo(-1.0, method="ioreg IOHIDSystem", error="HIDIdleTime not found")
        nanos = int(m.group(1))
        return IdleInfo(seconds=nanos / 1_000_000_000.0, tick=m.group(1),
                        method="ioreg IOHIDSystem HIDIdleTime (ns -> s)")

    # ------------------------------------------------------------------ suspend
    def suspend(self) -> str:
        blockers = self._assertions().get("blockers", [])
        if blockers:
            raise RuntimeError("idle-sleep assertions held by: " + "; ".join(blockers))
        _run(["pmset", "sleepnow"])
        return "pmset sleepnow"

    def _assertions(self) -> dict:
        out = _run(["pmset", "-g", "assertions"])
        info = {"display": False, "system": False, "audio": False, "blockers": []}
        for line in out.splitlines():
            low = line.lower()
            if "preventuseridledisplaysleep" in low and "1" in line.split()[-1:]:
                info["display"] = True
            if "preventuseridlesystemsleep" in low and "1" in line.split()[-1:]:
                info["system"] = True
            if "audio" in low and "prevent" in low:
                info["audio"] = True
            if "named:" in low and ("prevent" in low):
                info["blockers"].append(line.strip()[:120])
        return info

    # ------------------------------------------------------------------ wake source
    def wake_source(self) -> WakeSource:
        out = _run(["pmset", "-g", "log"])
        lines = out.splitlines()
        raw = ""
        reason = ""
        # scan backwards: the newest wake is the interesting one
        for i in range(len(lines) - 1, -1, -1):
            if "Wake from" in lines[i]:
                raw = lines[i].strip()
                for j in range(i, min(i + 8, len(lines))):
                    if lines[j].strip().startswith("Reason:"):
                        reason = lines[j].strip()
                        raw += " | " + reason
                        break
                break
        if not raw:
            return WakeSource(kind="unknown", method="pmset -g log")

        low = (raw + " " + reason).lower()
        wifi_names = [a.label or a.name for a in adapters() if a.wireless]
        eth_names = [a.label or a.name for a in adapters() if not a.wireless]
        who = ""
        if "rtc" in low or "maintenance" in low or "alarm" in low or "timer" in low:
            kind, who = "timer", "rtc/maintenance"
        elif any(s.lower() in low for s in SOCKS) or any(n.lower() in low for n in wifi_names):
            kind = "wifi-nic"
            who = "name:" + next((n for n in wifi_names if n.lower() in low), "wifi")
        elif any(s.lower() in low for s in WIRED) or any(n.lower() in low for n in eth_names):
            kind = "eth-nic"
            who = "name:" + next((n for n in eth_names if n.lower() in low), "glan")
        elif "usb" in low or "hid" in low or "bluetooth" in low:
            kind, who = "other-device", "usb/hid/bt"
        else:
            kind = "other-device"
        return WakeSource(kind=kind, who=who, raw=raw, timer=kind == "timer",
                          method="pmset -g log (tail)")

    # ------------------------------------------------------------------ link (driver-free)
    def link_state(self):
        """`airport -I` is a userspace Apple tool, not a driver query; networksetup is the fallback.

        NOTE: written from documentation, not measured on a Mac - the parsing is deliberately
        forgiving and returns connected=None when the output is unrecognised, which the engine
        reads as "cannot tell" rather than as a fact.
        """
        iface = "en0"
        airport = ("/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/"
                   "Resources/airport")
        out = _run([airport, "-I"]) if os.path.exists(airport) else ""
        if out.strip():
            connected: Optional[bool] = None
            ssid = bssid = ""
            for line in out.splitlines():
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if key == "state":
                    connected = value.startswith("running")
                elif key == "SSID":
                    ssid = value
                elif key == "BSSID":
                    bssid = value.lower()
            if bssid:
                connected = True
            return {"iface": iface, "connected": connected, "ssid": ssid, "bssid": bssid,
                    "changes": None, "method": "airport -I"}
        out = _run(["networksetup", "-getairportnetwork", iface])
        m = re.search(r":\s*(.+)$", out.strip())
        ssid = m.group(1).strip() if m else ""
        if not ssid and "not associated" in out.lower():
            return {"iface": iface, "connected": False, "ssid": "", "bssid": "", "changes": None,
                    "method": "networksetup -getairportnetwork"}
        return {"iface": iface, "connected": bool(ssid) or None, "ssid": ssid, "bssid": "",
                "changes": None, "method": "networksetup -getairportnetwork"}

    # ------------------------------------------------------------------ guards
    def guards(self) -> Guards:
        a = self._assertions()
        return Guards(remote_in_use=self._remote_sessions(),
                      display_in_use=a["display"],
                      media_playing=a["audio"],
                      update_running=False)

    def _remote_sessions(self) -> bool:
        out = _run(["who"])
        for line in out.splitlines():
            if "(" in line and "console" not in line.lower():
                return True
        ps = _run(["ps", "-Ao", "comm"])
        return bool(re.search(r"/sshd\s*$", ps, re.M))

    # ------------------------------------------------------------------ firewall
    def firewall(self) -> FirewallStatus:
        state_out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
        enabled = "enabled" in state_out.lower()
        binary = self._python_binary()
        hints = []
        if enabled:
            hints.append("sudo %s --add %s && sudo %s --unblockapp %s"
                         % (SOCKETFW, binary, SOCKETFW, binary))
            hints.append("the socket filter only gates incoming connections to signed apps; "
                         "verify with a real magic packet from another host")
        return FirewallStatus(state="enabled" if enabled else "disabled",
                              detail=state_out.strip() or "socketfilterfw not available",
                              hints=hints)

    def firewall_apply(self, ports: List[int], marker_port: int, program: str) -> List[str]:
        # macOS's application firewall is per-binary, not per-port: allow the interpreter once
        if shutil.which(SOCKETFW) is None:
            return []
        _run([SOCKETFW, "--add", program])
        _run([SOCKETFW, "--unblockapp", program])
        return ["%s --unblockapp %s" % (SOCKETFW, program)]

    def firewall_remove(self) -> List[str]:
        if shutil.which(SOCKETFW) is None:
            return []
        _run([SOCKETFW, "--remove", self._python_binary()])
        return ["%s --remove %s" % (SOCKETFW, self._python_binary())]

    def _python_binary(self) -> str:
        import sys
        return sys.executable


SOCKETFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"