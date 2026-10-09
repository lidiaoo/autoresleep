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
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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


def _fast_path_ready(cfg: Config, idle_probe):
    """Is it time to judge? -> (ready, via_window, window_stamp)

    Q4a: the window IS the wait for a NIC-caused wake, so the fast path has to fire when it closes
    - waiting for `idle_minutes` first adds two minutes to every cycle and makes the configured ten
    seconds meaningless. With no wake stamp (a box whose collector never ran) the old gate stands.

    `via_window` says the readiness came from the window closing, and `window_stamp` identifies
    WHICH window that was - the caller needs both to fire on the first second after the window
    closes without judging the same window over and over (the window state stays on disk, so
    `age >= delay` stays true for ever).
    """
    ev = read_evidence()
    age = _window_age(ev)
    if age is not None:
        try:
            stamp = float(getattr(ev, "observed_at", 0) or 0)
        except (TypeError, ValueError):
            stamp = 0.0
        return age >= float(cfg.resleep_delay_seconds), True, stamp
    return idle_probe() >= cfg.idle_minutes * 60.0, False, 0.0


def _fast_path_fire(cfg: Config, idle_probe, now: float, fast_deadline: float,
                    judged_window: float):
    """Should the listener judge right now? -> (fire, fast_deadline, judged_window)

    The window closing is the event the whole re-sleep feature waits for, so it fires on the FIRST
    second after it closes instead of waiting for the next `fast_path_seconds` tick - measured, that
    wait added up to 30 s to every NIC wake's decision (window 10 s + up to 30 s = up to 40 s before,
    ~10 s now). Everything else keeps the old rate limit: after a window has been judged, the loop
    falls back to one judgement per `fast_path_seconds`, which is what handles a machine that stays
    awake and should be slept again later.
    """
    ready, via_window, stamp = _fast_path_ready(cfg, idle_probe)
    if not ready:
        return False, fast_deadline, judged_window
    if via_window and stamp != judged_window:
        return True, now, stamp
    if now - fast_deadline >= cfg.fast_path_seconds:
        return True, now, judged_window
    return False, fast_deadline, judged_window


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
    g = _apply_guard_config(sysinfo.guards(), cfg)
    _print("  remote-in-use=%s display-in-use=%s media-playing=%s update-running=%s"
           % (g.remote_in_use, g.display_in_use, g.media_playing, g.update_running))
    _print("  switches     : remote=%s display=%s media=%s update=%s   (config: guard_*)"
           % (cfg.guard_remote_in_use, cfg.guard_display_in_use, cfg.guard_media_playing,
              cfg.guard_update_running))
    if g.ignored:
        # A guard that is switched off must be visible in doctor too: otherwise nobody can tell
        # "nothing was holding the machine" from "that check has been off for months".
        _print("  -> %s is switched OFF in the config, so it will not block a re-sleep" % g.ignored)
    elif g.active():
        _print("  -> a judgement right now would skip: %s" % g.label())
    elif g.unavailable:
        _print("  -> could not read the request list (%s): run doctor from an elevated shell"
               % g.unavailable)
    _print()

    _print("---- the resident jobs as REGISTERED (a config change does not edit them) ----")
    reg = _registered_task_settings("judge")
    if reg is None:
        _print("  nothing to compare on this platform (the unit/plist is rewritten by install)")
    else:
        want = "PT%dM" % int(cfg.repeat_minutes)
        got = reg.get("interval") or "?"
        if got == want:
            _print("  judge repeats every %s, config repeat_minutes=%d -> in sync"
                   % (got, int(cfg.repeat_minutes)))
        else:
            _print("  judge repeats every %s but the config asks for %s" % (got, want))
            _print("  -> run `autoresleep install` to rebuild the task (one UAC); editing the "
                   "config alone does not touch a registered task")
        limit = reg.get("limit") or ""
        if limit and limit != "PT0S":
            _print("  judge execution limit %s (a one-shot; PT1M..PT10M is a sane cap)" % limit)
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
    guards = _apply_guard_config(guards, cfg)
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


