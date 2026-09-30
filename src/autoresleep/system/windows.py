"""Windows: the platform v1 was written for, ported to the same interface as the others.

No PowerShell anywhere, and that is deliberate: v1 had to shell out to PowerShell for the
scheduler and for `powercfg /requests`, which made the whole tool Windows-only by
construction. Here the same information comes from the Win32 API (ctypes) or from
`powercfg.exe` directly, and scheduling is Task Scheduler XML + schtasks.
"""
from __future__ import annotations

import shutil
import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Sequence, Tuple

from ..decide import Guards
from ..pkt import Adapter, adapters
from .base import FirewallStatus, IdleInfo, System, WakeSource, decode_console, run_hidden

RULE_PREFIX = "AutoReSleep-WoL-UDP-"


def _run(cmd: Sequence[str], timeout: float = 20.0) -> str:
    """Run a console tool INVISIBLY and decode its output for this machine's code page.

    Invisible matters as much as decoded: the judge runs under pythonw.exe, and Windows pops a new
    console window for every console program started by a console-less process - so `powercfg`
    (called on every single judge run) made the screen blink every 30 seconds.

    A failure here MUST also be visible in the LOG: returning "" silently is how a broken parser
    turns into "the tool runs but never sees anything". (Measured: a missing `import os` inside the
    decoder made the firewall state read as "unknown" with no hint anywhere.)
    """
    try:
        return run_hidden(cmd, timeout)
    except Exception as exc:
        _warn("running %s failed: %s" % (cmd[0] if cmd else "?", exc))
        return ""


def _warn(message: str) -> None:
    try:
        from ..journal import get_logger
        get_logger().warning("system.windows: %s", message)
    except Exception:
        pass


