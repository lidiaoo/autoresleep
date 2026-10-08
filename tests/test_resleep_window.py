"""The re-sleep window: how long a NIC-caused wake waits, and what cancels the sleep.

The operator's rules, verbatim (docs/resleep-window-plan.md):
  * 唤醒之后发现不是键鼠唤醒，而且监听9端口发现也不是魔法包唤醒 → 回睡
  * 如果是魔法包唤醒或者键鼠唤醒 → 不回睡
  * 唤醒之后多久回睡，比如10秒后回睡                 -> resleep_delay_seconds
  * 是否被用户鼠标键盘操作行为打断                    -> resleep_break_on_input
  * 只允许 wakeonlan 的魔法包唤醒                    -> magic_only
  * `[]` 代表所有有线网卡 + 所有无线网卡              -> wake_nics
Two clocks answer "how long do we wait": the window (counted from the WAKE moment, Q3/Q4a) and,
only as a fallback for evidence with no session stamp, the old idle threshold.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import Action, Evidence, Guards, Params, decide  # noqa: E402

WAKE = 1000.0                      # the resume the evidence describes
WIFI = dict(wake_kind="wifi-nic", wake_who="name:Intel(R) Wi-Fi 6E AX210")


def at(**kw) -> Evidence:
    """Evidence collected at a resume `WAKE` - `judge` decides how long after it we are.

    `window_input=False` by default, because a machine running this tool HAS a watcher (evdev on
    Linux, GetLastInputInfo on Windows) and "it saw nothing in the window" is a statement, not an
    absence. Pass `window_input=None` to model a box where nothing wrote that state at all.
    """
    kw.setdefault("observed_at", WAKE)
    kw.setdefault("session", WAKE)
    kw.setdefault("window_input", False)
    return Evidence(**kw)


def judge(evidence: Evidence, seconds_after_wake: float, idle_s: float = None,
          params: Params = None, **kw):
    """Judge `evidence` this long after its wake.

    `idle_s` defaults to the same age, i.e. "nobody has touched it since the wake" - which is the
    situation the window logic is about. Tests that are about the idle gate pass it explicitly.
    """
    if idle_s is None:
        idle_s = seconds_after_wake
    return decide(evidence, Guards(), idle_s=idle_s, params=params or Params(),
                  now=WAKE + seconds_after_wake, **kw)


class TestInsideTheWindowNothingSleeps(unittest.TestCase):
    def test_a_nic_wake_inside_the_window_is_not_slept_yet(self):
        """The window is an observation period: the packet and the hands can still arrive."""
        d = judge(at(**WIFI), seconds_after_wake=3.0, idle_s=600.0)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "window-open")
        self.assertEqual(d.details.get("window_s"), 10.0)

    def test_the_window_is_configurable(self):
        p = Params(resleep_delay_seconds=30.0)
        self.assertEqual(judge(at(**WIFI), 20.0, params=p).reason, "window-open")
        self.assertEqual(judge(at(**WIFI), 31.0, params=p).action, Action.SLEEP)

    def test_a_moved_link_inside_the_window_also_waits(self):
        d = judge(at(os_link="LINK_LOST", os_link_why="bssid-changed"), 2.0)
        self.assertEqual(d.reason, "window-open")


class TestAfterTheWindow(unittest.TestCase):
    def test_an_unasked_nic_wake_is_slept_once_the_window_is_over(self):
        """Q4a: the window IS the wait - 10 s after the wake, not 2 minutes of idle."""
        d = judge(at(**WIFI), seconds_after_wake=11.0, idle_s=11.0)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WIFI_NIC")

    def test_a_magic_packet_inside_the_window_cancels_it(self):
        d = judge(at(pkt_kind="magic", pkt_age_s=4.0, **WIFI), seconds_after_wake=11.0, idle_s=11.0)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")

    def test_a_keypress_inside_the_window_cancels_it(self):
        d = judge(at(window_input=True, input_source="evdev", **WIFI), 11.0, idle_s=11.0)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-by-input")
        self.assertEqual(d.who, "input-in-window")

    def test_without_a_watcher_the_idle_clock_still_guards_the_machine(self):
        # window_input is None: nobody wrote the window's input state, so fall back to the desktop
        # idle clock instead of trusting a clock a resume resets to zero
        d = judge(at(window_input=None, **WIFI), 11.0, idle_s=5.0)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")
        d2 = judge(at(window_input=None, **WIFI), 11.0, idle_s=200.0)
        self.assertEqual(d2.action, Action.SLEEP)

    def test_with_a_watcher_that_saw_nothing_the_machine_sleeps(self):
        d = judge(at(window_input=False, **WIFI), 11.0, idle_s=11.0)
        self.assertEqual(d.action, Action.SLEEP)


class TestStrictMode(unittest.TestCase):
    """Q5: with `resleep_break_on_input=False` a keypress inside the window does not cancel it."""

    STRICT = Params(resleep_break_on_input=False)

    def test_input_inside_the_window_is_ignored_and_it_still_sleeps(self):
        d = judge(at(window_input=True, input_source="evdev", **WIFI), 11.0, idle_s=11.0,
                  params=self.STRICT)
        self.assertEqual(d.action, Action.SLEEP)
        # the card that woke it is still named, and the note is appended to it: both facts matter
        self.assertIn("input-ignored-by-strict-mode", d.who or "")

    def test_a_packet_still_cancels_it_in_strict_mode(self):
        d = judge(at(pkt_kind="magic", pkt_age_s=3.0, window_input=True, **WIFI), 11.0,
                  idle_s=11.0, params=self.STRICT)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")

    def test_the_note_is_appended_when_the_decision_already_names_something(self):
        d = judge(at(wake_kind="eth-nic", wake_who="name:eth0", window_input=True), 11.0,
                  idle_s=11.0, params=self.STRICT)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertIn("name:eth0", d.who or "")
        self.assertIn("input-ignored-by-strict-mode", d.who or "")


class TestWakeNics(unittest.TestCase):
    def test_a_nic_outside_the_list_is_not_policed(self):
        d = judge(at(wake_nic_policed=False, **WIFI), 11.0, idle_s=600.0)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-wifi-nic-not-policed")

    def test_a_wired_wake_is_judged_when_it_is_in_the_list(self):
        d = judge(at(wake_kind="eth-nic", wake_who="name:以太网", wake_nic_policed=True), 11.0,
                  idle_s=11.0)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_ETHERNET_NIC")


class TestMagicOnly(unittest.TestCase):
    """Q: 只允许 wakeonlan 的魔法包唤醒，仍要空闲门槛（Q2）。"""

    ONLY = Params(magic_only=True)

    def test_a_wifi_wake_with_no_packet_still_sleeps_after_the_window(self):
        d = judge(at(**WIFI), 11.0, idle_s=11.0, params=self.ONLY)
        self.assertEqual(d.action, Action.SLEEP)

    def test_it_still_waits_for_the_window(self):
        d = judge(at(**WIFI), 3.0, idle_s=600.0, params=self.ONLY)
        self.assertEqual(d.reason, "window-open")

    def test_a_magic_packet_still_exempts_the_machine(self):
        d = judge(at(pkt_kind="magic", pkt_age_s=2.0, **WIFI), 11.0, idle_s=11.0, params=self.ONLY)
        self.assertEqual(d.action, Action.SKIP)

    def test_the_nic_shaped_guess_is_no_longer_skipped_by_default(self):
        # fixed-feature-nic-suspect is "only NICs are armed and the OS cannot name one" - the
        # conservative default is to leave it alone; `magic_only` says a NIC wake is not wanted
        self.assertEqual(judge(at(wake_kind="fixed-feature-nic-suspect"), 11.0, idle_s=11.0,
                               params=self.ONLY).action, Action.SLEEP)
        self.assertEqual(judge(at(wake_kind="fixed-feature-nic-suspect"), 11.0, idle_s=11.0)
                         .action, Action.SKIP)

    def test_a_timer_wake_is_still_left_alone(self):
        # the OS programming its own wake-up is not the NIC poking an empty machine
        d = judge(at(wake_kind="timer"), 11.0, idle_s=600.0, params=self.ONLY)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-timer")


class TestTheOldClockStillWorksAsAFallback(unittest.TestCase):
    """Evidence with no session stamp cannot be measured against the window."""

    def test_idle_minutes_still_gates_it(self):
        d = decide(Evidence(**WIFI), Guards(), idle_s=30.0, params=Params())
        self.assertEqual(d.reason, "user-present")
        d2 = decide(Evidence(**WIFI), Guards(), idle_s=600.0, params=Params())
        self.assertEqual(d2.action, Action.SLEEP)


class TestAStaleWindowDoesNotSleepAWorkingMachine(unittest.TestCase):
    """The window is about the seconds after the WAKE, not about now.

    Measured risk (caught while this checkout was already live on a real machine): the listener
    samples the window the moment the resume is noticed, so from then on `window_input=False` is on
    record for the whole session. Hours later that stale verdict must not authorize a sleep on
    somebody who has been typing for the last ten seconds.
    """

    def test_a_typing_user_is_protected_by_the_idle_gate(self):
        d = judge(at(**WIFI), seconds_after_wake=7200, idle_s=10.0)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")

    def test_an_empty_machine_still_sleeps_hours_later(self):
        d = judge(at(**WIFI), seconds_after_wake=7200, idle_s=800.0)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WIFI_NIC")

    def test_with_no_idle_information_it_refuses_to_act(self):
        d = judge(at(**WIFI), seconds_after_wake=7200, idle_s=-1.0)
        self.assertEqual(d.action, Action.SKIP)


if __name__ == "__main__":
    unittest.main()