def _apply_guard_config(guards, cfg):
    """Switch the watchdogs on/off per config - and SAY when one was switched off.

    Chain 2 (the watchdogs) sits in front of every attribution chain, so a guard that is on blocks
    every re-sleep. Measured on the Windows box: a remote-desktop tool (CrossDesk.exe) held a
    DISPLAY power request the whole day, `display-in-use` fired on every judgement, and the operator
    saw a machine that never went back to sleep with nothing in the log to explain it. Hence
    `guard_display_in_use: false` by default, and hence `Guards.ignored`: masking a guard must not
    make it invisible, or the next reader cannot tell "nothing is holding the machine" from "that
    check is switched off".
    """
    masked: List[str] = []
    for name, enabled in (("remote-in-use", cfg.guard_remote_in_use),
                          ("display-in-use", cfg.guard_display_in_use),
                          ("media-playing", cfg.guard_media_playing),
                          ("update-running", cfg.guard_update_running)):
        if not enabled and getattr(guards, name.replace("-", "_")):
            masked.append(name)
    for name in masked:
        setattr(guards, name.replace("-", "_"), False)
    if masked:
        guards.ignored = "%s[%s]" % (masked[0], guards.detail) if guards.detail else masked[0]
    return guards


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
        idle.seconds, _window_label(ev), pkt_shown, ev.drv_reason or "-", guards.label())
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
        why = grace_verdict(before.tick, after.tick, after.seconds, needed,
                            _apply_guard_config(sysinfo.guards(), cfg).active())
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
    judged_window = 0.0        # which wake's window the fast path has already judged
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
            fire, fast_deadline, judged_window = _fast_path_fire(
                cfg, idle_probe, now2, fast_deadline, judged_window)
            if fire:
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
                            cfg.idle_minutes * 60.0,
                            _apply_guard_config(sysinfo.guards(), cfg).active())
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
    if getattr(args, "stop_pids", None):
        out += ["--stop-pids", str(args.stop_pids)]
    return out


def _data_dir_is_live() -> bool:
    """Is this run pointed at the machine's real data dir, or at a sandbox/temp one?

    A SANDBOXED install must never stop the listener that is serving the machine. Measured while
    testing the first-run config prompt: `LOCALAPPDATA=<temp> install` listed the LIVE listener's pid
    as a stale one - one UAC prompt away from stopping somebody's working installation. The default
    dir is recomputed from the home directory with an EMPTY environment, so this cannot be fooled by
    the same variable the sandbox uses to redirect the state.
    """
    try:
        live = str(paths.data_dir().resolve())
        default = str(paths.data_dir(env={}, home=Path.home()).resolve())
    except Exception:
        return False
    if os.name == "nt":
        # the same directory can come back with different capitalisation depending on who built the
        # Path (env var vs home), and a case-sensitive compare would call the live dir a sandbox
        return live.lower() == default.lower()
    return live == default


def _stop_pid_list(args) -> List[int]:
    """Parse the internal `--stop-pids 13676,34544` argument (answers travel as arguments)."""
    out: List[int] = []
    for chunk in str(getattr(args, "stop_pids", None) or "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


def _looks_like_our_listener(pid: int) -> bool:
    """Is this pid a Python process - i.e. plausibly one of OUR listeners?"""
    try:
        if os.name == "nt":
            from .system.base import decode_console, proc_kwargs
            out = decode_console(subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, **proc_kwargs()).stdout)
            return "python" in out.lower()
        comm = Path("/proc/%d/comm" % pid)
        if comm.exists():
            return "python" in comm.read_text(encoding="utf-8", errors="replace").lower()
        return False
    except Exception:
        return False


