"""`autoresleep <command>` - the whole tool, one entry point, three platforms.

Command set is deliberately small and identical everywhere:

  doctor     what this machine can actually do (run this first on a new box)
  judge      decide whether to go back to sleep; `--apply` actually suspends
  listen     resident daemon: magic packet / marker detection + resume detection
  collect    write the wake-source evidence (called on resume)
  install    config + native scheduler artifacts (+ firewall rule unless --no-firewall)
  uninstall  remove everything, with an explicit list of what it touched
  config     show or change the thresholds

Safety default that differs from v1 on purpose: `judge` decides and logs, and only suspends
with `--apply`. v1 shipped `-DryRun` as an extra flag; a cross-platform tool gets run by
people who have never read the script, so the destructive half is opt-in.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, journal, paths, resume as resume_mod
from .resume import SuspendLedger
from .config import LIST_OF_STR_KEYS, Config
from .decide import (Action, Decision, Evidence, Guards, decide,
                    packet_is_this_wakes_cause)
from .pkt import (DEFAULT_MARKER_PORT, DEFAULT_PORTS, WOL_PORTS, Listener, RawMagicSocket,
                  adapters, select_wake_nics, udp_ports_in_use, wake_macs)
from .inputwatch import InputWatcher, read_last_input
from . import windowwatch
from .iwevent import IwEventWatcher, wifi_moves_around_resume
from .scheduling import artifacts_for
from .scheduling.artifact import launcher_parts
from . import elevate
from .system import (get_system, read_evidence, read_session_cause, write_session_cause,
                     write_wake_evidence)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _system(platform: Optional[str] = None):
    return get_system(platform)


def _load_config(args) -> Config:
    cfg = Config.load(Path(args.config) if getattr(args, "config", None) else None)
    if getattr(args, "idle_minutes", None) is not None:
        cfg.idle_minutes = args.idle_minutes
    if getattr(args, "unknown_idle_minutes", None) is not None:
        cfg.unknown_idle_minutes = args.unknown_idle_minutes
    if getattr(args, "unattributed_wake_policy", None) is not None:
        cfg.unattributed_wake_policy = args.unattributed_wake_policy
    return cfg.validate()


def _nic_selection(cfg: Config, log=None):
    """(selection, warning): resolve `wake_nics` against THIS machine's adapters.

    An entry that matches nothing is a NIC nobody is policing - the failure mode is a machine
    that keeps waking itself, so it is never silent: `doctor` prints it and the listener logs it.
    """
    sel = select_wake_nics(cfg.wake_nics)
    warn = None
    if sel.unmatched:
        warn = ("wake_nics entries that match no adapter on this machine: %s - those NICs are NOT "
                "policed (`doctor` lists the adapters this box has)" % ", ".join(sel.unmatched))
        if log is not None:
            log.warning("wake_nics: %s", warn)
    return sel, warn


def _may_latch_input(source: Optional[str]) -> bool:
    """May this input source create a SESSION-wide exemption?

    Two clocks can now say "somebody was there", and only hardware-grade ones may latch (Q6):
    `evdev` (real events, Linux sensor) and a `*-window-tick` DELTA (the platform clock sampled
    across the window, which a resume cannot fake - only a CHANGE is ever counted). The
    wake-moment fallback (`x11-idle`) may not: a resume resets that counter, and latching on it is
    exactly how a wifi wake kept a machine awake for a whole day.
    """
    return bool(source) and (source == "evdev" or source.endswith("-window-tick"))


def _window_input(ev, sysinfo, cfg, sample: bool = False):
    """(input_in_window, source) for the wake this evidence describes - the window's one question.

    Linux answers it with hardware truth: real evdev events, written by the privileged sensor.
    The other two platforms have no such sensor, so the platform's own input clock is sampled
    across the window and judged by its DELTA (`windowwatch`) - an absolute reading is worthless
    there, because a resume resets the counter. `sample=True` (the listener, once a second) folds a
    fresh reading in; the judge just reads the state the listener left behind.

    None means "nothing sampled this window": the judgement then falls back to the desktop idle
    clock rather than pretending nobody was there.
    """
    anchor = float(getattr(ev, "observed_at", None) or 0)
    if not anchor:
        return None, None
    until = anchor + float(cfg.resleep_delay_seconds)
    epoch, _dev, live = read_last_input(paths.input_last_file())
    if live:
        # the hardware clock: an event inside the window is a person, one before it is the wake
        if epoch is None:
            return False, "evdev"
        return (anchor - 1.0) <= float(epoch) <= until, "evdev"
    try:
        tick = sysinfo.idle().tick
    except Exception:
        return None, None
    source = "%s-window-tick" % sysinfo.name
    if sample:
        windowwatch.sample(paths.window_input_file(), anchor, until, tick, source=source)
    state = windowwatch.read_state(paths.window_input_file())
    if state is None or float(state.anchor or 0) != anchor:
        return None, source
    return state.input, source


def _window_age(ev) -> Optional[float]:
    """Seconds since the wake this evidence describes, when it carries a stamp."""
    try:
        anchor = float(getattr(ev, "observed_at", None) or 0)
    except (TypeError, ValueError):
        return None
    return max(0.0, time.time() - anchor) if anchor else None


def _fast_path_ready(cfg: Config, idle_probe) -> bool:
    """Is it time to judge? The window when we can measure it, the idle threshold otherwise.

    Q4a: the window IS the wait for a NIC-caused wake, so the fast path has to fire when it closes
    - waiting for `idle_minutes` first adds two minutes to every cycle and makes the configured ten
    seconds meaningless. With no wake stamp (a box whose collector never ran) the old gate stands.
    """
    age = _window_age(read_evidence())
    if age is not None:
        return age >= float(cfg.resleep_delay_seconds)
    return idle_probe() >= cfg.idle_minutes * 60.0


def _sample_window(cfg: Config, sysinfo, log=None) -> None:
    """Fold one reading of the platform input clock into the window that is open right now.

    Called once a second by the listener - the only process awake for the whole window. On Linux
    this is a no-op while the privileged sensor is writing the hardware clock; on Windows and macOS
    it is the only way the window gets an answer at all (`windowwatch`).
    """
    ev = read_evidence()
    if not ev.observed_at:
        return
    try:
        _window_input(ev, sysinfo, cfg, sample=True)
    except Exception as exc:            # never let sampling break the packet chain
        if log is not None:
            log.debug("window sample failed: %s", exc)


def _window_label(ev) -> str:
    """`win=` for a decision line: was there input in the window, and who says so.

    Printed on every judgement because the next morning's question is "why did it sleep / why did
    it not", and "the window saw nobody" is a different answer from "nothing sampled the window".
    """
    if ev.window_input is None:
        return "-"
    source = ("@" + ev.input_source) if ev.input_source else ""
    return ("in" if ev.window_input else "none") + source


def _last_forced_sleep_info():
    """(epoch, input tick) of the sleep WE issued.

    The tick is what makes "nobody touched it since" a fact instead of a guess: if the current
    input tick equals the one recorded at the suspend, not a single input event happened in
    between. v1 kept the same pair in state-last-forced-sleep / state-input-tick-at-sleep.
    """
    f = paths.forced_sleep_marker()
    if not f.exists():
        return (None, "")
    try:
        parts = f.read_text(encoding="utf-8").strip().split()
        return (float(parts[0]), parts[1] if len(parts) > 1 else "")
    except Exception:
        return (None, "")


def _last_forced_sleep() -> Optional[float]:
    return _last_forced_sleep_info()[0]


def _mark_forced_sleep(when: Optional[float] = None, tick: str = "") -> None:
    paths.ensure_data_dir()
    paths.forced_sleep_marker().write_text("%s %s" % (when or time.time(), tick), encoding="utf-8")


# --------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    cfg = _load_config(args)
    sysinfo = _system(args.platform)
    log = journal.get_logger(also_stderr=True)

    _print("=" * 66)
    _print(" AutoReSleep v%s - doctor" % __version__)
    _print("=" * 66)
    _print(" python       : %s (%s)" % (sys.version.split()[0], sys.executable))
    _print(" platform     : %s" % sys.platform)
    _print(" backend      : %s" % sysinfo.describe())
    _print(" data dir     : %s" % paths.data_dir())
    _print(" config       : %s" % (Path(args.config) if getattr(args, "config", None)
                                   else paths.config_file()))
    _print()

    idle = sysinfo.idle()
    _print("---- idle probe (this decides everything) ----")
    if idle.seconds >= 0:
        _print("  idle         : %.1f s (%.1f min)   via %s" % (idle.seconds, idle.seconds / 60,
                                                                idle.method))
        _print("  raw tick     : %s" % (idle.tick or "-"))
    else:
        _print("  idle         : UNAVAILABLE - %s" % (idle.error or "unknown error"))
        _print("  -> without this the judge cannot run; the tool would either never sleep or")
        _print("     sleep a machine in use, so it refuses to act instead")
    _print()

    _print("---- adapters (MACs to match magic packets against) ----")
    ads = adapters()
    if not ads:
        _print("  none found - the packet chain will only see the sender marker")
    for a in ads:
        _print("  %-14s %-17s %s%s" % (a.name, a.mac, "wifi" if a.wireless else "wired",
                                       ("  " + a.label) if a.label and a.label != a.name else ""))
    _print()

    _print("---- which NICs are policed (config: wake_nics) ----")
    sel, nic_warn = _nic_selection(cfg)
    _print("  configured: %s" % (cfg.wake_nics or "[] = every wired + every wireless NIC"))
    _print("  resolved  : %s" % sel.line())
    _print("  magic MACs: %s" % (", ".join(sel.macs()) or "none - only the sender marker counts"))
    if nic_warn:
        _print("  [warn] %s" % nic_warn)
    _print()

    _print("---- last known wake source (evidence file) ----")
    ev = read_evidence()
    _print("  pkt   : %s" % ("%s at %+.0fs" % (ev.pkt_kind, ev.pkt_age_s or 0) if ev.pkt_kind else "no packet evidence"))
    _print("  drv   : %s" % (ev.drv_reason or "no driver report"))
    _print("  os    : %s%s" % (ev.os_link or "no link evidence",
                               (" (" + ev.os_link_why + ")") if ev.os_link_why else ""))
    live = sysinfo.wake_source()
    _print("  live  : kind=%s who=%s timer=%s" % (live.kind, live.who or "-", live.timer))
    _print("          method=%s" % live.method)
    if live.raw:
        _print("          raw=%s" % live.raw[:300])
    _print()

    _print("---- guards on this machine right now ----")
    g = sysinfo.guards()
    _print("  remote-in-use=%s display-in-use=%s media-playing=%s update-running=%s"
           % (g.remote_in_use, g.display_in_use, g.media_playing, g.update_running))
    if g.active():
        _print("  -> a judgement right now would skip: %s" % g.active())
    _print()

    _print("---- link (driver-free: is the network where we left it?) ----")
    st = sysinfo.link_state()
    if st is None:
        _print("  no link probe on this platform")
    else:
        link, why = sysinfo.link_evidence()
        _print("  state : connected=%s bssid=%s ssid=%s changes=%s"
               % (st.get("connected"), st.get("bssid") or "-", st.get("ssid") or "-",
                  st.get("changes")))
        _print("  vs pre-sleep snapshot: %s  (%s)" % (link, why))
    _print()

    _print("---- WoL ports the listener can watch ----")
    probe = Listener(ports=cfg.ports, marker_port=cfg.marker_port)
    probe.bind()
    _print("  bound : %s" % (", ".join(str(x) for x in sorted(probe.sockets)) or "none"))
    if probe.unavailable():
        _print("  refused: %s" % ", ".join("%d (%s)" % (x, e)
                                           for x, e in sorted(probe.unavailable().items())))
    _print("  hint  : %s" % (probe.advice() or "all magic ports are watchable"))
    probe.close()
    if sys.platform.startswith("linux"):
        from .scheduling.linux import SENSOR_UNIT_NAME
        active = _unit_active(SENSOR_UNIT_NAME)
        _print("  sensor: %s" % ("active - the privileged half watches UDP 9/7"
                                 if active
                                 else "not active (install registers it; see the hint above)"))
        _print("  raw   : %s" % ("bare 0x0842 magic frames are watched too (CAP_NET_RAW)"
                                 if active else "not watched without the sensor unit"))
        # the input clock decides keypress-vs-wifi wakes, so WHICH clock is in use must be visible
        epoch, dev, live = read_last_input(paths.input_last_file())
        if live:
            age = "never" if epoch is None else "%.0fs ago" % max(0.0, time.time() - epoch)
            from .inputwatch import discover_devices
            _print("  input : hardware (evdev), %d device(s), last real event %s%s"
                   % (len(discover_devices()), age, (" (%s)" % dev if dev else "")))
        else:
            _print("  input : no hardware clock (sensor not running) - falling back to the "
                   "desktop idle counter, which a resume resets to 0, and that fallback may not "
                   "exempt a session")
        events = paths.wlan_event_log()
        _print("  wlan  : %s" % ("nl80211 events -> %s" % events if active
                                 else "no iw-event tap without the sensor unit"))
    _print()

    _print("---- firewall (can the listener's UDP ports be reached?) ----")
    fw = sysinfo.firewall()
    _print("  state : %s   %s" % (fw.state, fw.detail))
    if fw.rule_names:
        _print("  rules : %s" % ", ".join(fw.rule_names[:10]))
    for hint in fw.hints:
        _print("  hint  : %s" % hint)
    _print()

    if sys.platform.startswith("linux"):
        from .scheduling.linux import linger_enabled
        _linger = linger_enabled()
        _print("---- will the units survive a logout? ----")
        if _linger is False:
            _print("  Linger=no: the user units stop at logout, so after a reboot with no logon")
            _print("             nothing is watching this machine. Fix once as root:")
            _print("             sudo loginctl enable-linger %s" % (os.environ.get("USER") or "$USER"))
        elif _linger is True:
            _print("  Linger=yes: the units keep running without a login session")
        else:
            _print("  could not tell (loginctl unavailable)")
        _print()

    _print("---- config ----")
    for key in ("unknown_idle_minutes", "unattributed_wake_policy", "repeat_minutes",
                "min_gap_minutes", "pkt_window_s", "ports", "marker_port"):
        _print("  %-26s %s" % (key, getattr(cfg, key)))
    _print("  %-26s %s   (deprecated as a wait: see the window below)"
           % ("idle_minutes", cfg.idle_minutes))
    _print()
    _print("---- re-sleep window (this is what decides how long a NIC wake waits) ----")
    _print("  %-26s %ss   measured from the WAKE moment" % ("resleep_delay_seconds",
                                                              cfg.resleep_delay_seconds))
    _print("  %-26s %s%s" % ("magic_only", cfg.magic_only,
                             "  (only a magic packet may wake this machine)" if cfg.magic_only
                             else "  (any NIC wake is judged: packets, then input, then the link)"))
    _print("  %-26s %s%s" % ("resleep_break_on_input", cfg.resleep_break_on_input,
                             "  (hands on the machine inside the window cancel the sleep)"
                             if cfg.resleep_break_on_input
                             else "  (strict: input inside the window is ignored and it still sleeps)"))
    _print("  %-26s %s" % ("wake_nics", cfg.wake_nics or "[] = all wired + all wireless"))
    # Where the window actually is right now: the answer to "did anybody touch it in those N
    # seconds" is the whole point of this feature, so it must be visible without reading logs.
    _ev = read_evidence()
    _age = _window_age(_ev)
    _wstate = windowwatch.read_state(paths.window_input_file())
    _print("  %-26s %s" % ("this wake", "no wake stamp yet (no collector has run)"
                           if _age is None else "%.0fs ago" % _age))
    if _age is not None:
        _print("  %-26s %s" % ("window now", "OPEN (still watching for a packet or hands)"
                               if _age < cfg.resleep_delay_seconds else "closed"))
    _print("  %-26s %s" % ("window input", _wstate.describe() if _wstate else "not sampled"))
    hb = journal.read_heartbeat()
    _print()
    _print("---- is it alive? ----")
    _print("  heartbeat: %s" % (hb or "never ran"))
    log.info("doctor: platform=%s idle=%.1fs", sys.platform, idle.seconds)
    return 0


# --------------------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------------------
def _collect_evidence(sysinfo, src=None, slept_total=None, session=None, observed_at=None):
    """Write the wake evidence for the current wake. The one writer, shared by the collector and
    by the judge's self-heal path, so both mint a session stamp the same way."""
    src = src or sysinfo.wake_source()
    link, link_why = sysinfo.link_evidence(src.wake_epoch)
    if slept_total is None:
        slept_total = SuspendLedger().state()
    path = write_wake_evidence(src, link=link, link_why=link_why, chain=src.chain,
                               session=session, observed_at=observed_at,
                               slept_total=slept_total)
    return src, link, link_why, path


