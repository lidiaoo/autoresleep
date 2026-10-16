#!/usr/bin/env python3
"""Prove the wake-CAUSE chain (nl80211 WoWLAN report -> session exemption) without sleeping.

The chain this checks exists because of one physical fact: the packet that wakes the machine is
consumed by the NIC firmware, so no socket ever sees it. On an Intel/CNVi wifi card the driver
still asks the firmware WHY it woke and hands the answer to cfg80211, and `iw event` prints it:

    1792130691.782796: wlan0 (phy #0): WoWLAN wakeup
        * magic packet received
    1792130720.783661: wlan0 (phy #0): WoWLAN wakeup
        wakeup not due to WoWLAN

The first line is a real magic packet (this box, 2026-10-16, from 192.168.0.140), the second a wake
the NIC itself says was NOT wireless. Reading them is what makes "one magic packet keeps the
machine awake" true without a second packet, and what stops a non-wireless wake from being filed
against the wifi adapter.

Nothing here sleeps the machine, binds UDP 9/7, or touches the real `iw`: a fake `iw` is put on
PATH for the duration of the run and the reader thread is the production one. Everything is written
under a temporary data directory.

    python tools/verify_wowlan_chain.py

Exit code 0 = every step passed. Any FAIL line names the step that broke.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: verbatim from the real box (2026-10-16 14:04:51 / 14:05:20). Kept as the literal text `iw`
#: prints, because the whole point of the parser is that it matches the kernel's shapes and not a
#: shape someone imagined.
MAGIC_REPORT = (
    "1792130691.782796: wlan0 (phy #0): WoWLAN wakeup\n"
    "\t* magic packet received\n"
    "\t* packet (might be truncated): ff:ff:ff:ff:ff:ff 96:56:06:9b:a8:24 08:00 45:00 00:54 "
    "\t  c0:a8:00:8c c0:a8:00:ff a4:07 00:09 00:6e ff:ff:ff:ff:ff:ff 28:d0:ea:27:40:0c "
    "28:d0:ea:27:40:0c\n"
)
NOT_WOWLAN_REPORT = (
    "1792130720.783661: wlan0 (phy #0): WoWLAN wakeup\n"
    "\twakeup not due to WoWLAN\n"
)
OTHER_REPORT = (
    "1792130800.100000: wlan0 (phy #0): WoWLAN wakeup\n"
    "\t* disconnect detected\n"
)

_FAILED: list = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    """One assertion, printed as it happens. Never raises: the whole run is the deliverable."""
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", label, ("  -> " + detail) if detail else ""))
    if not ok:
        _FAILED.append(label)
    return ok


def _fake_iw(bin_dir: Path, body: str) -> Path:
    """A stand-in `iw` that prints `body` and then stays alive like the real tap."""
    path = bin_dir / "iw"
    path.write_text("#!/bin/sh\nprintf '%s'\nsleep 30\n" % body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _wait_for(pred, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def main() -> int:
    from autoresleep import paths as _paths

    # Read BEFORE the environment is redirected: this is the INSTALLED data dir, the one the running
    # sensor writes to. Everything below works in a temporary copy, so step [6] has to be pointed
    # at the real file explicitly or it would report on its own sandbox.
    real_log = _paths.wowlan_event_log()

    data_dir = tempfile.mkdtemp(prefix="ar-verify-wowlan-")
    os.environ["LOCALAPPDATA"] = data_dir
    os.environ["XDG_STATE_HOME"] = data_dir
    os.environ["AUTORESLEEP_NO_ELEVATE"] = "1"

    from unittest import mock

    from autoresleep import cli, iwevent, journal, paths
    from autoresleep.config import Config
    from autoresleep.decide import Action
    from autoresleep.system import get_system, read_evidence, read_session_cause
    from autoresleep.system.base import WakeSource
    from autoresleep.system import linux as linux_mod
    from autoresleep.system.linux import LinuxSystem

    paths.ensure_data_dir()
    print("data dir : %s (temporary; the installed one is not touched)" % paths.data_dir())
    print("platform : %s" % sys.platform)

    # ---- 1. the parser, against the verbatim kernel output ------------------------------
    print("\n[1] parser: what `iw event` actually printed")
    rows = [("magic", MAGIC_REPORT), ("not-wowlan", NOT_WOWLAN_REPORT), ("other", OTHER_REPORT)]
    for want, text in rows:
        got = iwevent.classify_wowlan(text.splitlines()[1:])
        check(got.startswith(want), "report -> %s" % want, "got %r" % got)
    check(iwevent.is_wowlan_header(MAGIC_REPORT.splitlines()[0]),
          "the header line is recognised as a report")
    check(iwevent.parse_event_line("\t* magic packet received") is None,
          "a bullet line has no timestamp (that is how the block is delimited)")
    check(iwevent.is_link_move("1790738412.501000: wlp5s0 (phy #0): disconnected (by AP)"),
          "a real link move is still a link move (the two taps share one child)")

    # ---- 2. the tap, through the production reader thread --------------------------------
    print("\n[2] tap: a fake `iw` child -> wowlan-events.log")
    bin_dir = Path(data_dir) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _fake_iw(bin_dir, MAGIC_REPORT)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = "%s:%s" % (bin_dir, old_path)
    target = iwevent.wowlan_tap_target(True, paths.wowlan_event_log())
    watcher = iwevent.IwEventWatcher(Path(data_dir) / "wlan-events.log", wowlan_file=target)
    check(watcher.start(), "the tap started")
    wanted = _wait_for(lambda: bool(iwevent.read_wowlan_log(watcher.wowlan_file)))
    filed = iwevent.read_wowlan_log(watcher.wowlan_file)
    check(wanted and filed and filed[-1]["kind"] == "magic",
          "the report was filed as kind=magic",
          "rows=%s" % (filed or "none"))
    check("magic packet received" in (filed[-1]["detail"] if filed else ""),
          "the bullet text is kept in detail= (evidence, not a verdict)")
    check(len(watcher.take_wowlan()) >= 1, "the report was handed to the sensor's main loop")
    watcher.stop()

    # ---- 3. the exemption: report -> packet file -> engine layer 1 ----------------------
    print("\n[3] exemption: kind=magic must exempt the whole session, like a heard packet")
    from autoresleep.system import write_wake_evidence
    write_wake_evidence(WakeSource(kind="wifi-nic", who="wakeup-event:wlan0@1s"), link=None)
    session = read_evidence().session
    cli._record_wowlan({"kind": "magic", "epoch": time.time(), "detail": "verify"}, journal.get_logger())
    state = paths.packet_state().read_text(encoding="ascii").strip() if paths.packet_state().exists() else ""
    check(state.startswith("magic"), "packet-last.txt says magic", state)
    check(state.endswith("src=driver:wowlan|len=0"), "and names the driver report as its source", state)
    check(read_session_cause(session) == "asked-for", "the session latch says asked-for")
    check(read_evidence().pkt_kind == "magic", "the engine sees pkt_kind=magic")

    args = cli.build_parser().parse_args(["judge", "--config", str(paths.config_file())])
    decision, idle, ev, _guards, cfg = cli.build_judgement(args)
    print("       judgement: %s" % decision.line())
    check(decision.action == Action.SKIP and decision.reason == "wake-requested",
          "the judgement is skip/wake-requested", str(decision.reason))

    # a report the driver could not classify must exempt NOTHING (the failure this guards against
    # is a report that quietly counts as a packet)
    print("\n[3b] and an unclassified report must not exempt anything")
    with tempfile.TemporaryDirectory() as td2:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td2, "XDG_STATE_HOME": td2}):
            paths.ensure_data_dir()
            write_wake_evidence(WakeSource(kind="wifi-nic", who="x"), link=None)
            s2 = read_evidence().session
            cli._record_wowlan({"kind": "other:disconnect", "epoch": time.time(), "detail": "d"},
                               journal.get_logger())
            check(read_session_cause(s2) is None, "no latch")
            check(not paths.packet_state().exists(), "no packet file")

    # ---- 4. the denial: "not due to WoWLAN" must not be filed against the wifi adapter ---
    print("\n[4] denial: `wakeup not due to WoWLAN` vs the per-device counters")
    with tempfile.TemporaryDirectory() as td3:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td3, "XDG_STATE_HOME": td3}):
            paths.ensure_data_dir()
            now_ms = time.monotonic() * 1000.0
            paths.wowlan_event_log().write_text(
                "# wowlan tap started\n"
                "%s  wowlan  kind=not-wowlan epoch=%.6f detail=wlan0 (phy #0): WoWLAN wakeup | "
                "wakeup not due to WoWLAN\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), time.time()),
                encoding="utf-8")
            check(LinuxSystem._wowlan_says_not_wireless(), "the report is read as a denial")

            on = LinuxSystem(config=Config(wowlan_wake_reason=True))
            off = LinuxSystem(config=Config(wowlan_wake_reason=False))
            check(on.wowlan_wake_reason is True and off.wowlan_wake_reason is False,
                  "the config switch reaches the platform layer")

            # two fabricated counters: the wifi (what the denial is about) and the wired card
            fabricate = [
                {"iface": "wlan0", "kind": "wifi", "last_ms": now_ms, "active": 3},
                {"iface": "enp3s0", "kind": "eth", "last_ms": now_ms - 1500.0, "active": 1},
            ]
            with mock.patch.object(LinuxSystem, "_wakeup_event_rows", return_value=fabricate):
                plain = on._wakeup_event_nic(ignore_wifi=False)
                denied = on._wakeup_event_nic(ignore_wifi=True)
            check(plain and plain[0] == "wifi-nic", "counter hit: wifi-nic without the denial",
                  str(plain))
            check(denied and denied[0] == "eth-nic",
                  "with the denial the wifi counter loses, the wired one still wins", str(denied))

            # and the wiring itself: does wake_source() pass the denial down?
            with mock.patch.object(LinuxSystem, "_wowlan_says_not_wireless", return_value=True), \
                 mock.patch.object(LinuxSystem, "_wakeup_event_nic",
                                   return_value=("eth-nic", "wakeup-event:enp3s0@2s", {})) as counters, \
                 mock.patch.object(linux_mod, "_run", return_value=""), \
                 mock.patch("shutil.which", return_value=None):
                src = on.wake_source()
            check(counters.called and counters.call_args.kwargs.get("ignore_wifi") is True,
                  "wake_source(ignore_wifi=True) when the driver denied being the cause")
            check(src.kind == "eth-nic", "and it does not name the wifi adapter", src.kind)

            # ---- 5. the switch, from the other side --------------------------------------
            print("\n[5] switch off: the channel must disappear, not merely be ignored")
            check(LinuxSystem._wowlan_says_not_wireless(enabled=False) is False,
                  "the denial is not read at all when wowlan_wake_reason=false")
            check(iwevent.wowlan_tap_target(False, paths.wowlan_event_log()) is None,
                  "the sensor's tap target becomes None (no file is written, no report is queued)")

    with tempfile.TemporaryDirectory() as td5:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td5, "XDG_STATE_HOME": td5}):
            paths.ensure_data_dir()
            off_target = iwevent.wowlan_tap_target(False, paths.wowlan_event_log())
            w_off = iwevent.IwEventWatcher(Path(td5) / "wlan-off.log", wowlan_file=off_target)
            w_off.start()
            _wait_for(lambda: False, timeout=1.0)     # give the fake child time to print
            w_off.stop()
            check(not paths.wowlan_event_log().exists(),
                  "switched off, a magic report on the wire leaves no file behind")

    # ---- 6. this machine, right now ------------------------------------------------------
    print("\n[6] this machine right now (read-only, the INSTALLED data dir)")
    live = real_log
    unreadable = ""
    try:
        readable = live.exists()
    except Exception as exc:                      # a state dir we are not allowed to stat is not
        readable = False                          # a failed check - say so and carry on
        unreadable = "%s cannot be read here (%s)" % (live, exc)
        print("       " + unreadable)
    if readable:
        live_rows = iwevent.read_wowlan_log(live)
        print("       %s: %d report(s)" % (live, len(live_rows)))
        for row in live_rows[-3:]:
            print("         %s  kind=%s  %s" % (time.strftime("%H:%M:%S",
                                                              time.localtime(float(row["epoch"]))),
                                                row["kind"], row["detail"][:80]))
        newest = iwevent.last_wowlan(live)
        age = time.time() - float(newest["epoch"]) if newest else None
        print("       newest  : %s%s" % (newest["kind"], "  (%.0fs old)" % age if age else ""))
        if newest and newest["kind"] == "not-wowlan":
            print("       -> the last wake was NOT wireless; with the switch on, the wifi counter "
                  "is not allowed to name wlan0")
    else:
        if not unreadable:
            print("       %s has no report to read" % live)
        print("       -> no wake has been reported through this channel on this machine yet, the")
        print("          sensor predates it, or this shell cannot see that directory. The reader")
        print("          thread above proves the parse works; the row appears after the ON-MACHINE")
        print("          test below.")

    # ---- 7. the on-machine test that cannot be faked -------------------------------------
    print("\n[7] the one step a script cannot do: a real wake")
    print("       sudo systemctl restart autoresleep-wol-sensor   # the sensor is a unit: it must")
    print("                                                       # be restarted to take new code")
    print("       systemctl suspend                              # then send ONE magic packet")
    print("       python tools/verify_wowlan_chain.py            # after it wakes: step [6] must")
    print("                                                      # show kind=magic for this wake")
    print("       (capture the raw report yourself, if you want to see it unfiltered:)")
    print("       sudo iw event -t | tee /tmp/iwev.log           # then: grep -i wowlan /tmp/iwev.log")
    print("\n       the same question on the other two platforms (no reader here, by design):")
    print("       Windows : powercfg /lastwake                        -> names the DEVICE only;")
    print("                 Get-WinEvent -FilterHashtable @{LogName='System';"
          "ProviderName='Microsoft-Windows-Power-Troubleshooter';Id=1} -MaxEvents 1")
    print("                 -> WakeSourceText = an adapter name, never the packet. Arm magic-only")
    print("                    in the driver instead: Get-NetAdapterAdvancedProperty | ? DisplayName -Match 'Wake'")
    print("       macOS   : pmset -g log | grep -i 'Wake.*due to'   -> ARPT/GLAN/EHC/RTC codes")
    print("                 pmset -g assertions | grep -i magicwake  -> is a magic wake armed?")
    print("                 sudo pmset -a womp 1                     -> the OS-side equivalent switch")

    print("\n%s" % ("ALL CHECKS PASSED" if not _FAILED else
                    "FAILED: " + "; ".join(_FAILED)))
    os.environ["PATH"] = old_path          # the fake `iw` is only ours for as long as we run
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