def parse_event_time(text: str):
    """'2026-09-28T08:11:36.8914226Z' -> epoch seconds, or None.

    v1's lesson about this field: a `[datetime]` cast sometimes honours the trailing Z and
    sometimes drops it, so the same string can be UTC or local depending on host and shape.
    Here the Z is stripped explicitly and UTC is applied deliberately.
    """
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    raw = raw.split(".")[0]                      # drop sub-second noise
    try:
        import calendar
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-])(\d{2}):?(\d{2})$", raw)
        if m:
            base = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
            offset = (int(m.group(3)) * 3600 + int(m.group(4)) * 60) * (1 if m.group(2) == "+" else -1)
            return float(base - offset)
        return float(calendar.timegm(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception as exc:
        # never swallow this silently: an unparsable timestamp makes every attribution look stale,
        # which reads exactly like "the tool stopped working"
        _warn("cannot parse event time %r: %s" % (text, exc))
        return None


class WindowsSystem(System):
    name = "win32"
    display_name = "Windows"
    firewall_supported = True

    # ------------------------------------------------------------------ privileges
    def is_admin(self) -> bool:
        """Registering a HighestAvailable task and editing firewall rules both need this.

        v1's installer relaunched itself through UAC when it was not elevated; v2 has to do the
        same or `install` fails halfway with access-denied from schtasks - which is exactly the
        failure that makes people think the tool is broken.
        """
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    # ------------------------------------------------------------------ idle
    def idle(self) -> IdleInfo:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        try:
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return IdleInfo(-1.0, method="GetLastInputInfo", error="call failed")
            tick = ctypes.windll.kernel32.GetTickCount()
            # GetTickCount wraps at ~49.7 days; v1 handled that with uint arithmetic
            elapsed_ms = (int(tick) - int(info.dwTime)) & 0xFFFFFFFF
            return IdleInfo(seconds=elapsed_ms / 1000.0, tick=str(info.dwTime),
                            method="GetLastInputInfo (interactive session)")
        except Exception as exc:
            return IdleInfo(-1.0, method="GetLastInputInfo", error=str(exc))

    # ------------------------------------------------------------------ suspend
    def suspend(self) -> str:
        try:
            powrprof = ctypes.windll.powrprof
            if powrprof.SetSuspendState(False, True, False):
                return "SetSuspendState(hibernate=false, forceCritical=true)"
        except Exception:
            pass
        # documented fallback; also the path v1 fell back to when the P/Invoke returned false
        _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "rundll32 powrprof.dll,SetSuspendState 0,1,0"

    # ------------------------------------------------------------------ wake source
    def wake_source(self) -> WakeSource:
        """Power-Troubleshooter event 1 + Kernel-Power 107 + powercfg, same as v1 - minus PowerShell."""
        text = _run(["wevtutil", "qe", "System",
                     "/q:*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]",
                     "/c:1", "/rd:true", "/f:xml"])
        # event XML uses SINGLE quotes around Data attributes in wevtutil's output - a
        # double-quote-only pattern silently matched nothing and left the judge with no fields
        fields = dict(re.findall(r"<Data Name=['\"]([^'\"]+)['\"]>([^<]*)</Data>", text))
        wake_text = (fields.get("WakeSourceText") or "").strip()
        raw = "; ".join("%s=%s" % (k, v) for k, v in fields.items()) or text.strip()[:200]

        if not fields and not text.strip():
            return WakeSource(kind="unknown", method="Power-Troubleshooter event 1 unavailable")

        timer = self._has_wake_timer()
        kind, who = self._attribute(wake_text, timer)
        wake_time = (fields.get("WakeTime") or "").strip()
        return WakeSource(kind=kind, who=who, raw=raw, timer=timer,
                          method="Power-Troubleshooter event 1 + Kernel-Power 107 + lastwake",
                          wake_time=wake_time, wake_epoch=parse_event_time(wake_time))

    def _has_wake_timer(self) -> bool:
        text = _run(["wevtutil", "qe", "System",
                     "/q:*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=107)]]",
                     "/c:1", "/rd:true", "/f:xml"])
        m = re.search(r'<Data Name="ProgrammedWakeTimeAc">([^<]*)</Data>', text)
        value = (m.group(1) if m else "").strip()
        # 1601-01-01 is Windows' "no timer programmed"
        return bool(value) and not value.startswith("1601-01-01")

    def _attribute(self, wake_text: str, timer: bool) -> Tuple[str, str]:
        """Rebuild the adapter name list from THIS machine - never from a literal.

        v1's worst silent failure: a hardcoded card name made every wake after a card swap
        classify as 'not the NIC', while the reason chain still printed a NIC reason.
        """
        ads: List[Adapter] = adapters()
        wifi = [(a.label or a.name, a) for a in ads if a.wireless]
        eth = [(a.label or a.name, a) for a in ads if not a.wireless]

        def matches(candidates) -> Optional[str]:
            for label, a in candidates:
                for spelling in {label, a.name, re.sub(r"\s*#\d+$", "", label)}:
                    if spelling and wake_text and spelling.lower() in wake_text.lower():
                        return spelling
            return None

        if wake_text:
            hit = matches(wifi)
            if hit:
                return "wifi-nic", "name:" + hit
            hit = matches(eth)
            if hit:
                return "eth-nic", "name:" + hit
            return "other-device", "name:" + wake_text

        if timer:
            return "timer", "Kernel-Power107:ProgrammedWakeTime"

        armed = self._wake_armed()
        if armed:
            # v1's rule, which this must keep: Windows named no device and no timer exists, so
            # the only thing that can explain the wake is an armed NIC. It stays a SUSPECT (a
            # power-button wake looks identical), and re-sleeping on it is an explicit policy.
            all_names = {a.name for a in ads} | {(a.label or "") for a in ads}
            all_names |= {re.sub(r"\s*#\d+$", "", n) for n in list(all_names)}
            if all(name in all_names for name in armed):
                return "fixed-feature-nic-suspect", "only-nics-armed:" + ",".join(armed)
        return "fixed-feature" if armed else "unknown", "armed:" + ",".join(armed)

    def _wake_armed(self) -> List[str]:
        out = _run(["powercfg", "/devicequery", "wake_armed"])
        return [l.strip() for l in out.splitlines() if l.strip() and not l.lower().startswith("powercfg")]

    # ------------------------------------------------------------------ link hints
    def _wlan_state(self) -> dict:
        """`netsh wlan show interfaces`: association state, SSID, BSSID. No admin needed.

        The labels are localized, so the state is matched on both spellings; SSID/BSSID are not
        localized (and `^\\s*SSID` cannot match the BSSID line, which starts with 'B').
        """
        out = _run(["netsh", "wlan", "show", "interfaces"])
        info = {"raw": out, "ssid": "", "bssid": "", "connected": None}
        m = re.search(r"(?mi)^\s*BSSID\s*:\s*([0-9a-fA-F:]{17})", out)
        if m:
            info["bssid"] = m.group(1).lower()
        m = re.search(r"(?mi)^\s*SSID\s*:\s*(.+?)\s*$", out)
        if m:
            info["ssid"] = m.group(1).strip()
        m = re.search(r"(?mi)^\s*(?:State|状态)\s*:\s*(.+?)\s*$", out)
        if m:
            value = m.group(1)
            if "断开" in value or "disconnect" in value.lower():
                info["connected"] = False
            elif "已连接" in value or "connect" in value.lower():
                info["connected"] = True
        if info["connected"] is None:
            info["connected"] = bool(info["bssid"])
        return info

    def link_state(self):
        """What the WLAN looks like right now, straight from the OS's own view (netsh).

        No driver call anywhere: `netsh wlan show interfaces` is the same data the Wi-Fi UI
        shows. `changes` is None on Windows (no carrier counter), so the comparison leans on
        BSSID/association plus the WLAN-AutoConfig event log below.
        """
        st = self._wlan_state()
        return {"iface": "", "connected": st["connected"], "ssid": st["ssid"],
                "bssid": st["bssid"], "changes": None,
                "method": "netsh wlan show interfaces (OS view)"}

    def link_events_since(self, ts: float) -> List[str]:
        """WLAN AutoConfig's connect/disconnect events after `ts`.

        A Windows OS log (Microsoft-Windows-WLAN-AutoConfig/Operational), not a driver report:
        it records associated/disconnected with timestamps - exactly the evidence needed when
        the link re-attached to the SAME AP after a drop, which BSSID alone cannot show.
        """
        if not shutil.which("wevtutil"):
            return []
        out = _run(["wevtutil", "qe", "Microsoft-Windows-WLAN-AutoConfig/Operational",
                    "/c:20", "/rd:true", "/f:text"])
        hits = []
        for blk in re.split(r"\r?\n\r?\n", out):
            low = blk.lower()
            if not any(tok in low for tok in ("disconnect", "associat", "connection")):
                continue
            m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", blk)
            if m:
                try:
                    import datetime
                    when = datetime.datetime.strptime("%s %s" % (m.group(1), m.group(2)),
                                                      "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    continue
                if when < ts:
                    continue
            hits.append(" ".join(blk.split())[:160])
        return hits

    # ------------------------------------------------------------------ guards
    def guards(self) -> Guards:
        display, system, execution = self._power_requests()
        remote = self._remote_sessions()
        return Guards(remote_in_use=remote,
                      display_in_use=display,
                      media_playing=False,       # Windows cannot attribute audio to a request cleanly
                      update_running=self._update_running(execution))

    def _power_requests(self) -> Tuple[bool, bool, bool]:
        """Parse `powercfg /requests` positionally.

        The section headers are localized while their ORDER is fixed
        (DISPLAY, SYSTEM, AWAYMODE, EXECUTION, PERFBOOST, ACTIVELOCKSCREEN), so v1 parsed by
        index and this does the same - never by header text.
        """
        out = _run(["powercfg", "/requests"])
        if not out.strip():
            return (False, False, False)
        sections: List[List[str]] = []
        current: Optional[List[str]] = None
        for line in out.splitlines():
            if line.rstrip().endswith(":") and not line.startswith(" "):
                if current is not None:
                    sections.append(current)
                current = []
            elif current is not None:
                current.append(line)
        if current is not None:
            sections.append(current)

        def active(idx: int) -> bool:
            if idx >= len(sections):
                return False
            for line in sections[idx]:
                low = line.lower()
                if ".exe" in low or "playing audio" in low or "音频" in line:
                    return True
            return False

        display = active(0)
        system = active(1)
        execution = active(3)
        return display, system, execution

    def _remote_sessions(self) -> bool:
        out = _run(["qwinsta"])
        for line in out.splitlines():
            low = line.lower()
            if "rdp" in low or "tcp" in low:
                if "active" in low or "已连接" in line or "活动" in line:
                    return True
        return False

    def _update_running(self, execution: bool) -> bool:
        if not execution:
            return False
        out = _run(["tasklist", "/fo", "csv", "/nh"])
        for name in ("mousocoreworker", "tiworker", "trustedinstaller", "wusa", "windowssupdate"):
            if name in out.lower():
                return True
        return False

    # ------------------------------------------------------------------ firewall
    def firewall(self) -> FirewallStatus:
        out = _run(["netsh", "advfirewall", "show", "allprofiles", "state"])
        # netsh prints the label and the value in two columns with NO colon on a Chinese
        # Windows ("状态                                  关闭"), so the colon must be optional
        states = re.findall(r"(?im)^\s*(?:State|状态)\s*[:：]?\s*(\S+)\s*$", out)
        norm = [s.lower() for s in states]
        if not norm:
            return FirewallStatus(state="unknown", detail=out.strip()[:200])
        on = sum(1 for s in norm if s in ("on", "on.", "启用"))
        off = sum(1 for s in norm if s in ("off", "关闭"))
        state = "enabled" if on and not off else ("disabled" if off and not on else "mixed")
        existing = [l.strip() for l in _run(["netsh", "advfirewall", "firewall", "show", "rule",
                                             "name=all"]).splitlines()
                    if RULE_PREFIX in l]
        hints = []
        if state != "disabled":
            hints.append("firewall is %s: inbound UDP to the listener is dropped without a rule" % state)
            hints.append("apply with: autoresleep install   (or --no-firewall to skip)")
        return FirewallStatus(state=state, detail="profiles: " + ",".join(norm),
                              hints=hints, rule_names=existing)

    def firewall_apply(self, ports: List[int], marker_port: int, program: str) -> List[str]:
        done: List[str] = []
        for port in list(ports) + [marker_port]:
            name = RULE_PREFIX + str(port)
            base = ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + name,
                    "dir=in", "action=allow", "protocol=UDP", "localport=" + str(port),
                    "program=" + program, "remoteip=localsubnet", "profile=any"]
            _run(base)
            if _run(["netsh", "advfirewall", "firewall", "show", "rule", "name=" + name]).strip():
                done.append(name)
        return done

    def firewall_remove(self) -> List[str]:
        removed: List[str] = []
        shown = _run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all"])
        names = {RULE_PREFIX + str(p) for p in (9, 7, 67, 68, 45678)}
        for name in sorted(names):
            if name in shown:
                _run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + name])
                if name not in _run(["netsh", "advfirewall", "firewall", "show", "rule", "name=" + name]):
                    removed.append(name)
        return removed