def build_judgement(args, sysinfo=None, cfg: Optional[Config] = None):
    sysinfo = sysinfo or _system(args.platform)
    cfg = cfg or _load_config(args)
    idle = sysinfo.idle()

    # Attribute THIS wake, every run. The live query is cheap and it is the only way to know
    # which wake we are judging: a stored attribution from an earlier wake is history, not
    # evidence (read_evidence rejects it by wake time).
    live = sysinfo.wake_source()
    ev = read_evidence(current_wake_epoch=live.wake_epoch)
    # The live ledger reading: how much this machine has slept, in total. A latch written before it
    # moved must not apply, and evidence written before it moved describes an EARLIER wake - even
    # when nobody noticed that resume (a 17-second sleep is below the wall-vs-monotonic threshold,
    # which is exactly how an idle machine got stuck awake). Re-collect: that mints the new session
    # stamp which retires the old latch.
    slept_now = SuspendLedger().state()
    if (ev.slept_total is not None and slept_now is not None
            and slept_now > ev.slept_total + resume_mod.LEDGER_EPSILON_S):
        journal.get_logger().info(
            "judge: machine slept since the evidence was written (%.1fs vs %.1fs) -> re-collecting",
            ev.slept_total, slept_now)
        _collect_evidence(sysinfo, src=live, slept_total=slept_now)
        ev = read_evidence(current_wake_epoch=live.wake_epoch)
    # Probe the link whenever the stored evidence has none. This comparison against the
    # pre-sleep snapshot is what separates a wifi-disconnect wake from an asked-for one, and it
    # needs no driver - so it must not be gated on the OS having attributed the wake first.
    if not ev.os_link:
        link, link_why = sysinfo.link_evidence(live.wake_epoch)
        # Re-writing the SAME resume must keep its session stamp: a fresh one would look like a new
        # wake and drop the "this wake was asked for" latch that exempts the session from sleep.
        write_wake_evidence(live, link=link, link_why=link_why,
                            session=ev.session, observed_at=ev.observed_at,
                            slept_total=slept_now)
        ev = read_evidence(current_wake_epoch=live.wake_epoch)
    # Why this SESSION was wanted, if it was established near the resume (a magic packet heard, or
    # hands on the machine). Read from the latch because the facts themselves are only visible in a
    # window: the packet ages out and the idle clock moves on, the latch does not.
    # The live ledger reading: a latch written before the machine slept again must not apply, even
    # if nobody noticed that resume (a 17-second sleep is below the skew threshold).
    if ev.session_cause is None:
        ev.session_cause = read_session_cause(ev.session, slept_now)
    guards = sysinfo.guards()
    forced, forced_tick = _last_forced_sleep_info()
    # False is a positive statement ("no input since we slept it"), None means "cannot tell"
    input_since_sleep = None
    if forced_tick and idle.tick:
        input_since_sleep = (idle.tick != forced_tick)
    now = time.time()
    # best available stand-in for "when did this wake happen": the OS's own stamp, else the
    # moment the listener collected the evidence, else our own last forced sleep
    wake_epoch = live.wake_epoch or ev.observed_at or forced
    # Two input clocks, and the choice matters. The hardware one (real evdev events, written by the
    # privileged sensor) is truth. The desktop idle counter is reset BY the resume, so it says
    # "hands on it" for a wifi wake - it stays only as a fallback for a box with no watcher, and it
    # is never allowed to latch a session-wide exemption.
    hw_epoch, hw_dev, hw_live = read_last_input(paths.input_last_file())
    if hw_live:
        wake_by_input = hardware_input_at_wake(now, hw_epoch, wake_epoch)
        fallback_source = "evdev"
    else:
        wake_by_input = input_at_wake(now, idle.seconds, wake_epoch)
        fallback_source = "x11-idle" if wake_by_input is not None else None
    # The window asks a different question from "was there input AT the wake": that one is a
    # moment, this one is a period - the N seconds the operator configured. When the window has an
    # answer it is the better clock on all three platforms (a delta cannot be faked by a resume),
    # so it is the one the engine uses; the wake-moment fact stays in `who=` for forensics.
    window_input, window_source = _window_input(ev, sysinfo, cfg)
    ev.window_input = window_input
    if hw_live:
        ev.input_source = "evdev"
    elif window_input:
        ev.input_source = window_source
    else:
        ev.input_source = fallback_source
    decision = decide(ev, guards, idle_s=idle.seconds, params=cfg.params(), now=now,
                      last_forced_sleep_at=forced,
                      input_since_sleep=input_since_sleep, wake_kind=None,
                      wake_by_input=wake_by_input)
    # Latch the exemption for the rest of THIS session, here rather than only where the evidence is
    # seen: the judgement is the one place that says what a wake was, for the timer, the listener
    # fast path and `judge --dry-run` alike.
    if str(decision.reason) == "wake-requested" and ev.pkt_kind:
        write_session_cause("asked-for", ev.session, slept_now)
    elif str(decision.reason) == "wake-by-input" and _may_latch_input(ev.input_source):
        # ONLY a hardware-grade clock may create a session-wide exemption: the desktop idle counter
        # is reset by a resume, so latching on it exempts a wifi wake for the whole session and the
        # machine never sleeps again - exactly what the operator reported.
        write_session_cause("input", ev.session, slept_now)
    return decision, idle, ev, guards, cfg


