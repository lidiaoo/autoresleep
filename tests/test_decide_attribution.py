"""Slice 2: wake attribution, driver precedence and the intentional-wake cases.

The rule that cost v1 the most: the DRIVER's wake signal outranks whatever the OS claims,
and a hardcoded NIC name must never appear anywhere in this logic.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import Action, Evidence, Guards, Params, decide  # noqa: E402

P = Params()


class TestDriverOutranksOS(unittest.TestCase):
    def test_driver_non_magic_ap_assoc_lost_resleeps_when_idle(self):
        d = decide(Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=20.0), Guards(),
                   idle_s=600, params=P)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_AP_ASSOC_LOST")
        self.assertEqual(d.src, "drv")
        self.assertTrue(d.who.startswith("driver:"))

    def test_driver_does_not_win_against_a_packet(self):
        d = decide(Evidence(pkt_kind="marker", pkt_age_s=5.0,
                            drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0),
                   Guards(), idle_s=600, params=P)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.src, "pkt")

    def test_wired_media_change_is_not_treated_as_a_wifi_wake(self):
        # a wired NIC can report a cable/media change but never an AP association loss
        d = decide(Evidence(drv_reason="MEDIA_DISCONNECT", drv_age_s=10.0), Guards(),
                   idle_s=3600, params=P)
        self.assertEqual(d.action, Action.SKIP)


class TestIntentionalWakes(unittest.TestCase):
    def test_ethernet_nic_wake_is_intentional(self):
        d = decide(Evidence(), Guards(), idle_s=3600, params=P, wake_kind="eth-nic", wake_who="name:eth0")
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-eth-nic")

    def test_programmed_timer_wake_is_intentional(self):
        d = decide(Evidence(), Guards(), idle_s=3600, params=P, wake_kind="timer")
        self.assertEqual(d.reason, "wake-source-timer")

    def test_other_device_wake_is_intentional(self):
        d = decide(Evidence(), Guards(), idle_s=3600, params=P, wake_kind="other-device")
        self.assertEqual(d.reason, "wake-source-other-device")


class TestWifiNICIsTheCaseWeExistFor(unittest.TestCase):
    def test_wifi_nic_with_long_idle_resleeps(self):
        d = decide(Evidence(), Guards(), idle_s=600, params=P, wake_kind="wifi-nic")
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WIFI_NIC")
        self.assertEqual(d.src, "drv" if False else "os")  # attribution came from the OS inventory

    def test_wifi_nic_with_short_idle_leaves_the_user_alone(self):
        d = decide(Evidence(), Guards(), idle_s=30, params=P, wake_kind="wifi-nic")
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")

    def test_link_lost_alone_decides_nothing(self):
        # every resume re-associates or roams: LINK_LOST also fires on asked-for wakes
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="bssid-changed"), Guards(),
                   idle_s=30, params=P)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.link, "LINK_LOST")
        self.assertNotIn("LINK_LOST", d.reason)

    def test_link_lost_is_promoted_only_when_the_wifi_nic_is_the_waker(self):
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="bssid-changed"), Guards(),
                   idle_s=600, params=P, wake_kind="wifi-nic")
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_LOST")
        self.assertEqual(d.link, "LINK_LOST")


class TestDoNotGuess(unittest.TestCase):
    def test_unattributed_wake_demands_a_long_idle(self):
        d = decide(Evidence(), Guards(), idle_s=600, params=P)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "unknown-wake-needs-long-idle")

    def test_unattributed_wake_resleeps_after_the_long_idle(self):
        d = decide(Evidence(), Guards(), idle_s=16 * 60, params=P)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_UNATTRIBUTED")

    def test_fixed_feature_nic_suspect_is_skipped_by_default(self):
        d = decide(Evidence(), Guards(), idle_s=3600, params=P, wake_kind="fixed-feature-nic-suspect")
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-fixed-feature-nic-suspect")

    def test_fixed_feature_nic_suspect_resleeps_under_the_explicit_policy(self):
        p = Params(unattributed_wake_policy="resleep-if-unattended")
        d = decide(Evidence(), Guards(), idle_s=600, params=p, wake_kind="fixed-feature-nic-suspect")
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_FIXED_FEATURE_NIC_SUSPECT")


class TestDebounceAndUserPresence(unittest.TestCase):
    def test_a_person_who_left_again_does_not_block_the_sleep(self):
        """`input_since_sleep` must not veto forever: it stays true until the next forced sleep,
        so treating it as "user present" would stop the machine from ever sleeping again."""
        d = decide(Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0), Guards(),
                   idle_s=600, params=P, input_since_sleep=True)
        self.assertEqual(d.action, Action.SLEEP)

    def test_a_person_still_there_is_caught_by_the_idle_threshold(self):
        d = decide(Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0), Guards(),
                   idle_s=30, params=P, input_since_sleep=True)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")

    def test_a_recent_forced_sleep_debounces_the_next_one(self):
        d = decide(Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0), Guards(),
                   idle_s=600, params=P, last_forced_sleep_at=1000.0, now=1000.0 + 60)
        self.assertEqual(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("debounce"))

    def test_debounce_expires(self):
        d = decide(Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0), Guards(),
                   idle_s=600, params=P, last_forced_sleep_at=1000.0, now=1000.0 + 6 * 60)
        self.assertEqual(d.action, Action.SLEEP)


if __name__ == "__main__":
    unittest.main()