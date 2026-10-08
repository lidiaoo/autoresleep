"""Slice 7: the platform layer, and the CLI's refusal to assume it is on Windows.

These are the tests that make the cross-platform claim checkable from any one machine: the
Windows backend is exercised for real here, and the Linux/macOS backends are constructed and
driven through `doctor` to prove the dispatch works and that neither crashes when its OS tools
are missing (which is exactly the situation on a fresh machine with a half-installed system).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

# Everything in this file builds Windows backends and drives `doctor` for all three platforms,
# which runs OS probes and logs while it does. Keep the module in a throwaway state dir: the
# logger keys its file handler on the data dir, so this really is isolation - otherwise test
# noise lands in the machine's own journal.log and muddies a real diagnosis.
_STATE = tempfile.TemporaryDirectory()
mock.patch.dict(os.environ, {"LOCALAPPDATA": _STATE.name, "XDG_STATE_HOME": _STATE.name}).start()


from autoresleep import paths  # noqa: E402
from autoresleep.cli import main  # noqa: E402
from autoresleep.pkt import Adapter, adapters  # noqa: E402
from autoresleep.system import get_system  # noqa: E402


class TestDispatch(unittest.TestCase):
    def test_all_three_backends_are_constructible(self):
        for plat in ("win32", "linux", "darwin"):
            backend = get_system(plat)
            self.assertEqual(backend.name, plat)
            self.assertTrue(backend.capabilities())

    def test_an_unsupported_platform_says_so(self):
        with self.assertRaises(ValueError):
            get_system("plan9")

    def test_every_backend_implements_the_required_interface(self):
        for plat in ("win32", "linux", "darwin"):
            backend = get_system(plat)
            idle = backend.idle()
            # "-1 + an explanation" is a valid answer; crashing or silently returning 0 is not
            self.assertIsInstance(idle.seconds, float)
            if idle.seconds < 0:
                self.assertTrue(idle.method or idle.error)

    def test_wake_source_never_raises_and_keeps_its_evidence(self):
        for plat in ("win32", "linux", "darwin"):
            src = get_system(plat).wake_source()
            self.assertTrue(src.kind)
            self.assertTrue(src.method)


class TestCliIsPlatformAgnostic(unittest.TestCase):
    def test_doctor_runs_for_every_backend(self):
        for plat in ("win32", "linux", "darwin"):
            rc = main(["doctor", "--platform", plat])
            self.assertEqual(rc, 0, "doctor failed for %s" % plat)


@unittest.skipUnless(sys.platform == "win32", "Windows-only integration check")
class TestWindowsBackendForReal(unittest.TestCase):
    def test_idle_probe_measures_something_plausible(self):
        idle = get_system("win32").idle()
        self.assertGreaterEqual(idle.seconds, 0.0)
        self.assertLess(idle.seconds, 60 * 60 * 24 * 49)   # sanity: less than the tick wrap
        self.assertTrue(idle.tick.isdigit())

    def test_adapter_inventory_reports_macs_this_machine_has(self):
        found = adapters()
        self.assertTrue(found, "no adapters found at all")
        for a in found:
            self.assertRegex(a.mac, r"^[0-9a-f]{12}$", "MAC is normalised to 12 lowercase hex")

    def test_the_real_wifi_adapter_is_recognised_by_name_not_by_a_literal(self):
        # v1's rule under test: the wake-source string is rebuilt from THIS machine, so a card
        # swap or a driver reinstall (which appends ' #N') cannot silently break attribution
        backend = get_system("win32")
        wifi = [a for a in adapters() if a.wireless and "virtual" not in a.name.lower()]
        self.assertTrue(wifi, "expected at least one physical Wi-Fi adapter on this box")
        target = wifi[0]
        kind, who = backend._attribute(target.label or target.name, timer=False)
        self.assertEqual(kind, "wifi-nic")
        self.assertTrue(who)

    def test_a_foreign_device_name_is_not_mistaken_for_our_nic(self):
        backend = get_system("win32")
        kind, _ = backend._attribute("Some Vendor USB Audio Device", timer=False)
        self.assertEqual(kind, "other-device")


class TestInstallWithoutPrivileges(unittest.TestCase):
    """One command for the operator: the user-level half runs here, the privileged half elevates
    itself once - and nothing on this machine is ever left half-installed."""

    @unittest.skipUnless(sys.platform == "win32", "the elevation requirement is Windows-only")
    def test_install_without_privileges_hands_the_privileged_half_over(self):
        """v1 made the operator open an administrator terminal; this one asks for it itself.

        What must not change: it never fails HALFWAY and it never leaves a half-install. The
        user-level half runs here; the privileged half is either delegated (one UAC prompt, the
        hidden child's progress read out in this console) or - with elevation switched off - handed
        over as an exact command.
        """
        from autoresleep.system import get_system
        if get_system("win32").is_admin():
            self.skipTest("this shell is already elevated")
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            # AUTORESLEEP_NO_ELEVATE: a test suite must never raise a UAC prompt on a desktop
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "AUTORESLEEP_NO_ELEVATE": "1"}):
                rc = main(["install"])
            names = {p.name for p in Path(td).rglob("*")}
        self.assertEqual(rc, 2, "the privileged half was not done - the exit code must say so")
        self.assertIn("config.json", names)
        # the user-level half DID happen: the documents the elevated child will register
        self.assertIn("AutoReSleep-judge.xml", names)

    def test_the_privileged_half_is_delegated_exactly_once(self):
        """One elevation, not a second terminal - and no UAC prompt in this test either."""
        import os
        import tempfile
        from unittest import mock
        from autoresleep import elevate
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td}):
                with mock.patch.object(elevate, "delegate", return_value=0) as delegated:
                    with mock.patch.object(elevate, "is_elevated", return_value=False):
                        # --no-start: the real start path waits up to 20s for the listener's lock
                        # file, and no listener is going to appear inside a temporary data dir
                        rc = main(["install", "--no-start"])
        self.assertEqual(rc, 0)
        delegated.assert_called_once()
        self.assertEqual(delegated.call_args[0][0][0], "install")

    def test_dry_run_needs_no_privileges_anywhere(self):
        self.assertEqual(main(["install", "--dry-run"]), 0)


class TestConsoleDecoding(unittest.TestCase):
    """The localized-Windows trap: status words come back in the machine's code page."""

    def test_a_gbk_status_line_survives_decoding(self):
        from autoresleep.system.base import decode_console
        # '状态  ...  关闭' as a cp936 byte string - decoding this as cp1252 produces mojibake
        raw = "状态                                  关闭\r\n".encode("cp936")
        text = decode_console(raw)
        self.assertIn("关闭", text)

    def test_utf8_output_is_left_alone(self):
        from autoresleep.system.base import decode_console
        self.assertEqual(decode_console("State: ON\n".encode("utf-8")), "State: ON\n")

    def test_utf16_output_is_detected(self):
        from autoresleep.system.base import decode_console
        self.assertIn("WakeTime", decode_console("WakeTime=x".encode("utf-16")))

    def test_empty_input_is_empty_not_an_exception(self):
        from autoresleep.system.base import decode_console
        self.assertEqual(decode_console(b""), "")

    @unittest.skipUnless(sys.platform == "win32", "netsh is Windows-only")
    def test_the_firewall_parser_reads_a_real_state(self):
        """Regression for the silent failure: a broken decoder made this 'unknown'."""
        from autoresleep.system import get_system
        fw = get_system("win32").firewall()
        self.assertIn(fw.state, ("enabled", "disabled", "mixed"),
                      "firewall state came back %r - the parser or the decoder is broken" % fw.state)


class TestConsoleToolsRunHidden(unittest.TestCase):
    """Every console program must be started without a window.

    The judge runs under pythonw.exe, and Windows gives a console-less process a NEW console
    window for each console program it starts - `powercfg` is called on every judge run, so the
    screen blinked every 30 seconds until this was fixed.
    """

    def test_windows_child_processes_get_create_no_window(self):
        from autoresleep.system.base import proc_kwargs
        kw = proc_kwargs()
        if sys.platform == "win32":
            self.assertEqual(kw.get("creationflags"), 0x08000000)     # CREATE_NO_WINDOW
            info = kw.get("startupinfo")
            self.assertIsNotNone(info, "no STARTUPINFO means a window can still appear")
            self.assertTrue(info.dwFlags & 0x1)                       # STARTF_USESHOWWINDOW
            self.assertEqual(info.wShowWindow, 0)                     # SW_HIDE
        else:
            self.assertEqual(kw, {}, "these flags are Windows-only and must not leak")

    def test_no_console_call_escapes_the_hidden_helper(self):
        """Source-level guard: a bare `subprocess.run([...])` may not come back."""
        import re
        root = Path(__file__).resolve().parents[1] / "src" / "autoresleep"
        offenders = []
        for f in sorted(root.rglob("*.py")):
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r"subprocess\.run\(", text):
                window = text[m.start():m.start() + 220]
                if "proc_kwargs" not in window and "run_hidden" not in window:
                    offenders.append("%s:%d" % (f.name, text[:m.start()].count(chr(10)) + 1))
        self.assertEqual(offenders, [], "console call without hidden flags: %s" % offenders)