#: reasons that are a GUESS about an unattributable wake - these keep the pre-suspend grace
GUESS_REASONS = ("NON_MAGIC_FIXED_FEATURE_NIC_SUSPECT", "NON_MAGIC_UNATTRIBUTED")


def _is_guess(decision) -> bool:
    return str(decision.reason) in GUESS_REASONS or str(decision.src) == "policy"


def grace_verdict(before_tick: str, after_tick: str, idle_after_s: float, needed_s: float,
                  guards_active: Optional[str]) -> Optional[str]:
    """v1's tier-A grace, before actually suspending: did anything change while we waited?

    The decision to suspend on a *suspect* attribution is a guess by construction, so the last
    thing before the suspend is to look again: any input since the decision means a human is
    there, and a machine in use must never be slept. Returns a skip reason, or None to proceed.
    """
    if guards_active:
        return "guarded-after-grace:" + guards_active
    if before_tick and after_tick and before_tick != after_tick:
        return "input-after-grace"
    if idle_after_s >= 0 and idle_after_s < needed_s:
        return "user-present-after-grace"
    return None


def hardware_input_at_wake(now: float, event_epoch: Optional[float],
                           wake_epoch: Optional[float], window_s: float = 600.0,
                           slack_s: float = 45.0) -> Optional[bool]:
    """Did a REAL input event (evdev) land at the wake? The one clock a resume cannot fake.

    True = a press arrived with the wake. False = provably nobody touched it, a hard fact the
    desktop idle counter can never state: it is reset to zero BY the resume, so on the real box
    every wake looked like a keypress (`idle=0s`) and the session latch then kept the machine
    awake all day. None = cannot tell (no wake stamp, or too old to attribute).
    """
    if wake_epoch is None:
        return None
    if now - wake_epoch > window_s:
        return None
    if event_epoch is None:
        return False        # the watcher is live and has never seen an event: nobody is there
    return event_epoch >= (wake_epoch - slack_s)


def _input_loop(watcher) -> None:
    """The watcher sleeps in select() in its own thread, so packet polling is never delayed."""
    while True:
        try:
            watcher.poll(timeout=1.0)
        except Exception:
            time.sleep(1.0)


def input_at_wake(now: float, idle_s: float, wake_epoch: Optional[float],
                  window_s: float = 600.0, slack_s: float = 45.0) -> Optional[bool]:
    """Was this wake caused by hands on the machine (keyboard/mouse/power button)?

    Driver-free and platform-free: the last input event happened at `now - idle_s`; if that
    lands at or after the wake moment (within `slack_s`), somebody's hands were on it when it
    woke. The `window_s` guard is the important half - without it an old wake plus a fresh
    keystroke looks like an input wake forever, which is exactly how the removed blanket
    "input since last sleep" rule deadlocked the tool.
    """
    if wake_epoch is None or idle_s < 0:
        return None
    if now - wake_epoch > window_s:
        return None
    return (now - idle_s) >= (wake_epoch - slack_s)


def _sample_link(sysinfo, log) -> None:
    """Record link transitions the LISTENER observes (driver-free reconnect evidence).

    The listener is awake once a second, so a drop and return is witnessed as an event rather
    than reconstructed afterwards. `link_evidence` reads this log as one more observable fact.
    """
    try:
        cur = sysinfo.link_state()
    except Exception:
        return
    if cur is None:
        return
    prev = getattr(_sample_link, "_last", None)
    _sample_link._last = cur
    if prev is None:
        return
    if (prev.get("connected") == cur.get("connected")
            and prev.get("bssid") == cur.get("bssid")):
        return
    line = ("%s  link %s->%s  bssid=%s->%s  ssid=%s"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), prev.get("connected"), cur.get("connected"),
               prev.get("bssid") or "-", cur.get("bssid") or "-", cur.get("ssid") or "-"))
    target = paths.link_event_log()
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if target.stat().st_size > 256 * 1024:
            keep = target.read_text(encoding="utf-8").splitlines()[-500:]
            target.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass
    log.info("link event: %s", line)


