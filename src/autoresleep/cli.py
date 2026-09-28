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
import time
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, journal, paths, resume as resume_mod
from .config import Config
from .decide import Action, Decision, Evidence, Guards, decide
from .pkt import DEFAULT_MARKER_PORT, DEFAULT_PORTS, Listener, adapters
from .scheduling import artifacts_for
from .system import get_system, read_evidence, write_wake_evidence


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

    _print("---- firewall (can the listener's UDP ports be reached?) ----")
    fw = sysinfo.firewall()
    _print("  state : %s   %s" % (fw.state, fw.detail))
    if fw.rule_names:
        _print("  rules : %s" % ", ".join(fw.rule_names[:10]))
    for hint in fw.hints:
        _print("  hint  : %s" % hint)
    _print()

    _print("---- config ----")
    for key in ("idle_minutes", "unknown_idle_minutes", "unattributed_wake_policy",
                "repeat_minutes", "min_gap_minutes", "pkt_window_s", "ports", "marker_port"):
        _print("  %-26s %s" % (key, getattr(cfg, key)))
    hb = journal.read_heartbeat()
    _print()
    _print("---- is it alive? ----")
    _print("  heartbeat: %s" % (hb or "never ran"))
    log.info("doctor: platform=%s idle=%.1fs", sys.platform, idle.seconds)
    return 0


# --------------------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------------------
def build_judgement(args, sysinfo=None, cfg: Optional[Config] = None):
    sysinfo = sysinfo or _system(args.platform)
    cfg = cfg or _load_config(args)
    idle = sysinfo.idle()

    # Attribute THIS wake, every run. The live query is cheap and it is the only way to know
    # which wake we are judging: a stored attribution from an earlier wake is history, not
    # evidence (read_evidence rejects it by wake time).
    live = sysinfo.wake_source()
    ev = read_evidence(current_wake_epoch=live.wake_epoch)
    if _evidence_is_empty(ev) and live.kind != "unknown":
        link, link_why = sysinfo.link_evidence(live.wake_epoch)
        write_wake_evidence(live, link=link, link_why=link_why)
        ev = read_evidence(current_wake_epoch=live.wake_epoch)
    guards = sysinfo.guards()
    forced, forced_tick = _last_forced_sleep_info()
    # False is a positive statement ("no input since we slept it"), None means "cannot tell"
    input_since_sleep = None
    if forced_tick and idle.tick:
        input_since_sleep = (idle.tick != forced_tick)
    now = time.time()
    decision = decide(ev, guards, idle_s=idle.seconds, params=cfg.params(), now=now,
                      last_forced_sleep_at=forced,
                      input_since_sleep=input_since_sleep, wake_kind=None)
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


def _evidence_is_empty(ev: Evidence) -> bool:
    return not any((ev.pkt_kind, ev.drv_reason, ev.os_link, ev.wake_kind))


