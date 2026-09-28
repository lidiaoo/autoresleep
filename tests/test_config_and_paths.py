"""Slice 5: where v2 keeps its files and configuration, on each platform.

v1 hardcoded `%LOCALAPPDATA%\\HermesTools` in prose and relied on `$PSScriptRoot` everywhere.
v2 makes the location a single tested function so the Linux/macOS paths cannot drift apart
from the Windows one.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import paths  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()