"""The three listeners the design rests on, and the driver string that must decide nothing.

The operator's rule, verbatim: decide a wake from what the OS and the hardware publish -
`iw event` for a wifi disconnect, a magic packet on its port, a real keyboard/mouse/USB event -
and never from the wifi driver's own wake-reason field, because that field is a per-driver
private vocabulary ("通用性差, 每个不同的wifi网卡都可能驱动的字段不一样").

The failure this file exists to prevent, measured on the real box: the desktop idle counter is
reset to zero BY the resume, so EVERY wake looked like a keypress (`wake-by-input idle=0s`), the
session latch then held, and a wifi-disconnect wake never re-slept again for a whole day.

The WoWLAN wakeup report is NOT the forbidden thing. The rule bans each driver's private wake-reason
STRING, rendered by that driver's own code in its own words; the report read here is the kernel's
`struct cfg80211_wowlan_wakeup` - every wireless driver fills in the same fields and `iw` prints
them in one format (measured on this box: `* magic packet received` for a real magic packet,
`wakeup not due to WoWLAN` for a wake the NIC denies). Same protocol layer as the link moves above.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.decide import Action, Evidence, Guards, Params, decide  # noqa: E402
from autoresleep.inputwatch import InputWatcher, discover_devices, read_last_input  # noqa: E402
from autoresleep.iwevent import (is_link_move, parse_event_line,  # noqa: E402
                                 wifi_moves_around_resume)
from autoresleep.pkt import RawMagicSocket, is_magic_packet  # noqa: E402


#: `poll()` is driven through an `os.pipe()` in the tests below, and Windows' `select` cannot
#: wait on a pipe handle (sockets only): the call raises, `poll` catches it and returns 0. On a
#: POSIX host the code under test talks to real evdev devices, so on Windows these are SKIPPED
#: rather than left to pass for the wrong reason - an assertion of "0 events" that is really
#: "select() failed" would silently bless a watcher that never notices a keypress.
POSIX_PIPE_ONLY = unittest.skipUnless(os.name == "posix", "drives poll() through an os.pipe()")


class TestIwEventParsing(unittest.TestCase):
    """`iw event` is the portable wifi truth: nl80211's own account, same format for every driver."""

    def test_the_timestamp_and_text_are_separated(self):
        stamp = parse_event_line("[1759100000.123456] wlan0: disconnected")
        self.assertIsNotNone(stamp)
        self.assertAlmostEqual(float(stamp or 0), 1759100000.123456)
        self.assertIsNone(parse_event_line("garbage without a stamp"))

    def test_the_link_moves_we_care_about_are_recognised(self):
        for line in ("[1.0] wlan0: disconnected",
                     "[1.0] wlan0: deauthenticated",
                     "[1.0] wlan0: disassociated",
                     "[1.0] wlan0: new station 44:df:65:cd:00:00",
                     "[1.0] wlan0: associated",
                     "[1.0] wlan0: authenticated",
                     "[1.0] wlan0: roamed"):
            self.assertTrue(is_link_move(line), line)

    def test_noise_is_not_a_link_move(self):
        for line in ("[1.0] wlan0: scan started",
                     "[1.0] wlan0: regulatory domain change",
                     "[1.0] wlan0: connection failed"):
            self.assertFalse(is_link_move(line), line)

    def test_an_event_at_the_resume_convicts(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "wlan-events.log"
            log.write_text("2026-09-29 10:00:00  wlan-event  [1759100000.5] wlan0: disconnected\n",
                           encoding="utf-8")
            self.assertIsNotNone(wifi_moves_around_resume(log, 1759100000.0))
            # far away in time: somebody else's event, not this wake's cause
            self.assertIsNone(wifi_moves_around_resume(log, 1759109999.0))

    def test_a_missing_log_is_not_evidence(self):
        self.assertIsNone(wifi_moves_around_resume(Path("/nonexistent/wlan-events.log"), 1.0))


class TestTheWifiEventTap(unittest.TestCase):
    """`iw event` is the portable wifi witness; these lines are copied from the real box."""

    #: verbatim from `sudo iw event -t` on this machine
    REAL = (
        "1790738340.015097: wlp5s0 (phy #0): mgmt TX status (cookie 135): acked\n",
        "1790738412.501: wlp5s0 (phy #0): disconnected (by AP) reason: 3 (DEAUTH_LEAVING)\n",
        "1790738412.777: wlp5s0 (phy #0): new 44:df:65:cd:00:00 station became authorized\n",
        "1790738500.100: wlp5s0 (phy #0): scan started\n",
    )

    def test_a_real_mgmt_tx_line_parses(self):
        from autoresleep.iwevent import parse_event_line
        self.assertAlmostEqual(parse_event_line(self.REAL[0]), 1790738340.015097, places=3)

    def test_a_disconnect_is_a_link_move_but_a_scan_is_not(self):
        from autoresleep.iwevent import is_link_move
        self.assertTrue(is_link_move(self.REAL[1]), "a deauth is why the machine woke")
        self.assertTrue(is_link_move(self.REAL[2]), "re-association is the other half of the story")
        self.assertFalse(is_link_move(self.REAL[3]), "a scan we asked for is not a link move")
        self.assertFalse(is_link_move(self.REAL[0]), "a tx-status cookie is not a link move")

    def test_the_hello_line_is_not_an_event(self):
        from autoresleep.iwevent import parse_event_line, is_link_move
        hello = "# iw-event tap started 2026-09-30 11:13:31"
        self.assertIsNone(parse_event_line(hello))
        self.assertFalse(is_link_move(hello))

    def test_the_tap_says_it_is_up_even_with_no_events(self):
        """A wifi link that never drops produces no events at all, so the file must still exist -
        otherwise a reader cannot tell a healthy quiet tap from a broken one."""
        import tempfile
        from pathlib import Path as _P
        from autoresleep.iwevent import IwEventWatcher
        with tempfile.TemporaryDirectory() as td:
            log = _P(td) / "wlan-events.log"
            watcher = IwEventWatcher(log)
            watcher.proc = None
            watcher._append("# iw-event tap started 2026-09-30 11:13:31")
            self.assertTrue(log.exists())
            self.assertIn("tap started", log.read_text(encoding="utf-8"))

    def test_a_link_move_around_the_resume_is_evidence(self):
        import tempfile
        from pathlib import Path as _P
        from autoresleep.iwevent import wifi_moves_around_resume
        with tempfile.TemporaryDirectory() as td:
            log = _P(td) / "wlan-events.log"
            log.write_text(
                "2026-09-30 11:09:21  wlan-event  1790737761.5: wlp5s0 (phy #0): disconnected "
                "(by AP) reason: 3 (DEAUTH_LEAVING)\n", encoding="utf-8")
            hit = wifi_moves_around_resume(log, wake_epoch=1790737761.0)
            self.assertIsNotNone(hit)
            self.assertIn("disconnected", hit)

    def test_an_unrelated_event_is_not_evidence_for_this_wake(self):
        import tempfile
        from pathlib import Path as _P
        from autoresleep.iwevent import wifi_moves_around_resume
        with tempfile.TemporaryDirectory() as td:
            log = _P(td) / "wlan-events.log"
            log.write_text(
                "2026-09-30 08:00:00  wlan-event  1790726400.0: wlp5s0 (phy #0): disconnected\n",
                encoding="utf-8")
            self.assertIsNone(wifi_moves_around_resume(log, wake_epoch=1790737761.0))


class TestTheWifiEventTapCannotStallTheSensor(unittest.TestCase):
    """The regression that mattered most: a blocking read on the sensor's MAIN thread.

    `iw event` prints nothing until the link moves, and a pipe whose writer is silent blocks
    `readline()` forever. Measured on the real box with /proc/<pid>/stack: the sensor's main loop
    sat in `anon_pipe_read` and therefore stopped polling the UDP 9/7 and raw-0x0842 magic-packet
    sockets - a magic packet would have gone completely unseen. drain() must never block.
    """

    def _watcher_with_child(self, td, command):
        import subprocess
        from autoresleep.iwevent import IwEventWatcher
        watcher = IwEventWatcher(Path(td) / "wlan-events.log")
        watcher.proc = subprocess.Popen(["sh", "-c", command], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
        watcher._start_reader()
        return watcher

    def test_drain_returns_immediately_while_the_child_is_silent(self):
        import time as _time
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher_with_child(td, "sleep 30")
            self.addCleanup(watcher.proc.kill)
            start = _time.monotonic()
            self.assertEqual(watcher.drain(), 0)
            self.assertLess(_time.monotonic() - start, 0.3,
                            "drain() blocked on a silent pipe; the packet sockets would starve")

    def test_an_event_that_arrives_later_is_still_captured(self):
        import time as _time
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher_with_child(
                td, 'sleep 0.5; echo "1.5: wlp5s0 (phy #0): disconnected (by AP)"; sleep 30')
            self.addCleanup(watcher.proc.kill)
            self.assertEqual(watcher.drain(), 0, "nothing yet")
            _time.sleep(1.0)
            self.assertEqual(watcher.drain(), 1, "the reader thread must have picked it up")
            text = watcher.log_file.read_text(encoding="utf-8")
            self.assertIn("disconnected", text)

    def test_a_non_move_line_is_not_written(self):
        import time as _time
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher_with_child(
                td, 'sleep 0.4; echo "1.5: wlp5s0 (phy #0): scan started"; sleep 30')
            self.addCleanup(watcher.proc.kill)
            _time.sleep(1.0)
            self.assertEqual(watcher.drain(), 0, "a scan we asked for is not a link move")


class TestRawMagicFrames(unittest.TestCase):
    """A magic packet also travels as a bare 0x0842 ethernet frame - no IP, no UDP at all."""

    MAC = "28:d0:ea:27:40:0c"

    def _frame(self, mac_hex="28d0ea27400c", payload=None):
        dst = bytes.fromhex("ffffffffffff")
        src = bytes.fromhex("aabbccddeeff")
        ethertype = bytes.fromhex("0842")
        body = payload if payload is not None else (b"\xff" * 6 + bytes.fromhex(mac_hex) * 16)
        return dst + src + ethertype + body

    def test_the_ethernet_layout_is_parsed_and_the_frame_is_recognised(self):
        seen = RawMagicSocket.parse_frame(self._frame(), [self.MAC])
        self.assertIsNotNone(seen)
        assert seen is not None
        self.assertEqual(seen.kind, "magic")
        self.assertIn("raw:0x0842", seen.src)
        self.assertIn("aa:bb:cc:dd:ee:ff", seen.src)
        self.assertEqual(seen.length, 102)

    def test_a_frame_for_another_mac_is_not_ours(self):
        self.assertIsNone(RawMagicSocket.parse_frame(self._frame("001122334455"), [self.MAC]))

    def test_a_truncated_frame_is_ignored_rather_than_crashing(self):
        self.assertIsNone(RawMagicSocket.parse_frame(b"\xff" * 10, [self.MAC]))

    def test_the_udp_and_frame_paths_agree_on_what_a_magic_packet_is(self):
        payload = b"\xff" * 6 + bytes.fromhex("28d0ea27400c") * 16
        self.assertTrue(is_magic_packet(payload, [self.MAC]))

    def test_no_capability_is_reported_not_assumed(self):
        sock = RawMagicSocket(macs=[self.MAC])
        with mock.patch("socket.socket", side_effect=PermissionError(1, "Operation not permitted")):
            self.assertFalse(sock.bind())
        self.assertIsNotNone(sock.advice())
        self.assertIn("CAP_NET_RAW", sock.advice() or "")


class TestTheHardwareInputClock(unittest.TestCase):
    """evdev is the only clock a resume cannot fake; the desktop counter is reset to zero by it."""

    DEVICES = """I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
H: Handlers=sysrq kbd event3 leds
B: EV=120013
B: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe

I: Bus=0019 Vendor=0000 Product=0006 Version=0000
N: Name="Video Bus"
H: Handlers=kbd event6
B: EV=3
B: KEY=3e000b00000000 0 0 0

I: Bus=0003 Vendor=046d Product=c52b Version=0111
N: Name="Logitech USB Receiver Mouse"
H: Handlers=mouse0 event2
B: EV=17
B: KEY=ffff0000 0 0 0 0 0 0 0 0

I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
H: Handlers=kbd event5
B: EV=3
B: KEY=10000000000000 0 0 0
"""

    def _devices_file(self, td):
        p = Path(td) / "devices"
        p.write_text(self.DEVICES, encoding="utf-8")
        return str(p)

    def test_only_human_capable_devices_are_watched(self):
        with tempfile.TemporaryDirectory() as td:
            found = discover_devices(self._devices_file(td))
        self.assertEqual(found.get("event3"), "AT Translated Set 2 keyboard")
        self.assertEqual(found.get("event2"), "Logitech USB Receiver Mouse")

    def test_an_acpi_power_button_is_not_a_person(self):
        """Measured: `rtcwake -m mem -s 20` (RTC wake, nobody in the room) delivered KEY_POWER
        from this exact device two seconds before the resume. Counting it latched "keyboard woke
        it", and a wifi-disconnect wake was then exempt from re-sleeping for the whole session."""
        with tempfile.TemporaryDirectory() as td:
            found = discover_devices(self._devices_file(td))
        self.assertNotIn("event5", found, "an ACPI button must not be part of the human clock")

    @POSIX_PIPE_ONLY
    def test_a_system_key_code_is_not_a_keypress(self):
        """Drive the real poll() through a pipe: KEY_POWER is ignored, a real key is not.

        Measured on the box: `rtcwake -m mem -s 20` (RTC wake, nobody present) delivered
        KEY_POWER from the "Power Button" device at the resume, the clock called it a keypress,
        and the session latched "keyboard woke it" - which exempted a wifi-disconnect wake from
        re-sleeping for the whole session.
        """
        from autoresleep import inputwatch as iw
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "input-last.json"
            watcher = iw.InputWatcher(state)
            r, w = os.pipe()
            self.addCleanup(lambda: (os.close(r), os.close(w)))
            watcher.fds = {r: "Power Button"}

            os.write(w, iw.EVENT.pack(1_700_000_000, 0, iw.EV_KEY, 116, 1))   # KEY_POWER
            self.assertEqual(watcher.poll(timeout=0.2), 0,
                             "KEY_POWER must not count as human input")
            self.assertIsNone(watcher._read_state().get("epoch"),
                              "a system key must not move the clock")

            os.write(w, iw.EVENT.pack(1_700_000_123, 0, iw.EV_KEY, 30, 1))    # KEY_A
            self.assertEqual(watcher.poll(timeout=0.2), 1, "a real key still counts")
            self.assertAlmostEqual(watcher._read_state()["epoch"], 1_700_000_123.0, places=3)

    def test_the_video_bus_is_not_a_person(self):
        # every box has one, and counting it would make every resume look like a keypress
        with tempfile.TemporaryDirectory() as td:
            found = discover_devices(self._devices_file(td))
        self.assertNotIn("event6", found)

    def test_a_live_watcher_that_saw_nothing_is_a_hard_fact(self):
        # no event ever + fresh heartbeat = nobody touched it (a wifi wake), NOT "cannot tell"
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "input-last.json"
            state.write_text(json.dumps({"beat": time.time(), "epoch": None, "dev": ""}),
                             encoding="utf-8")
            epoch, _dev, live = read_last_input(state)
        self.assertTrue(live)
        self.assertIsNone(epoch)

    def test_a_stale_watcher_is_not_a_claim_about_the_machine(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "input-last.json"
            state.write_text(json.dumps({"beat": time.time() - 3600, "epoch": None}),
                             encoding="utf-8")
            _e, _d, live = read_last_input(state)
        self.assertFalse(live)

    def test_a_missing_file_reads_as_no_watcher(self):
        self.assertFalse(read_last_input(Path("/nonexistent/input-last.json"))[2])

    def test_an_event_writes_its_epoch_and_a_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "input-last.json"
            watcher = InputWatcher(state, devices_path=self._devices_file(td))
            watcher.fds = {}
            watcher.touch_beat(force=True)
            self.assertTrue(state.exists())
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertIn("beat", payload)

    def test_polling_with_no_devices_still_keeps_the_heartbeat_alive(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "input-last.json"
            watcher = InputWatcher(state, devices_path=self._devices_file(td))
            watcher.fds = {}
            watcher.poll(timeout=0.01)
            _e, _d, live = read_last_input(state)
        self.assertTrue(live)


@POSIX_PIPE_ONLY      # every test here feeds poll() through an os.pipe()
class TestTheBusResetReplayIsNotAPerson(unittest.TestCase):
    """Measured: a purely RTC wake with nobody in the room made the mouse emit 20+ REL events in
    the resume second, wheel codes alternating (8, 11, 8, 11...). Those events latched "keyboard
    woke it" and stopped the machine re-sleeping. Motion inside the settle window is ignored; a
    keypress is never ignored, because a real keypress at the wake is what the exemption is for.
    """

    def _watcher(self, td):
        from autoresleep import inputwatch as iw
        w = iw.InputWatcher(Path(td) / "input-last.json")
        r, wfd = os.pipe()
        self.addCleanup(lambda: (os.close(r), os.close(wfd)))
        w.fds = {r: "Logitech G502 HERO Gaming Mouse"}
        return w, wfd

    def test_motion_right_after_a_resume_is_ignored(self):
        from autoresleep import inputwatch as iw
        with tempfile.TemporaryDirectory() as td:
            watcher, wfd = self._watcher(td)
            watcher.mark_resume()
            for code in (8, 11, 8, 11, 1, 0):
                os.write(wfd, iw.EVENT.pack(1_700_000_000, 0, iw.EV_REL, code, 1))
            self.assertEqual(watcher.poll(timeout=0.3), 0, "device replay must not count")
            self.assertIsNone(watcher._read_state().get("epoch"),
                              "device replay must not move the clock")

    def test_a_keypress_right_after_a_resume_still_counts(self):
        from autoresleep import inputwatch as iw
        with tempfile.TemporaryDirectory() as td:
            watcher, wfd = self._watcher(td)
            watcher.mark_resume()
            os.write(wfd, iw.EVENT.pack(1_700_000_123, 0, iw.EV_KEY, 30, 1))
            self.assertEqual(watcher.poll(timeout=0.3), 1,
                             "a real keypress at the wake is exactly the exemption")

    def test_motion_outside_the_window_counts_again(self):
        from autoresleep import inputwatch as iw
        with tempfile.TemporaryDirectory() as td:
            watcher, wfd = self._watcher(td)
            watcher.mark_resume(settle_s=0.0)          # window already over
            os.write(wfd, iw.EVENT.pack(1_700_000_123, 0, iw.EV_REL, 0, 5))
            self.assertEqual(watcher.poll(timeout=0.3), 1, "a hand moving later is a person")

    def test_the_ignored_events_are_still_audited(self):
        from autoresleep import inputwatch as iw
        with tempfile.TemporaryDirectory() as td:
            watcher, wfd = self._watcher(td)
            watcher.mark_resume()
            os.write(wfd, iw.EVENT.pack(1_700_000_000, 0, iw.EV_REL, 8, 1))
            watcher.poll(timeout=0.3)
            text = watcher.audit_file.read_text(encoding="utf-8")
            self.assertIn("settling:ignored", text,
                          "the audit trail must show what was discarded and why")


class TestWhichClockDecides(unittest.TestCase):
    """The regression: the fallback clock must never latch a session-wide exemption."""

    NOW = 10000.0

    def test_the_hardware_clock_names_itself_in_the_decision(self):
        ev = Evidence(input_source="evdev")
        d = decide(ev, Guards(), idle_s=600.0, params=Params(resleep_break_on_input=True),
                   now=self.NOW, wake_by_input=True)
        self.assertIs(d.action, Action.SKIP)
        self.assertEqual(d.reason, "wake-by-input")
        self.assertIn("evdev", d.who or "")

    def test_the_fallback_clock_still_skips_but_says_it_is_the_fallback(self):
        ev = Evidence(input_source="x11-idle")
        d = decide(ev, Guards(), idle_s=600.0, params=Params(resleep_break_on_input=True),
                   now=self.NOW, wake_by_input=True)
        self.assertIs(d.action, Action.SKIP)
        self.assertIn("x11-idle", d.who or "")

    def test_a_driver_string_never_becomes_a_sleep_decision(self):
        # item 4: the driver may not convict, whatever it says
        for reason in ("NON_MAGIC_AP_ASSOC_LOST", "AP_DISASSOC", "ROAM", "GTK_REKEY",
                       "MEDIA_DISCONNECT", "4-Way Handshake"):
            d = decide(Evidence(drv_reason=reason, drv_age_s=5.0), Guards(), idle_s=600.0,
                       params=Params(unknown_idle_seconds=900), now=self.NOW)
            self.assertNotEqual(d.action, Action.SLEEP, reason)
            self.assertNotEqual(d.src, "drv", reason)

    def test_the_nic_caused_wake_still_sleeps_when_established_driver_free(self):
        ev = Evidence(os_link="LINK_LOST", os_link_why="wlan-event-at-resume:disconnected")
        d = decide(ev, Guards(), idle_s=600.0, params=Params(), now=self.NOW,
                   input_since_sleep=False)
        self.assertIs(d.action, Action.SLEEP)
        self.assertEqual(d.reason, "NON_MAGIC_WLAN_LINK_MOVED")


class TestTheWowlanWakeupReport(unittest.TestCase):
    """`iw event` names the wake CAUSE - the one question a per-device counter cannot answer.

    Verbatim from the dev box (2026-10-16, `sudo iw event -t -f`), two wakes in a row: a real magic
    packet from the phone, then a wake the NIC itself says was not wireless at all.
    """

    MAGIC = ["1792130691.782796: wlan0 (phy #0): WoWLAN wakeup\n",
             "\t* magic packet received\n",
             "\t* packet (might be truncated): ff:ff:ff:ff:ff:ff 96:56:06:9b:a8:24 "
             "00:60:17:01:c0:a8:00:8c:c0:a8:00:ff:00:09:ff:ff:ff:ff:ff:ff 28:d0:ea:27:40:0c\n"]
    NOT_WIRELESS = ["1792130720.783661: wlan0 (phy #0): WoWLAN wakeup\n",
                    "\twakeup not due to WoWLAN\n"]

    def test_the_header_is_recognised_and_the_bullets_are_not_events(self):
        from autoresleep.iwevent import is_wowlan_header
        self.assertTrue(is_wowlan_header(self.MAGIC[0]))
        self.assertTrue(is_wowlan_header(self.NOT_WIRELESS[0]))
        # a bullet line carries no timestamp of its own: that is how the block is delimited, and
        # why it can never be mistaken for an event
        self.assertIsNone(parse_event_line(self.MAGIC[1]))
        self.assertFalse(is_link_move(self.MAGIC[1]))
        self.assertFalse(is_wowlan_header("1792130691.9: wlan0 (phy #0): disconnected (by AP)"))

    def test_a_real_magic_packet_block_reads_as_magic(self):
        from autoresleep.iwevent import classify_wowlan
        self.assertEqual(classify_wowlan([l.strip() for l in self.MAGIC[1:]]), "magic")

    def test_a_wake_the_nic_denies_is_named_as_not_wireless(self):
        from autoresleep.iwevent import classify_wowlan
        self.assertEqual(classify_wowlan([l.strip() for l in self.NOT_WIRELESS[1:]]),
                         "not-wowlan")

    def test_other_causes_are_recorded_but_never_exempting(self):
        from autoresleep.iwevent import classify_wowlan
        self.assertEqual(classify_wowlan(["* disconnect detected"]), "other:disconnect")
        self.assertEqual(classify_wowlan(["* GTK rekeying failure"]), "other:gtk")
        self.assertEqual(classify_wowlan(["something this project never saw"]), "other")
        self.assertEqual(classify_wowlan([]), "other")

    def test_a_row_survives_the_round_trip_and_respects_its_window(self):
        from autoresleep.iwevent import parse_wowlan_row, last_wowlan
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "wowlan-events.log"
            log.write_text(
                "# wowlan tap started 2026-10-16 14:04:00\n"
                "2026-10-16 14:04:51  wowlan  kind=magic epoch=1792130691.782796 detail=wlan0 "
                "(phy #0): WoWLAN wakeup | * magic packet received\n"
                "2026-10-16 14:05:20  wowlan  kind=not-wowlan epoch=1792130720.783661 "
                "detail=wlan0 (phy #0): WoWLAN wakeup | wakeup not due to WoWLAN\n",
                encoding="utf-8")
            self.assertIsNone(parse_wowlan_row("# wowlan tap started 2026-10-16 14:04:00"))
            self.assertEqual(last_wowlan(log)["kind"], "not-wowlan")
            self.assertEqual(last_wowlan(log, max_age_s=1.0, now=1792130721.0)["kind"],
                             "not-wowlan")
            self.assertIsNone(last_wowlan(log, max_age_s=1.0, now=1792130800.0),
                              "an hour old: not evidence about anything happening now")
            self.assertIsNone(last_wowlan(log, max_age_s=30.0, now=1792130700.0),
                              "stamped ahead of now: a clock we cannot line up, not evidence")


class TestTheWowlanTapReadsTheWholeBlock(unittest.TestCase):
    """The bullets arrive after the header and carry no timestamp: read them together or lose them.

    Measured defect in the first version of the reader: it read the pipe through the text wrapper and
    then asked `select` whether more bytes had arrived. The burst was already sitting in Python's
    buffer, `select` said "nothing", and every report came out `kind=other` - a magic-packet wake
    would have been re-slept with a log line that looked like it had done its job. Hence: read the
    FD directly and keep our own buffer.

    And the line that ENDS the block is a real event, so it must be processed, not swallowed.
    """

    def _watcher_over(self, td, command):
        import subprocess
        from autoresleep.iwevent import IwEventWatcher
        watcher = IwEventWatcher(Path(td) / "wlan-events.log",
                                 wowlan_file=Path(td) / "wowlan-events.log")
        watcher.proc = subprocess.Popen(["sh", "-c", command], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.addCleanup(watcher.proc.kill)
        watcher._start_reader()
        return watcher

    @staticmethod
    def _wait_for(predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_the_report_is_filed_and_the_line_after_it_is_still_an_event(self):
        from autoresleep.iwevent import read_wowlan_log
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher_over(
                td,
                'printf "1792130691.782796: wlan0 (phy #0): WoWLAN wakeup\\n";'
                'printf "\\t* magic packet received\\n";'
                'printf "1792130691.9: wlan0 (phy #0): disconnected (by AP) reason 3\\n";'
                'sleep 30')
            link_log = Path(td) / "wlan-events.log"
            wow_log = Path(td) / "wowlan-events.log"
            self.assertTrue(self._wait_for(
                lambda: read_wowlan_log(wow_log) and link_log.exists()
                and "disconnected" in link_log.read_text(encoding="utf-8")),
                "block not filed, or the following event was swallowed")
            rows = read_wowlan_log(wow_log)
            self.assertEqual([r["kind"] for r in rows], ["magic"])
            self.assertAlmostEqual(float(rows[0]["epoch"]), 1792130691.782796, places=4)
            self.assertIn("magic packet received", str(rows[0]["detail"]))
            # the report is also handed to the sensor, exactly once
            drained = watcher.take_wowlan()
            self.assertEqual([r["kind"] for r in drained], ["magic"])
            self.assertEqual(watcher.take_wowlan(), [])

    def test_a_non_wireless_wake_is_recorded_and_queued_too(self):
        from autoresleep.iwevent import read_wowlan_log
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher_over(
                td,
                'printf "1792130720.783661: wlan0 (phy #0): WoWLAN wakeup\\n";'
                'printf "\\twakeup not due to WoWLAN\\n"; sleep 30')
            wow_log = Path(td) / "wowlan-events.log"
            self.assertTrue(self._wait_for(lambda: read_wowlan_log(wow_log)))
            self.assertEqual(read_wowlan_log(wow_log)[0]["kind"], "not-wowlan")
            self.assertEqual(watcher.take_wowlan()[0]["kind"], "not-wowlan")

    def test_a_link_move_alone_still_never_touches_the_wowlan_log(self):
        from autoresleep.iwevent import read_wowlan_log
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher_over(
                td, 'printf "1792130691.9: wlan0 (phy #0): disconnected (by AP)\\n"; sleep 30')
            link_log = Path(td) / "wlan-events.log"
            self.assertTrue(self._wait_for(
                lambda: link_log.exists() and "disconnected" in link_log.read_text(
                    encoding="utf-8")))
            self.assertEqual(read_wowlan_log(Path(td) / "wowlan-events.log"), [])
            self.assertEqual(watcher.take_wowlan(), [])


class TestTheSensorUnitGetsThePrivilegesItNeeds(unittest.TestCase):
    """Both new listeners need one privilege each - the unit must grant exactly those."""

    def test_the_unit_grants_raw_sockets_and_the_input_group(self):
        from autoresleep.scheduling import artifacts_for
        arts = artifacts_for("linux", "/usr/bin/python3", "/work", 2, user="jianghh",
                               config_path=None)
        sensor = [a for a in arts if "sensor" in a.name]
        self.assertTrue(sensor, "the privileged sensor unit must exist")
        body = sensor[0].content
        self.assertIn("CAP_NET_RAW", body)                 # bare 0x0842 magic frames
        self.assertIn("CAP_NET_BIND_SERVICE", body)        # UDP 9/7
        self.assertIn("SupplementaryGroups=input", body)   # /dev/input/event*
        self.assertIn("wol-sensor", body)
        # and it must still not be a root process: the operator's own uid runs it
        self.assertIn("User=", body)
        self.assertNotIn("User=root", body)


if __name__ == "__main__":
    unittest.main()