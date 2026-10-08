"""The window's input fact: judged by a DELTA, never by an absolute idle reading.

A resume resets the desktop idle counter, so `idle=0s` right after a wake means nothing - that is
the bug that made every wifi wake look like a keypress. What a resume cannot fake is a CHANGE in
the platform's input clock between two samples taken inside the window.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import windowwatch  # noqa: E402
from autoresleep.windowwatch import WindowState, observe, read_state, sample, write_state  # noqa: E402

WAKE = 1000.0
WINDOW = 10.0
UNTIL = WAKE + WINDOW
SRC = "windows-window-tick"


class TestTheDeltaRule(unittest.TestCase):
    def test_the_first_sample_says_nobody_yet(self):
        st = observe(None, WAKE, UNTIL, tick="100", now=WAKE + 1, source=SRC)
        self.assertIs(st.input, False)
        self.assertEqual(st.samples, 1)
        self.assertEqual(st.first_tick, "100")

    def test_a_changed_tick_inside_the_window_is_a_person(self):
        st = observe(None, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
        st = observe(st, WAKE, UNTIL, "220", now=WAKE + 4, source=SRC)
        self.assertIs(st.input, True)
        self.assertEqual(st.samples, 2)

    def test_a_steady_tick_stays_nobody(self):
        st = observe(None, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
        for i in range(2, 10):
            st = observe(st, WAKE, UNTIL, "100", now=WAKE + i, source=SRC)
        self.assertIs(st.input, False)
        self.assertEqual(st.samples, 9)

    def test_input_after_the_window_is_not_this_windows_input(self):
        """The regression that would otherwise cancel a sleep hours later."""
        st = observe(None, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
        st = observe(st, WAKE, UNTIL, "999", now=WAKE + 400, source=SRC)
        self.assertIs(st.input, False, "typing five minutes later says nothing about the window")
        self.assertTrue(st.ended)

    def test_a_new_wake_starts_over(self):
        st = observe(None, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
        st = observe(st, WAKE, UNTIL, "220", now=WAKE + 4, source=SRC)
        self.assertIs(st.input, True)
        fresh = observe(st, WAKE + 3600, WAKE + 3610, "300", now=WAKE + 3601, source=SRC)
        self.assertIs(fresh.input, False, "a new session never inherits the last one's verdict")
        self.assertEqual(fresh.anchor, WAKE + 3600)

    def test_a_missing_tick_is_tolerated(self):
        st = observe(None, WAKE, UNTIL, "", now=WAKE + 1, source=SRC)
        st = observe(st, WAKE, UNTIL, "", now=WAKE + 2, source=SRC)
        self.assertIs(st.input, False)


class TestTheStateFile(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            write_state(p, WindowState(anchor=WAKE, until=UNTIL, input=True, source=SRC,
                                       samples=3, updated_at=WAKE + 2))
            back = read_state(p)
        self.assertEqual(back.anchor, WAKE)
        self.assertIs(back.input, True)
        self.assertEqual(back.samples, 3)

    def test_a_missing_or_corrupt_file_reads_as_no_state(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            self.assertIsNone(read_state(p))
            p.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_state(p))
            p.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertIsNone(read_state(p))

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            p.write_text('{"anchor": 5, "until": 15, "input": false, "from_the_future": 1}',
                         encoding="utf-8")
            self.assertEqual(read_state(p).anchor, 5)

    def test_sample_accumulates_across_calls(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            sample(p, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
            sample(p, WAKE, UNTIL, "100", now=WAKE + 2, source=SRC)
            st = sample(p, WAKE, UNTIL, "140", now=WAKE + 3, source=SRC)
            self.assertEqual(st.samples, 3)
            self.assertIs(st.input, True)

    def test_a_different_anchor_makes_a_fresh_window(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            sample(p, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
            st = sample(p, WAKE + 60, UNTIL + 60, "100", now=WAKE + 61, source=SRC)
            self.assertEqual(st.anchor, WAKE + 60)
            self.assertEqual(st.samples, 1)

    def test_describe_names_both_answers(self):
        self.assertIn("not sampled", WindowState().describe())
        self.assertIn("input-in-window", WindowState(input=True, source=SRC).describe())
        self.assertIn("no-input-in-window", WindowState(input=False, source=SRC).describe())


class TestTheCliGlue(unittest.TestCase):
    def test_only_hardware_grade_clocks_may_latch_a_session(self):
        from autoresleep.cli import _may_latch_input
        self.assertTrue(_may_latch_input("evdev"))
        self.assertTrue(_may_latch_input("windows-window-tick"))
        self.assertTrue(_may_latch_input("macos-window-tick"))
        # the wake-moment fallback is reset by a resume: it may skip once, never latch
        self.assertFalse(_may_latch_input("x11-idle"))
        self.assertFalse(_may_latch_input(None))

    def _evidence(self, age_s):
        import time as _t
        from autoresleep.decide import Evidence
        anchor = _t.time() - age_s          # anchors are real epochs; the window is measured in them
        return Evidence(observed_at=anchor, session=anchor)

    def test_a_box_with_a_watcher_answers_from_the_hardware_clock(self):
        import time
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "input-last.json"
            # an event 2 s ago, i.e. inside the window of a wake 5 s ago
            last.write_text('{"beat": %s, "epoch": %s, "dev": "kbd"}'
                            % (time.time(), time.time() - 2), encoding="utf-8")
            with mock.patch.object(cli.paths, "input_last_file", return_value=last), \
                    mock.patch.object(cli.paths, "window_input_file",
                                      return_value=Path(td) / "window-input.json"):
                cfg = cli.Config(resleep_delay_seconds=10.0)
                got, source = cli._window_input(self._evidence(5.0), mock.Mock(), cfg)
        self.assertIs(got, True)
        self.assertEqual(source, "evdev")

    def test_an_event_before_the_wake_is_not_input_in_the_window(self):
        import time
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "input-last.json"
            last.write_text('{"beat": %s, "epoch": %s, "dev": "kbd"}'
                            % (time.time(), time.time() - 3900), encoding="utf-8")
            with mock.patch.object(cli.paths, "input_last_file", return_value=last), \
                    mock.patch.object(cli.paths, "window_input_file",
                                      return_value=Path(td) / "window-input.json"):
                cfg = cli.Config(resleep_delay_seconds=10.0)
                got, _src = cli._window_input(self._evidence(5.0), mock.Mock(), cfg)
        self.assertIs(got, False)

    def test_without_a_watcher_the_platform_clock_is_sampled_by_delta(self):
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "window-input.json"
            sysinfo = mock.Mock()
            sysinfo.name = "windows"
            sysinfo.idle.return_value = mock.Mock(tick="100", seconds=1.0)
            ev = self._evidence(1.0)        # ONE wake: the anchor must not move between samples
            with mock.patch.object(cli.paths, "input_last_file",
                                   return_value=Path(td) / "missing.json"), \
                    mock.patch.object(cli.paths, "window_input_file", return_value=state_path):
                cfg = cli.Config(resleep_delay_seconds=10.0)
                first, source = cli._window_input(ev, sysinfo, cfg, sample=True)
                self.assertIs(first, False)
                sysinfo.idle.return_value = mock.Mock(tick="250", seconds=0.2)
                second, _s = cli._window_input(ev, sysinfo, cfg, sample=True)
        self.assertEqual(source, "windows-window-tick")
        self.assertIs(second, True, "a changed tick inside the window is a person")

    def test_nothing_sampled_means_unknown_not_nobody(self):
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            sysinfo = mock.Mock()
            sysinfo.name = "windows"
            sysinfo.idle.return_value = mock.Mock(tick="100", seconds=1.0)
            with mock.patch.object(cli.paths, "input_last_file",
                                   return_value=Path(td) / "missing.json"), \
                    mock.patch.object(cli.paths, "window_input_file",
                                      return_value=Path(td) / "window-input.json"):
                cfg = cli.Config(resleep_delay_seconds=10.0)
                got, _src = cli._window_input(self._evidence(3.0), sysinfo, cfg, sample=False)
        self.assertIsNone(got, "no sample must never read as 'nobody touched it'")


if __name__ == "__main__":
    unittest.main()