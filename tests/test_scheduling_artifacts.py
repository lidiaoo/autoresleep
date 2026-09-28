"""Slice 6: the per-OS scheduler artifacts, generated as TEXT.

v1's install script talked to the Windows Task Scheduler API directly, so the whole design
was unrunnable anywhere else. v2 generates the native artifacts for each platform as text
(a Task Scheduler XML, systemd user units, launchd plists) and applies them with the OS's own
tool. That makes the interesting part - what the artifacts must contain - unit-testable on any
machine, including this Windows box for the Linux and macOS cases.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.scheduling import artifacts_for  # noqa: E402

PY = "/usr/bin/python3"
TOOL = "/opt/autoresleep"


def by_name(arts):
    return {a.name.lower(): a for a in arts}


class TestWindowsArtifacts(unittest.TestCase):
    def setUp(self):
        self.arts = by_name(artifacts_for("win32", python_exe=r"C:\Python311\python.exe",
                                         workdir=r"C:\Tools\AutoReSleep", interval_minutes=2,
                                         user=r"BOX\user"))

    def test_resume_trigger_is_wired_to_the_power_troubleshooter_event(self):
        xml = self.arts["autoresleep-judge"].content
        self.assertIn("Microsoft-Windows-Power-Troubleshooter", xml)
        # v1's proven query: provider + EventID in one XPath predicate. A Task Scheduler
        # subscription is XPath, so this is the form that works (not a <Value> element).
        self.assertIn("EventID=1", xml)
        self.assertIn("AutoReSleep", xml)

    def test_the_subscription_is_escaped_text_not_child_nodes(self):
        """The bug that made schtasks answer 'unexpected node: Subscription'.

        Task Scheduler serializes an event subscription as escaped TEXT inside <Subscription>;
        embedding the raw <QueryList> element tree is rejected, and the error message points at a
        column number instead of naming the cause.
        """
        for name in ("autoresleep-judge", "autoresleep-collector"):
            xml = self.arts[name].content
            self.assertIn("&lt;QueryList&gt;", xml)
            self.assertNotIn("<Subscription><QueryList>", xml)

    def test_settings_keep_the_order_windows_itself_emits(self):
        from autoresleep.scheduling.windows import SETTINGS_ORDER, REGISTRATION_ORDER
        for art in self.arts.values():
            body = art.content
            settings = body.split("<Settings>", 1)[1].split("</Settings>", 1)[0]
            present = [e for e in SETTINGS_ORDER if "<%s>" % e in settings]
            positions = [settings.index("<%s>" % e) for e in present]
            self.assertEqual(positions, sorted(positions),
                             "Settings elements are out of schema order: %s" % present)
            reg = body.split("<RegistrationInfo>", 1)[1].split("</RegistrationInfo>", 1)[0]
            present_reg = [e for e in REGISTRATION_ORDER if "<%s>" % e in reg]
            positions_reg = [reg.index("<%s>" % e) for e in present_reg]
            self.assertEqual(positions_reg, sorted(positions_reg),
                             "RegistrationInfo elements are out of order: %s" % present_reg)

    def test_the_action_runs_a_real_command_with_no_shell_redirection(self):
        """A scheduled action is launched directly: `>>` is not a redirection, it is argv noise."""
        for art in self.arts.values():
            args = art.content.split("<Arguments>", 1)[1].split("</Arguments>", 1)[0]
            self.assertNotIn("&gt;&gt;", args)
            self.assertNotIn("2&gt;&amp;1", args)
            self.assertNotIn(">>", args)
            self.assertTrue(args.strip())

    def test_repeat_interval_comes_from_the_config(self):
        self.assertIn("PT2M", self.arts["autoresleep-judge"].content)

    def test_judge_runs_in_the_interactive_user_session_at_highest_level(self):
        xml = self.arts["autoresleep-judge"].content
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)

    def test_listener_starts_at_logon(self):
        self.assertIn("<LogonTrigger>", self.arts["autoresleep-listener"].content)

    def test_tasks_are_registered_in_their_own_folder(self):
        # one folder to inspect, and `schtasks /Delete /TN \AutoReSleep\*` is a usable
        # emergency uninstall if the tool itself cannot be run
        self.assertEqual(self.arts["autoresleep-judge"].path, "\\AutoReSleep\\AutoReSleep-judge")
        self.assertIn("\\AutoReSleep\\", " ".join(self.arts["autoresleep-judge"].apply_cmd))

    def test_remove_uses_the_same_full_path_it_created(self):
        for a in self.arts.values():
            self.assertIn(a.path, " ".join(a.remove_cmd))

    def test_apply_and_remove_use_schtasks_only(self):
        for a in self.arts.values():
            self.assertIn("schtasks", " ".join(a.apply_cmd))
            self.assertIn("schtasks", " ".join(a.remove_cmd))


class TestLauncherAndLiveRegistration(unittest.TestCase):
    """How the action gets invoked, and - on request - whether Windows accepts the document."""

    def test_a_checkout_runs_the_launcher_with_its_backslashes_intact(self):
        import tempfile
        from autoresleep.scheduling.windows import windows_artifacts
        with tempfile.TemporaryDirectory() as td:
            Path(td, "autoresleep.py").write_text("# launcher", encoding="utf-8")
            arts = {a.name.lower(): a for a in
                    windows_artifacts(r"E:\ENV\Python\Python311\python.exe", td, 2)}
            judge = arts["autoresleep-judge"]
            command = judge.content.split("<Command>", 1)[1].split("</Command>", 1)[0]
            args = judge.content.split("<Arguments>", 1)[1].split("</Arguments>", 1)[0]
            # `shlex.split` in POSIX mode eats backslashes: E:\ENV\... -> E:ENV...
            self.assertTrue(command.lower().startswith("e:\\env\\python"), command)
            # a real install must run pythonw.exe so no console window flashes; this machine
            # has one next to python.exe, so the conversion has to have happened
            import os
            if os.path.exists("E:\ENV\Python\Python311\pythonw.exe"):
                self.assertTrue(command.lower().endswith("pythonw.exe"), command)
            self.assertNotIn("E:ENV", judge.content)
            self.assertIn("autoresleep.py", args)
            self.assertIn("judge", args)
            self.assertNotIn("-m autoresleep", args)

    def test_an_interpreter_without_pythonw_keeps_python_exe(self):
        """A missing pythonw.exe must not produce a dangling path - fall back, do not invent."""
        from autoresleep.scheduling.artifact import launcher_parts
        for exe in (r"C:\No\Such\Dir\python.exe", "/usr/bin/python3"):
            program, _ = launcher_parts(exe, "C:\Tools\AutoReSleep", "judge",
                                        None, windowless=True)
            self.assertEqual(program, exe)

    def test_an_installed_package_falls_back_to_the_module_form(self):
        import tempfile
        from autoresleep.scheduling.windows import windows_artifacts
        with tempfile.TemporaryDirectory() as td:          # no launcher here
            arts = {a.name.lower(): a for a in
                    windows_artifacts("python.exe", td, 2)}
            args = arts["autoresleep-judge"].content.split(
                "<Arguments>", 1)[1].split("</Arguments>", 1)[0]
            self.assertIn("-m autoresleep judge", args)

    @unittest.skipUnless(os.environ.get("AR_LIVE_TASK_TESTS") == "1",
                         "opt-in: really registers and deletes a task (AR_LIVE_TASK_TESTS=1)")
    def test_windows_accepts_the_generated_document(self):
        """The only test that can prove the schema: let Task Scheduler itself validate it.

        Runs unelevated by lowering the principal to LeastPrivilege (the rest of the document -
        triggers, action, encoding - is byte-for-byte what install registers).
        """
        import subprocess
        import sys as _sys
        import tempfile
        from autoresleep.scheduling.windows import windows_artifacts, task_path
        workdir = str(Path(__file__).resolve().parents[1])
        arts = {a.name.lower(): a for a in
                windows_artifacts(_sys.executable, workdir, 2)}
        xml = arts["autoresleep-judge"].content.replace(
            "<RunLevel>HighestAvailable</RunLevel>", "<RunLevel>LeastPrivilege</RunLevel>")
        path = Path(tempfile.gettempdir(), "autoresleep-test.xml")
        path.write_text(xml, encoding="utf-16")
        tn = task_path("AutoReSleep-selftest")
        try:
            created = subprocess.run(["schtasks", "/Create", "/TN", tn, "/XML", str(path), "/F"],
                                     capture_output=True)
            self.assertEqual(created.returncode, 0,
                             (created.stdout + created.stderr).decode("cp936", "replace"))
            query = subprocess.run(["schtasks", "/Query", "/TN", tn, "/V", "/FO", "LIST"],
                                   capture_output=True)
            text = (query.stdout + query.stderr).decode("cp936", "replace")
            self.assertIn("autoresleep.py", text)     # the action survived the round trip
        finally:
            subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"], capture_output=True)


class TestTheScheduledJudgeActuallySleeps(unittest.TestCase):
    """A timer that only decides is a timer that never suspends - and reports success forever.

    The CLI defaults to decide-only (safe for a human), so every platform's scheduled action has
    to carry `--apply` explicitly. Missing it is invisible: the task runs, exits 0, logs a
    decision, and the machine stays awake all night.
    """

    def test_every_platform_registers_the_judge_with_apply(self):
        for platform, exe, workdir in (("win32", r"C:\Python311\python.exe", r"C:\Tools\AutoReSleep"),
                                       ("linux", "/usr/bin/python3", "/opt/autoresleep"),
                                       ("darwin", "/usr/bin/python3", "/opt/autoresleep")):
            arts = {a.name.lower(): a for a in
                    artifacts_for(platform, python_exe=exe, workdir=workdir, interval_minutes=2)}
            # names differ per platform (the Linux judge is a .service + .timer pair)
            judge = next(a for k, a in arts.items() if k.startswith("autoresleep-judge"))
            self.assertIn("--apply", judge.content,
                          "%s judge artifact does not suspend anything" % platform)

    def test_the_listener_and_collector_do_not_get_apply(self):
        arts = {a.name.lower(): a for a in
                artifacts_for("linux", python_exe=PY, workdir=TOOL, interval_minutes=2)}
        for name in ("autoresleep-listener.service",):
            self.assertNotIn("--apply", arts[name].content)


class TestServiceAccountPrincipalAndErrorReporting(unittest.TestCase):
    """The last Windows trap: SYSTEM principals carry no <LogonType> in the XML."""

    def test_the_collector_omits_logontype(self):
        arts = by_name(artifacts_for("win32", python_exe=r"C:\Python311\python.exe",
                                     workdir=r"C:\Tools\AutoReSleep", interval_minutes=2))
        collector = arts["autoresleep-collector"].content
        self.assertIn("<UserId>S-1-5-18</UserId>", collector)
        self.assertNotIn("<LogonType>", collector,
                         "the XML enumeration has no ServiceAccount value - Windows itself "
                         "writes SYSTEM principals without a LogonType element")

    def test_interactive_tasks_keep_their_logon_type(self):
        arts = by_name(artifacts_for("win32", python_exe=r"C:\Python311\python.exe",
                                     workdir=r"C:\Tools\AutoReSleep", interval_minutes=2))
        for name in ("autoresleep-judge", "autoresleep-listener"):
            self.assertIn("<LogonType>InteractiveToken</LogonType>", arts[name].content)

    def test_error_excerpt_keeps_the_line_that_names_the_offending_field(self):
        from autoresleep.cli import _error_excerpt
        raw = ("错误: 任务 XML 包含格式不正确或超出范围的值。\r\n"
               "(7,33):LogonType:ServiceAccount\r\n").encode("cp936")
        text = _error_excerpt(raw)
        self.assertIn("LogonType", text)
        self.assertIn("(7,33)", text)

    def test_error_excerpt_handles_empty_output(self):
        from autoresleep.cli import _error_excerpt
        self.assertEqual(_error_excerpt(b""), "")


class TestLinuxArtifacts(unittest.TestCase):
    def setUp(self):
        self.arts = by_name(artifacts_for("linux", python_exe=PY, workdir=TOOL, interval_minutes=2))

    def test_judge_is_a_user_timer(self):
        self.assertIn("OnUnitActiveSec=2min", self.arts["autoresleep-judge.timer"].content)
        self.assertIn("WantedBy=timers.target", self.arts["autoresleep-judge.timer"].content)

    def test_judge_service_invokes_the_module_entry_point(self):
        self.assertIn("-m autoresleep", self.arts["autoresleep-judge.service"].content)
        self.assertIn("judge", self.arts["autoresleep-judge.service"].content)

    def test_listener_is_a_long_running_user_service(self):
        svc = self.arts["autoresleep-listener.service"].content
        self.assertIn("listen", svc)
        self.assertIn("Restart=always", svc)
        self.assertIn("WantedBy=default.target", svc)

    def test_units_live_under_the_user_config_dir(self):
        self.assertTrue(all("systemd/user" in a.path.replace("\\", "/") for a in self.arts.values()))

    def test_no_root_is_needed_to_apply_them(self):
        for a in self.arts.values():
            self.assertIn("systemctl --user", " ".join(a.apply_cmd))


class TestMacArtifacts(unittest.TestCase):
    def setUp(self):
        self.arts = by_name(artifacts_for("darwin", python_exe=PY, workdir=TOOL, interval_minutes=2))

    def test_judge_runs_every_interval_as_a_launch_agent(self):
        plist = self.arts["autoresleep-judge"].content
        self.assertIn("<key>StartInterval</key>", plist)
        self.assertIn("<integer>120</integer>", plist)

    def test_listener_runs_at_load_and_keeps_running(self):
        plist = self.arts["autoresleep-listener"].content
        self.assertIn("<key>RunAtLoad</key>", plist)
        self.assertIn("<true/>", plist)
        self.assertIn("KeepAlive", plist)

    def test_agents_live_under_the_user_launch_agents_dir(self):
        self.assertTrue(all("LaunchAgents" in a.path for a in self.arts.values()))

    def test_apply_uses_launchctl_bootstrap(self):
        for a in self.arts.values():
            self.assertIn("launchctl", " ".join(a.apply_cmd))


class TestUnknownPlatform(unittest.TestCase):
    def test_an_unknown_platform_is_an_explicit_error(self):
        with self.assertRaises(ValueError):
            artifacts_for("plan9", python_exe=PY, workdir=TOOL, interval_minutes=2)


if __name__ == "__main__":
    unittest.main()