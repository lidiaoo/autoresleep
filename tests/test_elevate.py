"""Self-elevation: one command a normal user can run, and a parent that can say what happened.

`docs/install-pitfalls.md` is the acceptance test. The rules that are testable without actually
raising a UAC prompt on this machine:

  * the elevated child never asks anything - `Read-Host`/`input()` in a window nobody can see is a
    hang, not a question;
  * it leaves a line-based, timestamped record that the parent reads WHILE it runs;
  * the parent decides success by reading that record, never by "I saw no output";
  * every answer the child needs travels as an argument (an environment variable does not survive
    UAC).
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import elevate  # noqa: E402
from autoresleep.elevate import Progress, _rc_from, child_argv  # noqa: E402


class TestTheProgressRecord(unittest.TestCase):
    def test_lines_are_timestamped_and_readable_while_the_child_runs(self):
        with tempfile.TemporaryDirectory() as td:
            p = Progress(Path(td) / "elevated-install.progress")
            p.start("install")
            p.step("write /etc/systemd/system/autoresleep-wol-sensor.service")
            p.done(0)
            lines = p.lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("start install", lines[0])
        self.assertIn("as=", lines[0])
        self.assertIn("step  write", lines[1])
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  ")

    def test_the_verdict_is_the_last_line_and_is_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Progress(Path(td) / "elevated-uninstall.progress")
            p.start("uninstall")
            p.done(1)
            self.assertEqual(_rc_from(p.lines()), 1)
            p.done(0)
            self.assertEqual(_rc_from(p.lines()), 0)

    def test_a_record_without_a_verdict_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as td:
            p = Progress(Path(td) / "x.progress")
            p.start("install")
            p.step("apply autoresleep-wol-sensor.service")
            self.assertIsNone(_rc_from(p.lines()))
            self.assertEqual(p.lines() and "install" in p.lines()[0], True)

    def test_clear_drops_the_previous_run(self):
        with tempfile.TemporaryDirectory() as td:
            p = Progress(Path(td) / "x.progress")
            p.done(0)
            p.clear()
            self.assertEqual(p.lines(), [])

    def test_a_missing_file_reads_as_no_lines(self):
        p = Progress(Path("/nonexistent/dir/x.progress"))
        self.assertEqual(p.lines(), [])


class TestTheChildCommandLine(unittest.TestCase):
    def test_it_marks_itself_internal_and_carries_the_answers(self):
        argv = child_argv(["install", "--no-firewall"], "/tmp/p.progress", config_path="/tmp/c.json")
        self.assertIn("--elevated", argv)
        self.assertIn("--progress", argv)
        self.assertIn("/tmp/p.progress", argv)
        self.assertIn("--no-firewall", argv)
        # an environment variable does not survive UAC: the config path must be an argument
        self.assertIn("--config", argv)
        self.assertIn("/tmp/c.json", argv)

    def test_it_does_not_duplicate_an_existing_config_flag(self):
        argv = child_argv(["install", "--config", "/a"], "/p", config_path="/b")
        self.assertEqual(argv.count("--config"), 1)
        self.assertIn("/a", argv)

    def test_the_launcher_is_a_real_command(self):
        cmd = elevate.launch_argv(["install"])
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(str(cmd[1]).endswith("autoresleep.py"), cmd)
        self.assertIn("install", cmd)


class TestTheParentReadsTheChild(unittest.TestCase):
    def test_it_returns_the_childs_verdict_and_echoes_its_lines(self):
        out = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "elevated-install.progress"

            def fake_start(cmd, progress_path, emit):
                Progress(progress_path).start("install")
                Progress(progress_path).step("apply AutoReSleep-judge")
                Progress(progress_path).done(0)
                return True, "fake"

            with mock.patch.object(elevate, "_start", side_effect=fake_start):
                rc = elevate.delegate(["install"], path, out.append)
        self.assertEqual(rc, 0)
        text = "\n".join(out)
        self.assertIn("apply AutoReSleep-judge", text)
        self.assertIn("elevated via fake", text)

    def test_a_child_that_never_reports_is_not_success(self):
        out = []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "elevated-install.progress"

            def fake_start(cmd, progress_path, emit):
                Progress(progress_path).start("install")     # starts, then hangs forever
                return True, "fake"

            with mock.patch.object(elevate, "_start", side_effect=fake_start):
                with mock.patch.object(elevate, "_wait") as wait:
                    wait.side_effect = lambda progress, emit, **kw: None
                    rc = elevate.delegate(["install"], path, out.append)
        self.assertIsNone(rc, "no verdict must never read as success")

    def test_silence_gets_a_useful_hint(self):
        out = []
        with tempfile.TemporaryDirectory() as td:
            p = Progress(Path(td) / "x.progress")
            rc = elevate._wait(p, out.append, silent_s=0.1, timeout_s=0.6)
        self.assertIsNone(rc)
        self.assertIn("nothing has started yet", "\n".join(out))

    def test_a_refused_prompt_returns_none_instead_of_rc(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(elevate, "_start", return_value=(False, "refused")):
                self.assertIsNone(elevate.delegate(["install"], Path(td) / "p", lambda _m: None))


class TestTheChildAsksNothing(unittest.TestCase):
    """Source-level rule (this repo already has one for bare subprocess calls)."""

    def _source(self, name: str) -> str:
        return (Path(__file__).resolve().parents[1] / "src" / "autoresleep" / name).read_text(encoding="utf-8")

    def test_the_elevation_module_never_prompts(self):
        import ast
        source = self._source("elevate.py")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                self.assertNotIn(name, ("input", "raw_input", "getpass"),
                                 "the elevation path must never block on a question")

    def test_it_refuses_to_prompt_when_switched_off(self):
        import os
        from unittest import mock
        out = []
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"AUTORESLEEP_NO_ELEVATE": "1"}):
                with mock.patch.object(elevate, "_start") as started:
                    rc = elevate.delegate(["install"], Path(td) / "p", out.append)
        self.assertIsNone(rc, "switched off must never read as success")
        started.assert_not_called()
        self.assertIn("switched off", "\n".join(out))

    def test_the_child_never_prompts_either(self):
        # and it is not just a convention: the switch is honoured at the choke point, so a future
        # caller that forgets it still cannot put a UAC box on somebody's desktop
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"AUTORESLEEP_NO_ELEVATE": "yes"}):
                self.assertIsNone(elevate.delegate(["uninstall"], Path(td) / "p", lambda _m: None))

    def test_the_elevated_halves_of_install_and_uninstall_never_prompt(self):
        text = self._source("cli.py")
        for name in ("_elevated_install", "_elevated_uninstall"):
            start = text.index("def %s(" % name)
            end = text.index("\ndef ", start + 1)
            body = text[start:end]
            self.assertNotIn("input(", body, name)
            self.assertNotIn("Read-Host", body, name)
            # and it must record what it is doing, for the parent it cannot see
            self.assertIn("progress.step(", body)

    def test_the_parent_reports_failure_by_reading_the_record(self):
        text = self._source("cli.py")
        self.assertIn("the privileged step did not finish. Read the record it left", text)


class TestStaleListenersAskerOrKilledByTheElevatedChild(unittest.TestCase):
    """A listener the parent cannot kill must still be killed - by the child that can.

    Measured on the Windows machine: the resident jobs carry `<RunLevel>HighestAvailable</RunLevel>`,
    so the listener processes run at HIGH integrity and a normal-user install gets
    "错误: 无法终止 PID 为 13676 的进程。原因: 拒绝访问。" from `taskkill /F`. The old code threw that
    result away and printed "stopped the previous listener", so two orphans stayed on UDP 9/7/67/68
    while the lock file pointed at a pid that no longer existed.
    """

    def test_the_pids_travel_to_the_child_as_an_argument(self):
        from autoresleep import cli
        args = mock.Mock()
        args.config = None
        args.no_firewall = False
        args.no_sensor = False
        args.stop_pids = "13676,34544"
        flags = cli._forwarded_flags(args)
        self.assertIn("--stop-pids", flags)
        self.assertEqual(flags[flags.index("--stop-pids") + 1], "13676,34544")

    def test_the_child_parses_only_real_numbers(self):
        from autoresleep import cli
        for raw, want in ((None, []), ("", []), ("13676", [13676]),
                          (" 13676 , 34544 ", [13676, 34544]), ("abc,7", [7]), ("-5", [])):
            args = mock.Mock()
            args.stop_pids = raw
            self.assertEqual(cli._stop_pid_list(args), want, raw)

    def test_only_python_processes_are_handed_over(self):
        """Port 9 may belong to somebody else's tool - killing a stranger's process is not our job."""
        from autoresleep import cli
        # this suite redirects LOCALAPPDATA (test_platform_layer), which the sandbox guard reads,
        # so say explicitly that this is the machine's real data dir
        with mock.patch.object(cli, "_data_dir_is_live", return_value=True):
            with mock.patch.object(cli, "_udp_owners",
                                   return_value={9: {111, 222, 333}, 7: {111}}):
                with mock.patch.object(cli, "_looks_like_our_listener",
                                       side_effect=lambda pid: pid in (111, 222)):
                    self.assertEqual(cli._listener_pids_to_stop([9, 7], 45678), [111, 222])

    def test_a_sandboxed_run_never_touches_the_live_listener(self):
        """Measured: `LOCALAPPDATA=<temp> install` listed the LIVE listener as a stale one."""
        from autoresleep import cli
        with mock.patch.object(cli, "_data_dir_is_live", return_value=False):
            with mock.patch.object(cli, "_udp_owners", return_value={9: {111}}):
                with mock.patch.object(cli, "_looks_like_our_listener", return_value=True):
                    self.assertEqual(cli._listener_pids_to_stop([9], 45678), [])

    def test_a_failed_scan_hands_over_nothing(self):
        from autoresleep import cli
        with mock.patch.object(cli, "_udp_owners", return_value=None):
            self.assertEqual(cli._listener_pids_to_stop([9, 7], 45678), [])

    def test_our_own_pid_is_never_in_the_list(self):
        import os
        from autoresleep import cli
        mine = os.getpid()
        with mock.patch.object(cli, "_data_dir_is_live", return_value=True):
            with mock.patch.object(cli, "_udp_owners", return_value={9: {mine}}):
                with mock.patch.object(cli, "_looks_like_our_listener", return_value=True):
                    self.assertEqual(cli._listener_pids_to_stop([9], 45678), [])

    def test_the_child_records_each_kill_and_reports_survivors(self):
        from autoresleep import cli

        class FakeProgress:
            def __init__(self):
                self.steps = []

            def step(self, msg):
                self.steps.append(msg)

        args = mock.Mock()
        args.stop_pids = "111,222"
        progress = FakeProgress()
        with mock.patch.object(cli, "_pid_alive", side_effect=lambda pid: pid == 222):
            with mock.patch.object(cli, "_stop_pid", side_effect=lambda pid: False):
                survivors = cli._stop_stale_listeners(args, progress)
        self.assertEqual(survivors, [222])
        self.assertIn("stale listener pid 111: already gone", progress.steps)
        self.assertIn("stale listener pid 222: STILL ALIVE - it owns the WoL ports", progress.steps)

    def test_the_port_scan_covers_ports_an_older_config_used(self):
        from autoresleep import cli
        cfg = mock.Mock()
        cfg.ports = [9, 7, 19090]
        ports = cli._ports_to_scan(cfg)
        for p in (9, 7, 67, 68, 45678, 19090):
            self.assertIn(p, ports)

    def test_both_elevated_halves_call_the_kill(self):
        text = self._source("cli.py")
        for name in ("_elevated_install", "_elevated_uninstall"):
            start = text.index("def %s(" % name)
            end = text.index("\ndef ", start + 1)
            self.assertIn("_stop_stale_listeners(args, progress)", text[start:end], name)

    def _source(self, name):
        root = Path(__file__).resolve().parents[1] / "src" / "autoresleep"
        return (root / name).read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()