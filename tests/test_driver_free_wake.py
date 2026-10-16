"""The driver-free wake classification: observable facts, no NIC driver in the loop.

The rule being pinned here: what marks a wake as "nobody asked for this" must be something the
OS itself publishes on every platform - the packet log, the input clock, and the link state
before/after the sleep. Driver-level wake reasons (Windows WakeSourceText, Linux dmesg driver
strings) either do not exist on a given box or go silent after a fixed-feature NIC wake, so they
may sharpen a decision but must never be required to make one.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# the repo root holds the entry script `autoresleep.py`, which shadows the package of the same
# name - every test file bootstraps `src` itself so a single file can be run on its own
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import Action, Evidence, Guards, Params, decide  # noqa: E402


class TestInputWakeIsDriverFree(unittest.TestCase):
    """Keyboard/mouse wake = "hands were on it", read off the input clock, on any platform."""

    def test_last_input_at_the_wake_moment_means_hands_were_on_it(self):
        from autoresleep.cli import input_at_wake
        now = 1000000.0
        self.assertTrue(input_at_wake(now, 30.0, now - 30.0))

    def test_input_long_before_the_wake_is_not_an_input_wake(self):
        from autoresleep.cli import input_at_wake
        now = 1000000.0
        self.assertFalse(input_at_wake(now, 300.0, now - 30.0))

    def test_an_old_wake_never_reads_as_an_input_wake(self):
        # the flag must not stay true forever - that deadlock is exactly what the removed
        # blanket "input since last forced sleep" rule caused
        from autoresleep.cli import input_at_wake
        now = 1000000.0
        self.assertIsNone(input_at_wake(now, 5.0, now - 3600.0))

    def test_unknown_input_or_wake_stamp_says_cannot_tell(self):
        from autoresleep.cli import input_at_wake
        self.assertIsNone(input_at_wake(1000.0, -1.0, 900.0))
        self.assertIsNone(input_at_wake(1000.0, 30.0, None))


class TestLinkMovedChain(unittest.TestCase):
    """A wifi that dropped and came back re-sleeps with NO driver attribution anywhere."""

    def _decide(self, link="LINK_LOST", why="carrier-changed-1x-since-sleep", idle=130.0,
                params=None, **kw):
        return decide(Evidence(os_link=link, os_link_why=why), Guards(), idle_s=idle,
                      params=params or Params(), now=10000.0, **kw)

    def test_a_moved_link_re_sleeps(self):
        d = self._decide()
        self.assertIs(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_MOVED")
        self.assertEqual(d.src, "link")
        self.assertEqual(d.who, "carrier-changed-1x-since-sleep")

    def test_a_moved_link_still_respects_the_idle_gate(self):
        d = self._decide(idle=30.0)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "user-present")

    def test_an_asked_for_wake_outranks_the_moved_link(self):
        ev = Evidence(pkt_kind="magic", pkt_age_s=10.0, os_link="LINK_LOST")
        d = decide(ev, Guards(), idle_s=600.0, params=Params(), now=10000.0)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-requested")

    def test_hands_on_the_machine_outrank_the_moved_link(self):
        # idle below the threshold: the INPUT chain answers ("wake-by-input"), not the link chain
        # (which would have said "user-present") - so the ordering is observable in the reason
        d = self._decide(idle=30.0, wake_by_input=True,
                         params=Params(resleep_break_on_input=True))
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-by-input")
        self.assertEqual(d.src, "input")

    def test_guards_beat_every_evidence_chain(self):
        d = decide(Evidence(os_link="LINK_LOST"), Guards(remote_in_use=True), idle_s=600.0,
                   params=Params(), now=10000.0)
        self.assertIs(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("guarded:"))

    def test_an_intact_link_is_still_not_evidence_of_anything(self):
        d = self._decide(link="LINK_INTACT", why="bssid=x ssid=y",
                         params=Params(unknown_idle_seconds=900))
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "unknown-wake-needs-long-idle")

    def test_an_input_wake_is_not_re_slept_once_the_human_is_gone(self):
        # OVERTURNED CONTRACT: an idle-gated re-sleep here was measured as "the mouse woke it and
        # three minutes later it slept again". A hand on the machine exempts the whole session.
        d = decide(Evidence(), Guards(), idle_s=600.0,
                   params=Params(resleep_break_on_input=True), now=10000.0,
                   wake_by_input=True)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-by-input")

    def test_the_conservative_gap_debounces_a_moved_link_when_we_cannot_tell(self):
        # input_since_sleep=None means "cannot tell whether anyone is around" -> the conservative
        # gap applies (explicit here: the shipped default is 30 s, this test pins the mechanism)
        d = decide(Evidence(os_link="LINK_LOST"), Guards(), idle_s=600.0,
                   params=Params(min_gap_seconds=300.0),
                   now=10000.0, last_forced_sleep_at=10000.0 - 120.0)
        self.assertIs(d.action, Action.SKIP)
        self.assertTrue(d.reason.startswith("debounce("))

    def test_the_short_gap_applies_when_the_machine_is_provably_empty(self):
        # nobody touched it since our own forced sleep: 1-minute gap, so a flapping NIC does not
        # keep an empty machine awake for 5 minutes per cycle
        d = decide(Evidence(os_link="LINK_LOST"), Guards(), idle_s=600.0, params=Params(),
                   now=10000.0, last_forced_sleep_at=10000.0 - 120.0, input_since_sleep=False)
        self.assertIs(d.action, Action.SLEEP)


def _system_with(state, events=()):
    """A concrete platform whose only real capability is the link probe."""
    from autoresleep.system.base import IdleInfo, System

    class _Sys(System):
        def idle(self):
            return IdleInfo(0.0, method="test")

        def suspend(self):
            return "never (test)"

        def link_state(self):
            return state

        def link_events_since(self, ts):
            return list(events)

    return _Sys()


class TestSharedLinkComparison(unittest.TestCase):
    """One comparison for all three platforms - three copies would drift apart."""

    def _evidence(self, state, snapshot, events=()):
        from autoresleep import paths
        with tempfile.TemporaryDirectory() as td:
            # both keys: the data dir follows LOCALAPPDATA on Windows, XDG_STATE_HOME on Linux
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
                paths.ensure_data_dir()
                if snapshot is not None:
                    paths.link_snapshot_file().write_text(json.dumps(snapshot), encoding="utf-8")
                return _system_with(state, events).link_evidence()

    def test_a_link_that_stayed_put_is_intact(self):
        link, why = self._evidence({"connected": True, "bssid": "aa:bb", "ssid": "home",
                                    "changes": 3},
                                   {"bssid": "aa:bb", "ssid": "home", "associated": True,
                                    "changes": 3, "at": 1.0})
        self.assertEqual(link, "LINK_INTACT")
        self.assertIn("bssid=aa:bb", why)

    def test_a_changed_bssid_is_a_loss(self):
        link, why = self._evidence({"connected": True, "bssid": "aa:11", "ssid": "new",
                                    "changes": 3},
                                   {"bssid": "aa:bb", "ssid": "old", "associated": True,
                                    "changes": 3, "at": 1.0})
        self.assertEqual(link, "LINK_LOST")
        self.assertIn("bssid-changed", why)

    def test_a_carrier_drop_to_the_same_ap_is_still_a_loss(self):
        # the signal a BSSID comparison cannot give: same AP, but the link physically bounced
        link, why = self._evidence({"connected": True, "bssid": "aa:bb", "ssid": "home",
                                    "changes": 5},
                                   {"bssid": "aa:bb", "ssid": "home", "associated": True,
                                    "changes": 4, "at": 1.0})
        self.assertEqual(link, "LINK_LOST")
        self.assertIn("carrier-changed-1x", why)

    def test_no_association_at_all_is_a_loss(self):
        link, why = self._evidence({"connected": False, "bssid": "", "ssid": "", "changes": None},
                                   {"bssid": "aa:bb", "ssid": "home", "associated": True,
                                    "changes": None, "at": 1.0})
        self.assertEqual(link, "LINK_LOST")
        self.assertIn("not-associated", why)

    def test_os_logged_reconnect_counts_when_counters_are_unavailable(self):
        link, why = self._evidence({"connected": True, "bssid": "aa:bb", "ssid": "home",
                                    "changes": None},
                                   {"bssid": "aa:bb", "ssid": "home", "associated": True,
                                    "changes": None, "at": 1.0},
                                   events=["2026-09-29T01:33:12+08:00 wlan0: associated"])
        self.assertEqual(link, "LINK_LOST")
        self.assertIn("reconnect-event-after-sleep", why)

    def test_without_a_snapshot_it_says_so_instead_of_guessing(self):
        link, why = self._evidence({"connected": True, "bssid": "aa:bb", "ssid": "home",
                                    "changes": None}, None)
        self.assertEqual(link, "LINK_INTACT")
        self.assertIn("no pre-sleep snapshot", why)

    def test_a_platform_without_a_probe_says_unknown(self):
        from autoresleep.system.base import IdleInfo, System

        class _Bare(System):
            def idle(self):
                return IdleInfo(0.0, method="test")

            def suspend(self):
                return "never (test)"

        self.assertEqual(_Bare().link_evidence()[0], "UNKNOWN")


class TestLinuxLinkProbes(unittest.TestCase):
    """Parsers only: the test runner has no say over the wifi card (or that one exists)."""

    def test_a_connected_link_is_parsed(self):
        from autoresleep.system.linux import LinuxSystem
        text = ("Connected to 28:D0:EA:27:40:0C (on wlan0)\n\tSSID: meiyoumima_5G\n"
                "\tfreq: 5200\n\tsignal: -41 dBm\n")
        st = LinuxSystem.parse_iw_link(text)
        self.assertTrue(st["connected"])
        self.assertEqual(st["bssid"], "28:d0:ea:27:40:0c")
        self.assertEqual(st["ssid"], "meiyoumima_5G")

    def test_a_disconnected_link_is_parsed(self):
        from autoresleep.system.linux import LinuxSystem
        st = LinuxSystem.parse_iw_link("Not connected.\n")
        self.assertFalse(st["connected"])
        self.assertEqual(st["bssid"], "")

    def test_unrecognised_output_says_cannot_tell(self):
        from autoresleep.system.linux import LinuxSystem
        self.assertIsNone(LinuxSystem.parse_iw_link("")["connected"])

    def test_wireless_interfaces_comes_from_the_kernel_list(self):
        from autoresleep.system.linux import LinuxSystem
        text = ("Inter-| sta-|   Quality        |   Discarded packets\n"
                " face | tus | link level noise |  nwid  crypt   frag  retry\n"
                " wlan0: 0000   69.  -41.  -256        0      0      0      0\n")
        self.assertEqual(LinuxSystem.wireless_interfaces(text), ["wlan0"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs the Linux sysfs counter")
    def test_the_carrier_counter_is_readable_without_privileges(self):
        from autoresleep.system.linux import LinuxSystem
        self.assertIsInstance(LinuxSystem.carrier_changes("lo"), int)
        self.assertIsNone(LinuxSystem.carrier_changes("does-not-exist"))


class TestTheListenerWitnessesReconnects(unittest.TestCase):
    """The listener is awake once a second, so a drop-and-return is an observed event."""

    def test_a_transition_is_written_and_a_steady_link_is_not(self):
        import tempfile
        from autoresleep import paths
        from autoresleep.cli import _sample_link

        class _Sys(object):
            state = {"connected": True, "bssid": "aa:bb", "ssid": "home"}

            def link_state(self):
                return dict(_Sys.state)

        class _Log(object):
            def info(self, *a):
                pass

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_STATE_HOME": td}):
                paths.ensure_data_dir()
                _sample_link._last = None          # fresh process would have no previous sample
                _sample_link(_Sys(), _Log())       # baseline only
                self.assertFalse(paths.link_event_log().exists())
                _Sys.state = {"connected": False, "bssid": "", "ssid": ""}
                _sample_link(_Sys(), _Log())       # the drop
                _sample_link(_Sys(), _Log())       # still down: not a new event
                text = paths.link_event_log().read_text(encoding="utf-8")
                self.assertEqual(len(text.strip().splitlines()), 1)
                self.assertIn("link True->False", text)


class TestMacLinkProbes(unittest.TestCase):
    """macOS: written from documentation (there is no Mac here), so the parsing is pinned by
    fixture - forgiving on unrecognised output, `connected=None` when it cannot tell."""

    def _state(self, airport="", networksetup="", airport_present=True):
        from autoresleep.system.macos import MacSystem
        outs = [airport, networksetup] if airport_present else [networksetup]
        with mock.patch("autoresleep.system.macos.os.path.exists", return_value=airport_present):
            with mock.patch("autoresleep.system.macos._run", side_effect=outs):
                return MacSystem().link_state()

    def test_airport_i_says_which_ap_we_are_on(self):
        out = ("     agrCtlRSSI: -41\n          state: running\n"
               "          BSSID: C8:BF:4C:EF:CF:BC\n           SSID: meiyoumima_5G\n")
        st = self._state(airport=out)
        self.assertTrue(st["connected"])
        self.assertEqual(st["bssid"], "c8:bf:4c:ef:cf:bc")     # lowercased for comparison
        self.assertEqual(st["ssid"], "meiyoumima_5G")
        self.assertEqual(st["method"], "airport -I")

    def test_an_initialised_state_without_a_bssid_is_not_associated(self):
        st = self._state(airport="          state: init\n")
        self.assertFalse(st["connected"])
        self.assertEqual(st["bssid"], "")

    def test_networksetup_is_the_fallback_when_airport_is_gone(self):
        # recent macOS ships no airport binary: the probe has to survive on the public tool
        st = self._state(airport="", networksetup="Current Wi-Fi Network: home\n",
                         airport_present=False)
        self.assertTrue(st["connected"])
        self.assertEqual(st["ssid"], "home")
        self.assertEqual(st["method"], "networksetup -getairportnetwork")

    def test_networksetup_reports_a_dropped_link(self):
        st = self._state(airport="",
                         networksetup="You are not associated with an AirPort network.\n",
                         airport_present=False)
        self.assertFalse(st["connected"])

    def test_unrecognised_output_says_cannot_tell_instead_of_guessing(self):
        st = self._state(airport="", networksetup="whatever\n", airport_present=False)
        self.assertIsNone(st["connected"])


class TestMacWakeReasonAttribution(unittest.TestCase):
    """macOS `pmset -g log` names the HARDWARE, never the packet.

    `Wake from ... : due to <CODE>/<CLASS>` - `ARPT` is Apple's wifi node, `GLAN` the gigabit LAN
    node, `EHC`/`OHC`/`XHC` are USB, `RTC (Alarm)` is a timer. There is no packet-level cause
    anywhere in it (see `system/macos.py`), so the class decides only WHICH device the engine may
    judge. A wired WoL wake used to fall through to `other-device` - and the engine leaves
    other-device wakes alone by design, so that machine woke on a cable and never re-slept.
    """

    def _source(self, log_text, adapters_=()):
        from autoresleep.system.macos import MacSystem
        with mock.patch("autoresleep.system.macos._run", return_value=log_text), \
             mock.patch("autoresleep.system.macos.adapters", return_value=list(adapters_)):
            return MacSystem().wake_source()

    def test_a_wired_wake_on_lan_wake_is_a_wired_nic(self):
        src = self._source("2026-10-16 14:04:51 +0800 Wake from Normal Sleep [CDNVA] : "
                           "due to GLAN/Network\n")
        self.assertEqual(src.kind, "eth-nic")
        self.assertEqual(src.who, "name:glan")
        self.assertIn("GLAN", src.raw, "the OS's own text is kept verbatim")

    def test_arpt_is_the_wifi_node(self):
        src = self._source("2026-10-16 14:04:51 +0800 Wake from Normal Sleep [CDNVA] : "
                           "due to ARPT/Network\n")
        self.assertEqual(src.kind, "wifi-nic")

    def test_an_rtc_alarm_is_a_timer(self):
        src = self._source("2026-10-16 03:00:01 +0800 Wake from Normal Sleep [CDNVA] : "
                           "due to RTC (Alarm)\n")
        self.assertEqual(src.kind, "timer")
        self.assertTrue(src.timer)

    def test_opening_the_lid_is_not_a_nic(self):
        src = self._source("2026-10-16 09:00:00 +0800 Wake from Deep Idle [CDNVA] : "
                           "due to EC.LidOpen/Lid Open\n")
        self.assertNotIn(src.kind, ("wifi-nic", "eth-nic"))

    def test_the_adapter_names_still_win_over_the_anonymous_codes(self):
        from autoresleep.pkt import Adapter
        wifi = Adapter(name="en1", label="Wi-Fi", mac="aa:bb:cc:dd:ee:ff", wireless=True)
        src = self._source("2026-10-16 14:04:51 +0800 Wake from Normal Sleep : "
                           "due to Wi-Fi/Network\n", [wifi])
        self.assertEqual(src.kind, "wifi-nic")
        self.assertEqual(src.who, "name:Wi-Fi")


class TestTheSnapshotIsActuallyTaken(unittest.TestCase):
    """`link_snapshot()` existed for Windows only and was never called, so the comparison
    against it had no baseline. Pin the call, since a live suspend cannot be tested."""

    def test_cli_takes_a_snapshot_before_every_suspend(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "autoresleep" / "cli.py").read_text(
            encoding="utf-8")
        # two suspend sites: the scheduled judge and the listener's fast path
        self.assertEqual(src.count("sysinfo.link_snapshot()"), 2)


if __name__ == "__main__":
    unittest.main()