def cmd_judge(args) -> int:
    decision, idle, ev, guards, cfg = build_judgement(args)
    log = journal.get_logger()
    dl = journal.decision_logger()

    line = decision.line()
    # Name the input device and how long before the judgement it fired. `input-at-wake(evdev)`
    # alone left the operator (and me) unable to tell a real keypress from a machine that woke
    # itself, which is the question this tool exists to answer.
    if ev.input_source == "evdev":
        hw_epoch, hw_dev, _hw_live = read_last_input(paths.input_last_file())
        if hw_dev:
            line += "  via=%s@%s" % (
                hw_dev, "%.0fs-before" % (time.time() - hw_epoch) if hw_epoch else "unknown")
    # Show the packet only while it is actually this wake's cause. Printing the raw kind made a
    # 40-minute-old magic packet look like it had just been heard (`pkt=magic` on every line), which
    # reads as "an asked-for wake" and hides what really decided the judgement.
    if ev.pkt_kind and packet_is_this_wakes_cause(ev.pkt_age_s, None, cfg.pkt_window_s):
        pkt_shown = "%s@%.0fs" % (ev.pkt_kind, ev.pkt_age_s or 0)
    elif ev.pkt_kind:
        pkt_shown = "%s(%.0fm-old:ignored)" % (ev.pkt_kind, (ev.pkt_age_s or 0) / 60.0)
    else:
        pkt_shown = "-"
    detail = "idle=%.0fs  win=%s  pkt=%s  drv=%s  guards=%s" % (
        idle.seconds, _window_label(ev), pkt_shown, ev.drv_reason or "-", guards.active() or "-")
    dl.info("%s  %s", line, detail)
    log.info("judge: %s | %s", line, detail)
    journal.heartbeat("%s  %s  idle=%.0fs" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                              decision.line(), idle.seconds))

    _print(line)
    _print("  " + detail)

    if decision.action is not Action.SLEEP:
        return 0
    if not args.apply:
        _print("  (dry run: add --apply to actually suspend)")
        return 0
    if idle.seconds < 0:
        _print("  refusing to suspend: no idle information on this platform")
        return 2

    # last look before the suspend; skipped only when --grace 0 is given explicitly.
    # The grace window exists to protect a GUESS (window cannot attribute the wake, or it looks
    # like a NIC with nothing naming it). A wake the OS or the driver actually attributed, with
    # the idle threshold already met, does not need another minute - and that minute is pure
    # latency on every cycle.
    grace = getattr(args, "grace", None)
    if grace is None:
        grace = cfg.grace_seconds
    if cfg.grace_only_for_guesses and not _is_guess(decision):
        grace = 0
    sysinfo = _system(args.platform)
    if grace > 0:
        needed = cfg.idle_minutes * 60.0
        before = sysinfo.idle()
        _print("  waiting %ds before suspending (any input cancels it)" % grace)
        time.sleep(grace)
        after = sysinfo.idle()
        why = grace_verdict(before.tick, after.tick, after.seconds, needed, sysinfo.guards().active())
        if why:
            dl.info("SKIP reason=%s", why)
            journal.heartbeat("%s  action=skip  reason=%s" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), why))
            _print("  cancelled after the grace window: %s" % why)
            return 0
    # remember the link we are sleeping under: the next resume compares against this. Without
    # it the comparison has no baseline and degrades to "no pre-sleep snapshot" forever.
    sysinfo.link_snapshot()
    try:
        mechanism = sysinfo.suspend()
    except Exception as exc:
        dl.info("SLEEP-FAILED reason=%s error=%s", decision.reason, exc)
        _print("  suspend failed: %s" % exc)
        return 3
    _mark_forced_sleep(tick=sysinfo.idle().tick)
    dl.info("SLEEP reason=%s via=%s", decision.reason, mechanism)
    _print("  suspending via %s" % mechanism)
    return 0


# --------------------------------------------------------------------------------------
# listen
# --------------------------------------------------------------------------------------
def cmd_listen(args) -> int:
    cfg = _load_config(args)
    sysinfo = _system(args.platform)
    log = journal.get_logger()
    dl = journal.decision_logger()

    lock = paths.lock_file()
    paths.ensure_data_dir()
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip().split()[0])
            if _pid_alive(pid) and pid != os.getpid():
                _print("another listener is already running (pid=%d); exiting" % pid)
                return 1
        except Exception:
            pass
    lock.write_text("%d started=%s" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")),
                    encoding="utf-8")

    sel, nic_warn = _nic_selection(cfg, log)
    listener = Listener(ports=cfg.ports, marker_port=cfg.marker_port, macs=sel.macs())
    listener.bind()
    if nic_warn:
        _print("  [warn] %s" % nic_warn)
    # The suspend ledger sees ANY sleep (CLOCK_BOOTTIME - CLOCK_MONOTONIC), which the
    # wall-vs-monotonic threshold cannot: a 17-second wifi-wake sleep went unnoticed, the collector
    # never ran, the session stamp never moved, and a stale latch then held the machine awake.
    detector = SuspendLedger(resume_mod.ResumeDetector(threshold_s=cfg.resume_threshold_s))
    detector.observe()
    log.info("listener started pid=%d ports=%s marker=%d macs=%s", os.getpid(),
             ",".join(str(p) for p in listener.sockets), cfg.marker_port, listener.macs)
    _print("listener up: ports=%s marker=%d macs=%s"
           % (sorted(listener.sockets), cfg.marker_port, listener.macs))
    if listener.unavailable():
        _print("  (not bound: %s)"
               % ", ".join("%d=%s" % (p, e) for p, e in sorted(listener.unavailable().items())))
    port_hint = listener.advice()
    if port_hint:
        # loud on purpose: a partial bind leaves the packet chain half blind, and the only
        # visible symptom would be an asked-for wake getting re-slept
        log.warning("listener: %s", port_hint)
        _print("  [warn] %s" % port_hint)

    def idle_probe() -> float:
        try:
            return sysinfo.idle().seconds
        except Exception:
            return -1.0

    fast_deadline = 0.0
    last_beat = 0.0
    last_link_sample = 0.0
    try:
        while True:
            for seen in listener.poll():
                _record_packet(seen, log)
            if detector.observe():
                log.info("resume detected")
                _print("resume detected -> collecting evidence")
                cmd_collect(args)
                fast_deadline = 0.0        # judge immediately after a resume

            # v1's fast path, and the reason every cycle was 5-8 minutes without it: the judge
            # task only fires every `repeat_minutes`, so waiting for it adds up to two minutes on
            # top of the wait. The listener is already awake once a second, so it samples the
            # re-sleep window and re-judges itself every `fast_path_seconds` - and the wait it
            # fires on is now the WINDOW, not the old idle threshold (Q4a).
            _sample_window(cfg, sysinfo, log)
            now2 = time.time()
            if (_fast_path_ready(cfg, idle_probe)
                    and now2 - fast_deadline >= cfg.fast_path_seconds):
                fast_deadline = now2
                _fast_path_judge(args, cfg, log)
            now = time.time()
            if now - last_link_sample >= cfg.link_sample_seconds:
                last_link_sample = now
                _sample_link(sysinfo, log)
            if now - last_beat > 30:
                last_beat = now
                dl.info("heartbeat pid=%d ports=%s", os.getpid(), len(listener.sockets))
            time.sleep(1.0)
    except KeyboardInterrupt:
        _print("stopping")
    finally:
        listener.close()
        try:
            lock.unlink()
        except Exception:
            pass
    return 0


def cmd_wol_sensor(args) -> int:
    """The privileged half of the packet chain: bind the magic ports and record what arrives.

    Registered as a root-owned *system* unit that runs as the operator with
    CAP_NET_BIND_SERVICE, because UDP 9/7 are privileged ports a `--user` listener cannot bind -
    and that failure is silent: a magic-packet wake would be re-slept two minutes later because
    nobody saw the packet. It deliberately does NOT judge or suspend: logind refuses `suspend` to
    a process with no session (polkit allow_inactive=auth_admin_keep), so keeping the sleep path
    in the user listener is exactly what avoids needing a polkit rule to sleep at all.

    It also does the two other jobs that need a privilege the user unit cannot have, and that the
    judgement cannot do for itself after the fact:
      * the bare-ethertype `0x0842` magic frames (CAP_NET_RAW), which `etherwake`-style senders use
        and a UDP-only listener never sees;
      * the hardware input clock (`SupplementaryGroups=input`), because the desktop idle counter is
        reset by the resume itself and therefore cannot tell a keypress wake from a wifi wake.
    """
    cfg = _load_config(args)
    log = journal.get_logger()
    sel, nic_warn = _nic_selection(cfg, log)
    lsn = Listener(ports=WOL_PORTS, marker_port=None, macs=sel.macs())
    lsn.bind()
    if nic_warn:
        _print("  [warn] %s" % nic_warn)
    raw = RawMagicSocket(macs=lsn.macs)
    raw.bind()
    watcher = InputWatcher(paths.input_last_file(), audit_file=paths.input_audit_log())
    devices = watcher.bind()
    # The sensor is the only process awake when the machine comes back, so it is the one that has
    # to notice the resume and tell the input watcher to distrust the HID replay that follows.
    sensor_ledger = SuspendLedger(resume_mod.ResumeDetector(threshold_s=cfg.resume_threshold_s))
    sensor_ledger.observe()
    wlan = IwEventWatcher(paths.wlan_event_log())
    wlan_ok = wlan.start()
    log.info("wol-sensor up pid=%d ports=%s raw-magic=%s macs=%s",
             os.getpid(), sorted(lsn.sockets), "listening" if raw.sock else "unavailable", lsn.macs)
    log.info("wol-sensor: input watcher on %d device(s): %s", devices,
             ", ".join(sorted(set(watcher.device_names))) or "-")
    log.info("wol-sensor: iw-event tap %s", "up" if wlan_ok else "unavailable (no nl80211 events)")
    for hint in (lsn.advice(), raw.advice()):
        if hint:
            log.warning("wol-sensor: %s", hint)
    if not devices:
        log.warning("wol-sensor: no /dev/input device could be read - the keyboard/mouse chain "
                    "falls back to the desktop idle clock, which a resume resets to 0, and that "
                    "fallback may not create a session exemption (needs SupplementaryGroups=input)")
    if not lsn.sockets and raw.sock is None:
        log.error("wol-sensor: nothing could be bound - the magic-packet chain is blind")
        return 1
    if devices:
        threading.Thread(target=_input_loop, args=(watcher,), daemon=True).start()
    try:
        while True:
            for seen in lsn.poll():
                _record_packet(seen, log)
            for seen in raw.poll():
                _record_packet(seen, log)
            if not wlan.alive():
                wlan.start()        # the child died (driver reload, iw upgraded): start it again
            wlan.drain()
            # The bus reset that follows a resume makes HID devices replay their state; tell the
            # input watcher so it does not read that as a person. The sensor is the only process
            # awake at the resume, so it has to be the one that notices.
            if sensor_ledger.observe():
                watcher.mark_resume()
                log.info("wol-sensor: resume -> motion settle window armed")
            time.sleep(1.0)
    except KeyboardInterrupt:
        _print("stopping")
    finally:
        wlan.stop()
        watcher.close()
        raw.close()
        lsn.close()
    return 0


