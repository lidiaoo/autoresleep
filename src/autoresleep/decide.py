"""The v2 decision engine: pure logic, no OS calls, no I/O.

This is the piece worth porting carefully from v1 (`resleep.ps1`): the *ordering* of the
evidence chains is the whole product. Everything OS-specific lives behind
`autoresleep.system`; everything here is plain data in, plain decision out, so it can be
unit-tested on every platform (and on a laptop with no Wi-Fi at all).

Precedence, strongest claim first (mirrors 00-README-sources.md section 5):

  1. a wake that was ASKED FOR (magic packet / sender marker)  -> never re-sleep
  2. nothing may sleep a machine that is in use                 -> guards
  3. the DRIVER's wake signal outranks Windows'/the OS's attribution
  4. non-Wi-Fi attribution (ethernet, other device, timer)      -> treated as intentional
  5. Wi-Fi NIC attribution                                      -> the case this tool exists for
  6. unattributable wake                                        -> do not guess: long idle + policy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class Action(str, Enum):
    SKIP = "skip"
    SLEEP = "sleep"

    def __str__(self) -> str:  # so the log line reads 'skip'/'sleep', not 'Action.SKIP'
        return self.value


# --------------------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------------------
@dataclass
class Evidence:
    """One wake's worth of evidence, assembled by the platform layer.

    Every field is optional on purpose: a chain that produced nothing must be
    distinguishable from a chain that said "nothing happened" (that distinction is exactly
    what v1's `NO_DATA` and `trace-` bookkeeping existed for).
    """
    pkt_kind: Optional[str] = None          # 'magic' | 'marker' | None
    pkt_age_s: Optional[float] = None       # seconds since the packet was seen
    drv_reason: Optional[str] = None        # 'NON_MAGIC_AP_ASSOC_LOST', 'MEDIA_DISCONNECT', ...
    drv_age_s: Optional[float] = None
    os_link: Optional[str] = None           # 'LINK_LOST' | 'LINK_INTACT' | None  (hint only)
    os_link_why: Optional[str] = None       # calibration detail: 'bssid-changed', 'gw-timeout', ...
    wake_kind: Optional[str] = None         # attribution carried by the evidence file
    wake_who: Optional[str] = None


@dataclass
class Guards:
    """Reasons a machine must stay awake no matter what the evidence says."""
    remote_in_use: bool = False
    display_in_use: bool = False
    media_playing: bool = False
    update_running: bool = False

    def active(self) -> Optional[str]:
        for name, flag in (("remote-in-use", self.remote_in_use),
                           ("display-in-use", self.display_in_use),
                           ("media-playing", self.media_playing),
                           ("update-running", self.update_running)):
            if flag:
                return name
        return None


@dataclass
class Params:
    """Tunables, all defaulted to v1's shipped values."""
    pkt_window_s: float = 180.0             # a packet older than this belongs to another wake
    idle_minutes: float = 2.0               # tier B: real input idle needed to re-sleep
    unknown_idle_minutes: float = 15.0      # no usable attribution: demand a long idle
    min_gap_minutes: float = 5.0            # debounce a router-driven wake/sleep loop ...
    #: ... but only while a human might be there. With no input since our own last forced sleep,
    #: a NIC-attributed wake is the machine being poked while nobody is around: 5 minutes of
    #: "awake" per poke is not a safety feature, it is a machine that never stays asleep.
    min_gap_unattended_minutes: float = 1.0
    unattributed_wake_policy: str = "skip"  # 'skip' | 'resleep-if-unattended'


@dataclass
class Decision:
    action: Action
    reason: str
    src: str = "-"                          # which chain decided: pkt | drv | os | mingle | policy
    link: Optional[str] = None              # the os- hint, recorded but usually not deciding
    who: Optional[str] = None               # the token that matched, for next-morning forensics
    details: Dict[str, object] = field(default_factory=dict)

    def line(self) -> str:
        parts = ["action=" + self.action.value, "reason=" + self.reason, "src=" + self.src]
        if self.link:
            parts.append("link=" + self.link)
        if self.who:
            parts.append("who=" + self.who)
        return "  ".join(parts)


def decide(evidence: Evidence, guards: Guards, idle_s: float, params: Params = Params(),
           now: Optional[float] = None, last_forced_sleep_at: Optional[float] = None,
           input_since_sleep: Optional[bool] = None, wake_kind: Optional[str] = None,
           wake_who: Optional[str] = None) -> Decision:
    """Decide whether to force the machine back to sleep. Never performs the sleep."""
    link = evidence.os_link

    # 1. asked for ---------------------------------------------------------------------
    if evidence.pkt_kind and evidence.pkt_age_s is not None and evidence.pkt_age_s <= params.pkt_window_s:
        return Decision(Action.SKIP, "wake-requested", "pkt", link=link,
                        who="%s@%+ds" % (evidence.pkt_kind, evidence.pkt_age_s))

    # 2. guards ------------------------------------------------------------------------
    active = guards.active()
    if active:
        return Decision(Action.SKIP, "guarded:" + active, "mingle", link=link)

    # 3. (removed) a blanket "input since the last forced sleep -> user-present".
    #
    # It is tempting and it is wrong: that flag stays true until the NEXT forced sleep, so one
    # glance at the machine (any input event) would veto every later judgement - the machine then
    # never sleeps again and the tool deadlocks itself. The real user-presence test is INPUT
    # IDLE, which every sleep path below already gates on, plus the grace window that re-checks
    # right before suspending. `input_since_sleep` is still used, but only to decide how
    # conservative the debounce must be (see _gap).

    # 4. the driver's own wake signal, which outranks the OS's attribution ---------------
    drv = _classify_drv(evidence.drv_reason)
    if drv == "non-magic-wifi":
        return _finish(_resleep(params.idle_minutes, evidence.drv_reason, "drv",
                                who="driver:" + str(evidence.drv_reason), idle_s=idle_s, link=link),
                       params, now, last_forced_sleep_at,
                       required_gap=_gap(params, input_since_sleep, nic_attributed=True))
    if drv == "media":
        # wired NICs only ever report media/cable changes - intentional, do not second-guess
        return Decision(Action.SKIP, "wake-source-drv-" + str(evidence.drv_reason).lower(), "drv", link=link)

    # 5. OS-level attribution -----------------------------------------------------------
    kind = (wake_kind or evidence.wake_kind or "").lower()
    if wake_who is None:
        wake_who = evidence.wake_who
    if kind in ("eth-nic", "other-device", "timer", "fixed-feature"):
        return Decision(Action.SKIP, "wake-source-" + kind, "os", link=link, who=wake_who)
    if kind == "fixed-feature-nic-suspect":
        # A power-button wake looks identical to an unattributed NIC wake, so this stays
        # an explicit policy switch instead of a silent default.
        if params.unattributed_wake_policy != "resleep-if-unattended":
            return Decision(Action.SKIP, "wake-source-fixed-feature-nic-suspect", "os", link=link)
        return _finish(_resleep(params.idle_minutes, "NON_MAGIC_FIXED_FEATURE_NIC_SUSPECT",
                                "os", who=wake_who, idle_s=idle_s, link=link),
                       params, now, last_forced_sleep_at)
    if kind == "wifi-nic":
        # LINK_LOST is a hint, not a classification - it is promoted here and only here
        reason = "NON_MAGIC_WLAN_LINK_LOST" if link == "LINK_LOST" else "NON_MAGIC_WIFI_NIC"
        return _finish(_resleep(params.idle_minutes, reason, "os", who=wake_who, idle_s=idle_s, link=link),
                       params, now, last_forced_sleep_at,
                       required_gap=_gap(params, input_since_sleep, nic_attributed=True))

    # 6. no usable attribution: do not guess, demand a long idle ------------------------
    if idle_s >= params.unknown_idle_minutes * 60.0:
        return _finish(Decision(Action.SLEEP, "NON_MAGIC_UNATTRIBUTED", "policy", link=link),
                       params, now, last_forced_sleep_at)
    return Decision(Action.SKIP, "unknown-wake-needs-long-idle", "policy", link=link)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
# The driver's wake reasons we can act on. Matching is on the uppercase string, no regex:
# v1 lost a whole night to a regex that had been double-escaped by a text-matching edit.
_WIFI_ONLY_REASONS = ("AP_ASSOC_LOST", "AP_ASSOCIATION_LOST", "AP_DISASSOC", "ROAM",
                      "GTK_REKEY", "4-WAY", "4WAY")
_MEDIA_REASONS = ("MEDIA_DISCONNECT", "MEDIA_CONNECT", "MEDIA_")


def _classify_drv(reason: Optional[str]) -> Optional[str]:
    if not reason:
        return None
    r = str(reason).upper()
    if any(tok in r for tok in _MEDIA_REASONS):
        return "media"
    if any(tok in r for tok in _WIFI_ONLY_REASONS):
        return "non-magic-wifi"
    return None          # unrecognised: record it, let it decide nothing


def _resleep(needed_minutes: float, reason: str, src: str, who: Optional[str],
             idle_s: float, link: Optional[str]) -> Decision:
    if idle_s < needed_minutes * 60.0:
        return Decision(Action.SKIP, "user-present", src, link=link, who=who,
                        details={"idle_s": round(idle_s, 1), "needed_s": needed_minutes * 60.0})
    return Decision(Action.SLEEP, reason, src, link=link, who=who,
                    details={"idle_s": round(idle_s, 1)})


def _gap(params: Params, input_since_sleep: Optional[bool], nic_attributed: bool) -> float:
    """How long to wait before sleeping the machine again after our own forced sleep.

    `input_since_sleep is False` is a positive statement: since the sleep WE issued, not a single
    input event arrived. Combined with a wake attributed to the NIC, that is an empty machine
    being poked - it gets the short gap. Anything else (input seen, or we cannot tell) keeps the
    conservative gap, because then the machine might be in front of somebody.
    """
    if nic_attributed and input_since_sleep is False:
        return params.min_gap_unattended_minutes
    return params.min_gap_minutes


def _finish(candidate: Decision, params: Params, now: Optional[float],
            last_forced_sleep_at: Optional[float],
            required_gap: Optional[float] = None) -> Decision:
    """Debounce a machine that is being poked into a wake/sleep loop."""
    if candidate.action is not Action.SLEEP or last_forced_sleep_at is None or now is None:
        return candidate
    gap = params.min_gap_minutes if required_gap is None else required_gap
    age = now - last_forced_sleep_at
    if age < gap * 60.0:
        return Decision(Action.SKIP, "debounce(%.0fs<%.0fs)" % (age, gap * 60.0),
                        candidate.src, link=candidate.link, who=candidate.who)
    return candidate