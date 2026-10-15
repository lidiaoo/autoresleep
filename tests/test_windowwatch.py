"""The window's input fact: judged by a DELTA, never by an absolute idle reading.

A resume resets the desktop idle counter, so `idle=0s` right after a wake means nothing - that is
the bug that made every wifi wake look like a keypress. What a resume cannot fake is a CHANGE in
the platform's input clock between two samples taken inside the window.
"""
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli  # noqa: E402
from autoresleep import windowwatch  # noqa: E402
from autoresleep.config import Config  # noqa: E402
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
                got, source, _settled = cli._window_input(self._evidence(5.0), mock.Mock(), cfg)
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
                got, _src, _settled = cli._window_input(self._evidence(5.0), mock.Mock(), cfg)
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
                # settle off on purpose: this case is about the DELTA rule itself. With the shipped
                # default the change below lands inside the settle window - see
                # TestTheResumeSettleWindow for that half.
                cfg = cli.Config(resleep_delay_seconds=10.0, input_settle_seconds=0.0)
                first, source, _settled = cli._window_input(ev, sysinfo, cfg, sample=True)
                self.assertIs(first, False)
                sysinfo.idle.return_value = mock.Mock(tick="250", seconds=0.2)
                second, _s, _n = cli._window_input(ev, sysinfo, cfg, sample=True)
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
                got, _src, _settled = cli._window_input(self._evidence(3.0), sysinfo, cfg,
                                                        sample=False)
        self.assertIsNone(got, "no sample must never read as 'nobody touched it'")


if __name__ == "__main__":
    unittest.main()

class TestTheWindowClosingTriggersTheJudgement(unittest.TestCase):
    """Window closed -> judge on the FIRST second after it, and each window exactly once.

    Two things are pinned here. (1) Waiting for the next `fast_path_seconds` tick added up to 30 s to
    every NIC wake's re-sleep decision (window 10 s + up to 30 s); the window closing is the event
    the whole feature waits for, so it fires immediately. (2) The window state stays on disk and
    `age >= delay` stays true for ever, so without remembering WHICH window was judged the listener
    would judge every single second (0.12 s of work per second, for nothing).
    """

    @staticmethod
    def _evidence(observed_at):
        return SimpleNamespace(observed_at=observed_at)

    def _fire(self, cfg, now, fast_deadline, judged_window, observed_at, idle=0.0):
        """`_fast_path_fire` with the clock and the evidence frozen, so the test is about the rule
        and not about how long the test itself took to run."""
        with mock.patch.object(cli, "read_evidence", return_value=self._evidence(observed_at)):
            with mock.patch.object(cli.time, "time", return_value=now):
                return cli._fast_path_fire(cfg, lambda: idle, now, fast_deadline, judged_window)

    def test_the_first_second_after_the_window_fires_even_when_the_rate_limit_says_no(self):
        cfg = Config(resleep_delay_seconds=10, fast_path_seconds=30)
        now = 1000.0
        fire, deadline, judged = self._fire(cfg, now, now, 0.0, now - 11)
        self.assertTrue(fire, "the window closing must not wait for the next 30 s tick")
        self.assertEqual(judged, now - 11, "the window is remembered by its stamp")
        self.assertEqual(deadline, now)

    def test_the_same_window_is_not_judged_again_every_second(self):
        cfg = Config(resleep_delay_seconds=10, fast_path_seconds=30)
        now, anchor = 1000.0, 989.0
        fire, _deadline, judged = self._fire(cfg, now, now, anchor, anchor)
        self.assertFalse(fire)
        self.assertEqual(judged, anchor)

    def test_a_new_wake_judges_again(self):
        cfg = Config(resleep_delay_seconds=10, fast_path_seconds=30)
        fire, _deadline, judged = self._fire(cfg, 2000.0, 1900.0, 989.0, 1989.0)
        self.assertTrue(fire)
        self.assertEqual(judged, 1989.0)

    def test_inside_the_window_nothing_fires(self):
        cfg = Config(resleep_delay_seconds=10, fast_path_seconds=30)
        fire, _deadline, _judged = self._fire(cfg, 1000.0, 999.0, 0.0, 997.0)
        self.assertFalse(fire, "the window IS the wait: nothing fires before it closes")

    def test_without_a_window_the_idle_threshold_still_rate_limits(self):
        cfg = Config(idle_minutes=2, fast_path_seconds=30)
        self.assertFalse(self._fire(cfg, 1000.0, 990.0, 0.0, None, idle=200.0)[0])
        self.assertTrue(self._fire(cfg, 1000.0, 960.0, 0.0, None, idle=200.0)[0])
        self.assertFalse(self._fire(cfg, 1000.0, 0.0, 0.0, None, idle=10.0)[0],
                         "no input yet: nothing to decide")