def _listener_pids_to_stop(ports: Sequence[int], marker: int) -> List[int]:
    """PIDs of our OWN listeners that still hold the WoL ports, besides this process.

    This list exists because the resident jobs are registered with `RunLevel=HighestAvailable`, so
    the listener runs at HIGH integrity and a normal-user install CANNOT kill it: measured on this
    machine, `taskkill /PID 13676 /F` answers "拒绝访问 / Access denied" - and the old code threw
    that result away and printed "stopped the previous listener". Two orphaned listeners then sat on
    UDP 9/7/67/68 while the lock file pointed at a pid that had already exited. The fix is not a
    bigger hammer: the pids are handed to the ELEVATED child, which can kill them.

    Only python processes are handed over: port 9 can legitimately belong to somebody else's tool (a
    v1 install, a WoL utility), and killing a stranger's process is not this tool's business.
    """
    if not _data_dir_is_live():
        return []
    owners = _udp_owners(list(ports) + [marker])
    if not owners:
        return []
    pids = set()
    for who in owners.values():
        pids.update(who)
    pids.discard(os.getpid())
    return [pid for pid in sorted(pids) if _looks_like_our_listener(pid)]


def _ports_to_scan(cfg=None) -> List[int]:
    """Which UDP ports to look at when hunting orphaned listeners.

    The union of the built-in WoL ports and whatever the config says - an orphan may have been
    started from an OLDER config whose port list has since changed, and it would keep holding the
    port we no longer think about.
    """
    from .pkt import DEFAULT_PORTS
    ports = set(DEFAULT_PORTS) | {45678}
    if cfg is not None:
        try:
            ports |= set(cfg.ports)
        except Exception:
            pass
    return sorted(ports)


def _stop_stale_listeners(args, progress) -> List[int]:
    """Kill the pids the parent handed over (child side). Returns the ones that survived."""
    survivors: List[int] = []
    for pid in _stop_pid_list(args):
        if not _pid_alive(pid):
            progress.step("stale listener pid %d: already gone" % pid)
            continue
        if _stop_pid(pid):
            progress.step("stale listener pid %d: stopped" % pid)
        else:
            survivors.append(pid)
            progress.step("stale listener pid %d: STILL ALIVE - it owns the WoL ports" % pid)
    return survivors


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
        # First thing the privileged half does: kill the listeners the parent could not kill. They
        # are high-integrity processes (the task XML says RunLevel=HighestAvailable), so only an
        # elevated process can stop them - which is exactly why the parent handed their pids over.
        _stop_stale_listeners(args, progress)
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
        # same as install: a listener registered with RunLevel=HighestAvailable can only be stopped
        # by an elevated process, so the parent hands its pid over instead of pretending to kill it
        _stop_stale_listeners(args, progress)
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


def _install_default_config(target: Path, cfg: Config) -> None:
    """Fresh machine: copy the project's `config.default.yaml` into the data dir.

    Ship-then-copy on purpose: the repository keeps the canonical default (so a rollout can edit it
    once and know what every new machine starts from), the machine gets a REAL copy, and every run
    reads that copy - `autoresleep config`, the judge and the listener all go through
    `paths.config_file()`, so editing the installed file is what changes this machine.
    """
    template = paths.default_config_template()
    if not template.exists():
        # a source tree without the template (an installed wheel, a minimal checkout) must still work
        cfg.save(target)
        _print("  %s written   (defaults)" % target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, target)
    _print("  %s  (从项目里的 %s 拷贝)" % (target, template.name))


def _same_values(a: Config, b: Config) -> bool:
    """Do two configs hold the same settings? (`extra` is a catch-all, not a setting.)"""
    try:
        left = {k: v for k, v in asdict(a).items() if k != "extra"}
        right = {k: v for k, v in asdict(b).items() if k != "extra"}
        return left == right
    except Exception:
        return False