class TestDataDirIsWritable(unittest.TestCase):
    def test_ensure_data_dir_creates_a_writable_directory(self):
        # this module runs in a throwaway state dir (see the top of the file), so the
        # property under test is "the dir gets created and is writable", not "it is the
        # machine's live one"
        d = paths.ensure_data_dir()
        self.assertTrue(d.is_dir())
        probe = d / "self-test.tmp"
        probe.write_text("ok", encoding="utf-8")
        self.assertEqual(probe.read_text(encoding="utf-8"), "ok")
        probe.unlink()


class TestInstallDryRunTouchesNothing(unittest.TestCase):
    """`--dry-run` is a promise, and on a new machine it is the first thing anyone runs."""

    def test_dry_run_creates_no_files_and_no_tasks(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            key = "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"
            with mock.patch.dict(os.environ, {key: td}):
                before = {p for p in Path(td).rglob("*")}
                rc = main(["install", "--dry-run"])
                after = {p for p in Path(td).rglob("*")}
            self.assertEqual(rc, 0)
            self.assertEqual(before, after,
                             "dry run created: %s" % sorted(str(p) for p in after - before))


class TestLinuxRemoteSessionGuard(unittest.TestCase):
    """A local graphical login must NOT count as a remote session.

    Measured on the real box: `who` prints `(:0)` for the local X display and the old
    `(`-in-line heuristic read that as a remote host, so guards() reported remote-in-use on
    every desktop machine and NOTHING ever went back to sleep.
    """

    def test_local_display_is_not_remote(self):
        from autoresleep.system.linux import LinuxSystem
        who = "jianghh  tty7         2026-09-28 16:11 (:0)\n" \
              "jianghh  pts/0        2026-09-28 16:11 (:0)\n"
        self.assertFalse(LinuxSystem.who_says_remote(who))

    def test_named_host_is_remote(self):
        from autoresleep.system.linux import LinuxSystem
        who = "jianghh  pts/1        2026-09-28 20:01 (192.168.0.10)\n" \
              "jianghh  pts/2        2026-09-28 20:02 (laptop.lan)\n"
        self.assertTrue(LinuxSystem.who_says_remote(who))

    def test_localhost_is_not_remote(self):
        from autoresleep.system.linux import LinuxSystem
        who = "jianghh  pts/3        2026-09-28 20:03 (localhost)\n"
        self.assertFalse(LinuxSystem.who_says_remote(who))

    def test_only_local_sshd_listens_count_as_remote_use(self):
        from autoresleep.system.linux import LinuxSystem
        # someone connected TO this machine's sshd (local column) = remote use
        inbound = "ESTAB 0      0       192.168.0.215:22       192.168.0.10:52242\n"
        # this machine ssh-ing OUT (peer column) is NOT remote usage of this machine
        outbound = "ESTAB 0      0       192.168.0.215:48176    203.0.113.9:22\n"
        self.assertTrue(LinuxSystem.ss_says_remote(inbound))
        self.assertFalse(LinuxSystem.ss_says_remote(outbound))
        self.assertFalse(LinuxSystem.ss_says_remote(""))


if __name__ == "__main__":
    unittest.main()

class TestAnIdleSoundServerWinsOverTheKernel(unittest.TestCase):
    """PipeWire keeps the analog PCM in `state: RUNNING` - silently streaming - while nothing
    plays at all, so the kernel reading alone made a quiet machine look busy forever and it never
    re-slept. The sound server knows which streams are real; the kernel reading stays as the
    fallback for machines that have no sound server to ask."""

    def setUp(self):
        from autoresleep.system.linux import LinuxSystem
        self.sys = LinuxSystem()
        self._tmp = tempfile.TemporaryDirectory()
        self.status = Path(self._tmp.name) / "status"
        self.status.write_text("state: RUNNING\nowner_pid   : 1481\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _kernel(self, running=True):
        files = [str(self.status)] if running else []
        return mock.patch("autoresleep.system.linux.glob.glob", return_value=files)

    def _which(self, *tools):
        return mock.patch("autoresleep.system.linux.shutil.which",
                          side_effect=lambda name: "/usr/bin/" + name if name in tools else None)

    def _rc(self, rc, out):
        return mock.patch("autoresleep.system.linux._run_rc", return_value=(rc, out))

    def test_an_idle_sound_server_beats_the_kernel_reading(self):
        with self._which("pactl"), self._rc(0, ""), self._kernel(True):
            self.assertFalse(self.sys._media_playing())

    def test_a_running_stream_is_playing(self):
        out = ('Sink Input #54\n\tState: RUNNING\n\tCorked: no\n'
               '\tapplication.name = "firefox"\n')
        with self._which("pactl"), self._rc(0, out), self._kernel(True):
            self.assertTrue(self.sys._media_playing())

    def test_a_corked_stream_is_not_playing(self):
        out = "Sink Input #54\n\tCorked: yes\n\tState: CORKED\n"
        with self._which("pactl"), self._rc(0, out), self._kernel(False):
            self.assertFalse(self.sys._media_playing())

    def test_an_unreachable_sound_server_falls_back_to_the_kernel(self):
        # exit code != 0 means "could not ask" (headless, no session bus) - not "nothing playing";
        # the safe direction there is to believe the kernel and stay awake
        with self._which("pactl"), self._rc(1, ""), self._kernel(True):
            self.assertTrue(self.sys._media_playing())

    def test_no_sound_server_at_all_falls_back_to_the_kernel(self):
        with self._which(), self._kernel(True):
            self.assertTrue(self.sys._media_playing())
        with self._which(), self._kernel(False):
            self.assertFalse(self.sys._media_playing())

    def test_pipewire_dump_is_understood_when_pactl_is_absent(self):
        running = ('[{"id": 54, "info": {"state": "running", '
                   '"props": {"media.class": "Stream/Output/Audio"}}}]')
        idle = ('[{"id": 54, "info": {"state": "idle", '
                '"props": {"media.class": "Stream/Output/Audio"}}}, '
                '{"id": 55, "info": {"state": "running", '
                '"props": {"media.class": "Audio/Sink"}}}]')
        with self._which("pw-dump"), self._rc(0, running), self._kernel(True):
            self.assertTrue(self.sys._media_playing())
        with self._which("pw-dump"), self._rc(0, idle), self._kernel(True):
            self.assertFalse(self.sys._media_playing())

    def test_a_broken_pipewire_dump_is_not_trusted(self):
        with self._which("pw-dump"), self._rc(0, "not json"), self._kernel(True):
            self.assertTrue(self.sys._media_playing())