class TestTheResumeSettleWindow(unittest.TestCase):
    """What a resume does to the platform input clock is not a person.

    Measured on the real box (2026-10-15, a 9-minute Wi-Fi wake with nobody at the machine): the
    window's first sample still read the PRE-SLEEP tick - `idle=603 s`, proof that no hand had
    touched it - and the tick then moved 604.6 s at anchor+1.6 s. That single change latched
    `session-cause=input` for the whole session (`who=input-this-session`) and the machine never
    re-slept again. It was the HID replay a resume triggers, which Linux has filtered with a settle
    window all along (`inputwatch.MOTION_SETTLE_S`); Windows/macOS have no device names to filter
    on, so `settle_s` is applied to the only signal they have - the clock's delta.
    """

    SETTLE = 5.0

    def _seed(self, path, anchor, tick="129287953"):
        return sample(path, anchor, anchor + 10.0, tick, now=anchor + 0.5,
                      source=SRC, settle_s=self.SETTLE)

    def test_a_change_inside_the_settle_window_is_not_a_person(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            self._seed(p, WAKE)
            st = sample(p, WAKE, UNTIL, "129892562", now=WAKE + 1.6, source=SRC,
                        settle_s=self.SETTLE)
        self.assertIs(st.input, False, "the resume pushed the clock, not a hand")
        self.assertEqual(st.settled, 1)
        self.assertEqual(st.samples, 2)

    def test_a_change_after_the_settle_window_is_still_a_person(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            self._seed(p, WAKE)
            st = sample(p, WAKE, UNTIL, "129892562", now=WAKE + 6.0, source=SRC,
                        settle_s=self.SETTLE)
        self.assertIs(st.input, True, "a hand that keeps moving past the window is counted")
        self.assertEqual(st.settled, 0)

    def test_an_absorbed_replay_cannot_come_back_as_input(self):
        """The new tick is folded in even when the change is ignored - otherwise the settle window
        would only postpone the same false positive to the second sample after it."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            self._seed(p, WAKE)
            sample(p, WAKE, UNTIL, "129892562", now=WAKE + 1.6, source=SRC, settle_s=self.SETTLE)
            st = sample(p, WAKE, UNTIL, "129892562", now=WAKE + 8.0, source=SRC,
                        settle_s=self.SETTLE)
        self.assertIs(st.input, False)
        self.assertEqual(st.settled, 1)

    def test_the_deadline_is_kept_in_the_file_so_both_readers_agree(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "window-input.json"
            self._seed(p, WAKE)
            back = read_state(p)
        self.assertEqual(back.settle_until, WAKE + self.SETTLE)

    def test_a_state_from_before_this_rule_derives_the_deadline_from_the_anchor(self):
        """A file written by the previous version has no `settle_until`; reading 0.0 as 'no settle'
        would make the listener and the judge disagree about the very window that straddles the
        upgrade - and two clocks that disagree at the wake is this module's whole failure mode."""
        st = WindowState(anchor=WAKE, until=UNTIL, input=False, first_tick="100", last_tick="100")
        st = observe(st, WAKE, UNTIL, "700", now=WAKE + 1.0, source=SRC, settle_s=self.SETTLE)
        self.assertIs(st.input, False)
        self.assertEqual(st.settled, 1)
        self.assertEqual(st.settle_until, WAKE + self.SETTLE)

    def test_without_a_settle_the_old_rule_stands(self):
        st = observe(None, WAKE, UNTIL, "100", now=WAKE + 1, source=SRC)
        st = observe(st, WAKE, UNTIL, "220", now=WAKE + 4, source=SRC)
        self.assertIs(st.input, True, "settle_s=0 is the pre-fix behaviour (and Linux's answer)")
        self.assertEqual(st.settled, 0)

    def test_describe_names_the_settled_changes(self):
        self.assertIn("1 change(s) ignored as resume settle",
                      WindowState(input=False, source=SRC, settled=1).describe())
        self.assertNotIn("settle", WindowState(input=False, source=SRC).describe(),
                         "a window that absorbed nothing must not mention settling")


class TestTheSettleWindowInTheCliGlue(unittest.TestCase):
    """The judgement must SEE which changes were absorbed - it is the answer to "why did it not
    re-sleep", and without it 'nobody was there' and 'we called it the bus' look identical."""

    SETTLE = 5.0

    def _evidence(self, age_s):
        import time as _t
        from autoresleep.decide import Evidence
        anchor = _t.time() - age_s
        return Evidence(observed_at=anchor, session=anchor)

    def _seed(self, state_path, anchor, tick="100"):
        from autoresleep import windowwatch as _ww
        _ww.sample(state_path, anchor, anchor + 10.0, tick, now=anchor + 0.5,
                   source="windows-window-tick", settle_s=self.SETTLE)

    def _probe(self, age_s, tick):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "window-input.json"
            sysinfo = mock.Mock()
            sysinfo.name = "windows"
            sysinfo.idle.return_value = mock.Mock(tick=tick, seconds=0.4)
            ev = self._evidence(age_s)
            self._seed(state_path, ev.observed_at)
            with mock.patch.object(cli.paths, "input_last_file",
                                   return_value=Path(td) / "missing.json"), \
                    mock.patch.object(cli.paths, "window_input_file", return_value=state_path):
                cfg = cli.Config(resleep_delay_seconds=10.0, input_settle_seconds=self.SETTLE)
                return cli._window_input(ev, sysinfo, cfg, sample=True)

    def test_a_replay_1_6_s_after_the_wake_is_reported_as_settled_not_as_input(self):
        got, source, settled = self._probe(1.0, "250")
        self.assertEqual(source, "windows-window-tick")
        self.assertIs(got, False, "the exact change that used to lock the session for the night")
        self.assertEqual(settled, 1)

    def test_a_change_after_the_settle_window_is_still_input(self):
        got, _source, settled = self._probe(7.0, "250")
        self.assertIs(got, True)
        self.assertEqual(settled, 0)

    def test_the_decision_line_names_the_settled_changes(self):
        from autoresleep.decide import Evidence
        ev = Evidence(window_input=False, input_source="windows-window-tick", window_settled=1)
        self.assertEqual(cli._window_label(ev), "none@windows-window-tick(1-settled)")
        self.assertEqual(cli._window_label(Evidence(window_input=False,
                                                    input_source="windows-window-tick")),
                         "none@windows-window-tick")

    def test_the_shipped_default_settles_without_eating_the_whole_window(self):
        cfg = Config()
        self.assertGreater(cfg.input_settle_seconds, 0.0, "0 = the replay false positive returns")
        self.assertLess(cfg.input_settle_seconds, cfg.resleep_delay_seconds,
                        "a settle that covers the whole window silently IS strict mode")


if __name__ == "__main__":
    unittest.main()
