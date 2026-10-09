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

    def test_the_resident_listener_has_no_time_limit(self):
        """`PT10M` (the default here) made Task Scheduler terminate the listener ten minutes in.

        Measured on the real machine: pid 31824 started 08:53:12, last heartbeat 09:02:49, task's
        last result 0x41306 ("the task was terminated") - and `doctor` could then bind UDP 9/7/45678
        because nothing was watching them. A resident listener must run until logoff.
        """
        xml = self.arts["autoresleep-listener"].content
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)

    def test_the_oneshot_tasks_keep_a_runaway_limit(self):
        """The judge and the collector are short-lived, so a cap there is a safety net, not a bug."""
        for name in ("autoresleep-judge", "autoresleep-collector"):
            xml = self.arts[name].content
            self.assertNotIn("PT0S", xml, name)
            self.assertIn("<ExecutionTimeLimit>PT", xml)

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

    def test_user_units_live_under_the_user_config_dir(self):
        user_units = [a for a in self.arts.values() if not a.needs_root]
        self.assertTrue(all("systemd/user" in a.path.replace("\\", "/") for a in user_units))
        self.assertEqual(len(user_units), 3)

    def test_exactly_one_artifact_is_root_owned_and_that_is_the_sensor(self):
        # UDP 9/7 are privileged ports: a --user listener cannot bind them, and the failure is
        # silent (a magic-packet wake re-slept because nobody saw the packet). Everything else
        # stays a user unit, because logind refuses `suspend` to a process with no session.
        root_owned = [a for a in self.arts.values() if a.needs_root]
        self.assertEqual([a.name for a in root_owned], ["autoresleep-wol-sensor.service"])
        self.assertEqual(root_owned[0].path, "/etc/systemd/system/autoresleep-wol-sensor.service")

    def test_no_root_is_needed_for_the_user_units(self):
        for a in self.arts.values():
            if a.needs_root:
                self.assertIn("sudo", " ".join(a.apply_cmd))
                continue
            self.assertIn("systemctl --user", " ".join(a.apply_cmd))
            self.assertNotIn("sudo", " ".join(a.apply_cmd))

    def test_apply_cmds_are_raw_argv_no_shell_operators(self):
        # _apply_cmd runs subprocess.run(argv) WITHOUT a shell: a literal "&&" becomes an
        # argument to systemctl ("Too many arguments") - the bug that broke every Linux apply
        for a in self.arts.values():
            cmds = [a.apply_cmd, a.remove_cmd, a.start_cmd] + list(a.apply_more) + \
                   list(a.remove_more)
            for cmd in cmds:
                for token in cmd:
                    for op in ("&&", "|", ";", ">"):
                        self.assertNotIn(op, token,
                                         "%s in %s" % (op, " ".join(cmd)))

    def test_only_units_with_install_sections_get_enabled(self):
        # judge.service is static (no [Install]) - `systemctl enable` on it fails; its timer
        # is what carries the schedule. regression guard for the install failure of 2026-09-29.
        self.assertEqual(self.arts["autoresleep-judge.service"].apply_cmd,
                         ["systemctl", "--user", "daemon-reload"])
        self.assertEqual(self.arts["autoresleep-judge.timer"].apply_cmd,
                         ["systemctl", "--user", "enable", "--now", "autoresleep-judge.timer"])
        self.assertEqual(self.arts["autoresleep-listener.service"].apply_cmd,
                         ["systemctl", "--user", "enable", "--now", "autoresleep-listener.service"])

    def test_judge_timer_fires_after_login_not_only_after_boot(self):
        # a --user manager starts at login; OnBootSec only fires for lingering users, so the
        # first judge run after install would never happen. OnActiveSec counts from enable.
        # (the explanatory comment in the template mentions OnBootSec - assert on directives only)
        timer = "\n".join(l for l in self.arts["autoresleep-judge.timer"].content.splitlines()
                          if not l.strip().startswith("#"))
        self.assertIn("OnActiveSec=1min", timer)
        self.assertNotIn("OnBootSec", timer)


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


class TestThePrivilegedSensorUnit(unittest.TestCase):
    """One root-owned unit, and the reason it is not the whole listener.

    UDP 9/7 are privileged ports, so the half that watches them has to live outside the user
    manager - but a *system* listener could not suspend anything (logind refuses `suspend` to a
    process with no session), so the sensor only records and the user listener decides."""

    def setUp(self):
        self.arts = by_name(artifacts_for("linux", python_exe=PY, workdir=TOOL,
                                          interval_minutes=2))
        self.unit = self.arts["autoresleep-wol-sensor.service"]

    def test_it_runs_as_the_operator_not_as_root(self):
        self.assertIn("User=", self.unit.content)
        self.assertNotIn("User=root", self.unit.content)
        self.assertIn("Environment=HOME=", self.unit.content)
        self.assertIn("Group=", self.unit.content)

    def test_it_gets_exactly_the_one_capability_it_needs(self):
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", self.unit.content)
        self.assertIn("CapabilityBoundingSet=CAP_NET_BIND_SERVICE", self.unit.content)
        # ambient capabilities cannot be granted under NoNewPrivileges
        self.assertIn("NoNewPrivileges=no", self.unit.content)

    def test_it_runs_the_sensor_mode_and_restarts(self):
        self.assertIn("wol-sensor", self.unit.content)
        self.assertIn("Restart=always", self.unit.content)
        self.assertIn("WantedBy=multi-user.target", self.unit.content)

    def test_install_stages_it_and_hands_it_to_sudo_install(self):
        self.assertTrue(self.unit.needs_root)
        self.assertIn("sudo", " ".join(self.unit.apply_cmd))
        # the system manager needs its OWN daemon-reload before the unit can be enabled
        self.assertEqual(self.unit.apply_cmd, ["sudo", "systemctl", "daemon-reload"])
        self.assertEqual(self.unit.apply_more[0],
                         ["sudo", "systemctl", "enable", "--now",
                          "autoresleep-wol-sensor.service"])

    def test_uninstall_stops_deletes_and_reloads(self):
        self.assertIn("disable", " ".join(self.unit.remove_cmd))
        lines = [" ".join(c) for c in self.unit.remove_more]
        self.assertTrue(any("rm -f" in l for l in lines))
        self.assertTrue(any("daemon-reload" in l for l in lines))

