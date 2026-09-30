"""A short sleep must not be invisible, and a latch must not outlive the sleep that ended it.

The failure this pins, measured on the real box:

    10:50:06  PM: suspend entry (deep)
    10:50:23  resume                        <- a 17-second wifi-disconnect wake
    ...       no "resume detected" in the journal, ever

The resume detector compared the wall clock with the monotonic clock and only believed a sleep
longer than 90 s, so the collector never ran, the session stamp never moved, and a stale
"keyboard woke it" latch (written during an earlier verification) kept matching. Every judgement
then said `wake-by-input who=input-this-session`, the 2-minute idle threshold was never reached,
and the machine stayed awake - which is exactly what the operator reported.

The fix is a second, exact clock: CLOCK_BOOTTIME counts suspend time and CLOCK_MONOTONIC does
not, so their difference is the machine's accumulated sleep and ANY sleep is visible in it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import paths, resume                      # noqa: E402
from autoresleep.system import (read_evidence, read_session_cause,   # noqa: E402
                                write_session_cause, write_wake_evidence)
from autoresleep.decide import Guards                        # noqa: E402
from autoresleep.system.base import WakeSource               # noqa: E402


def _wake(epoch: float = 1000.0) -> WakeSource:
    return WakeSource(kind="wol", who="wlan0", chain="os", wake_epoch=epoch, raw="test")


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self._tmp.name
        paths.ensure_data_dir()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._old
        self._tmp.cleanup()


class TestTheSuspendLedger(_Isolated):
    """The clock that cannot miss a sleep, however short."""

    def test_a_seventeen_second_sleep_is_seen_although_the_threshold_cannot_see_it(self):
        # the machine was away 17 s of wall time; monotonic barely moved
        seq = [(100.0, 5_000.0), (100.5, 5_017.0)]
        it = iter(seq)
        with mock.patch.object(resume, "suspend_total", return_value=100.0):
            ledger = resume.SuspendLedger(resume.ResumeDetector(threshold_s=90.0))
            ledger.observe(*next(it))
            with mock.patch.object(resume, "suspend_total", return_value=117.0):
                slept = ledger.observe(*next(it))
        self.assertIsNotNone(slept, "a 17-second sleep must not be invisible")
        self.assertAlmostEqual(slept, 17.0, places=1)

    def test_the_old_detector_alone_would_have_missed_it(self):
        det = resume.ResumeDetector(threshold_s=90.0)
        det.observe(100.0, 5_000.0)
        self.assertFalse(det.observe(100.5, 5_017.0), "the threshold heuristic is blind to 17 s")

    def test_a_platform_without_the_ledger_keeps_the_old_behaviour(self):
        with mock.patch.object(resume, "suspend_total", return_value=None):
            ledger = resume.SuspendLedger(resume.ResumeDetector(threshold_s=90.0))
            ledger.observe(100.0, 5_000.0)
            # wall moved 5 minutes while our process was not running: the fallback must still fire
            self.assertIsNotNone(ledger.observe(100.5, 5_300.0))

    def test_nothing_happened_means_nothing_is_reported(self):
        with mock.patch.object(resume, "suspend_total", return_value=100.0):
            ledger = resume.SuspendLedger(resume.ResumeDetector(threshold_s=90.0))
            ledger.observe(100.0, 5_000.0)
            self.assertIsNone(ledger.observe(100.5, 5_000.5))

    def test_the_real_clock_reads_a_positive_ledger_on_this_machine(self):
        # CLOCK_BOOTTIME - CLOCK_MONOTONIC is the machine's total sleep; on a box that has slept
        # at least once it is positive. Skipped only where the platform has neither clock.
        total = resume.suspend_total()
        if total is None:
            self.skipTest("this platform exposes no suspend ledger")
        self.assertGreaterEqual(total, 0.0)


class TestClockJitterIsNotASleep(_Isolated):
    """Two clocks read one after the other wobble by up to a tick; that is not a suspend.

    Measured on the real box: the ledger "detected a resume" four times in eleven seconds while the
    machine was wide awake (11:05:28/31/32/34), and every one of those rewrote the evidence and
    retired a legitimate latch. The jitter is sub-microsecond here, but it is a timer tick on other
    kernels, so the threshold has to exist.
    """

    def test_a_tick_of_jitter_is_not_a_sleep(self):
        with mock.patch.object(resume, "suspend_total", return_value=100.0):
            ledger = resume.SuspendLedger()
            ledger.observe(50.0, 1_000.0)
            with mock.patch.object(resume, "suspend_total", return_value=100.000001):
                self.assertIsNone(ledger.observe(50.5, 1_000.5),
                                  "sub-tick jitter must not look like a resume")

    def test_jitter_never_accumulates_into_a_false_sleep(self):
        # each reading creeps up by a hair; if the ledger summed the creep it would eventually
        # cross the threshold and retire a latch for no reason
        total = 100.0
        with mock.patch.object(resume, "suspend_total", side_effect=lambda: total):
            ledger = resume.SuspendLedger()
            ledger.observe(50.0, 1_000.0)
            for i in range(200):
                total = 100.0 + i * 0.001          # 0.2 s of creep over 200 reads
                self.assertIsNone(ledger.observe(50.5 + i, 1_000.5 + i))

    def test_a_real_sleep_well_above_the_tolerance_is_still_seen(self):
        with mock.patch.object(resume, "suspend_total", return_value=100.0):
            ledger = resume.SuspendLedger()
            ledger.observe(50.0, 1_000.0)
            with mock.patch.object(resume, "suspend_total", return_value=101.0):
                self.assertAlmostEqual(ledger.observe(50.5, 1_001.0), 1.0, places=6)


class TestALatchCannotOutliveTheSleepThatEndedIt(_Isolated):
    """The stamp alone was not enough; the ledger is what actually retires a latch."""

    def test_a_latch_from_before_a_later_sleep_does_not_apply(self):
        write_wake_evidence(_wake(), session=42.0, slept_total=100.0)
        write_session_cause("input", 42.0, slept_total=100.0)
        # same session stamp (nobody re-collected), but the machine has since slept 17 s more
        self.assertEqual(read_session_cause(42.0, 100.0), "input")
        self.assertIsNone(read_session_cause(42.0, 117.0),
                          "the machine slept again: that latch was about the earlier wake")

    def test_a_latch_still_applies_when_the_machine_did_not_sleep(self):
        write_wake_evidence(_wake(), session=42.0, slept_total=100.0)
        write_session_cause("asked-for", 42.0, slept_total=100.0)
        self.assertEqual(read_session_cause(42.0, 100.0), "asked-for")
        self.assertEqual(read_session_cause(42.0, None), "asked-for")

    def test_a_different_session_never_matches(self):
        write_session_cause("input", 42.0, slept_total=100.0)
        self.assertIsNone(read_session_cause(43.0, 100.0))

    def test_a_latch_written_without_a_ledger_still_matches_on_its_stamp(self):
        # backwards compatibility: an old-format file must not start lying about the session
        write_session_cause("input", 42.0)
        self.assertEqual(read_session_cause(42.0, 117.0), "input")


class TestTheEvidenceCarriesTheLedger(_Isolated):
    """The judge heals itself from the file, so the reading has to be in the file."""

    def test_the_ledger_reading_round_trips(self):
        write_wake_evidence(_wake(), session=7.0, slept_total=123.5)
        ev = read_evidence()
        self.assertAlmostEqual(ev.slept_total, 123.5, places=3)

    def test_evidence_written_without_a_ledger_reads_back_as_unknown(self):
        write_wake_evidence(_wake(), session=7.0)
        self.assertIsNone(read_evidence().slept_total)

    def test_jitter_does_not_retire_a_latch(self):
        # the failure mode this tolerance prevents: the ledger wobbles, read_session_cause stops
        # matching, and a magic-packet wake gets re-slept two minutes later
        write_wake_evidence(_wake(), session=42.0, slept_total=100.0)
        write_session_cause("asked-for", 42.0, slept_total=100.0)
        self.assertEqual(read_session_cause(42.0, 100.0 + resume.LEDGER_EPSILON_S / 2), "asked-for")
        self.assertIsNone(read_session_cause(42.0, 100.0 + resume.LEDGER_EPSILON_S * 2))


class TestTheJudgeReCollectsWhenTheMachineSlept(_Isolated):
    """The regression itself, through the production judgement path."""

    def test_evidence_from_an_earlier_sleep_is_replaced(self):
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as d:
            # build_judgement only BUILDS the decision; the suspend lives in cmd_judge, so this
            # test exercises the production path without a machine state change.
            cfg = cli.Config(idle_minutes=2.0)
            args = mock.Mock(platform=None, config=str(Path(d) / "absent.json"))
            # evidence collected when the machine had slept 100 s in total
            write_wake_evidence(_wake(), session=42.0, slept_total=100.0)
            sysinfo = mock.Mock()
            sysinfo.wake_source.return_value = _wake()
            sysinfo.idle.return_value = mock.Mock(seconds=200.0, tick="t1")
            sysinfo.link_evidence.return_value = ("LINK_LOST", "wlan-event-at-resume:disconnected")
            sysinfo.guards.return_value = Guards()
            with mock.patch.object(resume, "suspend_total", return_value=117.0):
                cli.build_judgement(args, sysinfo=sysinfo, cfg=cfg)
            ev = read_evidence()
            self.assertAlmostEqual(ev.slept_total, 117.0, places=1,
                                   msg="the judge must re-collect evidence from the earlier sleep")
            self.assertNotEqual(ev.session, 42.0, "re-collecting must mint a new session stamp")


if __name__ == "__main__":
    unittest.main()