def cmd_judge(args) -> int:
    decision, idle, ev, guards, cfg = build_judgement(args)
    log = journal.get_logger()
    dl = journal.decision_logger()

    line = decision.line()
    detail = "idle=%.0fs  pkt=%s  drv=%s  guards=%s" % (
        idle.seconds, ev.pkt_kind or "-", ev.drv_reason or "-", guards.active() or "-")
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

    listener = Listener(ports=cfg.ports, marker_port=cfg.marker_port)
    listener.bind()
    detector = resume_mod.ResumeDetector(threshold_s=cfg.resume_threshold_s)
    detector.observe()
    log.info("listener started pid=%d ports=%s marker=%d macs=%s", os.getpid(),
             ",".join(str(p) for p in listener.sockets), cfg.marker_port, listener.macs)
    _print("listener up: ports=%s marker=%d macs=%s"
           % (sorted(listener.sockets), cfg.marker_port, listener.macs))
    if listener.unavailable():
        _print("  (not bound, normal on a desktop: %s)"
               % ", ".join("%d=%s" % (p, e) for p, e in listener.unavailable().items()))

    def idle_probe() -> float:
        try:
            return _system(args.platform).idle().seconds
        except Exception:
            return -1.0

    fast_deadline = 0.0
    last_beat = 0.0
    try:
        while True:
            for seen in listener.poll():
                with open(paths.packet_log(), "a", encoding="utf-8") as fh:
                    fh.write("%s  DETECTED kind=%s src=%s len=%d\n"
                             % (time.strftime("%Y-%m-%d %H:%M:%S"), seen.kind, seen.src,
                                seen.length))
                paths.packet_state().write_text(seen.line(), encoding="ascii")
                log.info("packet: %s", seen.line())
                _print("detected %s from %s" % (seen.kind, seen.src))
            if detector.observe():
                log.info("resume detected")
                _print("resume detected -> collecting evidence")
                cmd_collect(args)
                fast_deadline = 0.0        # judge immediately after a resume

            # v1's fast path, and the reason every cycle was 5-8 minutes without it: the judge
            # task only fires every `repeat_minutes`, so waiting for it adds up to two minutes on
            # top of the idle threshold and the grace window. The listener is already awake once a
            # second, so once the idle threshold is met it re-judges itself every
            # `fast_path_seconds` and suspends on the spot.
            now2 = time.time()
            if (idle_probe() >= cfg.idle_minutes * 60.0 and now2 - fast_deadline >= cfg.fast_path_seconds):
                fast_deadline = now2
                _fast_path_judge(args, cfg, log)
            now = time.time()
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
    src = sysinfo.wake_source()
    path = write_wake_evidence(src, chain=src.chain)
    journal.get_logger().info("collect: kind=%s who=%s -> %s", src.kind, src.who, path)
    _print("evidence: kind=%s who=%s chain=%s" % (src.kind, src.who or "-", src.chain))
    if src.raw:
        _print("  raw: %s" % src.raw[:300])
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


def _apply_cmd(cmd: Sequence[str], dry_run: bool) -> None:
    if dry_run:
        _print("    [dry-run] %s" % " ".join(cmd))
        return
    try:
        from .system.base import proc_kwargs
        proc = subprocess.run(list(cmd), capture_output=True, **proc_kwargs())
        if proc.returncode == 0:
            _print("    [ok] %s" % " ".join(cmd))
            return
        _print("    [failed] %s" % " ".join(cmd))
        detail = _error_excerpt((proc.stderr or b"") + (proc.stdout or b""))
        if detail:
            _print("        %s" % detail)
    except Exception as exc:
        _print("    [failed] %s -> %s" % (" ".join(cmd), exc))


def _require_admin(sysinfo, argv: List[str], dry: bool, what: str) -> bool:
    """Refuse early, with the exact command to re-run, instead of failing halfway.

    On Windows registering a HighestAvailable task and editing firewall rules need elevation;
    on Linux and macOS the user-level units / LaunchAgents need none, so this is a no-op there.
    """
    if dry or sysinfo.is_admin() or os.name != "nt":
        return True
    _print("")
    _print("需要管理员权限：%s 都需要提权，否则会在中途以 access denied 失败。" % what)
    _print("请用管理员 PowerShell / 右键“以管理员身份运行”重新执行：")
    _print("    %s" % " ".join(['"%s"' % sys.executable] + argv))
    _print("（只想看看会改什么：在上面加 --dry-run，那个不需要提权）")
    return False


