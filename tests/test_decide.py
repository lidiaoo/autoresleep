"""Slice 1 of the v2 decision engine tests: the two rules that must never regress.

v1 lessons encoded here:
  * a wake that was ASKED FOR (magic packet / sender marker) never goes back to sleep;
  * anything holding a remote or display session outranks every evidence chain.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import Action, Evidence, Guards, Params, decide  # noqa: E402


class TestAskedForWakes(unittest.TestCase):
    def test_magic_packet_wake_never_resleeps(self):
        d = decide(Evidence(pkt_kind="magic", pkt_age_s=2.0), Guards(), idle_s=3600, params=Params())
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")
        self.assertEqual(d.src, "pkt")

    def test_sender_marker_never_resleeps(self):
        d = decide(Evidence(pkt_kind="marker", pkt_age_s=30.0), Guards(), idle_s=3600, params=Params())
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")

    def test_stale_packet_outside_the_window_does_not_count(self):
        # a packet seen 20 minutes ago belongs to a different wake cycle
        d = decide(Evidence(pkt_kind="magic", pkt_age_s=1200.0), Guards(), idle_s=3600,
                   params=Params(pkt_window_s=180.0))
        self.assertNotEqual(d.reason, "wake-requested")


class TestGuardsFirst(unittest.TestCase):
    def test_remote_session_blocks_resleep(self):
        d = decide(Evidence(pkt_kind=None), Guards(remote_in_use=True), idle_s=3600, params=Params())
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "guarded:remote-in-use")

    def test_display_in_use_blocks_resleep(self):
        d = decide(Evidence(), Guards(display_in_use=True), idle_s=3600, params=Params())
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "guarded:display-in-use")

    def test_guard_does_not_override_an_asked_for_wake(self):
        # both say skip, but the reason must attribute it to the packet: the packet is the
        # stronger claim and the log line is what a human reads the next morning
        d = decide(Evidence(pkt_kind="magic", pkt_age_s=1.0), Guards(remote_in_use=True),
                   idle_s=3600, params=Params())
        self.assertEqual(d.reason, "wake-requested")


if __name__ == "__main__":
    unittest.main()