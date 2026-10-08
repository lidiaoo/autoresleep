"""Slice 5: where v2 keeps its files and configuration, on each platform.

v1 hardcoded `%LOCALAPPDATA%\\HermesTools` in prose and relied on `$PSScriptRoot` everywhere.
v2 makes the location a single tested function so the Linux/macOS paths cannot drift apart
from the Windows one.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli, paths  # noqa: E402
from autoresleep.config import Config  # noqa: E402


class TestPaths(unittest.TestCase):
    def test_windows_data_dir_is_under_localappdata(self):
        p = paths.data_dir(platform="win32", env={"LOCALAPPDATA": r"C:\Users\x\AppData\Local"},
                           home=Path(r"C:\Users\x"))
        self.assertEqual(str(p).replace("/", "\\"), r"C:\Users\x\AppData\Local\AutoReSleep")

    def test_linux_data_dir_follows_xdg(self):
        p = paths.data_dir(platform="linux", env={"XDG_STATE_HOME": "/home/x/.state"},
                           home=Path("/home/x"))
        # compare with normalised separators: pathlib on Windows renders posix paths with
        # backslashes, and the point of the test is the LAYOUT, not the host's separator
        self.assertEqual(str(p).replace("\\", "/"), "/home/x/.state/autoresleep")

    def test_linux_data_dir_defaults_to_local_state(self):
        p = paths.data_dir(platform="linux", env={}, home=Path("/home/x"))
        self.assertEqual(str(p).replace("\\", "/"), "/home/x/.local/state/autoresleep")

    def test_macos_data_dir_is_application_support(self):
        p = paths.data_dir(platform="darwin", env={}, home=Path("/Users/x"))
        self.assertEqual(str(p).replace("\\", "/"), "/Users/x/Library/Application Support/AutoReSleep")

    def test_every_target_platform_is_covered(self):
        for plat in ("win32", "linux", "darwin"):
            self.assertIsInstance(paths.data_dir(platform=plat, env={}, home=Path("/tmp/h")), Path)


class TestConfig(unittest.TestCase):
    def test_defaults_match_v1_shipped_values(self):
        c = Config()
        self.assertEqual(c.idle_minutes, 2.0)
        self.assertEqual(c.unknown_idle_minutes, 15.0)
        self.assertEqual(c.unattributed_wake_policy, "skip")
        self.assertEqual(c.repeat_minutes, 2)
        self.assertEqual(c.marker_port, 45678)

    def test_round_trip_through_a_json_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            c = Config(idle_minutes=7.5, unattributed_wake_policy="resleep-if-unattended")
            c.save(f)
            back = Config.load(f)
            self.assertEqual(back.idle_minutes, 7.5)
            self.assertEqual(back.unattributed_wake_policy, "resleep-if-unattended")
            self.assertIn("unattributed_wake_policy", json.loads(f.read_text()))

    def test_load_of_a_missing_file_returns_defaults(self):
        c = Config.load(Path("/nonexistent/nope.json"))
        self.assertEqual(c.idle_minutes, 2.0)

    def test_load_ignores_unknown_keys_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            f.write_text(json.dumps({"idle_minutes": 3, "from_v1_someday": True}))
            self.assertEqual(Config.load(f).idle_minutes, 3.0)

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(unattributed_wake_policy="whatever").validate()

    def test_non_positive_repeat_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(repeat_minutes=0).validate()

    def test_params_for_the_engine_carry_the_thresholds(self):
        p = Config(idle_minutes=4, unknown_idle_minutes=20).params()
        self.assertEqual(p.idle_minutes, 4.0)
        self.assertEqual(p.unknown_idle_minutes, 20.0)


class TestTheResleepWindowConfig(unittest.TestCase):
    """The window that replaced `idle_minutes` as the wait (docs/resleep-window-plan.md)."""

    def _run_config(self, *argv):
        """Run `autoresleep config ...` and keep its chatter out of the test output."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["config"] + list(argv))
        return rc, buf.getvalue()

    def test_the_shipped_defaults_are_the_ones_the_plan_promised(self):
        c = Config()
        self.assertEqual(c.wake_nics, [], "[] means every wired and every wireless NIC")
        self.assertFalse(c.magic_only)
        self.assertEqual(c.resleep_delay_seconds, 10.0)
        self.assertTrue(c.resleep_break_on_input)

    def test_a_mac_is_normalised_and_a_name_is_left_alone(self):
        # the same card is written with dashes by netsh, colons by `ip link`, no padding by
        # ifconfig - one canonical form, or the comparison silently misses
        c = Config(wake_nics=["AA-BB-CC-DD-EE-FF", "wlp5s0", "0011.2233.4455"]).validate()
        self.assertEqual(c.wake_nics, ["aa:bb:cc:dd:ee:ff", "wlp5s0", "00:11:22:33:44:55"])

    def test_an_entry_that_could_never_match_an_adapter_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(wake_nics=[""]).validate()
        with self.assertRaises(ValueError):
            Config(wake_nics=[7]).validate()
        with self.assertRaises(ValueError):
            Config(wake_nics="wlp5s0").validate()          # a bare string, not a list

    def test_a_negative_window_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(resleep_delay_seconds=-1).validate()

    def test_the_switches_are_coerced_so_a_hand_edited_file_cannot_crash_a_judge(self):
        c = Config(magic_only=1, resleep_break_on_input=0).validate()
        self.assertIs(c.magic_only, True)
        self.assertIs(c.resleep_break_on_input, False)

    def test_they_survive_a_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            Config(wake_nics=["wlp5s0"], magic_only=True, resleep_delay_seconds=4.5,
                   resleep_break_on_input=False).save(f)
            back = Config.load(f)
            self.assertEqual(back.wake_nics, ["wlp5s0"])
            self.assertTrue(back.magic_only)
            self.assertEqual(back.resleep_delay_seconds, 4.5)
            self.assertFalse(back.resleep_break_on_input)

    def test_a_config_written_before_the_window_existed_still_loads(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            f.write_text(json.dumps({"idle_minutes": 3, "ports": [9, 7]}), encoding="utf-8")
            c = Config.load(f).validate()
        self.assertEqual(c.idle_minutes, 3.0)
        self.assertEqual(c.wake_nics, [])
        self.assertEqual(c.resleep_delay_seconds, 10.0)

    def test_set_from_the_command_line_keeps_adapter_names_as_strings(self):
        """`ports` is a list of ints, so the CLI cannot assume every list is numeric."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            rc, _out = self._run_config("--set", "wake_nics=AA-BB-CC-DD-EE-FF,wlp5s0",
                                        "--config", str(f))
            self.assertEqual(rc, 0)
            data = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(data["wake_nics"], ["aa:bb:cc:dd:ee:ff", "wlp5s0"])

    def test_set_clears_the_list_with_an_empty_value(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            self._run_config("--set", "wake_nics=wlp5s0", "--config", str(f))
            self._run_config("--set", "wake_nics=", "--config", str(f))
            self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["wake_nics"], [])

    def test_setting_ports_still_yields_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            self._run_config("--set", "ports=9,7", "--set", "magic_only=true",
                             "--set", "resleep_delay_seconds=30", "--config", str(f))
            data = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(data["ports"], [9, 7])
        self.assertIs(data["magic_only"], True)
        self.assertEqual(data["resleep_delay_seconds"], 30.0)

    def test_a_bad_value_leaves_the_file_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            rc, out = self._run_config("--set", "resleep_delay_seconds=-3", "--config", str(f))
        self.assertEqual(rc, 2)
        self.assertFalse(f.exists(), "a rejected value must not be written")
        self.assertIn("must not be negative", out)

    def test_the_documented_multi_pair_syntax_works(self):
        """`config --set a=1 b=2` is what every doc in this repo shows."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            rc, _out = self._run_config("--set", "resleep_delay_seconds=25",
                                        "resleep_break_on_input=false", "--config", str(f))
            self.assertEqual(rc, 0)
            data = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(data["resleep_delay_seconds"], 25.0)
        self.assertIs(data["resleep_break_on_input"], False)

    def test_an_adapter_name_with_a_space_survives(self):
        """Windows calls its cards "WLAN 4", "以太网 2", "vEthernet (Default Switch)"."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.json"
            rc, _out = self._run_config("--set", "wake_nics=WLAN 4,以太网 2,aa:bb:cc:dd:ee:ff",
                                        "--config", str(f))
            self.assertEqual(rc, 0)
            data = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(data["wake_nics"], ["WLAN 4", "以太网 2", "aa:bb:cc:dd:ee:ff"])


if __name__ == "__main__":
    unittest.main()