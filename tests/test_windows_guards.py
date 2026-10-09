"""The Windows watchdogs: `powercfg /requests` parsed, and WHO is holding each request.

The fixture below is the real output from the machine this was debugged on (elevated, because
`powercfg /requests` refuses a normal caller). It is the answer to "why did my machine not go back
to sleep": a remote-desktop tool held a DISPLAY power request all day, `guarded:display-in-use` fired
on every judgement, and nothing in the log said who - or that the check could be switched off.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.system import windows as win  # noqa: E402

# Real output, Chinese Windows: the 4th header really does come back as "执行:" while the first
# three stay English - which is exactly why the parser goes by POSITION and never by header text.
REAL = """\

DISPLAY:

[PROCESS] \\Device\\HarddiskVolume17\\Program Files (x86)\\CrossDesk\\CrossDesk.exe



SYSTEM:

[DRIVER] Realtek High Definition Audio (HDAUDIO\\FUNC_01&VEN_10EC&DEV_0B00&SUBSYS_10438797&REV_1000\\5&28911aa7&0&0001)

音频流当前正在使用中。

[PROCESS] \\Device\\HarddiskVolume17\\Program Files\\NetEase\\GameViewer\\bin\\GameViewer.exe



AWAYMODE:

无。



执行:

无。



PERFBOOST:

无。



ACTIVELOCKSCREEN:

无。"""

UPDATE_RUNNING = """\
DISPLAY:
无。
SYSTEM:
无。
AWAYMODE:
无。
执行:
[PROCESS] \\Device\\HarddiskVolume3\\Windows\\System32\\TiWorker.exe
PERFBOOST:
无。
ACTIVELOCKSCREEN:
无。"""

REFUSAL = "\r\n此命令需要管理员权限，并且必须从提升的命令提示符中执行。\r\n"


def fake_run(requests_text, tasklist_text="", qwinsta_text=""):
    """A `_run` that answers the three tools `guards()` uses, by argv[0]."""
    def _run(cmd, timeout=None):
        argv = [str(c) for c in cmd]
        if argv and argv[0] == "powercfg":
            return requests_text
        if argv and argv[0] == "tasklist":
            return tasklist_text
        if argv and argv[0] == "qwinsta":
            return qwinsta_text
        return ""
    return _run


def system():
    return object.__new__(win.WindowsSystem)


class TestWhatPowercfgSays(unittest.TestCase):
    def test_the_display_holder_is_named(self):
        with mock.patch.object(win, "_run", side_effect=fake_run(REAL)):
            display, system_, execution, holders, why = system()._power_requests()
        self.assertTrue(display)
        self.assertTrue(system_, "the audio stream in SYSTEM counts as a request")
        self.assertFalse(execution)
        self.assertEqual(holders[0], "CrossDesk.exe")
        self.assertEqual(holders[1], "audio-stream")
        self.assertEqual(holders[3], "")
        self.assertEqual(why, "")

    def test_a_localized_fourth_header_still_lands_in_the_execution_slot(self):
        """`执行:` (not `EXECUTION:`) - parsing by header text would have silently shifted a slot."""
        with mock.patch.object(win, "_run", side_effect=fake_run(UPDATE_RUNNING)):
            display, _system, execution, holders, _why = system()._power_requests()
        self.assertFalse(display)
        self.assertTrue(execution)
        self.assertEqual(holders[3], "TiWorker.exe")

    def test_a_refusal_is_not_read_as_a_clean_machine(self):
        """`powercfg /requests` needs administrator; "no sections" must not mean "no requests"."""
        with mock.patch.object(win, "_run", side_effect=fake_run(REFUSAL)):
            display, system_, execution, holders, why = system()._power_requests()
        self.assertFalse(display)
        self.assertFalse(system_)
        self.assertFalse(execution)
        self.assertEqual(holders, {})
        self.assertEqual(why, "powercfg needs administrator")

    def test_no_output_at_all_is_not_a_reason_either(self):
        with mock.patch.object(win, "_run", side_effect=fake_run("")):
            _d, _s, _e, _h, why = system()._power_requests()
        self.assertEqual(why, "")


class TestTheGuardsOnThatMachine(unittest.TestCase):
    def test_display_in_use_and_the_holder_travel_together(self):
        with mock.patch.object(win, "_run", side_effect=fake_run(REAL)):
            g = system().guards()
        self.assertTrue(g.display_in_use)
        self.assertFalse(g.update_running)
        self.assertEqual(g.active(), "display-in-use")
        self.assertEqual(g.label(), "display-in-use[CrossDesk.exe]")

    def test_a_windows_update_holder_is_named_too(self):
        with mock.patch.object(win, "_run",
                               side_effect=fake_run(UPDATE_RUNNING, tasklist_text="tiworker.exe")):
            g = system().guards()
        self.assertTrue(g.update_running)
        self.assertEqual(g.active(), "update-running")
        self.assertEqual(g.label(), "update-running[TiWorker.exe]")

    def test_a_remote_session_wins_and_is_reported_without_a_holder(self):
        qwinsta = " SESSIONNAME       USERNAME    ID  STATE\n rdp-tcp#3        jianghh      3  Active\n"
        with mock.patch.object(win, "_run", side_effect=fake_run(REAL, qwinsta_text=qwinsta)):
            g = system().guards()
        self.assertEqual(g.active(), "remote-in-use")
        self.assertEqual(g.label(), "remote-in-use")

    def test_a_refusal_surfaces_instead_of_looking_clean(self):
        with mock.patch.object(win, "_run", side_effect=fake_run(REFUSAL)):
            g = system().guards()
        self.assertIsNone(g.active())
        self.assertEqual(g.label(), "?[powercfg needs administrator]")

    def test_masking_the_display_guard_keeps_the_machine_sleepable(self):
        """The whole point of `guard_display_in_use: false` - and it must say so in the line."""
        from autoresleep import cli
        from autoresleep.config import Config
        with mock.patch.object(win, "_run", side_effect=fake_run(REAL)):
            g = cli._apply_guard_config(system().guards(), Config())
        self.assertIsNone(g.active())
        self.assertEqual(g.label(), "ignored:display-in-use[CrossDesk.exe]")


if __name__ == "__main__":
    unittest.main()