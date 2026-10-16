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
#: Wired NIC drivers, same use: a fallback. The primary route from "this IRQ/event" to "which
#: interface" is structural (PCI address -> /sys/class/net/<if>/device, then the interface name
#: itself), because on wired cards the kernel names the IRQ after the NETDEV ("enp45s0"), not the
#: driver ("r8169") - a driver table alone would miss every one of them (measured 2026-10-16).
WIRED_DRIVERS = ("r8169", "r8168", "e1000e", "e1000", "igb", "igc", "ixgbe", "atlantic",
                 "alx", "sky2", "forcedeth", "tg3", "bnx2", "vmxnet3", "virtio_net")
TIMER_SOURCES = ("rtc", "rtc0", "hpet")
#: How old (monotonic seconds) a per-device wakeup event may be and still describe THIS wake.
#: The judge runs `repeat_seconds` apart and the first judgement lands within ~1-2
#: minutes of the resume; 5x margin, old enough to never mis-claim an event from a PREVIOUS
#: sleep. `wakeup_last_time_ms` is CLOCK_MONOTONIC in ms (kernel ABI doc), suspend excluded:
#: the stamp is written when the kernel processes the event, i.e. just after the resume.
WAKEUP_EVENT_WINDOW_S = 300.0


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
    def suspend(self, force: bool = False) -> str:
        """Suspend. `force` (config `force_suspend`) skips the inhibitor pre-check and falls back
        to the kernel interface when logind refuses.

        Why force exists, measured on the real Mint box 2026-10-16: csd-power / workbuddy /
        NetworkManager hold sleep inhibitors on EVERY normal desktop session, so the pre-check
        refused on every single apply - the same "environment noise read as a human" trap the
        display guard fell into. And even without the pre-check, logind itself refuses
        `systemctl suspend` while a block inhibitor is held, which the old code did not notice
        (it ignored the exit code and reported success). `/sys/power/state` is the one door
        neither logind nor the inhibitors can close; it needs root, which the judge has.
        """
        if not force:
            blockers = self._inhibitors()
            if blockers:
                raise RuntimeError("sleep inhibitors held by: " + "; ".join(blockers))
        if shutil.which("systemctl"):
            rc, out = _run_rc(["systemctl", "suspend"], timeout=30.0)
            if rc == 0:
                return "systemctl suspend (logind)"
            if not force:
                raise RuntimeError("systemctl suspend failed: %s"
                                   % (out or "exit %d" % rc).strip())
        try:
            with open("/sys/power/state", "w", encoding="ascii") as f:
                f.write("mem\n")
            return "/sys/power/state (kernel, forced)"
        except OSError as exc:
            raise RuntimeError("no suspend mechanism worked (force=%s): %s" % (force, exc))

    def _inhibitors(self) -> List[str]:
        out = _run(["systemd-inhibit", "--list", "--no-pager", "--mode=block"])
        found: List[str] = []
        for line in out.splitlines()[1:]:
            if "sleep" in line.lower() and "block" in line.lower():
                found.append(" ".join(line.split()[:3]))
        return found

    # ------------------------------------------------------------------ wake source
    # Attribution order (unchanged, fills the gaps instead of reordering anything):
    #   1. the journal's own "wakeup source ..." line (unchanged)
    #   2. NEW: the wakeup IRQ resolved to a device - /proc/interrupts names the action AND, for
    #      PCI devices, the bus address, which /sys/class/net/<if>/device can be matched against.
    #      This is what recognises a WIRED NIC wake (r8169 & co were invisible before).
    #   3. NEW: the kernel's per-device wakeup-event counters (/sys/.../power/wakeup_last_time_ms,
    #      CLOCK_MONOTONIC ms). The fallback for x86, where a PCI PME wake arrives as IRQ 9
    #      ("acpi") - the one IRQ number that names no device at all.
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

        method = "journalctl/dmesg + /sys/power/pm_wakeup_irq"
        # Before the counters can speak: did the wireless driver deny being the cause? A "wakeup not
        # due to WoWLAN" report means the wifi wakeup counter can only be telling us that the device
        # resumed, so it is not allowed to name the wifi adapter as the wake source. Honours
        # `wowlan_wake_reason` (off = never even read the report).
        ignore_wifi = self._wowlan_says_not_wireless(enabled=self.wowlan_wake_reason)
        if ignore_wifi:
            method += " + wowlan:not-wowlan"
        if not raw and not irq:
            hit = self._wakeup_event_nic(ignore_wifi=ignore_wifi)   # no OS string: counters or nothing
            if hit:
                return self._wakeup_nic_source(hit, method + " + device-wakeup-events")
            return WakeSource(kind="unknown", method=method + " + device-wakeup-events")

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
            actions, pci = self._irq_identity(irq)
            nic = self._irq_nic(irq, actions, pci)
            if nic:
                kind, who = nic
            elif any(t in actions.lower() for t in TIMER_SOURCES):
                kind = "timer"
                who = "rtc"
            else:
                kind = "other-device"
                who = "irq:" + irq

        # The two weak verdicts are exactly the ones the counters can improve: "unknown" (the
        # journal printed nothing) and "other-device(irq:N)" (an IRQ number that names no device -
        # on x86 that is usually IRQ 9 = ACPI SCI, behind which any PME wake hides). A journal
        # line that DID name usb/hid stays decisive; the counters never override a named source.
        if kind == "unknown" or (kind == "other-device" and who.startswith("irq:")):
            hit = self._wakeup_event_nic(ignore_wifi=ignore_wifi)
            if hit:
                return self._wakeup_nic_source(hit, method + " + device-wakeup-events",
                                               prev_raw=raw)

        return WakeSource(kind=kind, who=who, raw=(raw + (" | pm_wakeup_irq=" + irq if irq else "")),
                          timer=kind == "timer", method=method)

    # ------------------------------------------------------- wake source helpers (new)
    @staticmethod
    def netdev_kind(iface: str) -> Optional[str]:
        """'wifi' | 'eth' for an interface with a backing device, else None (veth/bridge/lo).

        phy80211 is the kernel's own marker for a wireless interface - no driver table involved,
        so it cannot go stale the way a hardcoded name list does.
        """
        base = Path("/sys/class/net") / iface
        if not (base / "device").exists():
            return None
        return "wifi" if (base / "phy80211").exists() else "eth"

    def netdevs(self) -> List[Tuple[str, str]]:
        """[(iface, 'wifi'|'eth')] for every real interface on this machine."""
        out: List[Tuple[str, str]] = []
        try:
            for iface in os.listdir("/sys/class/net"):
                kind = self.netdev_kind(iface)
                if kind:
                    out.append((iface, kind))
        except OSError:
            pass
        return out

    @staticmethod
    def parse_irq_line(text: str, irq: str) -> Tuple[str, str]:
        """/proc/interrupts -> (action names, PCI address) for one IRQ. Pure.

        Measured formats (this host): ` 180: ... IR-PCI-MSIX-0000:2d:00.0  0-edge  enp45s0`
        and `   9: ... IR-IO-APIC  9-fasteoi  acpi`. The action names are everything after the
        edge/level flag field; the PCI address rides in the chip-info column.
        """
        for line in text.splitlines():
            parts = line.split()
            if not parts or parts[0] != "%s:" % irq:
                continue
            m = re.search(r"\b([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])\b", line)
            actions = ""
            for i, p in enumerate(parts):
                if re.search(r"-(edge|fasteoi|level|polled)$", p):
                    actions = " ".join(parts[i + 1:])
                    break
            if not actions:
                actions = parts[-1] if len(parts) >= 2 else ""
            return actions, (m.group(1) if m else "")
        return "", ""

    def _irq_identity(self, irq: str) -> Tuple[str, str]:
        """(actions, pci-address) for the wakeup IRQ: /sys/kernel/irq/<n>/actions first, then
        /proc/interrupts (which also carries the PCI address - sysfs does not)."""
        actions, pci = "", ""
        try:
            actions = Path("/sys/kernel/irq/%s/actions" % irq).read_text().strip()
        except OSError:
            pass
        try:
            out = Path("/proc/interrupts").read_text()
        except OSError:
            out = ""
        if out:
            p_actions, p_pci = self.parse_irq_line(out, irq)
            actions = actions or p_actions
            pci = pci or p_pci
        return actions, pci

    def _netdev_for_pci(self, addr: str) -> Optional[Tuple[str, str]]:
        """Which interface (if any) sits on this PCI address, per /sys/class/net/*/device."""
        if not addr:
            return None
        for iface, kind in self.netdevs():
            try:
                if (Path("/sys/class/net/%s/device" % iface).resolve()).name == addr:
                    return (iface, kind)
            except OSError:
                continue
        return None

    @staticmethod
    def nic_kind_from_tokens(text: str, wifi_names: Sequence[str],
                             eth_names: Sequence[str]) -> Optional[str]:
        """'wifi-nic' | 'eth-nic' | None for an IRQ action string. Pure.

        Order: wireless driver names, wired driver names, then exact interface names (the IRQ
        action for a wired card is the netdev name - `enp45s0`, not `r8169`). `acpi`, `nvme0q2`,
        `xhci_hcd` all return None: those are not NICs.
        """
        low = (text or "").lower()
        if not low:
            return None
        if any(d in low for d in WIRELESS_DRIVERS):
            return "wifi-nic"
        if any(d in low for d in WIRED_DRIVERS):
            return "eth-nic"
        for tok in re.split(r"[,\s]+", low):
            if not tok:
                continue
            for w in wifi_names:
                if tok == w.lower() or tok.startswith(w.lower() + ":"):
                    return "wifi-nic"
            for e in eth_names:
                if tok == e.lower() or tok.startswith(e.lower() + ":"):
                    return "eth-nic"
        return None

    def _irq_nic(self, irq: str, actions: str, pci: str) -> Optional[Tuple[str, str]]:
        """(kind, who) when the wakeup IRQ belongs to a network interface, else None."""
        if pci:
            hit = self._netdev_for_pci(pci)
            if hit:
                return (hit[1] + "-nic", "irq:%s:%s" % (irq, hit[0]))
        if actions:
            names = self.netdevs()
            kind = self.nic_kind_from_tokens(actions,
                                             [i for i, k in names if k == "wifi"],
                                             [i for i, k in names if k == "eth"])
            if kind:
                return (kind, "irq:%s:%s" % (irq, actions.split()[0][:40]))
        return None

    @staticmethod
    def _wakeup_attrs(node: str) -> Optional[int]:
        """wakeup_last_time_ms of one sysfs device node, None when it has none (pure-ish: I/O
        only, no decisions)."""
        try:
            val = int(Path(node, "power", "wakeup_last_time_ms").read_text().strip())
        except (OSError, ValueError):
            return None
        return val if val > 0 else None

    def _wakeup_event_rows(self) -> List[Dict[str, object]]:
        """Per-NIC wakeup-event facts from sysfs: [(iface, kind, last_ms, active)].

        The wakeup attributes live on the DEVICE, and for a USB NIC the netdev's `device` is the
        interface node (4-2:1.0), which has none - walk ONE parent hop to the USB device (4-2).
        Walking further would reach the xHCI controller and mis-attribute OTHER USB devices'
        wakes to this NIC, so the walk stops there. PCI NICs need no walk at all.
        """
        rows: List[Dict[str, object]] = []
        for iface, kind in self.netdevs():
            try:
                node = str(Path("/sys/class/net/%s/device" % iface).resolve())
            except OSError:
                continue
            last_ms = active = None
            for _ in range(2):
                last_ms = self._wakeup_attrs(node)
                if last_ms is not None:
                    try:
                        active = int(Path(node, "power", "wakeup_active_count").read_text().strip())
                    except (OSError, ValueError):
                        active = None
                    break
                parent = os.path.dirname(node)
                if parent == node:
                    break
                node = parent
            if last_ms is not None:
                rows.append({"iface": iface, "kind": kind, "last_ms": last_ms, "active": active})
        return rows

    @staticmethod
    def pick_wakeup_nic(rows: Sequence[Dict[str, object]], now_monotonic_ms: float,
                        window_s: float = WAKEUP_EVENT_WINDOW_S) -> Optional[Dict[str, object]]:
        """The NIC whose wakeup event is the NEWEST one still inside the window. Pure.

        Newest wins because, within one awake session, every NIC's counter is frozen at the
        moment its event was processed - a stale event from a previous sleep is excluded by the
        window, not by comparison against other devices.
        """
        best: Optional[Dict[str, object]] = None
        for r in rows or []:
            last = r.get("last_ms")
            if not isinstance(last, (int, float)) or last <= 0:
                continue
            age_s = (now_monotonic_ms - float(last)) / 1000.0
            if age_s < 0 or age_s > window_s:
                continue
            if best is None or float(last) > float(best["last_ms"]):
                best = dict(r, age_s=age_s)
        return best

    def _wakeup_event_nic(self, ignore_wifi: bool = False) -> Optional[Tuple[str, str, Dict[str, object]]]:
        """(kind, who, hit) when a NIC fired the newest recent wakeup event, else None.

        `ignore_wifi` drops wireless interfaces from the candidates: used when the driver itself has
        just reported that the wake was NOT wireless (`wakeup not due to WoWLAN`), because then the
        counter hit on wlan0 is bus traffic following the resume, not a cause. Measured 2026-10-16
        14:05: a non-wireless wake was filed as `kind=wifi-nic who=wakeup-event:wlan0@14s`.
        """
        rows = self._wakeup_event_rows()
        if ignore_wifi:
            rows = [r for r in rows if r.get("kind") != "wifi"]
        hit = self.pick_wakeup_nic(rows, time.monotonic() * 1000.0)
        if not hit:
            return None
        return (str(hit["kind"]) + "-nic",
                "wakeup-event:%s@%.0fs" % (hit["iface"], float(hit["age_s"])),
                hit)

    @staticmethod
    def _wowlan_says_not_wireless(now: Optional[float] = None, enabled: bool = True) -> bool:
        """Did the NIC's own wake report (nl80211, recorded by the sensor) deny being wireless?

        The only wake claim that survives the firmware eating the waking packet, and the only one
        that can contradict the per-device counters. No report at all (no `iw`, no sensor, a wired
        NIC) reads as False: absence of evidence, never evidence.

        `enabled` is the config switch `wowlan_wake_reason`: with the channel switched off the file
        is not consulted at all, so a row written before the switch was flipped cannot keep
        suppressing the wifi counter. Default True = the shipped behaviour, which is what a
        platform object with no config (installer probes, unit tests) must keep.
        """
        if not enabled:
            return False
        try:
            from ..iwevent import last_wowlan
            from .. import paths as _paths
            row = last_wowlan(_paths.wowlan_event_log(), max_age_s=WAKEUP_EVENT_WINDOW_S, now=now)
        except Exception:
            return False
        return bool(row) and str(row.get("kind")) == "not-wowlan"

    @property
    def wowlan_wake_reason(self) -> bool:
        """The `wowlan_wake_reason` switch, read off the attached config (None -> shipped = on)."""
        return bool(getattr(getattr(self, "config", None), "wowlan_wake_reason", True))

    @staticmethod
    def _wakeup_nic_source(hit: Tuple[str, str, Dict[str, object]], method: str,
                           prev_raw: str = "") -> WakeSource:
        kind, who, info = hit
        note = ("netdev-wakeup:%s last_ms=%s active=%s"
                % (info.get("iface"), info.get("last_ms"), info.get("active")))
        raw = (prev_raw + " | " if prev_raw else "") + note
        return WakeSource(kind=kind, who=who, raw=raw, method=method)

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
                # State: RUNNING is the only "playing". `Corked: no` is NOT a synonym: an app
                # that keeps its stream open but plays nothing reports State: IDLE, Corked: no
                # (browser tabs, notification daemons) - measured 2026-10-16, that clause made
                # this guard fire on a silent desktop and the machine never re-slept.
                for block in out.split("Sink Input #")[1:]:
                    if "State: RUNNING" in block:
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