def _prompt_after_first_config(args, target) -> bool:
    """First run: the config file has just appeared, so ask before doing anything else.

    Only the parent asks, and only on a real terminal. The elevated child is forbidden from asking
    anything (docs/install-pitfalls.md section 2 - a question inside a window nobody can see is a
    hang, not a question), and a test, a cron job or a CI run must never block on stdin. `--yes`
    answers it up front for scripts.
    """
    if (getattr(args, "elevated", False) or getattr(args, "yes", False)
            or os.environ.get("AUTORESLEEP_NO_PROMPT")):
        return True
    if not _stdin_can_answer():
        _print("  (非交互终端：按默认继续执行)")
        return True
    _print("")
    _print("  已生成默认配置文件：%s" % target)
    _print("  可以现在就去改（常动的几项：wake_nics / magic_only / resleep_delay_seconds /")
    _print("  resleep_break_on_input / guard_*），改完再跑一次 install 也一样。")
    try:
        answer = input("  是否继续执行 install？[Y/n] ")
    except EOFError:
        # Nobody is there to answer. Turning that into a STOP would silently skip the whole install
        # for a logon task, a cron job or a closed stdin - so it is the default (continue).
        _print("  (读不到输入：按默认继续执行)")
        return True
    except KeyboardInterrupt:
        _print("")
        _print("  已取消（没有注册任何计划任务）。")
        return False
    if answer.strip().lower() in ("n", "no", "0", "false"):
        _print("  已停止（没有注册任何计划任务）。改完配置后重新运行: autoresleep install")
        return False
    return True


