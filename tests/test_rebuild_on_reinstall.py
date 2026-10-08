"""Re-installing must REBUILD the job, not overwrite it (pitfalls 7).

Measured behaviour of the three managers, all of which fail silently in the same direction:
  * `schtasks /Create /XML /F` replaces the definition but keeps whatever the old task carried - a
    stale principal, old triggers, an ACL somebody set by hand;
  * `launchctl bootstrap` on a label that is still loaded just fails, and the agent keeps running
    with the old ProgramArguments;
  * `systemctl enable --now` on a unit that is already running does NOT restart it, so a re-install
    with a changed unit body (a new capability, a new ExecStart) would keep the old process alive.
So every artifact needs a best-effort "remove what is there now" step BEFORE its create step.
"""
import io
import os
import sys
import contextlib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli  # noqa: E402
from autoresleep.scheduling import artifacts_for  # noqa: E402

PY = "/usr/bin/python3"
TOOL = "/work"


def by_name(arts):
    return {a.name: a for a in arts}


class TestWindowsTasksAreRebuilt(unittest.TestCase):
    def setUp(self):
        self.tasks = by_name(artifacts_for("win32", python_exe=PY, workdir=TOOL, interval_minutes=2))

    def test_every_task_deletes_before_it_creates(self):
        for name, a in self.tasks.items():
            self.assertTrue(a.prepare_cmd, "%s has no rebuild step" % name)
            flat = " ".join(a.prepare_cmd[0])
            self.assertIn("schtasks", flat)
            self.assertIn("/Delete", flat)
            self.assertIn(a.path, flat, "the delete must name the very task being created")
            self.assertIn("/Create", " ".join(a.apply_cmd))

    def test_the_delete_is_forced_so_it_never_waits_for_a_prompt(self):
        # `schtasks /Delete` without /F asks for confirmation ON STDIN - and a hidden child cannot
        # answer it, which is precisely the "script hangs until you press enter" failure
        for name, a in self.tasks.items():
            self.assertIn("/F", a.prepare_cmd[0], name)


class TestTheLinuxSensorIsRestarted(unittest.TestCase):
    def setUp(self):
        self.arts = by_name(artifacts_for("linux", python_exe=PY, workdir=TOOL, interval_minutes=2))

    def test_the_root_unit_is_disabled_before_it_is_written_again(self):
        sensor = self.arts["autoresleep-wol-sensor.service"]
        self.assertTrue(sensor.prepare_cmd)
        flat = " ".join(" ".join(cmd) for cmd in sensor.prepare_cmd)
        self.assertIn("systemctl", flat)
        self.assertIn("disable", flat)
        self.assertIn("--now", flat, "disable alone leaves the old process running")
        self.assertIn(sensor.name, flat)
        # ... and the enable comes after the daemon-reload, or systemd has not seen the new body
        self.assertIn("daemon-reload", " ".join(sensor.apply_cmd))
        self.assertIn("enable", " ".join(sensor.apply_more[0]))

    def test_the_user_units_need_no_rebuild_step(self):
        # the listener is stopped and restarted by install itself (it holds a pid lock), and the
        # judge is a oneshot: every run is a fresh process, so there is no stale body to worry about
        for name in ("autoresleep-judge.service", "autoresleep-listener.service"):
            self.assertFalse(self.arts[name].prepare_cmd, name)


class TestMacosAgentsAreRebuilt(unittest.TestCase):
    def setUp(self):
        self.arts = by_name(artifacts_for("darwin", python_exe=PY, workdir=TOOL, interval_minutes=2))

    def test_the_domain_is_a_real_uid_not_a_shell_substitution(self):
        # `gui/$(id -u)` can never work: these argv's are run with no shell, so nothing expands it
        for name, a in self.arts.items():
            for cmd in list(a.prepare_cmd) + [a.apply_cmd, a.remove_cmd]:
                flat = " ".join(cmd)
                self.assertNotIn("$(", flat, name)
                self.assertIn("gui/", flat, name)

    def test_bootout_runs_before_bootstrap(self):
        for name, a in self.arts.items():
            self.assertTrue(a.prepare_cmd, name)
            self.assertIn("bootout", " ".join(a.prepare_cmd[0]))
            self.assertIn("bootstrap", " ".join(a.apply_cmd))
            self.assertIn(name.split("autoresleep-")[-1], " ".join(a.apply_cmd))


class TestTheRebuildOrderIsEnforced(unittest.TestCase):
    def test_prepare_then_apply_then_apply_more(self):
        from autoresleep.scheduling.artifact import Artifact
        a = Artifact(name="x", path="/p", content="", kind="unit",
                     prepare_cmd=[["prepare"]], apply_cmd=["apply"], apply_more=[["after"]])
        seen = []
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(cli, "_apply_cmd",
                                   side_effect=lambda cmd, dry, **kw: seen.append((list(cmd), kw))):
                cli._apply_artifact(a, False)
        self.assertEqual([cmd for cmd, _kw in seen], [["prepare"], ["apply"], ["after"]])
        self.assertTrue(seen[0][1].get("best_effort"),
                        "a missing thing to remove is normal on a first install")
        self.assertFalse(seen[1][1].get("best_effort"), "the create step is not best-effort")

    def test_a_flat_argv_is_treated_as_one_command(self):
        """`prepare_cmd=["cmd", "arg"]` instead of `[["cmd", "arg"]]` used to run "c", "m", "d"."""
        from autoresleep.scheduling.artifact import Artifact
        a = Artifact(name="x", path="/p", content="", kind="unit",
                     prepare_cmd=["sudo", "systemctl", "disable", "x"], apply_cmd=["apply"])
        seen = []
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(cli, "_apply_cmd",
                                   side_effect=lambda cmd, dry, **kw: seen.append(list(cmd))):
                cli._apply_artifact(a, False)
        self.assertEqual(seen[0], ["sudo", "systemctl", "disable", "x"])

    def test_remove_runs_remove_cmd_then_remove_more(self):
        from autoresleep.scheduling.artifact import Artifact
        a = Artifact(name="x", path="/p", content="", kind="unit",
                     remove_cmd=["stop"], remove_more=[["rm"], ["reload"]])
        seen = []
        with mock.patch.object(cli, "_apply_cmd",
                               side_effect=lambda cmd, dry, **kw: seen.append(list(cmd))):
            cli._remove_artifact(a, False)
        self.assertEqual(seen, [["stop"], ["rm"], ["reload"]])


class TestFailureWords(unittest.TestCase):
    """A best-effort step must not print 'failed' - that word has to keep its meaning."""

    def _run(self, best_effort: bool) -> str:
        cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._apply_cmd(cmd, False, best_effort=best_effort)
        return buf.getvalue()

    def test_a_missing_thing_is_a_note(self):
        out = self._run(True)
        self.assertIn("[note]", out)
        self.assertNotIn("[failed]", out)

    def test_a_real_failure_is_still_a_failure(self):
        self.assertIn("[failed]", self._run(False))


if __name__ == "__main__":
    unittest.main()