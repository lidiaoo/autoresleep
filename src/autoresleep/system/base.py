"""What the rest of v2 is allowed to assume about an operating system.

Everything OS-specific sits behind this interface, and every method is expected to be able to
answer "I don't know" instead of guessing - v1's rule was *do not guess*, and a portable tool
needs that even more, because two of the three platforms cannot be debugged from the machine
where the code was written.

Interface, smallest useful set:
  idle()          how long since the last real user input, in THIS session
  suspend()       put the machine to sleep
  wake_source()   what woke it last time
  guards()        is the machine busy in a way that must outrank every evidence chain
  firewall()      can the listener's UDP ports be reached, and how to open them
  capabilities()  honest per-feature support, so `doctor` can print a status matrix
"""
from __future__ import annotations

import abc
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..decide import Guards


@dataclass
class IdleInfo:
    seconds: float
    tick: str = ""                  # raw input tick where the platform offers one
    method: str = ""                # how it was measured - printed by `doctor`
    error: str = ""


@dataclass
class WakeSource:
    kind: str = "unknown"           # wifi-nic | eth-nic | other-device | timer |
                                    # fixed-feature | fixed-feature-nic-suspect | unknown
    who: str = ""                   # the token that matched (adapter name, event field...)
    raw: str = ""                   # whatever the OS actually said - never throw this away
    timer: bool = False             # a programmed wake timer exists
    method: str = ""
    chain: str = "os"               # 'drv' when the NIC driver itself reported the reason
    #: when the OS says this wake happened. v1 matched it against the wake it was judging: an
    #: attribution from a wake six hours ago is NOT evidence about the wake happening now.
    wake_time: str = ""
    wake_epoch: Optional[float] = None


@dataclass
class FirewallStatus:
    state: str = "unknown"          # enabled | disabled | mixed | unknown
    detail: str = ""
    hints: List[str] = field(default_factory=list)
    rule_names: List[str] = field(default_factory=list)


def decode_console(raw: bytes) -> str:
    """Decode output of an OS command line tool, respecting this machine's code page.

    Not UTF-8 then latin-1: on a Chinese Windows every status word and every error message comes
    back in cp936, and decoding that as cp1252 turns '关闭' into mojibake - which silently broke
    a log parser here, and made an error message unreadable right when it mattered. UTF-8 mode
    interpreters make it worse: `locale.getpreferredencoding()` answers 'utf-8' on a cp936 box,
    so the OEM code page has to be asked for by name.
    """
    if not raw:
        return ""
    if b"\x00" in raw[:200]:
        try:
            return raw.decode("utf-16-le", "replace")
        except Exception:
            pass
    candidates = ["utf-8"]
    if os.name == "nt":
        try:
            import ctypes
            for getter in ("GetOEMCP", "GetACP"):
                candidates.append("cp%d" % int(getattr(ctypes.windll.kernel32, getter)()))
        except Exception:
            pass
    import locale
    candidates.append(locale.getpreferredencoding(False))
    # GBK unconditionally, not only via GetOEMCP: console output can reach a non-Windows host
    # too (ssh to a cp936 machine, cross-platform test runs) and cp1252 below "succeeds" on
    # almost any byte string, so it must stay LAST - anything decodable as GBK first.
    candidates.append("gbk")
    candidates.append("cp1252")
    seen = []
    for enc in candidates:
        if enc.lower().replace("-", "") not in [s.lower().replace("-", "") for s in seen]:
            seen.append(enc)
    for enc in seen:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def proc_kwargs() -> dict:
    """subprocess kwargs that keep a console window from flashing.

    The tool now runs under pythonw.exe (no console of its own), and Windows gives every console
    program started by a console-less process a NEW console window - so `powercfg`, `netsh` and
    `wevtutil` calls made the screen blink every 30 seconds. CREATE_NO_WINDOW is the fix; the
    STARTUPINFO pair is belt and braces for the same thing.
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000                     # CREATE_NO_WINDOW
        try:
            import subprocess
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                                   # SW_HIDE
            kwargs["startupinfo"] = si
        except Exception:
            pass
    return kwargs


def run_hidden(cmd: Sequence[str], timeout: float = 20.0) -> str:
    """Run an OS tool invisibly and decode its output for this machine's code page."""
    import subprocess
    out = subprocess.run(list(cmd), capture_output=True, timeout=timeout, text=False,
                         **proc_kwargs())
    return decode_console((out.stdout or b"") + b"\n" + (out.stderr or b""))


