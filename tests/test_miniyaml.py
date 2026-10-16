"""The YAML subset the config file is written in (`miniyaml`).

No PyYAML on purpose: this tool has to run on a machine with nothing installed, and the config is a
flat mapping - a small enough grammar to own here. These tests are the contract: what the writer
emits, the reader must read back, and a file a human hand-edited must keep working.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import miniyaml  # noqa: E402


class TestRoundTrip(unittest.TestCase):
    def test_a_realistic_config_survives_a_round_trip(self):
        data = {
            "wake_nics": ["00:11:22:33:44:55", "以太网 2"],
            "magic_only": True,
            "resleep_delay_seconds": 10.0,
            "resleep_break_on_input": False,
            "repeat_seconds": 120,
            "ports": [9, 7, 67, 68],
            "unattributed_wake_policy": "skip",
            "empty_list": [],
        }
        back = miniyaml.load(miniyaml.dump(data))
        self.assertEqual(back, data)

    def test_types_are_preserved(self):
        data = {"a": True, "b": False, "c": 7, "d": 2.5, "e": -3, "f": "text"}
        back = miniyaml.load(miniyaml.dump(data))
        self.assertIs(back["a"], True)
        self.assertIs(back["b"], False)
        self.assertEqual(back["c"], 7)
        self.assertEqual(back["d"], 2.5)
        self.assertEqual(back["e"], -3)
        self.assertEqual(back["f"], "text")

    def test_a_string_that_looks_like_a_number_stays_a_string(self):
        # an adapter name could be anything; quoting is how the writer prevents "007" losing its
        # leading zero or a name becoming an int the moment it is read back
        data = {"name": "007", "weird": "true", "colon": "a: b", "hash": "x # y"}
        back = miniyaml.load(miniyaml.dump(data))
        self.assertEqual(back, data)

    def test_quotes_and_backslashes_survive(self):
        data = {"path": r"C:\Program Files\thing", "quote": 'he said "hi"'}
        self.assertEqual(miniyaml.load(miniyaml.dump(data)), data)

    def test_newlines_are_escaped_not_broken(self):
        data = {"note": "line one\nline two"}
        self.assertEqual(miniyaml.load(miniyaml.dump(data)), data)


class TestReadingWhatAHumanWrites(unittest.TestCase):
    def test_comments_and_blank_lines_are_skipped(self):
        text = ("# a header\n"
                "\n"
                "idle_seconds: 4   # trailing comment\n"
                "\n"
                "# another comment\n"
                "magic_only: false\n")
        got = miniyaml.load(text)
        self.assertEqual(got, {"idle_seconds": 4, "magic_only": False})

    def test_plain_unquoted_scalars_are_accepted(self):
        got = miniyaml.load("wake_nics: [00:11:22:33:44:55, 以太网 2]\nname: WLAN 4\n")
        self.assertEqual(got["wake_nics"], ["00:11:22:33:44:55", "以太网 2"])
        self.assertEqual(got["name"], "WLAN 4")

    def test_block_lists_are_accepted(self):
        got = miniyaml.load("wake_nics:\n  - 00:11:22:33:44:55\n  - 以太网 2\nmagic_only: true\n")
        self.assertEqual(got["wake_nics"], ["00:11:22:33:44:55", "以太网 2"])
        self.assertIs(got["magic_only"], True)

    def test_inline_lists_with_quoted_commas(self):
        got = miniyaml.load('names: ["a, b", c]\n')
        self.assertEqual(got["names"], ["a, b", "c"])

    def test_yaml_11_booleans(self):
        got = miniyaml.load("a: yes\nb: no\nc: on\nd: off\ne: true\n")
        self.assertEqual([got[k] for k in "abcde"], [True, False, True, False, True])

    def test_a_partial_file_only_returns_what_it_has(self):
        got = miniyaml.load("idle_seconds: 3\n")
        self.assertEqual(got, {"idle_seconds": 3})

    def test_an_empty_file_is_an_empty_mapping(self):
        self.assertEqual(miniyaml.load(""), {})
        self.assertEqual(miniyaml.load("# nothing but a comment\n"), {})

    def test_junk_lines_are_skipped_not_raised(self):
        # a corrupt config must not stop a judge whose only job is to decide whether to sleep
        self.assertEqual(miniyaml.load("this line has no colon\nkey: 1\n"), {"key": 1})


class TestWhatTheGeneratorEmits(unittest.TestCase):
    def test_the_order_and_the_comments_are_used(self):
        data = {"b": 1, "a": 2}
        text = miniyaml.dump(data, header="header line", comments={"b": "# note for b"},
                             order=["b", "a"])
        lines = text.splitlines()
        self.assertEqual(lines[0], "# header line")
        self.assertIn("# note for b", lines)
        self.assertLess(text.index("b: 1"), text.index("a: 2"), "order must be honoured")

    def test_an_empty_list_is_inline(self):
        self.assertIn("wake_nics: []", miniyaml.dump({"wake_nics": []}))

    def test_a_non_empty_list_is_a_block(self):
        text = miniyaml.dump({"wake_nics": ["aa:bb"]})
        self.assertIn("wake_nics:", text)
        self.assertIn("  - \"aa:bb\"", text)


if __name__ == "__main__":
    unittest.main()