def _stdin_can_answer() -> bool:
    """Is there a human on the other end of stdin? Everything else must not ask."""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _registered_task_settings(name: str) -> Optional[dict]:
    """What the SCHEDULER actually holds for one of our tasks (Windows), or None.

    The config and the task are two different places: `repeat_minutes` and the guard switches are
    read live from the file, but the repetition INTERVAL and the execution time limit are baked into
    the registered task and only change when `install` rebuilds it. Asking the scheduler is how the
    operator finds out that "I set repeat_minutes: 1" has not taken effect yet - or that a task from
    an older install still carries the PT10M limit that used to kill the listener every ten minutes.
    """
    if os.name != "nt":
        return None
    try:
        from .system.base import decode_console, proc_kwargs
        out = decode_console(subprocess.run(
            ["schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-%s" % name, "/XML"],
            capture_output=True, **proc_kwargs()).stdout)
    except Exception:
        return None
    if not out.strip():
        return None
    found: dict = {}
    m = re.search(r"<Interval>(PT[^<]*)</Interval>", out)
    if m:
        found["interval"] = m.group(1)
    m = re.search(r"<ExecutionTimeLimit>(PT[^<]*)</ExecutionTimeLimit>", out)
    if m:
        found["limit"] = m.group(1)
    return found


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
    fresh = not target.exists()
    migrated: Optional[Path] = None
    if not dry and fresh and not getattr(args, "config", None):
        # a machine installed before the config became YAML: carry its settings over, once
        migrated = Config.migrate_legacy()
        fresh = not target.exists()
    if dry:
        _print("  [dry-run] would write %s" % target)
    else:
        if fresh:
            _install_default_config(target, cfg)
            # READ BACK what was just installed: from here on the machine obeys its own copy, not
            # the project template and not whatever the CLI defaults happened to be
            cfg = Config.load(target).validate()
        elif migrated:
            _print("  %s written (migrated from config.json; the old file is now "
                   "config.json.migrated)" % target)
        elif _same_values(Config.load(target), cfg):
            _print("  %s 已存在，保持原样（注释与手工改动都留着）" % target)
        else:
            cfg.save(target)
            _print("  %s 已更新（命令行参数覆盖了文件里的值）" % target)
        if fresh and not _prompt_after_first_config(args, target):
            return 0

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
    delegated_stops: List[int] = []
    if not deferred:
        _print("  nothing needs elevation here")
    else:
        _print("  %s" % ", ".join(deferred))
        # Hand the orphaned listeners to the elevated child: they run at HIGH integrity
        # (RunLevel=HighestAvailable in the task XML), so this non-elevated process cannot kill them
        # - `taskkill /F` answers "拒绝访问 / Access denied". Measured consequence: two orphans left
        # holding UDP 9/7/67/68 while install happily printed "stopped the previous listener".
        stale = _listener_pids_to_stop(_ports_to_scan(cfg), cfg.marker_port) if not dry else []
        if stale:
            args.stop_pids = ",".join(str(p) for p in stale)
            delegated_stops = list(stale)
            _print("  stale listener(s) still on the WoL ports: %s (stopping them needs the "
                   "elevated step)" % ",".join(str(p) for p in stale))
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
    port_problems = 0
    stale_alive: List[int] = []
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
                    if _stop_pid(old_pid):
                        _print("  stopped the previous listener (pid %d)" % old_pid)
                    else:
                        stale_alive.append(old_pid)
                        # Say it out loud: "stopped" was printed unconditionally before, while the
                        # process was still alive and still owned UDP 9/7/67/68.
                        _print("  [failed] the previous listener (pid %d) is STILL ALIVE after "
                               "taskkill - it owns the WoL ports, so magic packets may be delivered "
                               "to it instead of the new listener: %s"
                               % (old_pid, _kill_hint(old_pid)))
                    time.sleep(1)
                elif old_pid in delegated_stops:
                    # we asked the elevated child to stop it a moment ago - saying "stale lock"
                    # here reads as "it died on its own" and sends the reader hunting a ghost
                    _print("  the previous listener (pid %d) was stopped by the elevated step"
                           % old_pid)
                elif old_pid != os.getpid():
                    # Measured here: the lock pointed at a pid that had already exited, while two
                    # ORPHANED listeners from earlier installs held UDP 9/7/67/68. Saying which pid
                    # is gone is the difference between "nothing to do" and "go look".
                    _print("  [note] the lock file points at pid %d, which is not running any more "
                           "(stale lock - replaced below)" % old_pid)
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
            _print("  [dry-run] would restart the listener and check which pid holds the ports")
        else:
            started = False
            for _ in range(20):
                time.sleep(1)
                if lock.exists():
                    started = True
                    break
            if started:
                new_pid = _lock_pid()
                _print("  listener: running (pid %s, lock file %s)" % (new_pid or "?", lock))
                # The lock file only says a listener is alive - it cannot tell a fresh one from a
                # stale one that still owns the magic port. Ask the OS.
                port_problems = _listener_port_check(new_pid, cfg.ports, cfg.marker_port)
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

    if port_problems or stale_alive:
        # Not a "done": the install did its work, but the packet chain is not watching, and that is
        # the one failure this tool cannot afford to report as success (an asked-for magic-packet
        # wake would be re-slept). Exit non-zero so a script wrapping install sees it.
        _print("=" * 66)
        _print(" done, but the packet chain is NOT fully watching")
        _print(" port problem(s): %d%s" % (port_problems,
                                          "" if not stale_alive else
                                          "   stale listener pid(s) alive: %s"
                                          % ",".join(str(p) for p in stale_alive)))
        _print(" magic-packet wakes can be re-slept while this is true - see the [failed] lines above")
        return 1
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
        # same reason as install: the listener runs at high integrity, so only the elevated child
        # can stop a leftover copy (measured: `taskkill /F` -> "拒绝访问 / Access denied")
        stale = _listener_pids_to_stop(_ports_to_scan(), 45678) if not dry else []
        if stale:
            args.stop_pids = ",".join(str(p) for p in stale)
            _print("  stale listener(s) still on the WoL ports: %s"
                   % ",".join(str(p) for p in stale))
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
                elif _stop_pid(pid):
                    _print("  stopped pid %d" % pid)
                else:
                    _print("  [failed] pid %d is STILL ALIVE (%s) - it keeps the WoL ports bound"
                           % (pid, _kill_hint(pid)))
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
        keep = {"config.yaml", "config.json"}
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


