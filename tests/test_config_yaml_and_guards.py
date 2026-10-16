"""The config file is YAML now, the watchdogs are switchable, and the first run asks.

Three things this pins down, all of them from a real morning on the Windows box:
  * the file is YAML with comments (a key like `guard_display_in_use: false` needs the sentence that
    says why it ships off);
  * the watchdogs can be switched off, and a switched-off guard is STILL VISIBLE in the decision
    line - `display-in-use` (CrossDesk.exe) was silently blocking every re-sleep on that machine;
  * `install` on a fresh machine generates the file and asks before going on.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli, miniyaml, paths  # noqa: E402
from autoresleep.config import Config  # noqa: E402
from autoresleep.decide import Guards  # noqa: E402


class TestTheFileIsYamlAndCommentsItself(unittest.TestCase):
    def test_saving_writes_yaml_with_the_operator_facing_knobs_first(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.yaml"
            Config().save(f)
            text = f.read_text(encoding="utf-8")
        for key in ("wake_nics", "magic_only", "resleep_delay_seconds", "resleep_break_on_input",
                    "input_settle_seconds",
                    "guard_remote_in_use", "guard_display_in_use", "guard_media_playing",
                    "guard_update_running"):
            self.assertIn("\n%s:" % key, text, key)
        self.assertLess(text.index("wake_nics:"), text.index("idle_seconds:"),
                        "the knobs an operator touches belong at the top")
        self.assertIn("# AutoReSleep 配置文件", text)
        self.assertIn("# 【默认关】", text, "the display guard ships off and must say why")

    def test_the_watchdogs_ship_off(self):
        c = Config()
        self.assertFalse(c.guard_display_in_use)
        self.assertFalse(c.guard_remote_in_use)
        self.assertFalse(c.guard_media_playing)
        self.assertFalse(c.guard_update_running)

    def test_the_wake_cause_channel_ships_on_and_can_be_switched_off(self):
        """A knob that cannot be turned off is not a knob: `wowlan_wake_reason` must survive the
        file round trip both ways, and the generated file must show it (the operator finds it by
        reading `config.default.yaml`, not by reading the source)."""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.yaml"
            Config().save(f)
            text = f.read_text(encoding="utf-8")
            self.assertTrue(Config().wowlan_wake_reason, "the channel ships on")
            self.assertIn("\nwowlan_wake_reason: true", text)
            self.assertIn("# 【Linux】", text, "the Linux-only scope has to be visible in the file")
            f.write_text("wowlan_wake_reason: false\n", encoding="utf-8")
            self.assertFalse(Config.load(f).validate().wowlan_wake_reason)
            f.write_text("wowlan_wake_reason: no\n", encoding="utf-8")
            self.assertFalse(Config.load(f).validate().wowlan_wake_reason,
                             "YAML 1.1 'no' is a false, not a truthy string")

    def test_a_hand_edited_yaml_file_decides_the_guards(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "config.yaml"
            f.write_text("guard_display_in_use: true\nwake_nics: [aa:bb:cc:dd:ee:ff]\n",
                         encoding="utf-8")
            c = Config.load(f).validate()
        self.assertTrue(c.guard_display_in_use)
        self.assertEqual(c.wake_nics, ["aa:bb:cc:dd:ee:ff"])

    def test_extra_is_never_written_back(self):
        c = Config(extra={"from_v1": 1})
        self.assertNotIn("extra", c.to_yaml())


class TestTheProjectTemplateIsTheSource(unittest.TestCase):
    """The project keeps `config.default.yaml`; install COPIES it; every run reads the copy."""

    def test_the_shipped_template_holds_exactly_the_known_keys(self):
        """What must not rot is the KEY SET: a key that exists in code but not in the file (or the
        other way round) is a knob that silently ignores the operator.

        The VALUES are deliberately not asserted - the project file is the canonical default a new
        machine is installed from, and an operator editing it (turning a watchdog off, binding
        wake_nics to their MACs) is exactly what it is for. A drifted file - missing key, renamed
        key, a value the validator rejects - is what this refuses.
        """
        from dataclasses import fields
        template = paths.default_config_template()
        self.assertTrue(template.exists(), "the project must ship %s" % template.name)
        on_disk = miniyaml.load(template.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Config)} - {"extra"}
        self.assertEqual(known - set(on_disk), set(),
                         "missing from the file: %s" % sorted(known - set(on_disk)))
        self.assertEqual(set(on_disk) - known, set(),
                         "not a config key: %s" % sorted(set(on_disk) - known))
        Config(**on_disk).validate()      # a hand-edited value must still validate

    def test_the_template_keeps_the_generated_comments_and_order(self):
        """The comments ARE the documentation of this file, and the order is the reading order."""
        from autoresleep.config import _YAML_HEADER
        on_disk = paths.default_config_template().read_text(encoding="utf-8")
        on_disk = on_disk.replace(chr(13) + chr(10), chr(10))
        gen = Config().to_yaml(_YAML_HEADER)
        comments_of = lambda text: [l for l in text.splitlines() if l.startswith("#")]
        self.assertEqual(comments_of(on_disk), comments_of(gen))
        keys_of = lambda text: [l.split(":")[0] for l in text.splitlines()
                                if l and not l.startswith(("#", " ", "-"))]
        self.assertEqual(keys_of(on_disk), keys_of(gen))

    def test_the_template_holds_the_shipped_defaults(self):
        c = Config.load(paths.default_config_template()).validate()
        self.assertEqual(c.wake_nics, [])
        self.assertFalse(c.magic_only)
        self.assertEqual(c.resleep_delay_seconds, 10.0)
        self.assertEqual(c.input_settle_seconds, 5.0,
                         "the resume's HID replay must be absorbed out of the box")
        self.assertFalse(c.guard_display_in_use)
        self.assertTrue(c.wowlan_wake_reason,
                        "the shipped template keeps the wake-cause channel on")
        self.assertTrue(c.force_suspend)

    def test_install_on_a_fresh_machine_copies_the_template(self):
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "config.yaml"
            cfg = Config()
            cli._install_default_config(target, cfg)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), paths.default_config_template().read_bytes(),
                             "the installed file must be a copy, byte for byte")
            back = Config.load(target).validate()
            self.assertEqual(back.resleep_delay_seconds, 10.0)

    def test_a_source_tree_without_the_template_still_installs(self):
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "config.yaml"
            with mock.patch.object(paths, "default_config_template",
                                   return_value=Path(td) / "missing.yaml"):
                cli._install_default_config(target, Config())
            self.assertTrue(target.exists(), "fall back to generating, never fail the install")

    def test_an_existing_file_is_left_alone_when_nothing_changed(self):
        """A hand-annotated file must survive a re-install."""
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "config.yaml"
            target.write_text("# my own comments" + chr(10) + "wake_nics: []" + chr(10), encoding="utf-8")
            before = target.read_bytes()
            self.assertTrue(cli._same_values(Config.load(target), Config()))
            self.assertEqual(target.read_bytes(), before)

    def test_a_real_difference_is_written(self):
        from autoresleep import cli
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "config.yaml"
            target.write_text("idle_seconds: 9" + chr(10), encoding="utf-8")
            self.assertFalse(cli._same_values(Config.load(target), Config()))


class TestMigrationFromTheJsonFile(unittest.TestCase):
    def test_the_old_json_becomes_yaml_and_is_set_aside(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "config.json"
            legacy.write_text('{"idle_minutes": 4, "wake_nics": ["aa:bb:cc:dd:ee:ff"]}',
                              encoding="utf-8")
            with mock.patch.object(paths, "data_dir", return_value=Path(td)):
                with mock.patch.object(paths, "config_file", return_value=Path(td) / "config.yaml"):
                    with mock.patch.object(paths, "legacy_config_file",
                                           return_value=legacy):
                        written = Config.migrate_legacy()
            self.assertEqual(written, Path(td) / "config.yaml")
            self.assertTrue((Path(td) / "config.yaml").exists())
            self.assertFalse(legacy.exists(), "the old file must not linger next to the new one")
            self.assertTrue((Path(td) / "config.json.migrated").exists())
            back = Config.load(Path(td) / "config.yaml")
            self.assertEqual(back.idle_seconds, 240.0)
            self.assertEqual(back.wake_nics, ["aa:bb:cc:dd:ee:ff"])

    def test_nothing_to_migrate_on_a_fresh_machine(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(paths, "legacy_config_file", return_value=Path(td) / "nope.json"):
                self.assertIsNone(Config.migrate_legacy())


class TestSwitchingAGuardOff(unittest.TestCase):
    def _guards(self, **kw):
        g = Guards(**kw)
        g.detail = "CrossDesk.exe"
        return g

    def test_an_off_guard_no_longer_blocks(self):
        cfg = Config(guard_display_in_use=False)
        g = cli._apply_guard_config(self._guards(display_in_use=True), cfg)
        self.assertFalse(g.display_in_use)
        self.assertIsNone(g.active())

    def test_but_it_is_still_reported(self):
        cfg = Config(guard_display_in_use=False)
        g = cli._apply_guard_config(self._guards(display_in_use=True), cfg)
        self.assertEqual(g.ignored, "display-in-use[CrossDesk.exe]")
        self.assertEqual(g.label(), "ignored:display-in-use[CrossDesk.exe]")

    def test_an_on_guard_still_blocks_and_names_its_holder(self):
        cfg = Config(guard_display_in_use=True)
        g = cli._apply_guard_config(self._guards(display_in_use=True), cfg)
        self.assertEqual(g.active(), "display-in-use")
        self.assertEqual(g.label(), "display-in-use[CrossDesk.exe]")

    def test_the_other_three_guards_are_unaffected(self):
        cfg = Config(guard_display_in_use=False, guard_remote_in_use=True,
                     guard_media_playing=True, guard_update_running=True)
        g = cli._apply_guard_config(self._guards(remote_in_use=True, display_in_use=True), cfg)
        self.assertTrue(g.remote_in_use)
        self.assertEqual(g.active(), "remote-in-use")

    def test_the_labels_for_the_other_states(self):
        self.assertEqual(Guards().label(), "-")
        self.assertEqual(Guards(display_in_use=True).label(), "display-in-use")
        self.assertEqual(Guards(unavailable="powercfg needs administrator").label(),
                         "?[powercfg needs administrator]")


class TestTheFirstRunPrompt(unittest.TestCase):
    class Args:
        elevated = False
        yes = False

    def _quiet(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli._prompt_after_first_config(self.Args(), "C:/tmp/config.yaml")

    def test_a_non_tty_never_asks(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=False):
            with mock.patch("builtins.input", side_effect=AssertionError("asked a non-tty!")):
                self.assertTrue(self._quiet())

    def test_an_eof_continues_instead_of_blocking(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertTrue(self._quiet())

    def test_no_stops_before_anything_is_registered(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            with mock.patch("builtins.input", return_value="n"):
                self.assertFalse(self._quiet())

    def test_enter_or_yes_continues(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            for answer in ("", "y", "Y", "yes", "1"):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertTrue(self._quiet(), answer)

    def test_yes_flag_and_env_skip_the_question(self):
        args = self.Args()
        args.yes = True
        self.assertTrue(cli._prompt_after_first_config(args, "x"))
        args.yes = False
        with mock.patch.dict(os.environ, {"AUTORESLEEP_NO_PROMPT": "1"}):
            self.assertTrue(cli._prompt_after_first_config(args, "x"))

    def test_the_elevated_child_never_asks(self):
        args = self.Args()
        args.elevated = True
        with mock.patch("builtins.input", side_effect=AssertionError("asked!")):
            self.assertTrue(cli._prompt_after_first_config(args, "x"))



class TestTheConfigPathCanBeAsked(unittest.TestCase):
    """`config --path` exists so a launcher script (install.sh / .cmd) never hard-codes the data
    dir: the answer comes from the same function the tool itself reads the file with."""

    def test_it_prints_the_very_file_the_tool_would_read(self):
        import io
        from contextlib import redirect_stdout
        from autoresleep import cli, paths
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["config", "--path"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), str(paths.config_file()))
        self.assertTrue(buf.getvalue().strip().endswith("config.yaml"))

if __name__ == "__main__":
    unittest.main()