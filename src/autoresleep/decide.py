"""The v2 decision engine: pure logic, no OS calls, no I/O.

This is the piece worth porting carefully from v1 (`resleep.ps1`): the *ordering* of the
evidence chains is the whole product. Everything OS-specific lives behind
`autoresleep.system`; everything here is plain data in, plain decision out, so it can be
unit-tested on every platform (and on a laptop with no Wi-Fi at all).

Precedence, strongest claim first (mirrors 00-README-sources.md section 5), and deliberately
DRIVER-FREE: no chain needs the NIC driver to explain itself, because no driver API is portable
across the three platforms (and the portable ones lie after a fixed-feature NIC wake).

  1. a wake that was ASKED FOR (magic packet / sender marker)  -> never re-sleep; latched for the session
  2. nothing may sleep a machine that is in use                 -> guards
  3. hands on the machine - at the wake, or anywhere inside the window -> intentional (input)
  4. the network MOVED while we slept (observed, not reported)   -> the case this tool exists for
  5. the DRIVER's wake reason, when a driver happens to give one  -> recorded, NEVER decisive
  6. an OS attribution naming a NIC                              -> judged against `wake_nics`
     (timer / other device / fixed-feature                        -> treated as intentional)
  7. unattributable wake                                          -> do not guess: long idle + policy

Two clocks, one wait (docs/resleep-window-plan.md, section 1.1): a NIC-caused wake waits
`resleep_delay_seconds` counted from the WAKE moment, and inside that window a magic packet or a
keypress cancels the re-sleep. `idle_minutes` survives only as the fallback for evidence with no
session stamp (a box whose collector never ran), which is what every pre-window test pins.
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
    os_link: Optional[str] = None           # 'LINK_LOST' | 'LINK_INTACT' | 'UNKNOWN' | None
    os_link_why: Optional[str] = None       # 'bssid-changed:..->..', 'carrier-changed-1x-..', ...
    wake_kind: Optional[str] = None         # attribution carried by the evidence file
    wake_who: Optional[str] = None
    wake_by_input: Optional[bool] = None    # hands on the machine at the wake moment
    observed_at: Optional[float] = None     # when the evidence was collected (= resume moment)
    session: Optional[float] = None         # which wake session this evidence describes
    session_cause: Optional[str] = None     # 'asked-for' | 'input', latched for that session
    slept_total: Optional[float] = None     # the machine's accumulated suspend time when the
                                            # evidence was written; a latch written at a smaller
                                            # value has been overtaken by a later sleep
    input_source: Optional[str] = None      # 'evdev' (hardware truth) | 'x11-idle' (reset by a resume)
    #: Did an input event land INSIDE the re-sleep window? The hardware watcher states this as a
    #: fact; None means no watcher wrote it (and the desktop idle clock, which a resume resets to
    #: zero, must not be trusted to say "somebody is there").
    window_input: Optional[bool] = None
    #: Is the adapter that woke the machine one the operator asked us to police (`wake_nics`)?
    #: False means "not ours, leave it alone"; None means "cannot tell which adapter it was".
    wake_nic_policed: Optional[bool] = None


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
    # --- the re-sleep window (docs/resleep-window-plan.md) ---------------------------------
    #: How long a NIC-caused wake waits before it is slept again, counted from the WAKE moment.
    #: Inside this window a magic packet or a keypress cancels the re-sleep; the machine is never
    #: slept inside it. This replaced `idle_minutes` as the wait (Q4a).
    resleep_delay_seconds: float = 10.0
    #: "Only a WoL magic packet may wake this machine": NIC-class wakes stop being judged on the
    #: OS/driver's account of them and become re-sleep candidates outright (packets and hands still
    #: cancel it). The idle gate still applies - Q2: not "sleep it instantly".
    magic_only: bool = False
    #: Hands on the machine inside the window cancel the re-sleep. False = the strict mode the
    #: operator can choose: input inside the window is recorded and ignored, and it still sleeps.
    resleep_break_on_input: bool = True


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


def _session_age(evidence: Evidence, now: Optional[float]) -> Optional[float]:
    """How long this wake session has been running, when the evidence carries a stamp."""
    if not now or not evidence.observed_at:
        return None
    return max(0.0, now - float(evidence.observed_at))


def packet_is_this_wakes_cause(pkt_age_s: Optional[float], session_age_s: Optional[float],
                              window_s: float) -> bool:
    """Was the packet heard around THIS resume, rather than hours into an already-awake session?

    Both ages run off the same clock, so the packet that woke us and the resume itself show up as
    two nearly equal numbers; a packet sent to a machine that was already awake shows a small
    packet age against a large session age. Falling back to the plain window when there is no
    session stamp keeps this usable on a box whose collector never ran.
    """
    if pkt_age_s is None or pkt_age_s > window_s:
        return False
    if session_age_s is None:
        return True
    return abs(pkt_age_s - session_age_s) <= window_s


def decide(evidence: Evidence, guards: Guards, idle_s: float, params: Params = Params(),
           now: Optional[float] = None, last_forced_sleep_at: Optional[float] = None,
           input_since_sleep: Optional[bool] = None, wake_kind: Optional[str] = None,
           wake_who: Optional[str] = None, wake_by_input: Optional[bool] = None) -> Decision:
    """Decide whether to force the machine back to sleep. Never performs the sleep."""
    link = evidence.os_link

    # 1. asked for ---------------------------------------------------------------------
    # FIRST, and never entered into the sleep logic at all: a listened-for packet means somebody
    # wants this machine awake. Two halves, because they answer different questions:
    #  - the latch: "this session was asked for" - once established it holds for the whole
    #    session, so the machine is not re-slept minutes later when the packet ages out;
    #  - the window: "the packet heard just now IS this wake's cause" - a packet seen while the
    #    machine has been awake for hours is a packet, not the reason we are awake.
    if evidence.session_cause == "asked-for":
        return Decision(Action.SKIP, "wake-requested", "pkt", link=link,
                        who="asked-for-this-session")
    if evidence.pkt_kind and packet_is_this_wakes_cause(evidence.pkt_age_s,
                                                       _session_age(evidence, now),
                                                       params.pkt_window_s):
        return Decision(Action.SKIP, "wake-requested", "pkt", link=link,
                        who="%s@%+ds" % (evidence.pkt_kind, evidence.pkt_age_s))

    # 2. guards ------------------------------------------------------------------------
    active = guards.active()
    if active:
        return Decision(Action.SKIP, "guarded:" + active, "mingle", link=link)

    # 3. hands on the machine: at the wake, or anywhere inside the window -----------------
    # Driver-free, and now with two clocks that answer the same question:
    #  * `wake_by_input` - the input clock says the last input landed at/after the wake moment.
    #    True of a USB/HID wake on all three platforms; the desktop idle counter is reset BY the
    #    resume, so only the hardware (evdev) clock may ever latch a session on this.
    #  * `evidence.window_input` - an input event landed during the `resleep_delay_seconds` that
    #    follow the wake. Broader, and it is what makes "I nudged the mouse five seconds later"
    #    survive a resume that reset every desktop idle counter in the room.
    # Like the packet chain this exempts the WHOLE session (Q6): an operator who nudged the mouse
    # does not expect the machine asleep three minutes later. The one exception is the operator's
    # own strict mode, below - and it is never silent about it.
    if evidence.session_cause == "input":
        return Decision(Action.SKIP, "wake-by-input", "input", link=link,
                        who="input-this-session")
    input_in_window = (evidence.window_input if evidence.window_input is not None
                       else wake_by_input)
    strict_note: Optional[str] = None
    if input_in_window:
        if params.resleep_break_on_input:
            # name the clock in `who=`: the hardware clock and a desktop idle counter disagree
            # exactly when it matters, so every decision must say which one produced it
            who = ("input-at-wake" if evidence.input_source is None
                   else "input-at-wake(%s)" % evidence.input_source)
            if evidence.window_input is True and not wake_by_input:
                who = "input-in-window"
            return Decision(Action.SKIP, "wake-by-input", "input", link=link, who=who)
        # Strict mode (resleep_break_on_input=False): the operator asked for the machine to be
        # slept even with hands on it. Recorded here and named in whatever decision follows, so
        # "we knew somebody pressed a key and slept it anyway" is visible in the log.
        strict_note = "input-ignored-by-strict-mode"

    # 4. the network moved while we slept ----------------------------------------------
    # THE portable replacement for "ask the NIC driver why it woke us": the pre-sleep snapshot
    # compared against what is observable now (association, BSSID, the kernel's carrier-change
    # counter, the OS's own reconnect events). A wifi that dropped and came back - even to the
    # same AP - is a fact the OS publishes, not an opinion the driver volunteers.
    if link == "LINK_LOST":
        return _finish(_resleep(params.idle_minutes, "NON_MAGIC_WLAN_LINK_MOVED", "link",
                                who=evidence.os_link_why or "link-moved", idle_s=idle_s, link=link,
                                evidence=evidence, params=params, now=now),
                       params, now, last_forced_sleep_at,
                       required_gap=_gap(params, input_since_sleep, nic_attributed=True),
                       strict_note=strict_note)

    # 5. the driver's own wake signal: RECORDED, NEVER DECISIVE -------------------------
    # Deliberately no decision here. A driver's wake reason is a per-driver private string: it
    # exists only when that driver chooses to print one, and two cards describing the same event
    # use different fields. Sleeping on it means sleeping on a vendor's vocabulary.
    # The portable replacement lives above (packets, input, the link) - this block only makes the
    # string visible in the log so a human can compare it with what the listeners recorded.
    drv = _classify_drv(evidence.drv_reason)
    if drv:
        link = link or None
        # (no return: the decision falls through to the OS attribution / long-idle rules)

    # 6. OS-level attribution: which device the OS says poked us ------------------------
    kind = (wake_kind or evidence.wake_kind or "").lower()
    if wake_who is None:
        wake_who = evidence.wake_who
    if kind in ("eth-nic", "wifi-nic"):
        # A NIC the operator asked us to police (`wake_nics`; [] = every wired + every wireless).
        # A wired card is judged exactly like a wireless one now - "防止 wifi 或者有线网卡等设备异常
        # 唤醒" is one requirement, not two - and an adapter that is NOT in the list is somebody
        # else's wake, left alone.
        if evidence.wake_nic_policed is False:
            return Decision(Action.SKIP, "wake-source-" + kind + "-not-policed", "os",
                            link=link, who=wake_who)
        if kind == "eth-nic":
            reason = "NON_MAGIC_ETHERNET_NIC"
        else:
            reason = "NON_MAGIC_WLAN_LINK_LOST" if link == "LINK_LOST" else "NON_MAGIC_WIFI_NIC"
        return _finish(_resleep(params.idle_minutes, reason, "os", who=wake_who, idle_s=idle_s,
                                link=link, evidence=evidence, params=params, now=now),
                       params, now, last_forced_sleep_at,
                       required_gap=_gap(params, input_since_sleep, nic_attributed=True),
                       strict_note=strict_note)
    if kind == "fixed-feature-nic-suspect":
        # The OS could not name the device, no wake timer exists, and the only armed devices are
        # NICs. A power-button wake looks identical, so this stays an explicit policy switch rather
        # than a silent default - except under `magic_only`, where the operator has already said a
        # NIC-shaped wake is unwanted unless a magic packet asked for it.
        if params.magic_only or params.unattributed_wake_policy == "resleep-if-unattended":
            return _finish(_resleep(params.idle_minutes, "NON_MAGIC_FIXED_FEATURE_NIC_SUSPECT",
                                    "os", who=wake_who, idle_s=idle_s, link=link,
                                    evidence=evidence, params=params, now=now),
                           params, now, last_forced_sleep_at, strict_note=strict_note)
        return Decision(Action.SKIP, "wake-source-fixed-feature-nic-suspect", "os", link=link)
    if kind in ("other-device", "timer", "fixed-feature"):
        # Not a NIC: a timer the OS programmed, a HID/other device, or a wake the OS attributed to
        # something that is not a network card. Deliberately left alone - `magic_only` narrows the
        # NIC cases, it does not turn this into "sleep the machine on anything".
        return Decision(Action.SKIP, "wake-source-" + kind, "os", link=link, who=wake_who)

    # 7. no usable attribution: do not guess, demand a long idle ------------------------
    if idle_s >= params.unknown_idle_minutes * 60.0:
        return _finish(Decision(Action.SLEEP, "NON_MAGIC_UNATTRIBUTED", "policy", link=link),
                       params, now, last_forced_sleep_at, strict_note=strict_note)
    return Decision(Action.SKIP, "unknown-wake-needs-long-idle", "policy", link=link)

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


def _sleep_blocked(evidence: Optional[Evidence], params: Optional[Params], now: Optional[float],
                   needed_minutes: float, idle_s: float) -> Optional[str]:
    """Why a NIC-caused wake may NOT be slept yet: 'window-open', 'user-present', or None.

    Two clocks, one wait (docs/resleep-window-plan.md section 1.1). Which one is in play depends
    on whether the evidence carries a session stamp, i.e. whether the window can be measured:

      * measurable (collected at the resume): the WINDOW is the wait. Inside
        `resleep_delay_seconds` the machine is never slept - we are still watching for the packet
        and for hands - and once it is over, "nobody is using it" means no input event landed in
        it (`window_input`). If no watcher wrote that state, fall back to the desktop idle clock
        rather than trusting a clock a resume resets to zero.
      * not measurable (a box whose collector never ran - and every pre-window test): the old IDLE
        gate stands, so the tool still refuses to sleep a machine somebody is using.
    """
    if evidence is None or params is None:
        return None if idle_s >= needed_minutes * 60.0 else "user-present"
    age = _session_age(evidence, now)
    if age is None:
        return None if idle_s >= needed_minutes * 60.0 else "user-present"
    if age < params.resleep_delay_seconds:
        return "window-open"
    # The window's verdict is about the N seconds right after the WAKE - it must not still be
    # authorizing sleeps hours later. If the machine has been used since the wake (the last input
    # is NEWER than the wake), that verdict is stale and the ordinary idle gate stands: otherwise a
    # session that began this morning would let a sleep land on somebody who has been typing for
    # the last ten seconds. `idle_s < age` is exactly "touched after the wake".
    if idle_s < age:
        return None if idle_s >= needed_minutes * 60.0 else "user-present"
    if evidence.window_input is None and idle_s < needed_minutes * 60.0:
        return "user-present"
    return None


def _resleep(needed_minutes: float, reason: str, src: str, who: Optional[str],
             idle_s: float, link: Optional[str], evidence: Optional[Evidence] = None,
             params: Optional[Params] = None, now: Optional[float] = None,
             skip_reason: str = "user-present") -> Decision:
    """May this NIC-caused wake be slept again, on the clock the plan chose (see _sleep_blocked)."""
    blocked = _sleep_blocked(evidence, params, now, needed_minutes, idle_s)
    if blocked == "window-open":
        age = _session_age(evidence, now) if evidence is not None else None
        return Decision(Action.SKIP, "window-open", src, link=link, who=who,
                        details={"age_s": round(age, 1) if age is not None else None,
                                 "window_s": params.resleep_delay_seconds if params else None})
    if blocked:
        return Decision(Action.SKIP, skip_reason, src, link=link, who=who,
                        details={"idle_s": round(idle_s, 1),
                                 "needed_s": needed_minutes * 60.0})
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
            required_gap: Optional[float] = None,
            strict_note: Optional[str] = None) -> Decision:
    """The last gates a sleeping candidate passes: the strict-mode record, then the debounce.

    Strict mode (`resleep_break_on_input=False`) *chose* to sleep a machine with hands on it, and
    the log has to say so: `who=input-ignored-by-strict-mode` is the difference between "we missed
    the keypress" and "we were told to ignore it" - which is the whole question the next morning's
    log is read to answer.
    """
    if candidate.action is Action.SLEEP and strict_note:
        candidate = Decision(candidate.action, candidate.reason, candidate.src,
                             link=candidate.link,
                             who=("%s+%s" % (candidate.who, strict_note) if candidate.who
                                  else strict_note),
                             details=candidate.details)
    if candidate.action is not Action.SLEEP or last_forced_sleep_at is None or now is None:
        return candidate
    gap = params.min_gap_minutes if required_gap is None else required_gap
    age = now - last_forced_sleep_at
    if age < gap * 60.0:
        return Decision(Action.SKIP, "debounce(%.0fs<%.0fs)" % (age, gap * 60.0),
                        candidate.src, link=candidate.link, who=candidate.who)
    return candidate