def _signal_pid(pid: int, force: bool) -> None:
    try:
        if os.name == "nt":
            from .system.base import proc_kwargs
            cmd = ["taskkill", "/PID", str(pid)]
            if force:
                cmd.append("/F")
            subprocess.run(cmd, capture_output=True, **proc_kwargs())
        else:
            import signal as _signal
            os.kill(pid, _signal.SIGKILL if force else _signal.SIGTERM)
    except Exception:
        pass


def _stop_pid(pid: int, wait: float = 3.0) -> bool:
    """Kill a stale process and CONFIRM it is gone. False means it is still there.

    Confirming is the whole point. Measured: `taskkill /F`'s exit code was thrown away and install
    printed "stopped the previous listener (pid N)" regardless - while the process stayed alive and
    bound to UDP 9/7/67/68. With SO_REUSEADDR (which on Windows *permits* sharing) the newly started
    listener then held the same ports, and which of the two receives a magic packet is not ours to
    decide. Silent either way, so the caller has to be able to tell.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    if not _pid_alive(pid):
        return True
    for force in (False, True):
        _signal_pid(pid, force)
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.2)
    return False


def _lock_pid() -> Optional[int]:
    """The pid the listener wrote into its lock file, or None."""
    lock = paths.lock_file()
    try:
        return int(lock.read_text(encoding="utf-8").strip().split()[0])
    except Exception:
        return None


def _parse_netstat_udp(out: str, want: set) -> dict:
    """`netstat -ano -p UDP` lines: protocol, local `addr:port`, foreign, then the pid."""
    owners: dict = {}
    for line in out.splitlines():
        m = re.match(r"\s*UDP\s+\S+:(\d+)\s+\S+\s+(\d+)\s*$", line)
        if m and int(m.group(1)) in want:
            owners.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
    return owners


def _parse_ss_udp(out: str, want: set) -> dict:
    """`ss -lunp` carries `pid=N` on the line; `lsof -nP -iUDP` puts the pid first and the port
    last (`python3  28088 jianghh ... UDP *:9`), so the two need different patterns."""
    owners: dict = {}
    for line in out.splitlines():
        m = re.search(r":(\d+)\s.*?pid=(\d+)", line)
        if m:
            port, pid = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"^\S+\s+(\d+)\s+\S+\s+.*:(\d+)\s*$", line)
            if not m:
                continue
            pid, port = int(m.group(1)), int(m.group(2))
        if port in want:
            owners.setdefault(port, set()).add(pid)
    return owners


def _udp_owners(ports: Sequence[int]) -> Optional[dict]:
    """Which PIDs hold each of these UDP ports. None = could not check (never "all clear").

    Best effort on purpose, and honest about it: a missing `ss`/`lsof`/`netstat` returns None so the
    caller says so instead of implying the ports are fine.
    """
    want = set(int(p) for p in ports)
    try:
        if os.name == "nt":
            from .system.base import decode_console, proc_kwargs
            out = decode_console(subprocess.run(["netstat", "-ano", "-p", "UDP"],
                                                capture_output=True, **proc_kwargs()).stdout)
            if not out.strip():
                # netstat missing or refused: NOT "nothing holds these ports" - that reading would
                # turn "cannot check" into a false [failed] alarm about the listener.
                return None
            return _parse_netstat_udp(out, want)
        out = ""
        # every subprocess call in this package goes through proc_kwargs() (or run_hidden): on
        # Windows a console-less judge otherwise pops a console window for each program it starts
        from .system.base import proc_kwargs
        for argv in (["ss", "-lunp"], ["lsof", "-nP", "-iUDP"]):
            try:
                proc = subprocess.run(argv, capture_output=True, **proc_kwargs())
            except Exception:
                continue
            if proc.returncode == 0 and proc.stdout:
                out = proc.stdout.decode("utf-8", "replace")
                break
        if not out:
            return None
        return _parse_ss_udp(out, want)
    except Exception:
        return None


def _listener_port_report(pid: Optional[int], ports: Sequence[int],
                          marker: int) -> Tuple[int, List[str]]:
    """Ask the OS who owns the ports the listener is supposed to watch: (problems, lines).

    The lock file only proves that *some* listener is alive. It cannot tell a fresh listener from a
    stale one that owns the magic port - and that is the state measured on a real machine: three
    processes bound to UDP 9/7/67/68, install reporting "listener: running". Ports 67/68 are held by
    the DHCP client by design, so they are reported as information only; on Linux as a normal user
    the magic ports legitimately belong to the privileged sensor instead.
    """
    expects_ownership = (os.name == "nt") or (sys.platform == "darwin") or _is_root()
    # ASK ABOUT THE MARKER PORT TOO. Asking for `ports` only (cfg.ports = 9,7,67,68) meant the
    # marker was never queried, so every install ended with "[failed] nothing holds UDP 45678" for
    # a listener that held it perfectly well - a self-inflicted false alarm, and the one that made
    # this check untrustworthy.
    owners = _udp_owners(list(ports) + [marker])
    if owners is None:
        return 0, ["  ports : cannot check ownership on this machine (needs ss/lsof) - if the packet "
                   "chain looks blind, check `autoresleep doctor`"]
    problems = 0
    lines: List[str] = []
    for port in sorted(set(list(ports) + [marker])):
        who = sorted(owners.get(port) or [])
        if port in (67, 68):
            # The DHCP client owns these by design, and a machine can legitimately have nobody on
            # them at all - either way this is information, never a problem. (It used to fall
            # through to the "somebody else holds it" branch and cried wolf twice per install.)
            lines.append("  port %-6d held by pid %s (the DHCP client - expected)"
                         % (port, ",".join(str(p) for p in who) or "-"))
            continue
        others = [p for p in who if p != pid]
        if pid and pid in who:
            if others:
                lines.append("  [failed] UDP %d is held by the new listener (pid %d) AND by pid %s - "
                             "a stale listener can steal magic packets: %s"
                             % (port, pid, ",".join(str(p) for p in others), _kill_hint(others[0])))
                problems += 1
            else:
                lines.append("  port %-6d held by pid %d" % (port, pid))
        elif who:
            level = "[failed]" if expects_ownership else "(info)  "
            lines.append("  %s UDP %d is held by pid %s, not by the new listener%s"
                         % (level, port, ",".join(str(p) for p in who),
                            "" if expects_ownership else " (the privileged sensor covers those ports)"))
            if expects_ownership:
                problems += 1
        else:
            level = "[failed]" if expects_ownership else "(info)  "
            lines.append("  %s nothing holds UDP %d after starting the listener - it is not watching "
                         "(see the listener log)" % (level, port))
            if expects_ownership:
                problems += 1
    return problems, lines


def _listener_port_check(pid: Optional[int], ports: Sequence[int], marker: int,
                         tries: int = 6) -> int:
    """Poll the port ownership briefly, then report the last snapshot.

    `cmd_listen` writes its lock file BEFORE it calls `bind()`, so a single snapshot can land in that
    gap and blame a listener that is perfectly healthy; the retries cost nothing when the answer is
    clean.
    """
    problems, lines = 1, []
    for _ in range(max(1, tries)):
        problems, lines = _listener_port_report(pid, ports, marker)
        if problems == 0:
            break
        time.sleep(1)
    for line in lines:
        _print(line)
    return problems


def _kill_hint(pid: int) -> str:
    return ("taskkill /PID %d /F" % pid) if os.name == "nt" else ("kill -9 %d" % pid)


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

    _print("config file: %s" % (Path(args.config) if getattr(args, "config", None)
                               else paths.config_file()))
    _print(cfg.to_yaml())
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
    sp.add_argument("--stop-pids", default=None, help=argparse.SUPPRESS)
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
    sp.add_argument("--yes", "-y", action="store_true", default=False,
                    help="do not stop to ask after generating a config file (scripts, cron)")
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
    sp.add_argument("--stop-pids", default=None, help=argparse.SUPPRESS)
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