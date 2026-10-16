"""Slice 8: the evening that went wrong.

An install at 18:15 on a machine that was already logged in, then six hours of "action=skip
reason=wake-source-fixed-feature-nic-suspect" while the machine kept being woken by its Wi-Fi
NIC. Every rule below exists because of that evening:

  * the attribution must belong to THIS wake - a stored conclusion from 16:11 is history;
  * the listener must be started at install time, not at the next logon;
  * a decision to suspend on a guess must look again right before suspending;
  * the Wi-Fi link can be sampled without admin, so the log can say what actually happened.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

# Everything in this file builds Windows backends and drives `doctor` for all three platforms,
# which runs OS probes and logs while it does. Keep the module in a throwaway state dir: the
# logger keys its file handler on the data dir, so this really is isolation - otherwise test
# noise lands in the machine's own journal.log and muddies a real diagnosis.
_STATE = tempfile.TemporaryDirectory()
mock.patch.dict(os.environ, {"LOCALAPPDATA": _STATE.name, "XDG_STATE_HOME": _STATE.name}).start()


from autoresleep.cli import grace_verdict  # noqa: E402
from autoresleep.decide import Action, Evidence, Guards, decide  # noqa: E402
from autoresleep.system import read_evidence, write_wake_evidence  # noqa: E402
from autoresleep.system.base import WakeSource  # noqa: E402
from autoresleep.system.windows import parse_event_time  # noqa: E402

WAKE_2053 = "2026-09-28T12:53:50.8581864Z"


class TestWakeTimestamps(unittest.TestCase):
    def test_the_real_event_timestamp_parses_as_utc(self):
        epoch = parse_event_time(WAKE_2053)
        self.assertIsNotNone(epoch)
        import time as _t
        self.assertEqual(_t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(epoch)),
                         "2026-09-28 12:53:50")

    def test_an_explicit_offset_is_honoured(self):
        # same instant expressed in local time: must NOT come back four hours away
        self.assertEqual(parse_event_time("2026-09-28T20:53:50+08:00"),
                         parse_event_time(WAKE_2053))

    def test_an_unparsable_timestamp_is_none_not_an_exception(self):
        self.assertIsNone(parse_event_time("last tuesday"))
        self.assertIsNone(parse_event_time(""))


class TestEvidenceMustBelongToThisWake(unittest.TestCase):
    """The core bug: evidence from an earlier wake was reused for a later one.

    No save/restore of the live file any more: this module writes into a throwaway state dir
    (see the top of the file). Saving and putting back the machine's real evidence file also
    raced the running listener and judge, which rewrite it on every actual resume.
    """

    def test_evidence_from_another_wake_is_refused(self):
        write_wake_evidence(WakeSource(kind="fixed-feature-nic-suspect", who="only-nics-armed",
                                       wake_time="2026-09-28T08:11:36.8914226Z",
                                       wake_epoch=parse_event_time("2026-09-28T08:11:36.8914226Z")))
        # the machine has woken again since - this file describes the OLD wake
        ev = read_evidence(current_wake_epoch=parse_event_time(WAKE_2053))
        self.assertEqual(ev.wake_kind, None)
        self.assertEqual(ev.drv_reason, None)

    def test_evidence_for_the_same_wake_is_used(self):
        epoch = parse_event_time(WAKE_2053)
        write_wake_evidence(WakeSource(kind="wifi-nic", who="name:Killer(R) Wi-Fi 6 AX1650i",
                                       wake_time=WAKE_2053, wake_epoch=epoch),
                            link="LINK_INTACT", link_why="bssid=44:df:65:cd:00:00")
        ev = read_evidence(current_wake_epoch=epoch)
        self.assertEqual(ev.wake_kind, "wifi-nic")
        self.assertEqual(ev.os_link, "LINK_INTACT")
        self.assertIn("44:df", ev.os_link_why)

    def test_unverifiable_evidence_is_not_treated_as_verified(self):
        # an old file with no wake stamp at all: cannot be checked, so refuse it and re-query
        import json
        from autoresleep import paths
        paths.ensure_data_dir()
        (paths.data_dir() / "evidence-last.json").write_text(json.dumps({
            "observed_at": __import__("time").time(), "kind": "wifi-nic", "who": "x",
            "chain": "os"}), encoding="utf-8")
        ev = read_evidence(current_wake_epoch=parse_event_time(WAKE_2053))
        self.assertIsNone(ev.wake_kind)


class TestTheEveningThatWentWrong(unittest.TestCase):
    def test_a_wifi_nic_wake_with_long_idle_would_have_slept(self):
        """With the correct attribution, the 20:53 decision is SLEEP, not skip."""
        ev = Evidence(wake_kind="wifi-nic", wake_who="name:Killer(R) Wi-Fi 6 AX1650i",
                      os_link="LINK_INTACT", os_link_why="bssid=44:df:65:cd:00:00")
        d = decide(ev, Guards(), idle_s=231, params=__import__(
            "autoresleep.decide", fromlist=["Params"]).Params())
        self.assertEqual(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WIFI_NIC")

    def test_the_stale_attribution_reproduces_the_wrong_skip(self):
        """Same machine, same idle, wrong (six-hour-old) attribution -> skip. The bug, pinned."""
        from autoresleep.decide import Params
        ev = Evidence(wake_kind="fixed-feature-nic-suspect", wake_who="only-nics-armed")
        d = decide(ev, Guards(), idle_s=231, params=Params(unattributed_wake_policy="skip"))
        self.assertEqual(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-source-fixed-feature-nic-suspect")


class TestGraceBeforeSuspend(unittest.TestCase):
    """v1's tier-A grace: never suspend a machine someone just touched."""

    def test_input_during_the_grace_window_cancels_the_suspend(self):
        self.assertEqual(grace_verdict("1000", "1001", idle_after_s=70.0, needed_s=120.0,
                                       guards_active=None), "input-after-grace")

    def test_idle_reset_during_the_grace_window_cancels_the_suspend(self):
        self.assertEqual(grace_verdict("1000", "1000", idle_after_s=5.0, needed_s=120.0,
                                       guards_active=None), "user-present-after-grace")

    def test_a_guard_appearing_during_the_grace_window_cancels_the_suspend(self):
        self.assertEqual(grace_verdict("1000", "1000", idle_after_s=200.0, needed_s=120.0,
                                       guards_active="display-in-use"),
                         "guarded-after-grace:display-in-use")

    def test_an_untouched_idle_machine_proceeds(self):
        self.assertIsNone(grace_verdict("1000", "1000", idle_after_s=200.0, needed_s=120.0,
                                        guards_active=None))

    def test_a_platform_without_a_tick_still_checks_idle(self):
        self.assertEqual(grace_verdict("", "", idle_after_s=30.0, needed_s=120.0,
                                       guards_active=None), "user-present-after-grace")


