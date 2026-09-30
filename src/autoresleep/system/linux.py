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
import json
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


def _run_rc(cmd: Sequence[str], timeout: float = 15.0):
    """`_run` plus the exit code, so "asked and nothing is playing" can be told apart from
    "could not ask at all" - the two need opposite defaults."""
    try:
        from .base import decode_console, proc_kwargs
        proc = subprocess.run(list(cmd), capture_output=True, timeout=timeout, **proc_kwargs())
        return int(proc.returncode), decode_console(proc.stdout or b"")
    except Exception:
        return 127, ""


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

    # ------------------------------------------------------------------ link (driver-free)
    # No driver is ever asked why the machine woke. These probes read what the OS already
    # publishes: NetworkManager's view, the kernel's own interface list, and the carrier-change
    # counter - which is the portable answer to "did the wifi drop and come back?", including
    # the case where it re-attached to the SAME access point.
    @staticmethod
    def wireless_interfaces(text: str) -> List[str]:
        """/proc/net/wireless -> interface names (the kernel's list, no driver query)."""
        names: List[str] = []
        for line in text.splitlines()[2:]:
            name = line.split(":")[0].strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def parse_iw_link(text: str) -> Dict[str, object]:
        """`iw dev <if> link` -> association facts.

        Connected: "Connected to 28:d0:ea:.. (on wlan0)" + "SSID: x"
        Not:       "Not connected."
        """
        info: Dict[str, object] = {"connected": None, "ssid": "", "bssid": ""}
        m = re.search(r"Connected to ([0-9a-fA-F:]{17})", text)
        if m:
            info["connected"] = True
            info["bssid"] = m.group(1).lower()
        elif re.search(r"Not connected", text):
            info["connected"] = False
        m = re.search(r"^\s*SSID:\s*(.+)$", text, re.M)
        if m:
            info["ssid"] = m.group(1).strip()
        return info

    @staticmethod
    def carrier_changes(iface: str) -> Optional[int]:
        """Kernel counter: how often this interface's carrier went down/up since boot."""
        try:
            return int(Path("/sys/class/net/%s/carrier_changes" % iface).read_text().strip())
        except Exception:
            return None

    def link_state(self):
        try:
            wl = Path("/proc/net/wireless").read_text()
        except Exception:
            wl = ""
        names = self.wireless_interfaces(wl)
        if not names:
            out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"])
            names = [l.split(":")[0] for l in out.splitlines() if l.strip().endswith(":wifi")]
        if not names:
            return None
        iface = names[0]
        link = self.parse_iw_link(_run(["iw", "dev", iface, "link"]))
        connected = link["connected"]
        if connected is None:
            out = _run(["nmcli", "-t", "-f", "GENERAL.STATE", "device", "show", iface])
            if out.strip():
                connected = "(connected" in out
        return {"iface": iface, "connected": connected, "ssid": link["ssid"],
                "bssid": link["bssid"], "changes": self.carrier_changes(iface),
                "method": "iw + /proc/net/wireless + carrier_changes"}

    def link_events_since(self, ts: float) -> List[str]:
        """NetworkManager's own disconnect/associate lines after `ts` (OS log, not the driver).

        Fallback for kernels without carrier_changes and for the "re-attached to the same AP"
        case. Unreadable journal (no permission) simply means "nothing observed".
        """
        if not shutil.which("journalctl"):
            return []
        out = _run(["journalctl", "--since", "@%d" % int(ts), "-n", "80", "--no-pager",
                    "-o", "short-iso", "-u", "NetworkManager"], timeout=10.0)
        hits = []
        for line in out.splitlines():
            low = line.lower()
            if any(tok in low for tok in ("disconnect", "associat", "deauth", "link is down",
                                          "link is up")):
                hits.append(line.strip())
        return hits

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
        """Is audio actually playing right now?

        `/proc/asound/**/status` is NOT evidence on its own: PipeWire keeps the analog PCM in
        `state: RUNNING` - silently streaming, pointers advancing - with nothing playing at all,
        so the kernel reading alone made this machine look busy forever and it never re-slept.
        Ask the sound server first (it knows which streams are real) and keep the ALSA reading
        as the fallback for machines with no sound server to ask, where RUNNING really does mean
        a client is playing.
        """
        server = self._sound_server_streams()
        if server is not None:
            return server
        for status in glob.glob("/proc/asound/card*/pcm*/sub*/status"):
            try:
                if "RUNNING" in Path(status).read_text():
                    return True
            except Exception:
                continue
        return False

    def _sound_server_streams(self) -> Optional[bool]:
        """True/False from the sound server's own list of streams; None if it cannot be asked."""
        if shutil.which("pactl"):
            rc, out = _run_rc(["pactl", "list", "sink-inputs"], timeout=5.0)
            if rc == 0:
                for block in out.split("Sink Input #")[1:]:
                    if "State: RUNNING" in block or "Corked: no" in block:
                        return True
                return False
        if shutil.which("pw-dump"):
            rc, out = _run_rc(["pw-dump"], timeout=10.0)
            if rc == 0:
                return self.playback_streams_running(out)
        return None

    @staticmethod
    def playback_streams_running(pw_dump: str) -> Optional[bool]:
        """PipeWire's own JSON: any output stream actually in state `running`?"""
        try:
            nodes = json.loads(pw_dump)
        except Exception:
            return None
        if not isinstance(nodes, list):
            return None
        for node in nodes:
            info = (node or {}).get("info") or {}
            props = info.get("props") or {}
            if props.get("media.class") != "Stream/Output/Audio":
                continue
            if info.get("state") == "running":
                return True
        return False

    def _remote_sessions(self) -> bool:
        # loginctl is authoritative (Remote=yes only for real remote sessions); who/ss are the
        # fallback. The old `(`-in-line heuristic was a false-positive machine: a LOCAL graphical
        # login writes `(:0)` - a display number, not a host - so the guard fired on every
        # desktop session and nothing ever slept.
        return (self._loginctl_remote()
                or self.who_says_remote(_run(["who"]))
                or self.ss_says_remote(_run(["ss", "-tn", "state", "established"])))

    def _loginctl_remote(self) -> bool:
        if not shutil.which("loginctl"):
            return False
        out = _run(["loginctl", "list-sessions", "--no-legend"])
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            val = _run(["loginctl", "show-session", parts[0], "--property=Remote", "--value"])
            if val.strip() == "yes":
                return True
        return False

    @staticmethod
    def who_says_remote(text: str) -> bool:
        for line in text.splitlines():
            m = re.search(r"\(([^)]*)\)\s*$", line.rstrip())
            if not m:
                continue
            host = m.group(1).strip()
            # `(:0)` / `(:0.0)` = the local display; only a named host/IP means remote
            if not host or host.startswith(":") or host.lower().startswith("localhost"):
                continue
            return True
        return False

    @staticmethod
    def ss_says_remote(text: str) -> bool:
        # columns: State Recv-Q Send-Q Local Peer - only a LOCAL port 22 means someone
        # connected to THIS machine's sshd; a peer :22 is this machine ssh-ing OUT
        for line in text.splitlines():
            parts = line.split()
            local = parts[3] if len(parts) >= 5 else (parts[2] if len(parts) == 4 else None)
            if local is not None and local.rsplit(":", 1)[-1] == "22":
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