def _fast_path_judge(args, cfg, log) -> None:
    """Run one judgement from inside the listener and act on it (no new process, no timer wait).

    Deliberately the SAME `build_judgement` the scheduled judge uses: two decision paths that can
    drift apart is how a tool starts disagreeing with itself. Only the logging policy differs -
    the listener logs the sleeping decisions and stays quiet about the rest, so decisions.log
    does not fill up with a line every 30 seconds.
    """
    dl = journal.decision_logger()
    sysinfo = _system(args.platform)
    try:
        decision, idle, ev, guards, cfg = build_judgement(args, sysinfo=sysinfo, cfg=cfg)
    except Exception as exc:
        log.warning("fast path judge failed: %s", exc)
        return
    if decision.action is not Action.SLEEP:
        journal.heartbeat("%s  %s  idle=%.0fs" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                 decision.line(), idle.seconds))
        # Log when the REASON changes, not every 30 s: without this the fast path is invisible
        # and a 5-minute wait looks like "the fast path is not running" (that is exactly how an
        # evening was spent reading the wrong code).
        if decision.reason != getattr(_fast_path_judge, "_last_reason", None):
            _fast_path_judge._last_reason = decision.reason
            dl.info("%s  idle=%.0fs  (listener fast path)", decision.line(), idle.seconds)
        return
    dl.info("%s  idle=%.0fs  (listener fast path)", decision.line(), idle.seconds)
    log.info("fast path: %s idle=%.0fs", decision.line(), idle.seconds)
    grace = cfg.grace_seconds if (_is_guess(decision) or not cfg.grace_only_for_guesses) else 0
    if grace > 0:
        before = sysinfo.idle()
        time.sleep(grace)
        after = sysinfo.idle()
        why = grace_verdict(before.tick, after.tick, after.seconds,
                            cfg.idle_minutes * 60.0, sysinfo.guards().active())
        if why:
            dl.info("SKIP reason=%s (fast path)", why)
            log.info("fast path cancelled: %s", why)
            return
    # remember the link we are sleeping under: the next resume compares against this. Without
    # it the comparison has no baseline and degrades to "no pre-sleep snapshot" forever.
    sysinfo.link_snapshot()
    try:
        mechanism = sysinfo.suspend()
    except Exception as exc:
        dl.info("SLEEP-FAILED reason=%s error=%s", decision.reason, exc)
        log.warning("fast path suspend failed: %s", exc)
        return
    _mark_forced_sleep(tick=sysinfo.idle().tick)
    dl.info("SLEEP reason=%s via=%s (listener fast path)", decision.reason, mechanism)
    log.info("fast path: suspending via %s", mechanism)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        from .system.base import decode_console, proc_kwargs
        out = decode_console(subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                           capture_output=True, **proc_kwargs()).stdout)
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------------------
def cmd_collect(args) -> int:
    sysinfo = _system(args.platform)
    src, link, link_why, path = _collect_evidence(sysinfo)
    journal.get_logger().info("collect: kind=%s who=%s -> %s", src.kind, src.who, path)
    _print("evidence: kind=%s who=%s chain=%s" % (src.kind, src.who or "-", src.chain))
    if src.raw:
        _print("  raw: %s" % src.raw[:300])
    _print("  link: %s (%s)" % (link, link_why))
    return 0


# --------------------------------------------------------------------------------------
# install / uninstall
# --------------------------------------------------------------------------------------
def _error_excerpt(raw: bytes, max_lines: int = 4) -> str:
    """Keep the lines that identify a failure.

    Windows command line tools put the human message on line 1 and the USEFUL part - the row,
    column, element and offending value, e.g. "(7,33):LogonType:ServiceAccount" - on line 2.
    Printing only the first line (as this did) throws away the only actionable information.
    """
    from .system.base import decode_console
    lines = [l.strip() for l in decode_console(raw).splitlines() if l.strip()]
    return " | ".join(lines[:max_lines])


def _apply_cmd(cmd: Sequence[str], dry_run: bool, best_effort: bool = False) -> None:
    if dry_run:
        _print("    [dry-run] %s" % " ".join(cmd))
        return
    try:
        from .system.base import proc_kwargs
        proc = subprocess.run(list(cmd), capture_output=True, **proc_kwargs())
        if proc.returncode == 0:
            _print("    [ok] %s" % " ".join(cmd))
            return
        if best_effort:
            # "nothing to remove" is the NORMAL case on a first install. Calling that [failed]
            # would train the operator to ignore the word, and the next real failure with it.
            _print("    [note] %s (nothing to remove - normal on a first install)" % " ".join(cmd))
            return
        _print("    [failed] %s" % " ".join(cmd))
        detail = _error_excerpt((proc.stderr or b"") + (proc.stdout or b""))
        if detail:
            _print("        %s" % detail)
    except Exception as exc:
        _print("    [failed] %s -> %s" % (" ".join(cmd), exc))


def _apply_artifact(a, dry_run: bool) -> None:
    """Rebuild one artifact: best-effort remove, create, then whatever the manager needs after.

    The order matters and is the whole point of `prepare_cmd` (pitfalls 7): an in-place overwrite
    keeps a Task Scheduler task's old principal/ACL, launchd refuses a label that is still loaded,
    and `systemctl enable --now` does not restart a unit that is already running - in all three
    cases a re-install would leave the OLD job in place while printing success.
    """
    _print("  apply %s:" % getattr(a, "name", "?"))
    commands = list(getattr(a, "prepare_cmd", []))
    if commands and isinstance(commands[0], str):
        # A flat argv (`prepare_cmd=["cmd", "arg"]`) instead of a list of commands. Treat it as ONE
        # command: iterating it would run "c", "m", "d" - each failing as a harmless-looking note.
        commands = [commands]
    for cmd in commands:
        _apply_cmd(cmd, dry_run, best_effort=True)
    _apply_cmd(a.apply_cmd, dry_run)
    for extra in getattr(a, "apply_more", []):
        _apply_cmd(extra, dry_run)


def _remove_artifact(a, dry_run: bool) -> None:
    _apply_cmd(a.remove_cmd, dry_run)
    for extra in getattr(a, "remove_more", []):
        _apply_cmd(extra, dry_run)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _unit_active(name: str) -> bool:
    """Is a *system* unit running? `is-active` is a read-only query, no privileges needed."""
    try:
        from .system.base import run_hidden
        return run_hidden(["systemctl", "is-active", name], 5.0).strip() == "active"
    except Exception:
        return False


def _record_packet(seen, log) -> None:
    """Write down an asked-for packet - the two files the judge reads back.

    Shared by the user listener (marker port) and the privileged wol-sensor (magic ports), so both
    halves of the packet chain leave identical evidence in identical places.
    """
    with open(paths.packet_log(), "a", encoding="utf-8") as fh:
        fh.write("%s  DETECTED kind=%s src=%s len=%d\n"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), seen.kind, seen.src, seen.length))
    paths.packet_state().write_text(seen.line(), encoding="ascii")
    log.info("packet: %s", seen.line())
    # Latch immediately: the machine must be exempt from sleep the moment the packet is heard, not
    # only from the next judgement onwards (and the next judgement may be a whole window later).
    latch = read_evidence()
    if latch.session is not None:
        write_session_cause("asked-for", latch.session)
    _print("detected %s from %s" % (seen.kind, seen.src))