def cmd_install(args) -> int:
    cfg = _load_config(args).validate()
    sysinfo = _system(args.platform)
    dry = args.dry_run

    if not _require_admin(sysinfo, [str(Path(__file__).resolve().parents[2] / "autoresleep.py"),
                                    "install"] ,
                          dry, "注册计划任务、写防火墙规则"):
        return 2

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
    for a in artifacts:
        _print("  %-28s %s" % (a.name, a.path))
        if a.kind == "task":
            _print("      %s   (xml persisted next to the state files)"
                   % (a.note if not dry else "xml would be written to " + a.note.split()[-1]))
        else:
            if dry:
                _print("      [dry-run] would write %s (%d bytes)" % (a.path, len(a.content)))
            else:
                p = Path(os.path.expanduser(a.path))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(a.content, encoding="utf-8")
                _print("      written %s" % p)
    if not dry:
        _write_windows_xml(artifacts)
    for a in artifacts:
        _print("  apply:")
        _apply_cmd(a.apply_cmd, dry)

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
        program = sys.executable
        if dry:
            _print("  [dry-run] would allow UDP %s + marker %d for %s"
                   % (",".join(str(p) for p in cfg.ports), cfg.marker_port, program))
        else:
            done = sysinfo.firewall_apply(list(cfg.ports), cfg.marker_port, program)
            _print("  applied: %s" % (", ".join(done) if done else "nothing (see state above)"))
        for h in fw.hints:
            _print("  hint: %s" % h)

    _print("---- 4. start the resident jobs now ----")
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

    _print()
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
    sysinfo = _system(args.platform)
    dry = args.dry_run
    if not _require_admin(sysinfo, [str(Path(__file__).resolve().parents[2] / "autoresleep.py"),
                                    "uninstall"], dry,
                          "删除计划任务、删防火墙规则"):
        return 2
    _print("=" * 66)
    _print(" AutoReSleep v%s - uninstall%s" % (__version__, "  (dry run)" if dry else ""))
    _print("=" * 66)

    artifacts = artifacts_for(sys.platform, python_exe=sys.executable,
                              workdir=str(Path(__file__).resolve().parents[2]),
                              interval_minutes=2, config_path=getattr(args, "config", None))
    _print("---- 1. scheduler artifacts ----")
    for a in artifacts:
        _print("  %-28s %s" % (a.name, a.path))
        _apply_cmd(a.remove_cmd, dry)
        if a.kind != "task" and not dry:
            try:
                Path(os.path.expanduser(a.path)).unlink()
                _print("      removed file %s" % a.path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                _print("      [failed] removing %s -> %s" % (a.path, exc))

    _print("---- 2. firewall rules ----")
    if args.no_firewall:
        _print("  skipped by --no-firewall")
    else:
        if dry:
            _print("  [dry-run] would remove the AutoReSleep-WoL-UDP-* rules")
        else:
            removed = sysinfo.firewall_remove()
            _print("  removed: %s" % (", ".join(removed) if removed else "none found"))

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
    if args.set:
        for pair in args.set:
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
                    setattr(cfg, key, [int(v) for v in re.split(r"[,\s]+", value.strip()) if v])
                else:
                    setattr(cfg, key, value)
            except ValueError as exc:
                _print("cannot set %s=%r: %s" % (key, value, exc))
                return 2
        target = cfg.save(Path(args.config) if getattr(args, "config", None) else None)
        _print("saved: %s" % target)
        for pair in args.set:
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
    sp.add_argument("--grace", type=int, default=60, metavar="SECONDS",
                    help="wait this long before suspending and re-check for user input "
                         "(0 disables; v1's tier-A grace)")
    sp.add_argument("--idle-minutes", type=float, default=None)
    sp.add_argument("--unknown-idle-minutes", type=float, default=None)
    sp.add_argument("--unattributed-wake-policy", choices=["skip", "resleep-if-unattended"],
                    default=None)
    sp.set_defaults(func=cmd_judge)

    sp = sub.add_parser("listen", help="resident packet/marker listener")
    common(sp)
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("collect", help="record the wake-source evidence (run on resume)")
    common(sp)
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("install", help="write config + native scheduler artifacts")
    common(sp)
    sp.add_argument("--dry-run", action="store_true", default=False,
                    help="print exactly what would change, touch nothing")
    sp.add_argument("--no-firewall", action="store_true", default=False)
    sp.add_argument("--no-start", action="store_true", default=False,
                    help="do not start the resident listener now (it starts at next logon)")
    sp.add_argument("--idle-minutes", type=float, default=None)
    sp.add_argument("--unknown-idle-minutes", type=float, default=None)
    sp.add_argument("--unattributed-wake-policy", choices=["skip", "resleep-if-unattended"],
                    default=None)
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="remove tasks/units/agents, firewall rules, daemon")
    common(sp)
    sp.add_argument("--dry-run", action="store_true", default=False)
    sp.add_argument("--no-firewall", action="store_true", default=False)
    sp.add_argument("--purge", action="store_true", help="also delete logs and evidence")
    sp.add_argument("--keep-logs", action="store_true", help="with --purge: keep *.log")
    sp.set_defaults(func=cmd_uninstall)

    sp = sub.add_parser("config", help="show or change the thresholds")
    common(sp)
    sp.add_argument("--set", action="append", metavar="KEY=VALUE")
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