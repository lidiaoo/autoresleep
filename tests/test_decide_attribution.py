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
    def test_a_driver_string_alone_never_sleeps_the_machine(self):
        # item 4 of the contract: a driver's wake reason is a per-driver private string, so it is
        # recorded and shown but can never convict. Sleeping on it means sleeping on a vendor's
        # vocabulary - the portable chains (packet / input / link) are what decide.
        d = decide(Evidence(drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=20.0), Guards(),
                   idle_s=600, params=Params(unknown_idle_seconds=900))
        self.assertNotEqual(d.action, Action.SLEEP)

    def test_the_portable_link_evidence_resleeps_when_idle(self):
        # the same event, established the portable way: nl80211 said the link moved at the resume
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected"), Guards(), idle_s=600, params=P, input_since_sleep=False)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_MOVED")
        self.assertEqual(d.src, "link")

    def test_driver_does_not_win_against_a_packet(self):
        d = decide(Evidence(pkt_kind="marker", pkt_age_s=5.0,
                            drv_reason="NON_MAGIC_AP_ASSOC_LOST", drv_age_s=5.0),
                   Guards(), idle_s=600, params=P)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.src, "pkt")

    def test_a_wired_media_string_is_ignored_like_every_other_driver_string(self):
        # a wired NIC can report a cable/media change - and like every driver string it decides
        # nothing. With no portable evidence at all, an hour of confirmed-idle time falls to the
        # unattributed policy (which is what should sleep an empty machine), NOT to the driver.
        d = decide(Evidence(drv_reason="MEDIA_DISCONNECT", drv_age_s=10.0), Guards(),
                   idle_s=3600, params=P)
        self.assertNotEqual(d.src, "drv")
        self.assertEqual(d.src, "policy")
        self.assertEqual(d.reason, "NON_MAGIC_UNATTRIBUTED")


class TestIntentionalWakes(unittest.TestCase):
    def test_an_ethernet_nic_wake_is_judged_like_a_wifi_one(self):
        # Q1(a): the operator's requirement is ONE rule - "防止 wifi 或者有线网卡等设备异常唤醒".
        # A wired wake used to be "intentional, skip"; with `wake_nics` it is a re-sleep candidate.
        d = decide(Evidence(), Guards(), idle_s=3600, params=P, wake_kind="eth-nic",
                   wake_who="name:eth0")
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_ETHERNET_NIC")

    def test_an_adapter_that_is_not_in_wake_nics_is_left_alone(self):
        # `wake_nics` names the cards we police; a wake from any other card is somebody else's
        d = decide(Evidence(wake_nic_policed=False), Guards(), idle_s=3600, params=P,
                   wake_kind="eth-nic", wake_who="name:usb-lan")
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-eth-nic-not-policed")

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

    def test_a_moved_link_respects_the_idle_gate_like_every_other_chain(self):
        # LINK_LOST used to be a hint that only mattered when the OS had attributed the wake to
        # the wifi NIC. It is now its own chain - the portable one - so the idle gate (not the
        # attribution) is what keeps a machine in use awake.
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="bssid-changed"), Guards(),
                   idle_s=30, params=P)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")
        self.assertEqual(d.link, "LINK_LOST")

    def test_a_moved_link_re_sleeps_without_any_driver_or_os_attribution(self):
        # the point of the driver-free rewrite: no wake_kind, no drv_reason, still decides
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="carrier-changed-1x-since-sleep"),
                   Guards(), idle_s=600, params=P)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_MOVED")
        self.assertEqual(d.src, "link")
        self.assertEqual(d.link, "LINK_LOST")


class TestDoNotGuess(unittest.TestCase):
    def test_unattributed_wake_demands_its_idle(self):
        p = Params(unknown_idle_seconds=900)
        d = decide(Evidence(), Guards(), idle_s=600, params=p)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "unknown-wake-needs-long-idle")

    def test_unattributed_wake_resleeps_after_the_long_idle(self):
        d = decide(Evidence(), Guards(), idle_s=16 * 60, params=P)
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_UNATTRIBUTED")

    def test_fixed_feature_nic_suspect_resleeps_by_default(self):
        d = decide(Evidence(), Guards(), idle_s=3600, params=P, wake_kind="fixed-feature-nic-suspect")
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_FIXED_FEATURE_NIC_SUSPECT")

    def test_fixed_feature_nic_suspect_is_skipped_under_the_skip_policy(self):
        p = Params(unattributed_wake_policy="skip")
        d = decide(Evidence(), Guards(), idle_s=3600, params=p, wake_kind="fixed-feature-nic-suspect")
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
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected"), Guards(),
                   idle_s=600, params=P, input_since_sleep=True)
        self.assertEqual(d.action, Action.SLEEP)

    def test_a_person_still_there_is_caught_by_the_idle_threshold(self):
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected"), Guards(),
                   idle_s=30, params=P, input_since_sleep=True)
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")

    def test_a_recent_forced_sleep_debounces_the_next_one(self):
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected"), Guards(),
                   idle_s=600, params=Params(min_gap_seconds=300),
                   last_forced_sleep_at=1000.0, now=1000.0 + 60)
        self.assertEqual(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("debounce"))

    def test_debounce_expires(self):
        d = decide(Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected"), Guards(),
                   idle_s=600, params=P, last_forced_sleep_at=1000.0, now=1000.0 + 6 * 60)
        self.assertEqual(d.action, Action.SLEEP)


if __name__ == "__main__":
    unittest.main()