def _require_admin(sysinfo, argv: List[str], dry: bool, what: str) -> bool:
    """Kept only for `doctor`-style checks: install no longer refuses, it elevates itself.

    v1 told the operator to open an administrator terminal; this tool now does what they asked for
    in one command (docs/resleep-window-plan.md section 5), so the refusal path is gone.
    """
    if dry or sysinfo.is_admin():
        return True
    _print("")
    _print("需要管理员权限：%s。" % what)
    _print("请用管理员 PowerShell / 右键“以管理员身份运行”重新执行：")
    _print("    %s" % " ".join(['"%s"' % sys.executable] + argv))
    _print("（只想看看会改什么：在上面加 --dry-run，那个不需要提权）")
    return False


# --------------------------------------------------------------------------------------
# the privileged half, run by an elevated child (see elevate.py)
# --------------------------------------------------------------------------------------
def _needs_elevation(a) -> bool:
    """Does applying this artifact need administrator / root?

    Linux says so on the artifact itself (`needs_root`: the system unit for the WoL ports). On
    Windows every Task Scheduler registration does - the judge and listener run at
    `HighestAvailable` and the collector as SYSTEM - and on all three platforms the firewall does.
    """
    if getattr(a, "needs_root", False):
        return True
    return a.kind == "task" and os.name == "nt"


def _workdir() -> str:
    return str(Path(__file__).resolve().parents[2])


def _firewall_program(config_path: Optional[str]) -> str:
    """The interpreter the LISTENER actually runs under - pythonw.exe on Windows, not python.exe.

    Opening the port for a binary that never binds it is exactly as good as not opening it, which
    is how a magic-packet chain goes quiet with nothing in any log to say why.
    """
    try:
        return launcher_parts(sys.executable, _workdir(), "listen", config_path,
                              windowless=True)[0]
    except Exception:
        return sys.executable


def _forwarded_flags(args) -> List[str]:
    """The operator's answers, passed to the elevated child as ARGUMENTS, never as environment.

    An environment variable does not survive UAC, and the child is forbidden from asking anything
    (docs/install-pitfalls.md section 2) - so everything it needs must already be on its command
    line.
    """
    out: List[str] = []
    if getattr(args, "config", None):
        out += ["--config", str(args.config)]
    if getattr(args, "no_firewall", False):
        out += ["--no-firewall"]
    if getattr(args, "no_sensor", False):
        out += ["--no-sensor"]
    return out


def _attach_progress(args):
    """The child's stdout/stderr go INTO the progress file.

    With pythonw.exe there is no console at all, and a line the parent cannot read is a line that
    does not exist: the parent is the operator's only window into this process.
    """
    progress = elevate.Progress(args.progress)
    try:
        fh = open(progress.path, "a", encoding="utf-8", buffering=1)
        sys.stdout = fh
        sys.stderr = fh
    except Exception:
        pass
    return progress


def _elevated_install(args) -> int:
    """The privileged half of `install`: the root-owned unit, the task registrations, the firewall."""
    progress = _attach_progress(args)
    progress.start("install")
    rc = 1
    try:
        cfg = _load_config(args).validate()
        sysinfo = _system(args.platform)
        artifacts = artifacts_for(sys.platform, python_exe=sys.executable, workdir=_workdir(),
                                  interval_minutes=int(cfg.repeat_minutes),
                                  config_path=getattr(args, "config", None))
        for a in artifacts:
            if getattr(args, "no_sensor", False) and getattr(a, "needs_root", False):
                progress.step("skipped by --no-sensor: %s" % a.name)
                continue
            if not _needs_elevation(a):
                continue
            if a.kind != "task" and a.content:
                # we ARE root here, so the file is written directly (no staging, no sudo)
                target = Path(os.path.expanduser(a.path))
                progress.step("write %s" % target)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(a.content, encoding="utf-8")
                try:
                    os.chmod(target, 0o644)
                except Exception:
                    pass
                _print("      written %s" % target)
            progress.step("apply %s" % a.name)
            _apply_artifact(a, False)
        if not getattr(args, "no_firewall", False) and sysinfo.firewall_supported:
            program = _firewall_program(getattr(args, "config", None))
            progress.step("firewall (UDP %s for %s)"
                          % ("/".join(str(p) for p in cfg.ports), program))
            done = sysinfo.firewall_apply(list(cfg.ports), cfg.marker_port, program)
            _print("      firewall: %s" % (", ".join(done) if done else "nothing to do"))
        rc = 0
    except Exception as exc:
        progress.say("failed %s: %s" % (type(exc).__name__, exc))
        rc = 1
    progress.done(rc)
    return rc


def _elevated_uninstall(args) -> int:
    """The privileged half of `uninstall`: drop the tasks / the system unit / the firewall rules."""
    progress = _attach_progress(args)
    progress.start("uninstall")
    rc = 1
    try:
        cfg = _load_config(args).validate()
        sysinfo = _system(args.platform)
        artifacts = artifacts_for(sys.platform, python_exe=sys.executable, workdir=_workdir(),
                                  interval_minutes=int(cfg.repeat_minutes),
                                  config_path=getattr(args, "config", None))
        for a in artifacts:
            if not _needs_elevation(a):
                continue
            progress.step("remove %s" % a.name)
            _remove_artifact(a, False)
        if not getattr(args, "no_firewall", False) and sysinfo.firewall_supported:
            progress.step("firewall rules")
            removed = sysinfo.firewall_remove()
            _print("      removed: %s" % (", ".join(removed) if removed else "none found"))
        rc = 0
    except Exception as exc:
        progress.say("failed %s: %s" % (type(exc).__name__, exc))
        rc = 1
    progress.done(rc)
    return rc


def _delegate(args, command: str, label: str, dry: bool) -> Optional[int]:
    """Hand the privileged half to an elevated child and read its progress out loud."""
    if dry:
        _print("  [dry-run] would elevate for: %s" % label)
        return 0
    if elevate.is_elevated():
        return 0            # already running as administrator/root: do the work right here
    progress_path = Path(paths.data_dir()) / ("elevated-%s.progress" % command)
    manual = " ".join(['"%s"' % sys.executable, str(Path(_workdir()) / "autoresleep.py"),
                       command, "--elevated", "--progress", str(progress_path)]
                      + _forwarded_flags(args))
    # A switch that refuses to ask for privileges at all: a test suite, a CI job or a sandbox must
    # never make a UAC / pkexec prompt appear on somebody's desktop, and an unattended install that
    # silently waits 15 minutes for an answer nobody will give is worse than a clear refusal.
    if getattr(args, "no_elevate", False) or _no_elevate_env():
        _print("")
        _print("  elevation is switched off (--no-elevate / AUTORESLEEP_NO_ELEVATE).")
        _print("  run the privileged half yourself, as administrator / root:")
        _print("    %s" % manual)
        return 2
    rc = elevate.delegate([command] + _forwarded_flags(args), progress_path, _print,
                          config_path=getattr(args, "config", None), label=label)
    if rc is None or rc != 0:
        _print("")
        _print("  the privileged step did not finish. Read the record it left:")
        _print("    %s" % progress_path)
        _print("  then run it yourself in an administrator shell / with sudo:")
        _print("    %s" % manual)
        return rc if rc is not None else 2
    return 0


def _no_elevate_env() -> bool:
    return os.environ.get("AUTORESLEEP_NO_ELEVATE", "").strip().lower() in ("1", "true", "yes", "on")


