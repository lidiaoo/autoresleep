"""The second-run menu, the uninstall confirmation and `--regen-config`.

The rule these tests defend: asking is allowed ONLY when a human is there and nothing told us to
keep quiet. A menu that appears in a cron job, in a test, in the elevated child or in a dry run is
a hang dressed as a feature - and every one of those cases is a test below.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli  # noqa: E402


def _state(installed=True):
    st = cli._InstallState(config=Path("C:/data/config.yaml"))
    if installed:
        st.present = ["AutoReSleep-judge", "AutoReSleep-listener"]
        st.unknown = ["AutoReSleep-collector"]
    st.listener_pid = 4242 if installed else None
    return st


def _args(**kw):
    base = dict(platform=None, config=None, dry_run=False, elevated=False, verbose=False,
                no_elevate=False, no_firewall=False, no_sensor=False, no_start=False,
                yes=False, regen_config=False, purge=False, keep_logs=False,
                stop_pids=None, progress=None, idle_seconds=None,
                unknown_idle_seconds=None, unattributed_wake_policy=None)
    base.update(kw)
    return cli.argparse.Namespace(**base)


class TestWhetherTheMenuIsAllowed(unittest.TestCase):
    """`_menu_wanted` - the gate. Every 'no' here is a case that used to be able to block."""

    def test_a_plain_interactive_run_may_ask(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            self.assertTrue(cli._menu_wanted(_args()))

    def test_the_elevated_child_never_asks(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            self.assertFalse(cli._menu_wanted(_args(elevated=True)))

    def test_yes_never_asks(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            self.assertFalse(cli._menu_wanted(_args(yes=True)))

    def test_a_dry_run_never_asks(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True):
            self.assertFalse(cli._menu_wanted(_args(dry_run=True)))

    def test_the_env_switch_never_asks(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True), \
             mock.patch.dict(cli.os.environ, {"AUTORESLEEP_NO_PROMPT": "1"}):
            self.assertFalse(cli._menu_wanted(_args()))

    def test_without_a_terminal_it_never_asks(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=False):
            self.assertFalse(cli._menu_wanted(_args()))


class TestTheMenuItself(unittest.TestCase):
    def _menu(self, answers, state=None):
        buf = io.StringIO()
        with mock.patch.object(cli, "input", side_effect=answers), redirect_stdout(buf):
            choice = cli._install_menu(_args(), state or _state())
        return choice, buf.getvalue()

    def test_it_shows_what_is_there_and_offers_five_choices(self):
        choice, text = self._menu(["5"])
        self.assertEqual(choice, "quit")
        self.assertIn("配置文件", text)
        self.assertIn("AutoReSleep-judge", text)
        self.assertIn("4242", text)
        self.assertIn("AutoReSleep-collector", text, "the unaskable job must be visible as such")
        for item in ("1)", "2)", "3)", "4)", "5)"):
            self.assertIn(item, text)
        self.assertIn("覆盖安装", text)
        self.assertIn("卸载", text)
        self.assertIn("重新生成配置", text)

    def test_enter_means_overwrite_install(self):
        self.assertEqual(self._menu([""])[0], "install")

    def test_the_numbers_map_to_the_actions(self):
        self.assertEqual(self._menu(["1"])[0], "install")
        self.assertEqual(self._menu(["2"])[0], "uninstall")
        self.assertEqual(self._menu(["3"])[0], "regen")
        self.assertEqual(self._menu(["4"])[0], "doctor")
        self.assertEqual(self._menu(["5"])[0], "quit")

    def test_words_work_too(self):
        self.assertEqual(self._menu(["doctor"])[0], "doctor")
        self.assertEqual(self._menu(["uninstall"])[0], "uninstall")
        self.assertEqual(self._menu(["q"])[0], "quit")

    def test_a_wrong_answer_is_asked_again_and_then_gives_up(self):
        choice, text = self._menu(["x", "9", "zzz"])
        self.assertEqual(choice, "quit")
        self.assertEqual(text.count("没听懂"), 3, "three tries, then stop instead of looping for ever")

    def test_ctrl_c_does_nothing_at_all(self):
        choice, _ = self._menu([KeyboardInterrupt])
        self.assertEqual(choice, "quit")

    def test_eof_means_continue_installing(self):
        """A closed stdin is not an answer to change behaviour on - the same rule as the
        first-run config question."""
        choice, _ = self._menu([EOFError])
        self.assertEqual(choice, "install")


class TestWhatInstallDoesWithTheMenu(unittest.TestCase):
    def _install(self, argv_extra, answers, installed=True, menu=True):
        """`menu=True` forces the gate open (the gate itself has its own tests above) and every case
        runs as a dry run, so the install body below the menu can never touch this machine."""
        args = _args(dry_run=True, yes="--yes" in argv_extra)
        calls = {"uninstall": 0, "doctor": 0}
        buf = io.StringIO()
        with mock.patch.object(cli, "_installed_state", return_value=_state(installed)), \
             mock.patch.object(cli, "_menu_wanted", return_value=menu), \
             mock.patch.object(cli, "input", side_effect=answers), \
             mock.patch.object(cli, "cmd_uninstall",
                               side_effect=lambda a: (calls.__setitem__("uninstall",
                                                                        calls["uninstall"] + 1), 7)[1]), \
             mock.patch.object(cli, "cmd_doctor",
                               side_effect=lambda a: (calls.__setitem__("doctor",
                                                                        calls["doctor"] + 1), 9)[1]), \
             mock.patch.object(cli, "_load_config", return_value=cli.Config()), \
             mock.patch.object(cli, "_regen_config", return_value="（测试：已重新生成）"), \
             redirect_stdout(buf):
            rc = cli.cmd_install(args)
        return rc, calls, buf.getvalue(), args

    def test_choosing_uninstall_runs_uninstall(self):
        rc, calls, _text, _ = self._install([], ["2"])
        self.assertEqual(calls["uninstall"], 1)
        self.assertEqual(rc, 7, "the uninstall verdict is what install returns")

    def test_choosing_doctor_runs_doctor(self):
        rc, calls, _text, _ = self._install([], ["4"])
        self.assertEqual(calls["doctor"], 1)
        self.assertEqual(rc, 9)

    def test_choosing_quit_does_not_install_anything(self):
        rc, calls, _text, args = self._install([], ["5"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"uninstall": 0, "doctor": 0})
        self.assertFalse(args.regen_config, "quit must not turn into a reinstall")

    def test_choosing_regen_sets_the_flag_and_continues(self):
        rc, calls, text, args = self._install(["--dry-run"], ["3"])
        self.assertTrue(args.regen_config)
        self.assertEqual(rc, 0)
        self.assertIn("已重新生成", text, "the plan line must say what happened to the config")

    def test_a_script_gets_no_menu_but_is_told_what_was_found(self):
        rc, calls, text, _ = self._install(["--yes"], [AssertionError("input must not be called")],
                                           menu=False)
        self.assertEqual(calls, {"uninstall": 0, "doctor": 0})
        self.assertEqual(rc, 0)
        self.assertIn("本机已安装", text, "a logged second run should still say what it found")

    def test_a_fresh_machine_sees_the_same_menu_with_an_honest_headline(self):
        """First install included: the choices are identical, the state lines say "nothing yet"."""
        rc, calls, text, _ = self._install(["--dry-run"], ["5"], installed=False)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"uninstall": 0, "doctor": 0})
        self.assertIn("还没有安装在这台机器上", text)
        self.assertIn("请选择", text)
        self.assertNotIn("已经安装在这台机器上", text)

    def test_on_a_fresh_machine_enter_installs(self):
        rc, _calls, text, args = self._install(["--dry-run"], [""], installed=False)
        self.assertEqual(rc, 0)
        self.assertFalse(args.regen_config)
        self.assertIn("1) 配置", text, "the plan below the menu must have run")

    def test_on_a_fresh_machine_regen_is_still_offered_and_works(self):
        rc, _calls, text, args = self._install(["--dry-run"], ["3"], installed=False)
        self.assertTrue(args.regen_config, "3) is exactly what a first run wants: template first")
        self.assertEqual(rc, 0)


class TestRegenerateConfig(unittest.TestCase):
    def test_dry_run_only_says_what_it_would_do(self):
        with mock.patch.object(cli, "paths") as fake_paths:
            fake_paths.default_config_template.return_value = Path(__file__)
            line = cli._regen_config(Path("C:/nowhere/config.yaml"), dry=True)
        self.assertIn("[dry-run]", line)
        self.assertIn("config.yaml.bak", line)

    def test_it_backs_up_the_old_file_and_copies_the_template(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live = tmp / "config.yaml"
            live.write_text("wake_nics: []\n# 我的手改注释\n", encoding="utf-8")
            template = tmp / "config.default.yaml"
            template.write_text("wake_nics: []\n# 出厂\n", encoding="utf-8")
            with mock.patch.object(cli.paths, "default_config_template", return_value=template):
                line = cli._regen_config(live, dry=False)
            self.assertIn("已重新生成", line)
            self.assertTrue((tmp / "config.yaml.bak").exists())
            self.assertIn("我的手改注释", (tmp / "config.yaml.bak").read_text(encoding="utf-8"))
            self.assertIn("出厂", live.read_text(encoding="utf-8"))

    def test_no_live_file_means_no_backup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            template = tmp / "config.default.yaml"
            template.write_text("wake_nics: []\n", encoding="utf-8")
            with mock.patch.object(cli.paths, "default_config_template", return_value=template):
                cli._regen_config(tmp / "config.yaml", dry=False)
            self.assertFalse((tmp / "config.yaml.bak").exists())
            self.assertTrue((tmp / "config.yaml").exists())

    def test_a_missing_template_is_reported_not_guessed(self):
        with mock.patch.object(cli.paths, "default_config_template",
                               return_value=Path("C:/nowhere/config.default.yaml")):
            line = cli._regen_config(Path("C:/nowhere/config.yaml"), dry=False)
        self.assertIn("无法重新生成", line)


class TestTheUninstallConfirmation(unittest.TestCase):
    def test_yes_skips_it(self):
        with mock.patch.object(cli, "input", side_effect=AssertionError("must not ask")):
            self.assertTrue(cli._confirm_uninstall(_args(yes=True)))

    def test_without_a_terminal_it_continues(self):
        with mock.patch.object(cli, "_stdin_can_answer", return_value=False), \
             mock.patch.object(cli, "input", side_effect=AssertionError("must not ask")):
            self.assertTrue(cli._confirm_uninstall(_args()))

    def test_the_answer_decides(self):
        for answer, expected in (("", True), ("y", True), ("n", False), ("NO", False)):
            with mock.patch.object(cli, "_stdin_can_answer", return_value=True), \
                 mock.patch.object(cli, "_installed_state", return_value=_state()), \
                 mock.patch.object(cli, "input", side_effect=[answer]):
                self.assertIs(cli._confirm_uninstall(_args()), expected, "answer=%r" % answer)

    def test_it_names_what_goes_and_what_stays(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "_stdin_can_answer", return_value=True), \
             mock.patch.object(cli, "_installed_state", return_value=_state()), \
             mock.patch.object(cli, "input", side_effect=["n"]), redirect_stdout(buf):
            cli._confirm_uninstall(_args())
        text = buf.getvalue()
        self.assertIn("AutoReSleep-judge", text)
        self.assertIn("防火墙", text)
        self.assertIn("保留", text)


class TestVerbosityIsWhatHidesDetails(unittest.TestCase):
    def test_detail_is_silent_unless_verbose(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._detail("路径细节")
        self.assertEqual(buf.getvalue(), "")
        with redirect_stdout(buf):
            cli._VERBOSE = True
            try:
                cli._detail("路径细节")
            finally:
                cli._VERBOSE = False
        self.assertIn("路径细节", buf.getvalue())

    def test_a_step_line_always_shows(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._step(2, "计划任务  ", "3 个：judge / listener / collector")
        self.assertIn("2) 计划任务   3 个", buf.getvalue())


if __name__ == "__main__":
    unittest.main()