class System(abc.ABC):
    name = "generic"
    display_name = "generic"
    #: how the platform can be told "the listener needs these UDP ports open"
    firewall_supported = False
    #: The operator's loaded `Config`, when one was available - attached by `cli._system()`.
    #: Deliberately optional: the platform layer must stay constructible without a config (the
    #: installer probes a machine before one exists, and every unit test builds a bare instance).
    #: A method that reads a switch must therefore default to the SHIPPED behaviour when this is
    #: None, never to "off".
    config = None

    def __init__(self, config=None):
        self.config = config

    # ------------------------------------------------------------------ required
    @abc.abstractmethod
    def idle(self) -> IdleInfo:
        ...

    @abc.abstractmethod
    def suspend(self) -> str:
        """Put the machine to sleep; return the mechanism actually used."""

    def wake_source(self) -> WakeSource:
        return WakeSource(kind="unknown", method="not implemented on " + self.name)

    def guards(self) -> Guards:
        return Guards()

    def firewall(self) -> FirewallStatus:
        return FirewallStatus()

    def firewall_apply(self, ports: List[int], marker_port: int, program: str) -> List[str]:
        return []

    def firewall_remove(self) -> List[str]:
        return []

    def is_admin(self) -> bool:
        """Whether this process can register services / open the firewall.

        Default False: on Linux and macOS the user-level units and LaunchAgents need no
        privileges at all, so "not admin" is the normal, working state there.
        """
        return False

    # ------------------------------------------------------------------ link facts
    # Driver-free by construction. No platform asks its NIC driver WHY the machine woke: every
    # probe reads what the OS itself already knows (NetworkManager/`iw`/sysfs on Linux, the WLAN
    # autoconfig state on Windows, `airport` on macOS). The tools differ, the question does not:
    # is the link up, and is it where we left it before we slept?
    def link_state(self) -> Optional[Dict[str, object]]:
        """The link as it is right now: connected / ssid / bssid / changes. None = no probe."""
        return None

    def link_events_since(self, ts: float) -> List[str]:
        """Disconnect/reconnect lines the OS logged after `ts`. Empty = nothing observed."""
        return []

    def link_snapshot(self):
        """Write the pre-sleep link state so the next resume can tell whether it moved.

        Called immediately before suspending (see cli.cmd_judge): without this the comparison
        below has no baseline and silently degrades to "no pre-sleep snapshot" forever.
        """
        import json
        from .. import paths
        st = self.link_state()
        if st is None:
            return None
        snap = dict(st)
        snap["at"] = time.time()
        try:
            paths.ensure_data_dir()
            paths.link_snapshot_file().write_text(json.dumps(snap, default=str), encoding="utf-8")
        except Exception:
            pass
        return snap

    def link_evidence(self, wake_epoch: Optional[float] = None):
        """(link, why): did the network move while the machine was asleep?

        This is the portable answer to "was this wake a Wi-Fi event?" - it never asks the NIC
        driver for a wake reason, it compares the pre-sleep snapshot with what is observable now:
          * not associated any more
          * associated to a different BSSID (roamed / re-associated)
          * the kernel's carrier-change counter moved (Linux: /sys/class/net/*/carrier_changes) -
            "the link physically dropped and came back" even when it re-attached to the SAME AP
          * the OS logged a disconnect/associate after the snapshot was taken
        LINK_INTACT means "we looked and nothing moved", UNKNOWN means "no probe here".
        """
        st = self.link_state()
        if st is None:
            return ("UNKNOWN", "no link probe on this platform")
        snap = None
        try:
            import json
            from .. import paths
            if paths.link_snapshot_file().exists():
                snap = json.loads(paths.link_snapshot_file().read_text(encoding="utf-8"))
        except Exception:
            snap = None

        if st.get("connected") is False:
            return ("LINK_LOST", "not-associated")
        if snap:
            if snap.get("bssid") and st.get("bssid") and str(snap["bssid"]) != str(st["bssid"]):
                return ("LINK_LOST", "bssid-changed:%s->%s" % (snap["bssid"], st["bssid"]))
            if snap.get("associated") and st.get("connected") is False:
                return ("LINK_LOST", "assoc-lost-after-resume")
            before, now = snap.get("changes"), st.get("changes")
            if isinstance(before, int) and isinstance(now, int) and now > before:
                # the same AP can be re-attached after a drop, so BSSID alone is not proof
                return ("LINK_LOST", "carrier-changed-%dx-since-sleep" % (now - before))
            try:
                events = self.link_events_since(float(snap.get("at") or 0))
            except Exception:
                events = []
            if events:
                return ("LINK_LOST", "reconnect-event-after-sleep:" + str(events[0])[:80])
        # nl80211's own account of the link (`iw event`), recorded by the privileged sensor: the
        # portable wifi-disconnect signal. One format for every driver, and it catches a roam -
        # measured on the real box, the BSSID changes while `iw dev wlan0 link` still says
        # "Connected", so an instantaneous probe cannot see it.
        try:
            from .. import paths as _paths
            from ..iwevent import wifi_moves_around_resume
            if wake_epoch:
                moved = wifi_moves_around_resume(_paths.wlan_event_log(), float(wake_epoch))
                if moved:
                    return ("LINK_LOST", "wlan-event-at-resume:" + str(moved)[:80])
        except Exception:
            pass
        why = "bssid=%s ssid=%s%s" % (st.get("bssid") or "-", st.get("ssid") or "-",
                                      "" if snap else " (no pre-sleep snapshot)")
        if st.get("connected"):
            return ("LINK_INTACT", why)
        return ("UNKNOWN", why)

    # ------------------------------------------------------------------ reporting
    def capabilities(self) -> Dict[str, bool]:
        return {
            "idle": True,
            "suspend": True,
            "wake-source": self.wake_source.__func__ is not System.wake_source,
            "guards": self.guards.__func__ is not System.guards,
            "link": self.link_state.__func__ is not System.link_state,
            "firewall": self.firewall_supported,
            "scheduler": True,
        }

    def describe(self) -> str:
        caps = ", ".join("%s=%s" % (k, "yes" if v else "no")
                         for k, v in sorted(self.capabilities().items()))
        return "%s (%s): %s" % (self.display_name, self.name, caps)