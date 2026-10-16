"""Slice 9: the two-gap debounce, and why the machine never stayed asleep.

Measured on the real box: the judgement itself was instant (0-30 s) but two consecutive cycles
took 4.9 and 5.1 minutes - exactly the old `min_gap` gap. The log showed `debounce(278s<300s)` and
`link=LINK_LOST` at the moment of the wake: the Wi-Fi NIC kept poking the machine back awake
1 s to 1.4 minutes after every suspend, and the anti-flap gap kept it awake for five minutes in
between. The gap is there to avoid fighting a HUMAN, so it must not apply to an empty machine.
"""
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
        p = Params(min_gap_seconds=300.0)   # the conservative gap, explicit: the default is now 30
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=p,
                   now=1000.0, last_forced_sleep_at=1000.0 - 60,
                   input_since_sleep=True)
        self.assertEqual(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("debounce"), d.reason)
        self.assertIn("300s", d.reason)

    def test_an_unknown_input_state_keeps_the_long_gap(self):
        p = Params(min_gap_seconds=300.0)
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=p,
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

    def test_a_portable_link_wake_also_gets_the_short_gap(self):
        # the NIC-caused case, established driver-free (nl80211 events) rather than by a driver
        # string: it still gets the 1-minute unattended gap instead of the conservative 5
        ev = Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected")
        d = decide(ev, Guards(), idle_s=300, params=Params(), now=1000.0,
                   last_forced_sleep_at=1000.0 - 60, input_since_sleep=False)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_MOVED")

    def test_both_gaps_are_configurable(self):
        p = Params(min_gap_unattended_seconds=30.0)
        d = decide(Evidence(**WIFI), Guards(), idle_s=300, params=p, now=1000.0,
                   last_forced_sleep_at=1000.0 - 40, input_since_sleep=False)
        self.assertEqual(d.action, Action.SLEEP)


class TestTheMarkerIsWrittenBeforeTheSuspend(unittest.TestCase):
    """`suspend()` does not come back at the moment of sleeping - on Windows it comes back at the
    WAKE, and the marker was being written after it.

    Measured on the real box: the sleep issued at 21:25:07 was stamped 21:34:22, the wake-up
    moment. Two consequences, both real: `last_forced_sleep_at` read "we slept 0 s ago" (so the
    debounce baseline was gone), and `decisions.log` grew a second `SLEEP ... (listener fast path)`
    line AT the wake, which reads as "the tool tried to sleep the machine the instant it returned".
    The tick has the same problem: `input_since_sleep` compares the CURRENT tick against it, and
    after a resume that clock has already been moved by the resume's own HID replay.
    """

    def _sysinfo(self, on_suspend, tick="987654"):
        return SimpleNamespace(suspend=on_suspend,
                               idle=lambda: SimpleNamespace(seconds=300.0, tick=tick))

    def _patched_marker(self, td):
        """Both the marker path AND the data-dir creation are redirected.

        `_mark_forced_sleep` calls `paths.ensure_data_dir()` first, which resolves the home
        directory - and another test in this suite fakes HOME/USERPROFILE, so under full discovery
        that raises. This test is about the ORDER of two writes, not about creating directories.
        """
        from autoresleep import cli
        stack = ExitStack()
        stack.enter_context(mock.patch.object(cli.paths, "ensure_data_dir"))
        stack.enter_context(mock.patch.object(cli.paths, "forced_sleep_marker",
                                             return_value=Path(td) / "last-forced-sleep.txt"))
        return stack

    def test_the_marker_is_there_while_the_machine_is_still_going_down(self):
        import time as _t
        from autoresleep.cli import _last_forced_sleep_info, _suspend_with_marker
        seen = {}

        def on_suspend():
            seen["during"] = _last_forced_sleep_info()
            _t.sleep(0.15)
            seen["returned_at"] = _t.time()
            return "SetSuspendState(for the test)"

        with tempfile.TemporaryDirectory() as td, self._patched_marker(td):
            self.assertEqual(_suspend_with_marker(self._sysinfo(on_suspend)),
                             "SetSuspendState(for the test)")
        stamped, tick = seen["during"]
        self.assertIsNotNone(stamped, "the marker must exist BEFORE suspend() blocks")
        self.assertEqual(tick, "987654", "the tick must be the pre-sleep one")
        self.assertLess(stamped, seen["returned_at"] - 0.1,
                        "a marker written after suspend() returns carries the WAKE-up moment")

    def test_a_failed_suspend_takes_the_marker_back(self):
        from autoresleep import cli
        from autoresleep.cli import _suspend_with_marker

        def boom():
            raise RuntimeError("access denied")

        with tempfile.TemporaryDirectory() as td, self._patched_marker(td):
            marker = Path(td) / "last-forced-sleep.txt"
            with self.assertRaises(RuntimeError):
                _suspend_with_marker(self._sysinfo(boom, tick="1"))
        self.assertFalse(marker.exists(),
                         "a marker for a sleep that never happened suppresses the next re-sleep")

    def test_a_failed_suspend_puts_the_previous_marker_back(self):
        from autoresleep.cli import _suspend_with_marker

        def boom():
            raise RuntimeError("access denied")

        with tempfile.TemporaryDirectory() as td, self._patched_marker(td):
            marker = Path(td) / "last-forced-sleep.txt"
            marker.write_text("111.0 222", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                _suspend_with_marker(self._sysinfo(boom, tick="1"))
            self.assertEqual(marker.read_text(encoding="utf-8"), "111.0 222")

    def test_an_unreadable_clock_does_not_lose_the_suspend(self):
        """A machine whose idle clock cannot be read must still be slept: the marker then carries no
        tick, which degrades `input_since_sleep` to "cannot tell" (the conservative gap) instead of
        turning a suspend into an exception."""
        from autoresleep import cli
        from autoresleep.cli import _last_forced_sleep_info, _suspend_with_marker

        def no_clock():
            raise RuntimeError("no interactive session")

        sysinfo = SimpleNamespace(suspend=lambda: "SetSuspendState(for the test)", idle=no_clock)
        with tempfile.TemporaryDirectory() as td, self._patched_marker(td):
            self.assertEqual(_suspend_with_marker(sysinfo), "SetSuspendState(for the test)")
            _epoch, tick = _last_forced_sleep_info()
        self.assertEqual(tick, "")


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