def cmd_install(args) -> int:
    if getattr(args, "elevated", False):
        # the privileged child: asks nothing, records every step, returns its verdict
        return _elevated_install(args)
    cfg = _load_config(args).validate()
    sysinfo = _system(args.platform)
    dry = args.dry_run
    priv = elevate.is_elevated()
    deferred: List[str] = []

    _print("=" * 66)
    _print(" AutoReSleep v%s - install%s" % (__version__, "  (dry run)" if dry else ""))
    _print("=" * 66)

    _print("---- 1. config ----")
    target = Path(args.config) if getattr(args, "config", None) else paths.config_file()
    if dry:
        _print("  [dry-run] would write %s" % target)
    else:
        cfg.save(target)
        _print("  %s written" % target)

    _print("---- 2. scheduler artifacts (%s) ----" % sys.platform)
    artifacts = artifacts_for(sys.platform, python_exe=sys.executable, workdir=str(Path(__file__).resolve().parents[2]),
                              interval_minutes=int(cfg.repeat_minutes),
                              config_path=getattr(args, "config", None))
    if getattr(args, "no_sensor", False):
        # the one root-owned artifact - refused explicitly, and then the privileged WoL ports are
        # simply not watched (doctor will say so, and the sysctl/marker alternatives apply)
        kept = [a for a in artifacts if not getattr(a, "needs_root", False)]
        if len(kept) != len(artifacts):
            _print("  (--no-sensor: the privileged wol-sensor unit is not registered)")
        artifacts = kept
    for a in artifacts:
        _print("  %-28s %s" % (a.name, a.path))
        if a.kind == "task":
            _print("      %s   (xml persisted next to the state files)"
                   % (a.note if not dry else "xml would be written to " + a.note.split()[-1]))
        elif getattr(a, "needs_root", False) and not priv:
            # the elevated child writes this one - as root, directly, with nothing staged
            _print("      deferred to the elevated step (%s)" % a.note)
        elif dry:
            _print("      [dry-run] would write %s (%d bytes)" % (a.path, len(a.content)))
        else:
            p = Path(os.path.expanduser(a.path))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a.content, encoding="utf-8")
            _print("      written %s" % p)
    if not dry:
        _write_windows_xml(artifacts)
    for a in artifacts:
        if _needs_elevation(a) and not priv:
            _print("  %-28s %s" % (a.name, "apply: deferred to the elevated step"))
            deferred.append(a.name)
            continue
        _apply_artifact(a, dry)
    _print("---- 3. firewall (the pkt chain needs this on a default install) ----")
    fw = sysinfo.firewall()
    _print("  state: %s  %s" % (fw.state, fw.detail))
    if args.no_firewall:
        _print("  skipped by --no-firewall")
    elif not sysinfo.firewall_supported:
        _print("  this platform has no per-port API here; apply manually:")
        for h in fw.hints:
            _print("    %s" % h)
    else:
        # Apply the rules even when the firewall LOOKS off: netsh and the Win32 profile API
        # disagree on this very machine (netsh says all three profiles off, Get-NetFirewallProfile
        # said Public=on), and a rule on a disabled firewall is inert - whereas a missing rule on
        # an enabled one silently kills the pkt chain.
        program = _firewall_program(getattr(args, "config", None))
        if dry:
            _print("  [dry-run] would allow UDP %s + marker %d for %s"
                   % (",".join(str(p) for p in cfg.ports), cfg.marker_port, program))
        elif not priv:
            _print("  deferred to the elevated step (rules for %s)" % program)
            deferred.append("firewall")
        else:
            done = sysinfo.firewall_apply(list(cfg.ports), cfg.marker_port, program)
            _print("  applied: %s" % (", ".join(done) if done else "nothing (see state above)"))
        for h in fw.hints:
            _print("  hint: %s" % h)
    probe = Listener(ports=cfg.ports, marker_port=cfg.marker_port)
    probe.bind()
    port_hint = probe.advice()
    probe.close()
    root_owned = [a for a in artifacts if getattr(a, "needs_root", False)]
    if port_hint and root_owned:
        # the privileged ports are handled by a dedicated unit further down: do not tell the
        # operator to relax a kernel boundary that the install is about to work around properly
        from .scheduling.linux import SENSOR_UNIT_NAME, operator_identity
        port_hint = ("UDP %s are privileged ports; '%s' (step 4) binds them as %s with "
                     "CAP_NET_BIND_SERVICE - no system-wide change needed"
                     % ("/".join(str(x) for x in WOL_PORTS), SENSOR_UNIT_NAME,
                        operator_identity()[0]))
    if port_hint:
        _print("  hint: %s" % port_hint)

    _print("---- 4. the privileged half (one elevation, not a second terminal) ----")
    if not deferred:
        _print("  nothing needs elevation here")
    else:
        _print("  %s" % ", ".join(deferred))
        rc = _delegate(args, "install",
                       "register the scheduled jobs + open the firewall (%s)" % ", ".join(deferred),
                       dry)
        if rc:
            _print("  [error] the privileged step did not finish; the resident jobs were not started")
            return rc

    _print("---- 5. start the resident jobs now ----")
    # v1 started the listener as part of install, and v2 must too: it only has a logon trigger,
    # so without this nothing runs until the next logon - the packet chain looks dead and no
    # resume is noticed (measured: an install at 18:15 on a machine already logged in left the
    # listener stopped all evening, so `pkt-` stayed empty).
    listener = next((a for a in artifacts if "listener" in a.name.lower()), None)
    if args.no_start:
        _print("  skipped by --no-start")
    elif listener is None:
        _print("  no listener artifact for this platform")
    else:
        # RESTART, not start: an upgraded install must not leave the previous listener running.
        # It holds the single-instance lock, so a freshly started copy exits immediately while the
        # install's own verification sees the lock file and reports success - and the new code
        # (e.g. the fast path that removes the timer latency) never takes effect.
        lock = paths.lock_file()
        if lock.exists() and not dry:
            try:
                old_pid = int(lock.read_text(encoding="utf-8").strip().split()[0])
                if _pid_alive(old_pid) and old_pid != os.getpid():
                    _stop_pid(old_pid)
                    _print("  stopped the previous listener (pid %d)" % old_pid)
                    time.sleep(1)
            except Exception as exc:
                _print("  [note] could not stop the previous listener: %s" % exc)
        if lock.exists() and not dry:
            try:
                lock.unlink()
            except Exception:
                pass
        if listener.start_cmd:
            _apply_cmd(listener.start_cmd, dry)
        if dry:
            _print("  [dry-run] would restart the listener and wait for its lock file")
        else:
            started = False
            for _ in range(20):
                time.sleep(1)
                if lock.exists():
                    started = True
                    break
            if started:
                _print("  listener: running (lock file %s)" % lock)
            else:
                _print("  [warning] listener did not produce its lock file within 20s;")
                _print("            start it manually: %s"
                       % " ".join(listener.start_cmd or ["(platform-specific)"]))

    sensor = next((a for a in artifacts if getattr(a, "needs_root", False)), None)
    if sensor is not None:
        if args.no_start:
            _print("  sensor : skipped by --no-start")
        elif dry:
            _print("  sensor : [dry-run] would start %s" % sensor.name)
        else:
            _apply_cmd(sensor.start_cmd, dry)
            # a just-restarted process has not called bind() yet: poll briefly instead of
            # announcing a failure that is really a race (measured: the check ran between the
            # restart and the first bind and reported "magic ports still unbound")
            holds = set()
            for _ in range(5):
                holds = udp_ports_in_use(WOL_PORTS)
                if holds:
                    break
                time.sleep(1)
            missing = sorted(set(WOL_PORTS) - holds)
            _print("  sensor : %s%s%s"
                   % ("running" if _unit_active(sensor.name) else "NOT running",
                      (", UDP %s bound" % "/".join(str(x) for x in sorted(holds))) if holds else "",
                      (", UDP %s NOT bound - check: sudo journalctl -u %s"
                       % ("/".join(str(x) for x in missing), sensor.name)) if missing else ""))

    _print()
    if sys.platform.startswith("linux"):
        from .scheduling.linux import linger_enabled
        _linger = linger_enabled()
        if _linger is False:
            _print("  hint: user units stop at logout (Linger=no) - on a machine with no")
            _print("        interactive logon, run once as root: sudo loginctl enable-linger %s"
                   % (os.environ.get("USER") or "$USER"))

    _print("==================== done ====================")
    _print(" data dir : %s" % paths.data_dir())
    _print(" check    : autoresleep doctor")
    _print(" uninstall: autoresleep uninstall%s"
           % ("" if not dry else "   (nothing was changed: this was a dry run)"))
    return 0


def _write_windows_xml(artifacts) -> None:
    if os.name != "nt":
        return
    from .scheduling.windows import write_xml_files
    for path in write_xml_files([a for a in artifacts]):
        _print("      xml %s" % path)


