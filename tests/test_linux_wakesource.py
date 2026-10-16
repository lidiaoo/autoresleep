"""Linux wake attribution: the wired-NIC / per-device-wakeup-event chains.

Everything pure is tested against REAL data lifted from the dev box (CachyOS, 2026-10-16):
the /proc/interrupts line of the r8169 wired card names the NETDEV ("enp45s0") while the
iwlwifi queues name the driver - and the ACPI SCI is just "acpi", which is why the
per-device wakeup counters exist as the last-resort chain. The flow test patches the
sysfs-reading helpers so no test depends on the machine it runs on.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.system.linux import (  # noqa: E402
    LinuxSystem, WAKEUP_EVENT_WINDOW_S,
)


class TestParseIrqLine(unittest.TestCase):
    # verbatim from /proc/interrupts on the dev box (columns trimmed, format intact)
    LINE_ETH = (" 180:          0    105091  IR-PCI-MSIX-0000:2d:00.0    0-edge      enp45s0")
    LINE_WLAN = (" 182:        5652          0  IR-PCI-MSIX-0000:00:14.3    1-edge      iwlwifi:queue_1")
    LINE_ACPI = ("   9:     226986          0  IR-IO-APIC                  9-fasteoi   acpi")
    LINE_RTC = ("   8:          0          0  IR-IO-APIC                  8-edge      rtc0")
    BOTH = "\n".join([LINE_ETH, LINE_WLAN, LINE_ACPI, LINE_RTC])

    def test_wired_nic_line_names_the_netdev_and_the_pci_address(self):
        actions, pci = LinuxSystem.parse_irq_line(self.BOTH, "180")
        self.assertEqual(actions, "enp45s0")
        self.assertEqual(pci, "0000:2d:00.0")

    def test_wireless_line_names_the_driver(self):
        actions, pci = LinuxSystem.parse_irq_line(self.BOTH, "182")
        self.assertEqual(actions, "iwlwifi:queue_1")
        self.assertEqual(pci, "0000:00:14.3")

    def test_acpi_sci_names_nothing_but_itself(self):
        actions, pci = LinuxSystem.parse_irq_line(self.BOTH, "9")
        self.assertEqual(actions, "acpi")
        self.assertEqual(pci, "")

    def test_rtc_line_is_found(self):
        actions, _ = LinuxSystem.parse_irq_line(self.BOTH, "8")
        self.assertEqual(actions, "rtc0")

    def test_missing_irq_is_empty_both_ways(self):
        self.assertEqual(LinuxSystem.parse_irq_line(self.BOTH, "999"), ("", ""))
        self.assertEqual(LinuxSystem.parse_irq_line("", "9"), ("", ""))


class TestNicKindFromTokens(unittest.TestCase):
    WIFI = ["wlan0"]
    ETH = ["enp45s0", "enp0s20f0u2"]

    def test_irq_action_named_after_the_netdev(self):
        # the r8169 case: MSI-X actions are "enp45s0" - no driver name anywhere in the string
        self.assertEqual(LinuxSystem.nic_kind_from_tokens("enp45s0", self.WIFI, self.ETH),
                         "eth-nic")

    def test_driver_names(self):
        self.assertEqual(LinuxSystem.nic_kind_from_tokens("iwlwifi:default_queue", [], []),
                         "wifi-nic")
        self.assertEqual(LinuxSystem.nic_kind_from_tokens("r8169", [], []), "eth-nic")
        self.assertEqual(LinuxSystem.nic_kind_from_tokens("e1000e", [], []), "eth-nic")

    def test_non_nic_tokens_are_none(self):
        for tok in ("acpi", "nvme0q2", "xhci_hcd", "dmar1", "i8042", ""):
            self.assertIsNone(LinuxSystem.nic_kind_from_tokens(tok, self.WIFI, self.ETH), tok)

    def test_netdev_names_beat_nothing_but_lose_to_drivers(self):
        self.assertEqual(LinuxSystem.nic_kind_from_tokens("wlan0", self.WIFI, self.ETH),
                         "wifi-nic")
        # a queue suffix keeps the interface prefix matchable
        self.assertEqual(LinuxSystem.nic_kind_from_tokens("wlan0:tx", self.WIFI, self.ETH),
                         "wifi-nic")


class TestPickWakeupNic(unittest.TestCase):
    NOW_MS = 1000.0 * 1000.0 * 1000.0          # arbitrary monotonic ms

    def rows(self, *entries):
        return [{"iface": i, "kind": k, "last_ms": m, "active": 1} for i, k, m in entries]

    def test_newest_event_inside_the_window_wins(self):
        rows = self.rows(("wlan0", "wifi", self.NOW_MS - 40_000),        # 40 s ago
                         ("enp45s0", "eth", self.NOW_MS - 7_200_000))    # 2 h ago
        hit = LinuxSystem.pick_wakeup_nic(rows, self.NOW_MS)
        self.assertEqual(hit["iface"], "wlan0")
        self.assertAlmostEqual(hit["age_s"], 40.0)

    def test_everything_stale_is_none(self):
        rows = self.rows(("wlan0", "wifi", self.NOW_MS - int((WAKEUP_EVENT_WINDOW_S + 60) * 1000)))
        self.assertIsNone(LinuxSystem.pick_wakeup_nic(rows, self.NOW_MS))

    def test_zero_and_future_timestamps_never_win(self):
        rows = self.rows(("wlan0", "wifi", 0),
                         ("enp45s0", "eth", self.NOW_MS + 5_000))        # clock skew, future
        self.assertIsNone(LinuxSystem.pick_wakeup_nic(rows, self.NOW_MS))

    def test_garbage_rows_are_tolerated(self):
        rows = [{"iface": "x", "kind": "eth", "last_ms": None},
                {"iface": "y", "kind": "wifi"}]
        self.assertIsNone(LinuxSystem.pick_wakeup_nic(rows, self.NOW_MS))
        self.assertIsNone(LinuxSystem.pick_wakeup_nic([], self.NOW_MS))

    def test_window_boundary_is_inclusive(self):
        rows = self.rows(("wlan0", "wifi", self.NOW_MS - int(WAKEUP_EVENT_WINDOW_S * 1000)))
        hit = LinuxSystem.pick_wakeup_nic(rows, self.NOW_MS)
        self.assertIsNotNone(hit)


class TestWakeupAttrs(unittest.TestCase):
    def test_reads_a_real_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "power")
            p.mkdir()
            (p / "wakeup_last_time_ms").write_text("4078414\n")
            self.assertEqual(LinuxSystem._wakeup_attrs(tmp), 4078414)

    def test_zero_means_never_fired(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "power")
            p.mkdir()
            (p / "wakeup_last_time_ms").write_text("0\n")
            self.assertIsNone(LinuxSystem._wakeup_attrs(tmp))

    def test_missing_and_garbage_are_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(LinuxSystem._wakeup_attrs(tmp))
            p = Path(tmp, "power")
            p.mkdir()
            (p / "wakeup_last_time_ms").write_text("not-a-number\n")
            self.assertIsNone(LinuxSystem._wakeup_attrs(tmp))


class TestWakeSourceFlow(unittest.TestCase):
    """The ordering contract: journal first, IRQ second, counters last - and the counters only
    ever refine a WEAK verdict (unknown / other-device-irq), never a named one."""

    @staticmethod
    def _fake_read_text(wakeup_irq=""):
        """Path.read_text replacement: the wakeup-IRQ file answers, every other sysfs read acts
        unreadable - so a test never depends on the machine it runs on."""
        def fake(self, *args, **kwargs):
            if str(self) == "/sys/power/pm_wakeup_irq":
                return wakeup_irq
            raise OSError("no sysfs in tests")
        return fake

    def _sys(self, wakeup_irq=""):
        ctxs = [mock.patch("shutil.which", return_value=None),
                # Never read the machine's own state dir: the dev box has a real wowlan log, and a
                # test whose verdict depends on what happened on this computer yesterday is not a
                # test. The denial path has its own test below.
                mock.patch.object(LinuxSystem, "_wowlan_says_not_wireless", return_value=False),
                mock.patch.object(Path, "read_text", autospec=True,
                                  side_effect=self._fake_read_text(wakeup_irq))]
        for c in ctxs:
            c.start()
            self.addCleanup(c.stop)
        return LinuxSystem()

    def test_no_raw_no_irq_plus_recent_nic_event_is_attributed(self):
        s = self._sys()
        hit = ("wifi-nic", "wakeup-event:wlan0@42s", {"iface": "wlan0", "last_ms": 1,
                                                      "active": 2, "age_s": 42.0})
        with mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=hit):
            src = s.wake_source()
        self.assertEqual(src.kind, "wifi-nic")
        self.assertEqual(src.who, "wakeup-event:wlan0@42s")
        self.assertIn("netdev-wakeup:wlan0", src.raw)

    def test_no_raw_no_irq_and_no_event_stays_unknown(self):
        s = self._sys()
        with mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=None):
            src = s.wake_source()
        self.assertEqual(src.kind, "unknown")

    def test_acpi_irq_is_refined_by_the_counters(self):
        # the x86 trap: IRQ 9 = "acpi" names no device; the per-device counters must get the say
        s = self._sys(wakeup_irq="9\n")
        hit = ("eth-nic", "wakeup-event:enp45s0@7s", {"iface": "enp45s0", "last_ms": 2,
                                                      "active": 1, "age_s": 7.0})
        with mock.patch.object(LinuxSystem, "_irq_identity", return_value=("acpi", "")), \
             mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=hit):
            src = s.wake_source()
        self.assertEqual(src.kind, "eth-nic")
        self.assertEqual(src.who, "wakeup-event:enp45s0@7s")

    def test_acpi_irq_without_events_falls_back_to_other_device(self):
        s = self._sys(wakeup_irq="9\n")
        with mock.patch.object(LinuxSystem, "_irq_identity", return_value=("acpi", "")), \
             mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=None):
            src = s.wake_source()
        self.assertEqual(src.kind, "other-device")
        self.assertEqual(src.who, "irq:9")

    def test_pci_address_in_the_irq_line_names_the_wired_card_directly(self):
        s = self._sys(wakeup_irq="180\n")
        with mock.patch.object(LinuxSystem, "_irq_identity",
                               return_value=("enp45s0", "0000:2d:00.0")), \
             mock.patch.object(LinuxSystem, "_netdev_for_pci",
                               return_value=("enp45s0", "eth")):
            src = s.wake_source()
        self.assertEqual(src.kind, "eth-nic")
        self.assertEqual(src.who, "irq:180:enp45s0")

    def test_journal_naming_usb_stays_decisive_over_the_counters(self):
        s = self._sys()
        hit = ("eth-nic", "wakeup-event:enp45s0@7s", {"iface": "enp45s0", "last_ms": 3,
                                                      "active": 1, "age_s": 7.0})
        with mock.patch("shutil.which", return_value="journalctl"), \
             mock.patch("autoresleep.system.linux._run",
                        return_value="x Wakeup source: USB x"), \
             mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=hit) as counters:
            src = s.wake_source()
        self.assertEqual(src.kind, "other-device")
        self.assertEqual(src.who, "usb/hid")
        counters.assert_not_called()

    def test_timer_line_stays_timer(self):
        s = self._sys()
        with mock.patch("shutil.which", return_value="journalctl"), \
             mock.patch("autoresleep.system.linux._run",
                        return_value="x ACPI: PM: Wakeup source: rtc0 x"):
            src = s.wake_source()
        self.assertEqual(src.kind, "timer")
        self.assertTrue(src.timer)

    def test_a_driver_denial_keeps_the_wifi_counter_out_of_the_verdict(self):
        """`wakeup not due to WoWLAN` -> the counter hit on wlan0 is not allowed to name it.

        Measured 2026-10-16 14:05: a wake the NIC itself denied being wireless was filed as
        `kind=wifi-nic who=wakeup-event:wlan0@14s`.
        """
        s = self._sys()
        with mock.patch.object(LinuxSystem, "_wowlan_says_not_wireless", return_value=True), \
             mock.patch.object(LinuxSystem, "_wakeup_event_nic",
                               return_value=None) as counters:
            src = s.wake_source()
        self.assertEqual(src.kind, "unknown")
        counters.assert_called_with(ignore_wifi=True)
        self.assertIn("wowlan:not-wowlan", src.method)

    def test_without_a_denial_the_wifi_counter_keeps_its_voice(self):
        s = self._sys()
        hit = ("wifi-nic", "wakeup-event:wlan0@42s", {"iface": "wlan0", "last_ms": 1,
                                                      "active": 2, "age_s": 42.0})
        with mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=hit) as counters:
            src = s.wake_source()
        self.assertEqual(src.kind, "wifi-nic")
        counters.assert_called_with(ignore_wifi=False)
        self.assertNotIn("wowlan:not-wowlan", src.method)


class TestTheWifiDenialComesFromTheSensorLog(unittest.TestCase):
    """The report the sensor's `iw event` tap wrote - read here, since the judge cannot run `iw`."""

    def _log(self, td, kind, epoch):
        log = Path(td) / "wowlan-events.log"
        log.write_text("2026-10-16 14:05:20  wowlan  kind=%s epoch=%.6f detail=wlan0 (phy #0): "
                       "WoWLAN wakeup | %s\n" % (kind, epoch, kind), encoding="utf-8")
        return log

    def test_a_fresh_denial_is_believed_and_an_old_one_is_not(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            log = self._log(td, "not-wowlan", now)
            with mock.patch("autoresleep.paths.wowlan_event_log", return_value=log):
                self.assertTrue(LinuxSystem._wowlan_says_not_wireless(now=now + 1))
                self.assertFalse(
                    LinuxSystem._wowlan_says_not_wireless(now=now + WAKEUP_EVENT_WINDOW_S + 5),
                    "a report from five minutes ago describes a different wake")

    def test_only_a_denial_counts_never_a_magic_report(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            log = self._log(td, "magic", now)
            with mock.patch("autoresleep.paths.wowlan_event_log", return_value=log):
                self.assertFalse(LinuxSystem._wowlan_says_not_wireless(now=now + 1))

    def test_no_sensor_no_log_is_not_a_denial(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("autoresleep.paths.wowlan_event_log",
                            return_value=Path(td) / "wowlan-events.log"):
                self.assertFalse(LinuxSystem._wowlan_says_not_wireless(now=time.time()))

    def test_an_unreadable_log_is_not_a_denial_either(self):
        with mock.patch("autoresleep.paths.wowlan_event_log",
                        side_effect=OSError("no home directory")):
            self.assertFalse(LinuxSystem._wowlan_says_not_wireless(now=time.time()))

    def test_the_denial_drops_only_the_wireless_candidate(self):
        now_ms = 1000.0 * 1000.0 * 1000.0
        s = LinuxSystem()
        rows = [{"iface": "wlan0", "kind": "wifi", "last_ms": now_ms - 1000, "active": 3},
                {"iface": "enp45s0", "kind": "eth", "last_ms": now_ms - 1500, "active": 1}]
        with mock.patch.object(LinuxSystem, "_wakeup_event_rows", return_value=rows), \
             mock.patch("autoresleep.system.linux.time.monotonic", return_value=now_ms / 1000.0):
            self.assertEqual(s._wakeup_event_nic()[0], "wifi-nic")
            self.assertEqual(s._wakeup_event_nic(ignore_wifi=True)[0], "eth-nic")
        with mock.patch.object(LinuxSystem, "_wakeup_event_rows", return_value=rows[:1]), \
             mock.patch("autoresleep.system.linux.time.monotonic", return_value=now_ms / 1000.0):
            self.assertIsNone(s._wakeup_event_nic(ignore_wifi=True),
                              "denied and nothing else left: no attribution at all")


class TestTheWowlanChannelCanBeSwitchedOff(unittest.TestCase):
    """`config.wowlan_wake_reason` (shipped: true). Off must mean "the file is never consulted",
    not "the file is read and then ignored" - a row written before the switch was flipped would
    otherwise keep suppressing the wifi counter for another five minutes."""

    def test_a_switched_off_channel_ignores_a_fresh_denial(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "wowlan-events.log"
            log.write_text("2026-10-16 14:05:20  wowlan  kind=not-wowlan epoch=%.6f detail=denial\n"
                           % now, encoding="utf-8")
            with mock.patch("autoresleep.paths.wowlan_event_log", return_value=log):
                self.assertTrue(LinuxSystem._wowlan_says_not_wireless(now=now + 1),
                                "on (the default): the denial is read")
                self.assertFalse(LinuxSystem._wowlan_says_not_wireless(now=now + 1, enabled=False),
                                 "off: not read at all")

    def test_the_switch_reaches_the_platform_layer_from_the_config(self):
        from autoresleep.config import Config
        self.assertTrue(LinuxSystem().wowlan_wake_reason,
                        "no config (installer probe, unit test) = the shipped behaviour")
        self.assertTrue(LinuxSystem(config=Config()).wowlan_wake_reason)
        self.assertFalse(LinuxSystem(config=Config(wowlan_wake_reason=False)).wowlan_wake_reason)
        self.assertTrue(LinuxSystem(config=Config.load(Path("/nonexistent/config.yaml")))
                        .wowlan_wake_reason, "an unreadable file must fall back to the default")

    def test_wake_source_asks_the_switch_before_the_channel(self):
        s = LinuxSystem(config=type("Cfg", (), {"wowlan_wake_reason": False})())
        with mock.patch.object(LinuxSystem, "_wowlan_says_not_wireless") as denial, \
             mock.patch.object(LinuxSystem, "_wakeup_event_nic", return_value=None), \
             mock.patch("autoresleep.system.linux._run", return_value=""), \
             mock.patch("shutil.which", return_value=None):
            s.wake_source()
        self.assertEqual(denial.call_args.kwargs.get("enabled"), False,
                         "the switch is handed to the reader, not checked after it")


if __name__ == "__main__":
    unittest.main()
