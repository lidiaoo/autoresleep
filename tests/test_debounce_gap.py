"""Slice 9: the two-gap debounce, and why the machine never stayed asleep.

Measured on the real box: the judgement itself was instant (0-30 s) but two consecutive cycles
took 4.9 and 5.1 minutes - exactly `min_gap_minutes`. The log showed `debounce(278s<300s)` and
`link=LINK_LOST` at the moment of the wake: the Wi-Fi NIC kept poking the machine back awake
1 s to 1.4 minutes after every suspend, and the anti-flap gap kept it awake for five minutes in
between. The gap is there to avoid fighting a HUMAN, so it must not apply to an empty machine.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import Action, Evidence, Guards, Params, decide  # noqa: E402

WIFI = dict(wake_kind="wifi-nic", wake_who="name:Killer(R) Wi-Fi 6 AX1650i")


class TestDebounceGap(unittest.TestCase):
    def test_an_unattended_nic_wake_uses_the_short_gap(self):
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=Params(),
                   now=1000.0, last_forced_sleep_at=1000.0 - 60,      # slept 1 min ago
                   input_since_sleep=False)
        self.assertEqual(d.action, Action.SLEEP)

    def test_the_same_case_with_input_since_the_sleep_keeps_the_long_gap(self):
        # someone touched the machine since we slept it: still sleeps it (they left), but with
        # the conservative gap, so a sticky flag cannot deadlock the tool
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=Params(),
                   now=1000.0, last_forced_sleep_at=1000.0 - 60,
                   input_since_sleep=True)
        self.assertEqual(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("debounce"), d.reason)
        self.assertIn("300s", d.reason)

    def test_an_unknown_input_state_keeps_the_long_gap(self):
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=Params(),
                   now=1000.0, last_forced_sleep_at=1000.0 - 60,
                   input_since_sleep=None)                            # cannot tell -> be careful
        self.assertEqual(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("debounce"), d.reason)
        self.assertIn("300s", d.reason)

    def test_a_non_nic_wake_keeps_the_long_gap_even_when_unattended(self):
        # a timer or an unnamed device: not our poking-NIC case, so do not get aggressive
        d = decide(Evidence(wake_kind="timer"), Guards(), idle_s=300, params=Params(),
                   now=1000.0, last_forced_sleep_at=1000.0 - 60, input_since_sleep=False)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-timer")

    def test_a_driver_attributed_wake_also_gets_the_short_gap(self):
        ev = Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0)
        d = decide(ev, Guards(), idle_s=300, params=Params(), now=1000.0,
                   last_forced_sleep_at=1000.0 - 60, input_since_sleep=False)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_AP_ASSOC_LOST")

    def test_both_gaps_are_configurable(self):
        p = Params(min_gap_unattended_minutes=0.5)
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=p, now=1000.0,
                   last_forced_sleep_at=1000.0 - 40, input_since_sleep=False)
        self.assertEqual(d.action, Action.SLEEP)


class TestForcedSleepMarker(unittest.TestCase):
    """The marker carries the input tick, which is what makes 'nobody touched it' a fact."""

    def test_the_marker_round_trips_epoch_and_tick(self):
        from autoresleep import paths
        from autoresleep.cli import _last_forced_sleep_info, _mark_forced_sleep
        saved = None
        if paths.forced_sleep_marker().exists():
            saved = paths.forced_sleep_marker().read_text(encoding="utf-8")
        try:
            _mark_forced_sleep(when=1234.5, tick="987654")
            epoch, tick = _last_forced_sleep_info()
            self.assertEqual(epoch, 1234.5)
            self.assertEqual(tick, "987654")
        finally:
            if saved is None:
                paths.forced_sleep_marker().unlink(missing_ok=True)
            else:
                paths.forced_sleep_marker().write_text(saved, encoding="utf-8")

    def test_a_marker_without_a_tick_is_tolerated(self):
        from autoresleep import paths
        from autoresleep.cli import _last_forced_sleep_info
        saved = None
        if paths.forced_sleep_marker().exists():
            saved = paths.forced_sleep_marker().read_text(encoding="utf-8")
        try:
            paths.ensure_data_dir()
            paths.forced_sleep_marker().write_text("999.0", encoding="utf-8")
            epoch, tick = _last_forced_sleep_info()
            self.assertEqual(epoch, 999.0)
            self.assertEqual(tick, "")
        finally:
            if saved is None:
                paths.forced_sleep_marker().unlink(missing_ok=True)
            else:
                paths.forced_sleep_marker().write_text(saved, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()