def cmd_uninstall(args) -> int:
    if getattr(args, "elevated", False):
        # the privileged child: asks nothing, records every step, returns its verdict
        return _elevated_uninstall(args)
    sysinfo = _system(args.platform)
    dry = args.dry_run
    priv = elevate.is_elevated()
    deferred: List[str] = []
    _print("=" * 66)
    _print(" AutoReSleep v%s - uninstall%s" % (__version__, "  (dry run)" if dry else ""))
    _print("=" * 66)

    artifacts = artifacts_for(sys.platform, python_exe=sys.executable,
                              workdir=str(Path(__file__).resolve().parents[2]),
                              interval_minutes=2, config_path=getattr(args, "config", None))
    _print("---- 1. scheduler artifacts ----")
    for a in artifacts:
        _print("  %-28s %s" % (a.name, a.path))
        if _needs_elevation(a) and not priv:
            _print("      remove: deferred to the elevated step")
            deferred.append(a.name)
            continue
        _remove_artifact(a, dry)
        if a.kind != "task" and not dry and not getattr(a, "needs_root", False):
            # user-level files are ours to delete; a root-owned one was removed by its remove_cmd
            try:
                Path(os.path.expanduser(a.path)).unlink()
                _print("      removed file %s" % a.path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                _print("      [failed] removing %s -> %s" % (a.path, exc))

    # systemd caches unit files; without a reload it keeps "knowing" the unlinked units until
    # the next daemon-reload happens to occur (and `is-enabled` shows ghosts)
    if not dry and sys.platform.startswith("linux"):
        _apply_cmd(["systemctl", "--user", "daemon-reload"], dry)
        _print("  systemctl --user daemon-reload")

    _print("---- 2. firewall rules ----")
    if args.no_firewall:
        _print("  skipped by --no-firewall")
    else:
        if dry:
            _print("  [dry-run] would remove the AutoReSleep-WoL-UDP-* rules")
        elif not priv and sysinfo.firewall_supported:
            _print("  deferred to the elevated step")
            deferred.append("firewall")
        else:
            removed = sysinfo.firewall_remove()
            _print("  removed: %s" % (", ".join(removed) if removed else "none found"))

    _print("---- 2b. the privileged half (one elevation, not a second terminal) ----")
    if not deferred:
        _print("  nothing needs elevation here")
    else:
        _print("  %s" % ", ".join(deferred))
        rc = _delegate(args, "uninstall",
                       "delete the scheduled jobs / system unit and the firewall rules (%s)"
                       % ", ".join(deferred), dry)
        if rc:
            _print("  [error] the privileged step did not finish; the rest was still removed")
            return rc

    _print("---- 3. listener process ----")
    lock = paths.lock_file()
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip().split()[0])
            if _pid_alive(pid) and pid != os.getpid():
                if dry:
                    _print("  [dry-run] would stop pid %d" % pid)
                else:
                    _stop_pid(pid)
                    _print("  stopped pid %d" % pid)
        except Exception as exc:
            _print("  [note] could not read %s: %s" % (lock, exc))
        if not dry:
            try:
                lock.unlink()
            except Exception:
                pass
    else:
        _print("  no listener lock file")

    if args.purge:
        _print("---- 4. purge state ----")
        keep = {"config.json"}
        for f in sorted(paths.data_dir().glob("*")):
            if f.name in keep or args.keep_logs and f.suffix == ".log":
                continue
            if dry:
                _print("  [dry-run] would delete %s" % f)
            else:
                try:
                    f.unlink()
                    _print("  deleted %s" % f)
                except Exception as exc:
                    _print("  [failed] %s -> %s" % (f, exc))
    else:
        _print("---- 4. state files kept (use --purge to delete logs/evidence) ----")

    _print()
    _print("==================== done ====================")
    _print(" nothing outside this list was touched: no power-plan change, no NIC setting.")
    return 0


def _stop_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            from .system.base import proc_kwargs
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           **proc_kwargs())
        else:
            os.kill(pid, 15)
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------------------
def cmd_config(args) -> int:
    cfg = _load_config(args)
    # `--set` is append+nargs, so it arrives as a list of groups; flatten it once here
    pairs = [pair for group in (args.set or []) for pair in group]
    if pairs:
        for pair in pairs:
            if "=" not in pair:
                _print("ignoring %r (expected key=value)" % pair)
                continue
            key, value = pair.split("=", 1)
            if not hasattr(cfg, key):
                _print("unknown key %r" % key)
                continue
            current = getattr(cfg, key)
            try:
                if isinstance(current, bool):
                    setattr(cfg, key, value.strip().lower() in ("1", "true", "yes", "on"))
                elif isinstance(current, int) and not isinstance(current, bool):
                    setattr(cfg, key, int(value))
                elif isinstance(current, float):
                    setattr(cfg, key, float(value))
                elif isinstance(current, list):
                    if key in LIST_OF_STR_KEYS:
                        # adapter names and MACs. COMMA is the only separator here, because
                        # Windows adapter names contain spaces ("WLAN 4", "以太网 2", "vEthernet
                        # (Default Switch)") - splitting on whitespace made them impossible to
                        # configure, which is the silent-failure shape this project keeps hunting.
                        setattr(cfg, key, [v.strip() for v in value.split(",") if v.strip()])
                    else:
                        setattr(cfg, key, [int(v) for v in re.split(r"[,\s]+", value.strip()) if v])
                else:
                    setattr(cfg, key, value)
            except ValueError as exc:
                _print("cannot set %s=%r: %s" % (key, value, exc))
                return 2
        target = cfg.save(Path(args.config) if getattr(args, "config", None) else None)
        _print("saved: %s" % target)
        for pair in pairs:
            key = pair.split("=", 1)[0]
            if hasattr(cfg, key):
                _print("  %-26s %s" % (key, getattr(cfg, key)))
        return 0

    from dataclasses import asdict
    import json
    _print("config file: %s" % (Path(args.config) if getattr(args, "config", None)
                               else paths.config_file()))
    _print(json.dumps(asdict(cfg), indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoresleep",
                                description="Cross-platform wake/re-sleep control "
                                            "(Windows, Linux, macOS)")
    p.add_argument("--version", action="version", version="autoresleep " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--config", help="explicit config file path")
        sp.add_argument("--platform", help="pretend to be another platform (for tests)")

    sp = sub.add_parser("doctor", help="report what this machine supports")
    common(sp)
    sp.add_argument("--idle-minutes", type=float, default=None)
    sp.add_argument("--unknown-idle-minutes", type=float, default=None)
    sp.add_argument("--unattributed-wake-policy", choices=["skip", "resleep-if-unattended"],
                    default=None)
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("judge", help="decide whether to go back to sleep")
    common(sp)
    sp.add_argument("--apply", action="store_true", help="actually suspend (default: decide only)")
    sp.add_argument("--grace", type=int, default=None, metavar="SECONDS",
                    help="wait this long before suspending and re-check for user input "
                         "(default: grace_seconds from the config; 0 disables)")
    sp.add_argument("--idle-minutes", type=float, default=None)
    sp.add_argument("--unknown-idle-minutes", type=float, default=None)
    sp.add_argument("--unattributed-wake-policy", choices=["skip", "resleep-if-unattended"],
                    default=None)
    sp.set_defaults(func=cmd_judge)

    sp = sub.add_parser("wol-sensor", help="bind the privileged magic-packet ports (UDP 9/7; "
                                           "runs as a system unit)")
    common(sp)
    sp.set_defaults(func=cmd_wol_sensor)

    sp = sub.add_parser("listen", help="resident packet/marker listener")
    common(sp)
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("collect", help="record the wake-source evidence (run on resume)")
    common(sp)
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("install", help="write config + native scheduler artifacts")
    common(sp)
    # internal: the elevated child's own flags. Not for humans - `install` elevates itself, and the
    # child is forbidden from asking anything, so its answers arrive here as arguments.
    sp.add_argument("--elevated", action="store_true", default=False, help=argparse.SUPPRESS)
    sp.add_argument("--progress", default=None, help=argparse.SUPPRESS)
    sp.add_argument("--dry-run", action="store_true", default=False,
                    help="print exactly what would change, touch nothing")
    sp.add_argument("--no-sensor", action="store_true",
                    help="do not register the root-owned wol-sensor unit (then UDP 9/7 are "
                         "unwatched; see README, Linux WoL ports)")
    sp.add_argument("--no-firewall", action="store_true", default=False)
    sp.add_argument("--no-elevate", action="store_true", default=False,
                    help="never ask for privileges: print the privileged command to run instead "
                         "(also AUTORESLEEP_NO_ELEVATE=1; for CI, sandboxes and tests)")
    sp.add_argument("--no-start", action="store_true", default=False,
                    help="do not start the resident listener now (it starts at next logon)")
    sp.add_argument("--idle-minutes", type=float, default=None)
    sp.add_argument("--unknown-idle-minutes", type=float, default=None)
    sp.add_argument("--unattributed-wake-policy", choices=["skip", "resleep-if-unattended"],
                    default=None)
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="remove tasks/units/agents, firewall rules, daemon")
    common(sp)
    # internal: see `install`
    sp.add_argument("--elevated", action="store_true", default=False, help=argparse.SUPPRESS)
    sp.add_argument("--progress", default=None, help=argparse.SUPPRESS)
    sp.add_argument("--dry-run", action="store_true", default=False)
    sp.add_argument("--no-firewall", action="store_true", default=False)
    sp.add_argument("--no-elevate", action="store_true", default=False,
                    help="never ask for privileges: print the privileged command to run instead")
    sp.add_argument("--purge", action="store_true", help="also delete logs and evidence")
    sp.add_argument("--keep-logs", action="store_true", help="with --purge: keep *.log")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("config", help="show or change the thresholds")
    common(sp)
    # nargs="+" as well as append: both `--set a=1 --set b=2` and `--set a=1 b=2` must work -
    # every document in this repo (README, docs/resleep-window-plan.md) shows the second form,
    # and without this the extra pairs were parsed as stray positionals and argparse exited 2.
    sp.add_argument("--set", nargs="+", action="append", metavar="KEY=VALUE")
    sp.set_defaults(func=cmd_config)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        _print("error: %s" % exc)
        return 2