class TestWlanLinkProbe(unittest.TestCase):
    """netsh output parsing, without needing a Wi-Fi card in the test runner."""

    def _state(self, text):
        """Patch the MODULE-level _run.

        `_wlan_state` calls it as a global, so assigning to the instance does nothing: the test
        then interrogates the real Wi-Fi card (measured: it returned this machine's SSID and two
        assertions failed for a reason that had nothing to do with the parser).
        """
        from unittest import mock
        from autoresleep.system import windows as win
        with mock.patch.object(win, "_run", lambda *a, **k: text):
            return win.WindowsSystem()._wlan_state()

    def test_an_associated_interface_reports_ssid_and_bssid(self):
        text = ("    名称                   : WLAN\n"
                "    描述                   : Killer(R) Wi-Fi 6 AX1650i\n"
                "    状态                   : 已连接\n"
                "    SSID                   : meiyoumima_5G\n"
                "    BSSID                  : 44:df:65:cd:00:00\n")
        st = self._state(text)
        self.assertTrue(st["connected"])
        self.assertEqual(st["ssid"], "meiyoumima_5G")
        self.assertEqual(st["bssid"], "44:df:65:cd:00:00")

    def test_a_disconnected_interface_is_not_read_as_connected(self):
        st = self._state("    名称 : WLAN\n    状态 : 已断开连接\n")
        self.assertFalse(st["connected"])

    def test_english_output_works_too(self):
        st = self._state("    Name : WLAN\n    State : connected\n    SSID : home\n"
                         "    BSSID : aa:bb:cc:dd:ee:ff\n")
        self.assertTrue(st["connected"])
        self.assertEqual(st["bssid"], "aa:bb:cc:dd:ee:ff")

    def test_a_lost_link_with_a_changed_bssid_is_reported_as_link_lost(self):
        import json
        import tempfile
        from unittest import mock
        from autoresleep import paths
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict("os.environ", {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
                paths.ensure_data_dir()
                paths.link_snapshot_file().write_text(json.dumps(
                    {"bssid": "aa:bb:cc:dd:ee:00", "ssid": "old", "associated": True}),
                    encoding="utf-8")
                from autoresleep.system import windows as win
                with mock.patch.object(win, "_run", lambda *a, **k: (
                        "    状态 : 已连接\n    SSID : new\n    BSSID : aa:bb:cc:dd:ee:11\n")):
                    link, why = win.WindowsSystem().link_evidence()
        self.assertEqual(link, "LINK_LOST")
        self.assertIn("bssid-changed", why)


if __name__ == "__main__":
    unittest.main()