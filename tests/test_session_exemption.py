"""Two wakes must never be re-slept - a listened-for packet, and hands on the machine.

Both are facts about an INSTANT near the resume, so both can only be established inside a window.
The first version stopped there, and the consequence was measured on the real box: a magic-packet
wake was re-slept about two minutes after the window expired (`pkt_window_s=180`), and a keyboard
wake was re-slept as soon as the idle clock passed the threshold. An operator reads that as "the
tool does nothing".

So each fact is established in a window and then LATCHED for the whole wake session, keyed by the
session stamp written at resume: the next resume makes the latch stop matching, with nobody having
to clear it. What must NOT change is the other half of the contract - a wake caused by the wifi
NIC's own doing (a link that moved while we slept) still re-sleeps.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# the repo root holds the entry script `autoresleep.py`, which shadows the package of the same
# name - every test file bootstraps `src` itself so a single file can be run on its own
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import (Action, Evidence, Guards, Params, decide,  # noqa: E402
                                packet_is_this_wakes_cause)
from autoresleep import cli, paths  # noqa: E402
from autoresleep.system import (read_evidence, read_session_cause,  # noqa: E402
                                write_session_cause, write_wake_evidence)
from autoresleep.system.base import WakeSource  # noqa: E402


class TestThePacketExemption(unittest.TestCase):
    NOW = 10000.0

    def test_a_packet_heard_at_this_resume_exempts_the_session(self):
        # packet and resume happened together: the packet IS the cause of this wake
        ev = Evidence(pkt_kind="magic", pkt_age_s=2.0, observed_at=self.NOW - 2.0,
                      session=self.NOW - 2.0)
        d = decide(ev, Guards(), idle_s=600.0, params=Params(), now=self.NOW)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")

    def test_the_latch_keeps_it_awake_after_the_packet_ages_out(self):
        # THE regression: no packet in the evidence any more (it aged out), but this session was
        # established as asked-for - so the judgement must not re-enter the sleep logic at all,
        # even with a dead link and hours of idle.
        ev = Evidence(session_cause="asked-for", session=self.NOW - 3600.0,
                      observed_at=self.NOW - 3600.0, os_link="LINK_LOST",
                      os_link_why="carrier-changed-2x-since-sleep")
        d = decide(ev, Guards(), idle_s=3600.0, params=Params(), now=self.NOW)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")
        self.assertEqual(d.who, "asked-for-this-session")

    def test_the_latch_outranks_the_debounce_too(self):
        # even a fresh forced sleep must not take an asked-for session back down
        ev = Evidence(session_cause="asked-for", observed_at=self.NOW - 3600.0)
        d = decide(ev, Guards(), idle_s=3600.0, params=Params(), now=self.NOW,
                   last_forced_sleep_at=self.NOW - 30.0)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")

    def test_a_packet_sent_to_an_already_awake_machine_is_not_the_wakes_cause(self):
        # same clock, wildly different ages: the packet arrived minutes ago into a session that has
        # been running for hours, so it is a packet, not the reason we are awake
        ev = Evidence(pkt_kind="magic", pkt_age_s=3.0, observed_at=self.NOW - 3600.0,
                      session=self.NOW - 3600.0, os_link="LINK_LOST")
        d = decide(ev, Guards(), idle_s=600.0, params=Params(), now=self.NOW,
                   input_since_sleep=False)
        self.assertIs(d.action, Action.SLEEP)

    def test_an_old_packet_without_a_session_stamp_still_uses_the_window(self):
        # a box whose collector never ran must not lose the plain rule
        ev = Evidence(pkt_kind="marker", pkt_age_s=10.0)
        d = decide(ev, Guards(), idle_s=600.0, params=Params(), now=self.NOW)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")


class TestPacketAttribution(unittest.TestCase):
    def test_no_packet_is_never_a_cause(self):
        self.assertFalse(packet_is_this_wakes_cause(None, 5.0, 180.0))

    def test_beyond_the_window_is_not_fresh(self):
        self.assertFalse(packet_is_this_wakes_cause(400.0, 400.0, 180.0))

    def test_a_missing_session_stamp_falls_back_to_the_plain_window(self):
        self.assertTrue(packet_is_this_wakes_cause(5.0, None, 180.0))

    def test_packet_and_resume_together_is_the_cause(self):
        self.assertTrue(packet_is_this_wakes_cause(5.0, 9.0, 180.0))

    def test_packet_hours_into_a_session_is_not_the_cause(self):
        self.assertFalse(packet_is_this_wakes_cause(3.0, 3600.0, 180.0))


class TestTheInputExemption(unittest.TestCase):
    NOW = 10000.0

    def test_a_human_wake_is_not_re_slept_when_the_human_walks_away(self):
        # the old contract slept here (idle >= idle_seconds) and that is exactly what the operator
        # reported as "the mouse woke it and three minutes later it slept again"
        d = decide(Evidence(), Guards(), idle_s=600.0,
                   params=Params(resleep_break_on_input=True), now=self.NOW,
                   wake_by_input=True)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-by-input")
        self.assertEqual(d.src, "input")

    def test_the_input_latch_holds_for_the_rest_of_the_session(self):
        d = decide(Evidence(session_cause="input", os_link="LINK_LOST"), Guards(),
                   idle_s=3600.0, params=Params(), now=self.NOW)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-by-input")
        self.assertEqual(d.who, "input-this-session")


class TestTheNicStillReSleeps(unittest.TestCase):
    """The other half of the contract, and the reason the latch must be keyed to a session."""

    NOW = 10000.0

    def test_a_moved_link_with_no_latch_still_sleeps(self):
        ev = Evidence(os_link="LINK_LOST", os_link_why="bssid-changed:a->b",
                      session=self.NOW - 600.0, observed_at=self.NOW - 600.0)
        d = decide(ev, Guards(), idle_s=600.0, params=Params(), now=self.NOW,
                   input_since_sleep=False)
        self.assertIs(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_MOVED")

    def test_a_latch_from_another_session_does_not_apply(self):
        # the latch is on disk; only the session it was written for may use it
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
                write_session_cause("asked-for", 1000.0)
                self.assertEqual(read_session_cause(1000.0), "asked-for")
                self.assertIsNone(read_session_cause(2000.0))
                self.assertIsNone(read_session_cause(None))


class TestTheLatchFile(unittest.TestCase):
    def _in_tmp(self):
        td = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td})
        patcher.start()
        self.addCleanup(patcher.stop)
        return td

    def test_a_written_latch_is_read_back_for_its_session(self):
        self._in_tmp()
        write_session_cause("input", 1234.5)
        self.assertEqual(read_session_cause(1234.5), "input")

    def test_an_unknown_cause_is_refused_rather_than_guessed(self):
        self._in_tmp()
        paths.ensure_data_dir()
        paths.session_cause_file().write_text(
            json.dumps({"cause": "because-i-said-so", "session": 7.0}), encoding="utf-8")
        self.assertIsNone(read_session_cause(7.0))

    def test_a_corrupt_latch_reads_as_no_latch(self):
        self._in_tmp()
        paths.ensure_data_dir()
        paths.session_cause_file().write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_session_cause(7.0))
        self.assertIsNone(read_session_cause(None))

    def test_no_session_means_nothing_is_written(self):
        self._in_tmp()
        write_session_cause("input", None)
        self.assertFalse(paths.session_cause_file().exists())

    def test_the_latch_lives_in_the_data_dir(self):
        td = self._in_tmp()
        self.assertTrue(str(paths.session_cause_file()).startswith(td))


class TestTheSessionStamp(unittest.TestCase):
    """The stamp is what makes the latch self-invalidating, so it must survive a re-write."""

    def _in_tmp(self):
        td = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td})
        patcher.start()
        self.addCleanup(patcher.stop)
        return td

    @staticmethod
    def _source():
        return WakeSource(kind="unknown", method="test", wake_time="2026-09-29 09:20:00")

    def test_a_first_write_invents_a_session_stamp(self):
        self._in_tmp()
        before = time.time()
        write_wake_evidence(self._source())
        ev = read_evidence()
        self.assertIsNotNone(ev.session)
        self.assertGreaterEqual(float(ev.session or 0), before)
        self.assertEqual(ev.session, ev.observed_at)

    def test_a_link_probe_rewrite_keeps_the_same_stamp(self):
        # the judge re-writes the evidence when the collector could not probe the link. A fresh
        # stamp there would look like a new wake and silently drop the exemption.
        self._in_tmp()
        write_wake_evidence(self._source(), link="UNKNOWN", link_why="no-probe")
        first = read_evidence()
        time.sleep(0.01)
        write_wake_evidence(self._source(), link="LINK_LOST", link_why="carrier-changed-1x",
                            session=first.session, observed_at=first.observed_at)
        second = read_evidence()
        self.assertEqual(second.session, first.session)
        self.assertEqual(second.observed_at, first.observed_at)
        self.assertEqual(second.os_link, "LINK_LOST")

    def test_an_evidence_file_without_a_stamp_falls_back_to_its_timestamp(self):
        self._in_tmp()
        paths.ensure_data_dir()
        # a file from before the stamp existed, and still fresh enough to be evidence at all
        stamp = time.time() - 5.0
        (paths.data_dir() / "evidence-last.json").write_text(
            json.dumps({"observed_at": stamp, "chain": "os", "kind": "unknown"}),
            encoding="utf-8")
        self.assertEqual(read_evidence().session, stamp)


class TestRecordingAPacketLatchesImmediately(unittest.TestCase):
    """The moment the packet is heard, not the next judgement - which may be a window later."""

    def test_a_heard_packet_latches_the_current_session(self):
        td = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
            write_wake_evidence(TestTheSessionStamp._source())
            session = read_evidence().session
            seen = type("Seen", (), {"kind": "magic", "epoch": time.time(),
                                     "src": "192.168.0.10:9", "length": 102,
                                     "line": lambda self: "magic|epoch=%d|src=x|len=102"
                                     % int(self.epoch)})()
            cli._record_packet(seen, mock.Mock())
            self.assertEqual(read_session_cause(session), "asked-for")
            self.assertTrue(paths.packet_log().read_text(encoding="utf-8").strip())

    def test_no_evidence_means_no_latch_to_write(self):
        td = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
            paths.ensure_data_dir()
            seen = type("Seen", (), {"kind": "marker", "epoch": time.time(),
                                     "src": "127.0.0.1:5", "length": 41,
                                     "line": lambda self: "marker|epoch=%d|src=x|len=41"
                                     % int(self.epoch)})()
            cli._record_packet(seen, mock.Mock())
            self.assertFalse(paths.session_cause_file().exists())

    def test_a_driver_reported_magic_packet_latches_exactly_like_a_heard_one(self):
        """`iw event` says the firmware matched a magic packet - the observation the UDP listener
        could never make, because the firmware consumes the waking packet."""
        td = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
            write_wake_evidence(TestTheSessionStamp._source())
            session = read_evidence().session
            cli._record_wowlan({"kind": "magic", "epoch": time.time(),
                                "detail": "wlan0 (phy #0): WoWLAN wakeup | * magic packet "
                                          "received"}, mock.Mock())
            self.assertEqual(read_session_cause(session), "asked-for")
            self.assertTrue(paths.packet_state().read_text(encoding="utf-8")
                            .startswith("magic|"), "the engine reads this file, not the report")

    def test_a_wake_the_nic_denies_exempts_nothing(self):
        """`wakeup not due to WoWLAN` is the opposite claim: recorded, and nothing else happens."""
        td = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
            write_wake_evidence(TestTheSessionStamp._source())
            session = read_evidence().session
            log = mock.Mock()
            cli._record_wowlan({"kind": "not-wowlan", "epoch": time.time(),
                                "detail": "wlan0 (phy #0): WoWLAN wakeup | wakeup not due to "
                                          "WoWLAN"}, log)
            self.assertIsNone(read_session_cause(session))
            self.assertFalse(paths.packet_state().exists())
            self.assertFalse(paths.packet_log().exists())
            self.assertTrue(log.info.called, "it must still be visible in the journal")

    def test_an_unclassified_report_exempts_nothing(self):
        td = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
            write_wake_evidence(TestTheSessionStamp._source())
            session = read_evidence().session
            cli._record_wowlan({"kind": "other:disconnect", "epoch": time.time(),
                                "detail": "* disconnect detected"}, mock.Mock())
            self.assertIsNone(read_session_cause(session))
            self.assertFalse(paths.packet_state().exists())

    def test_the_driver_report_reaches_the_engine_through_the_packet_file(self):
        """End to end, minus the CLI plumbing: report -> files -> Evidence -> layer 1 -> no re-sleep.

        This is the whole point of filing a driver report as a packet: the engine keeps its ordering
        and needs no rule for "the firmware said so".
        """
        td = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
            write_wake_evidence(TestTheSessionStamp._source())
            cli._record_wowlan({"kind": "magic", "epoch": time.time(),
                                "detail": "wlan0 (phy #0): WoWLAN wakeup | * magic packet "
                                          "received"}, mock.Mock())
            ev = read_evidence()
            self.assertEqual(ev.pkt_kind, "magic")
            self.assertLess(ev.pkt_age_s, 5.0)
            d = decide(ev, Guards(), idle_s=600.0, params=Params(),
                       now=time.time(), wake_kind=ev.wake_kind)
            self.assertIs(d.action, Action.SKIP)
            self.assertEqual(d.reason, "wake-requested")


if __name__ == "__main__":
    unittest.main()