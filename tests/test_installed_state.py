"""`_installed_state()` - the one probe the menu and doctor both read.

Why this needs its own test file: the difference between "the task is not there" and "this account
is not allowed to see it" is invisible in a naive check, and the menu would then offer to
"overwrite install" on a machine where nothing is installed - or worse, report a healthy machine as
empty. Every case below is one side of that distinction.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli  # noqa: E402


class _FakeRun:
    """A `subprocess.run` stand-in: argv prefix -> (returncode, stdout); listed tools are missing."""

    def __init__(self, table=None, missing=()):
        self.table = table or {}
        self.missing = set(missing)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        tool = os.path.basename(cmd[0])
        if tool in self.missing:
            raise FileNotFoundError(tool)
        for key, (rc, out) in self.table.items():
            if cmd[:len(key)] == list(key):
                return SimpleNamespace(returncode=rc, stdout=out, stderr=b"")
        raise AssertionError("unexpected command: %r" % (cmd,))


WIN = "win32"
LINUX = "linux"
MAC = "darwin"

TASKS = ["AutoReSleep-judge", "AutoReSleep-listener", "AutoReSleep-collector"]


def _uid():
    """The uid the production code would use - os.getuid does not exist on Windows."""
    return getattr(os, "getuid", lambda: 0)()


def _csv(*names):
    """A schtasks /Query /FO CSV /NH listing, the way it really comes back (quoted, backslashes)."""
    return "\n".join('"\\\\AutoReSleep\\\\%s","N/A","Ready"' % n for n in names).encode("ascii")


class _StateBase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(cli.paths, "config_file",
                                    side_effect=lambda plat=None: Path("/nonexistent/config.yaml"))
        self.cfg_patch = patcher.start()
        self.addCleanup(patcher.stop)

    def _state(self, plat, fake, *, elevated=False, exists=False, lock=None, alive=True,
               config=None, missing=()):
        cfg = Path(config) if config else Path("/nonexistent/config.yaml")
        with mock.patch.object(cli.subprocess, "run", fake), \
             mock.patch.object(cli, "_is_elevated", return_value=elevated), \
             mock.patch.object(cli, "_path_exists", return_value=exists), \
             mock.patch.object(cli, "_lock_pid", return_value=lock), \
             mock.patch.object(cli, "_pid_alive", return_value=alive), \
             mock.patch.object(cli.paths, "config_file", return_value=cfg):
            return cli._installed_state(plat=plat)


class TestWindows(_StateBase):
    def test_three_registered_tasks_are_all_seen(self):
        fake = _FakeRun({
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-judge"): (0, b""),
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-listener"): (0, b""),
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-collector"): (0, b""),
            ("schtasks", "/Query", "/FO", "CSV", "/NH"): (0, _csv(*TASKS)),
        })
        st = self._state(WIN, fake)
        self.assertEqual(sorted(st.present), sorted(TASKS))
        self.assertEqual(st.absent, [])
        self.assertEqual(st.unknown, [])
        self.assertTrue(st.installed)

    def test_a_task_we_may_not_see_is_unknown_not_missing(self):
        """The collector is SYSTEM-owned: a non-elevated query fails for it exactly like for a task
        that is not installed. Saying "missing" here is the lie this test exists to prevent."""
        fake = _FakeRun({
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-judge"): (0, b""),
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-listener"): (0, b""),
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-collector"): (1, b"ERROR"),
            ("schtasks", "/Query", "/FO", "CSV", "/NH"): (0, _csv("AutoReSleep-judge",
                                                                  "AutoReSleep-listener")),
        })
        st = self._state(WIN, fake, elevated=False)
        self.assertIn("AutoReSleep-collector", st.unknown)
        self.assertNotIn("AutoReSleep-collector", st.absent)
        self.assertTrue(st.installed, "two visible tasks already prove the install is here")

    def test_with_admin_rights_the_same_task_is_reported_missing(self):
        fake = _FakeRun({
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-judge"): (0, b""),
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-listener"): (1, b"ERROR"),
            ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-collector"): (1, b"ERROR"),
            ("schtasks", "/Query", "/FO", "CSV", "/NH"): (0, _csv("AutoReSleep-judge")),
        })
        st = self._state(WIN, fake, elevated=True)
        self.assertEqual(sorted(st.absent), ["AutoReSleep-collector", "AutoReSleep-listener"])

    def test_a_machine_that_cannot_be_listed_makes_no_claims(self):
        fake = _FakeRun({("schtasks", "/Query", "/FO", "CSV", "/NH"): (1, b"ERROR"),
                         ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-judge"): (1, b""),
                         ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-listener"): (1, b""),
                         ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-collector"): (1, b"")})
        st = self._state(WIN, fake, elevated=False)
        self.assertFalse(st.installed)
        self.assertEqual(st.absent, [], "an unlistable machine is not an empty machine")
        self.assertEqual(sorted(st.unknown), sorted(TASKS))

    def test_nothing_installed_and_admin_rights_says_so(self):
        fake = _FakeRun({("schtasks", "/Query", "/FO", "CSV", "/NH"): (0, b"no tasks here"),
                         ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-judge"): (1, b""),
                         ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-listener"): (1, b""),
                         ("schtasks", "/Query", "/TN", r"\AutoReSleep\AutoReSleep-collector"): (1, b"")})
        st = self._state(WIN, fake, elevated=True)
        self.assertFalse(st.installed)
        self.assertEqual(sorted(st.absent), sorted(TASKS))
        self.assertEqual(st.unknown, [])


class TestLinux(_StateBase):
    def test_units_are_asked_through_systemctl(self):
        fake = _FakeRun({
            ("systemctl", "--user", "is-enabled", "autoresleep-judge.service"): (0, b"enabled"),
            ("systemctl", "--user", "is-enabled", "autoresleep-judge.timer"): (0, b"enabled"),
            ("systemctl", "--user", "is-enabled", "autoresleep-listener.service"): (0, b"enabled"),
            ("systemctl", "is-enabled", "autoresleep-wol-sensor.service"): (1, b"not-found"),
        })
        st = self._state(LINUX, fake, exists=True, elevated=False)
        self.assertEqual(len(st.present), 4, "the root-owned sensor unit is there as a file")
        self.assertTrue(st.installed)
        self.assertEqual(st.unknown, [])

    def test_without_systemd_the_files_are_the_evidence(self):
        fake = _FakeRun(missing=["systemctl"])
        st = self._state(LINUX, fake, exists=True)
        self.assertEqual(len(st.present), 4)
        self.assertTrue(st.installed)

    def test_without_systemd_and_without_files_there_is_nothing(self):
        fake = _FakeRun(missing=["systemctl"])
        st = self._state(LINUX, fake, exists=False)
        self.assertFalse(st.installed)
        self.assertEqual(sorted(st.absent), ["autoresleep-judge.service",
                                             "autoresleep-judge.timer",
                                             "autoresleep-listener.service",
                                             "autoresleep-wol-sensor.service"])


class TestMacOs(_StateBase):
    def test_agents_are_asked_through_launchctl(self):
        fake = _FakeRun({
            ("launchctl", "print", "gui/%d/com.autoresleep.judge" % _uid()): (0, b""),
            ("launchctl", "print", "gui/%d/com.autoresleep.listener" % _uid()): (0, b""),
        })
        st = self._state(MAC, fake)
        self.assertEqual(sorted(st.present), ["autoresleep-judge", "autoresleep-listener"])
        self.assertTrue(st.installed)

    def test_a_not_loaded_agent_still_counts_if_its_plist_is_there(self):
        fake = _FakeRun({
            ("launchctl", "print", "gui/%d/com.autoresleep.judge" % _uid()): (1, b"no such"),
            ("launchctl", "print", "gui/%d/com.autoresleep.listener" % _uid()): (1, b"no such"),
        })
        st = self._state(MAC, fake, exists=True)
        self.assertEqual(len(st.present), 2, "installed but not loaded is still installed")


class TestTheFactsNextToTheJobs(_StateBase):
    def test_a_lock_file_pointing_at_a_dead_pid_is_a_note_not_a_listener(self):
        fake = _FakeRun(missing=["schtasks"])
        st = self._state(WIN, fake, lock=4242, alive=False)
        self.assertIsNone(st.listener_pid)
        self.assertTrue(any("4242" in n for n in st.notes), st.notes)

    def test_a_live_pid_is_reported(self):
        fake = _FakeRun(missing=["schtasks"])
        st = self._state(WIN, fake, lock=4242, alive=True)
        self.assertEqual(st.listener_pid, 4242)

    def test_the_config_file_alone_is_not_an_installation(self):
        """Uninstall keeps config.yaml on purpose - so its presence must never read as "installed"."""
        fake = _FakeRun(missing=["schtasks", "systemctl", "launchctl"])
        st = self._state(LINUX, fake, exists=False, config=__file__)
        self.assertIsNotNone(st.config)
        self.assertFalse(st.installed)

    def test_an_unsupported_platform_does_not_crash(self):
        fake = _FakeRun(missing=["schtasks"])
        st = self._state("sunos", fake)
        self.assertFalse(st.installed)
        self.assertTrue(any("产物清单" in n for n in st.notes), st.notes)


if __name__ == "__main__":
    unittest.main()