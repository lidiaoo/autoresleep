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


class TestInstallRefusesWithoutPrivileges(unittest.TestCase):
    """A half-finished install is worse than a refusal: refuse first, tell them the command."""

    @unittest.skipUnless(sys.platform == "win32", "the elevation requirement is Windows-only")
    def test_install_without_admin_returns_2_and_changes_nothing(self):
        from autoresleep.system import get_system
        if get_system("win32").is_admin():
            self.skipTest("this shell is already elevated")
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": td}):
                before = {p for p in Path(td).rglob("*")}
                rc = main(["install"])
                after = {p for p in Path(td).rglob("*")}
            self.assertEqual(rc, 2, "install must refuse (rc=2), not fail halfway")
            self.assertEqual(before, after)

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
    def test_ensure_data_dir_creates_the_real_directory(self):
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


if __name__ == "__main__":
    unittest.main()