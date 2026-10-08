"""`wake_nics`: which NICs are policed, and which MACs count as a magic packet.

The bug this file exists to prevent (docs/resleep-window-plan.md, section 4.3): the listener used
to match magic packets against the WIRELESS MACs only. On a box that has wifi, a magic packet
sent to the ETHERNET card's MAC was therefore not recognised as a magic packet at all - no packet
evidence, so the wake was judged non-magic and the machine was put back to sleep. Exactly the
opposite of "[] = all wired + all wireless".
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.pkt import (Adapter, Listener, select_wake_nics, wake_macs)  # noqa: E402

WIRED = Adapter(name="Ethernet", mac="00:11:22:33:44:55", wireless=False, label="Intel I219-V")
WIFI = Adapter(name="wlp5s0", mac="aa:bb:cc:dd:ee:ff", wireless=True, label="Intel AX210")
VIRTUAL = Adapter(name="vEthernet (WSL)", mac="0a:0b:0c:0d:0e:0f", wireless=False)


class TestWhichNicsArePoliced(unittest.TestCase):
    ADS = [WIRED, WIFI, VIRTUAL]

    def test_an_empty_list_means_every_wired_and_every_wireless_nic(self):
        for empty in ([], None):
            sel = select_wake_nics(empty, ads=self.ADS)
            self.assertEqual([a.name for a in sel.adapters], ["Ethernet", "wlp5s0",
                                                              "vEthernet (WSL)"])
            self.assertEqual(sel.unmatched, [])

    def test_a_mac_matches_however_the_human_wrote_it(self):
        for written in ("AA-BB-CC-DD-EE-FF", "aabbccddeeff", "aabb.ccdd.eeff", "aa:bb:cc:dd:ee:ff"):
            sel = select_wake_nics([written], ads=self.ADS)
            self.assertEqual([a.name for a in sel.adapters], ["wlp5s0"], written)

    def test_a_name_matches_case_insensitively_or_by_its_human_label(self):
        self.assertEqual([a.name for a in select_wake_nics(["WLP5S0"], ads=self.ADS).adapters],
                         ["wlp5s0"])
        self.assertEqual([a.name for a in select_wake_nics(["Intel I219-V"], ads=self.ADS).adapters],
                         ["Ethernet"])

    def test_a_wired_nic_can_be_selected_on_its_own(self):
        sel = select_wake_nics(["Ethernet"], ads=self.ADS)
        self.assertEqual(sel.macs(), ["00:11:22:33:44:55"])

    def test_an_entry_that_matches_nothing_is_reported_not_swallowed(self):
        sel = select_wake_nics(["wlp5s0", "Typo0", "de:ad:be:ef:00:01"], ads=self.ADS)
        self.assertEqual([a.name for a in sel.adapters], ["wlp5s0"])
        self.assertEqual(sel.unmatched, ["Typo0", "de:ad:be:ef:00:01"])

    def test_two_entries_naming_the_same_card_do_not_duplicate_it(self):
        sel = select_wake_nics(["Intel AX210", "aa:bb:cc:dd:ee:ff"], ads=self.ADS)
        self.assertEqual(len(sel.adapters), 1)

    def test_blank_entries_are_ignored_rather_than_matching_everything(self):
        sel = select_wake_nics(["", "   "], ads=self.ADS)
        self.assertEqual([a.name for a in sel.adapters], ["Ethernet", "wlp5s0", "vEthernet (WSL)"])

    def test_nothing_matching_still_returns_no_adapters(self):
        sel = select_wake_nics(["Typo0"], ads=self.ADS)
        self.assertEqual(sel.adapters, [])
        self.assertEqual(sel.macs(), [])
        self.assertEqual(sel.line(), "none")


class TestWhichMacsCountAsMagic(unittest.TestCase):
    ADS = [WIRED, WIFI, VIRTUAL]

    def test_unset_means_every_adapter_not_just_wifi(self):
        # both spellings of "all of them" must agree, against whatever this host reports
        self.assertEqual(wake_macs(None), wake_macs([]))

    def test_the_wired_mac_is_in_the_default_set(self):
        """The measured regression: a wired magic packet must be recognised on a box with wifi."""
        with_wifi = [WIRED, WIFI]
        macs = select_wake_nics([], ads=with_wifi).macs()
        self.assertIn("00:11:22:33:44:55", macs)
        self.assertIn("aa:bb:cc:dd:ee:ff", macs)

    def test_the_listener_uses_the_configured_set(self):
        lsn = Listener(ports=[], marker_port=None, macs=["00:11:22:33:44:55"])
        self.assertEqual(lsn.macs, ["00:11:22:33:44:55"])

    def test_the_listener_default_is_every_adapter(self):
        lsn = Listener(ports=[], marker_port=None)
        self.assertEqual(lsn.macs, wake_macs([]))


class TestAgainstThisMachine(unittest.TestCase):
    """The resolver must cope with whatever this host actually reports (0 adapters included)."""

    def test_it_resolves_the_host_config_without_raising(self):
        from autoresleep.pkt import adapters
        ads = adapters()
        sel = select_wake_nics([], ads=ads)
        self.assertEqual(len(sel.adapters), len(ads))
        if ads:
            first = ads[0]
            by_mac = select_wake_nics([first.mac], ads=ads)
            self.assertIn(first.name, [a.name for a in by_mac.adapters])


if __name__ == "__main__":
    unittest.main()