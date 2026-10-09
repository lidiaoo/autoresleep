"""Who owns the WoL ports, and does the install admit when it is somebody else?

The lock file only proves that *some* listener is alive - it cannot tell a freshly started one from
a stale one that still owns the magic port. On the real machine that was the exact state: three
processes bound to UDP 9/7/67/68 at once (the current listener plus two stale ones whose kill had
failed) while install printed "listener: running". So install asks the OS instead of trusting the
lock, and this file pins down both halves: the parsing, and the reporting that must not be quiet.
"""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep import cli  # noqa: E402

# Real output from this machine (Chinese Windows, netstat -ano -p UDP, decoded from cp936):
# three pids on port 9 at once - the current listener and two stale ones.
NETSTAT = """\
活动连接

  协议  本地地址          外部地址        状态
  UDP    0.0.0.0:9              *:*                                    28088
  UDP    0.0.0.0:9              *:*                                    13676
  UDP    0.0.0.0:7              *:*                                    34544
  UDP    0.0.0.0:67             *:*                                    13676
  UDP    0.0.0.0:45678          *:*                                    28088
  UDP    127.0.0.1:19009        *:*                                    9999
"""

SS = """\
State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
UNCONN 0      0            0.0.0.0:9        0.0.0.0:*    users:(("python3",pid=28088,fd=5))
UNCONN 0      0            0.0.0.0:7        0.0.0.0:*    users:(("python3",pid=13676,fd=6))
UNCONN 0      0            0.0.0.0:45678    0.0.0.0:*    users:(("python3",pid=28088,fd=7))
UNCONN 0      0          127.0.0.1:19009    0.0.0.0:*    users:(("other",pid=9999,fd=8))
"""


def expects_ownership() -> bool:
    """Mirrors `_listener_port_check`: on this platform the listener is supposed to end up owning
    the magic ports (on Linux as a normal user they legitimately belong to the root sensor)."""
    return (os.name == "nt") or (sys.platform == "darwin") or (
        hasattr(os, "geteuid") and os.geteuid() == 0)


def run_check(owners, pid=28088, ports=(9, 7), marker=45678):
    # tries=1: the polling wrapper is tested on its own; here each snapshot is the one under test
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        with mock.patch.object(cli, "_udp_owners", return_value=owners):
            problems = cli._listener_port_check(pid, list(ports), marker, tries=1)
    return problems, buf.getvalue()


class TestParsing(unittest.TestCase):
    def test_netstat_lines_are_read_with_their_pid(self):
        got = cli._parse_netstat_udp(NETSTAT, {9, 7, 67, 45678})
        self.assertEqual(got[9], {28088, 13676})
        self.assertEqual(got[7], {34544})
        self.assertEqual(got[67], {13676})
        self.assertEqual(got[45678], {28088})

    def test_ports_we_did_not_ask_about_are_ignored(self):
        self.assertNotIn(19009, cli._parse_netstat_udp(NETSTAT, {9, 7}))

    def test_ss_lines_are_read_with_their_pid(self):
        got = cli._parse_ss_udp(SS, {9, 7, 45678})
        self.assertEqual(got[9], {28088})
        self.assertEqual(got[7], {13676})
        self.assertEqual(got[45678], {28088})
        self.assertNotIn(19009, got)

    def test_lsof_style_lines_are_read_too(self):
        out = ("python3  28088 jianghh    5u  IPv4 12345      0t0  UDP *:9\n"
               "python3  13676 jianghh    6u  IPv4 12346      0t0  UDP *:7\n")
        got = cli._parse_ss_udp(out, {9, 7})
        self.assertEqual(got[9], {28088})
        self.assertEqual(got[7], {13676})


class TestCannotCheck(unittest.TestCase):
    def test_a_missing_netstat_is_not_an_all_clear(self):
        if os.name != "nt":
            self.skipTest("windows branch")
        class P:
            stdout = b""
        with mock.patch.object(cli.subprocess, "run", return_value=P()):
            self.assertIsNone(cli._udp_owners([9, 7]))

    def test_cannot_check_says_so_instead_of_reporting_problems(self):
        problems, out = run_check(None)
        self.assertEqual(problems, 0)
        self.assertIn("cannot check ownership", out)


class TestPollingRatherThanOneSnapshot(unittest.TestCase):
    """A just-started listener has not called bind() yet when its lock file appears.

    `cmd_listen` writes the lock file before it calls `bind()`, so a snapshot taken right after the
    lock appears can blame a listener that is perfectly healthy. A race is not a failure.
    """

    def test_the_marker_port_is_actually_asked_about(self):
        """The regression that made every install end with "[failed] nothing holds UDP 45678":
        the OS was only asked about cfg.ports, so the marker port could never be found."""
        from autoresleep import cli
        asked = []

        def fake_owners(ports):
            asked.append(list(ports))
            return {9: {111}, 7: {111}, 45678: {111}}

        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(cli, "_udp_owners", side_effect=fake_owners):
                problems = cli._listener_port_check(111, [9, 7], 45678, tries=1)
        self.assertEqual(problems, 0)
        self.assertIn(45678, asked[0], "the marker port must be part of the question")

    def test_a_late_bind_is_not_reported_as_a_problem(self):
        snapshots = [{9: {111}, 7: {111}},                     # lock exists, bind not finished
                     {9: {111}, 7: {111}, 45678: {111}}]       # ... and now it is
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            with mock.patch.object(cli, "_udp_owners", side_effect=snapshots):
                with mock.patch.object(cli.time, "sleep"):
                    problems = cli._listener_port_check(111, [9, 7], 45678, tries=3)
        self.assertEqual(problems, 0)
        self.assertNotIn("[failed]", buf.getvalue())
        self.assertIn("port 45678  held by pid 111", buf.getvalue())

    def test_a_real_problem_is_still_reported_after_the_retries(self):
        owners = {9: {111}, 7: {111}}                           # never gets the marker port
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            with mock.patch.object(cli, "_udp_owners", return_value=owners):
                with mock.patch.object(cli.time, "sleep"):
                    problems = cli._listener_port_check(111, [9, 7], 45678, tries=3)
        self.assertEqual(problems, 1 if expects_ownership() else 0)
        self.assertIn("nothing holds UDP 45678", buf.getvalue())


class TestReporting(unittest.TestCase):
    def test_a_healthy_install_reports_the_owner(self):
        problems, out = run_check({9: {28088}, 7: {28088}, 45678: {28088}})
        self.assertEqual(problems, 0)
        self.assertIn("port 9      held by pid 28088", out)

    def test_a_stale_listener_on_the_magic_port_is_a_failure_with_a_way_out(self):
        problems, out = run_check({9: {28088, 34544}, 7: {28088}, 45678: {28088}})
        self.assertEqual(problems, 1)
        self.assertIn("[failed]", out)
        self.assertIn("stale listener", out)
        self.assertIn("34544", out)
        self.assertIn("taskkill /PID 34544 /F" if os.name == "nt" else "kill -9 34544", out)

    def test_a_magic_port_owned_by_somebody_else_is_a_failure(self):
        problems, out = run_check({9: {13676}, 7: {28088}, 45678: {28088}})
        self.assertEqual(problems, 1 if expects_ownership() else 0)
        self.assertIn("13676", out)

    def test_a_port_nobody_holds_means_the_listener_is_not_watching(self):
        problems, out = run_check({7: {28088}, 45678: {28088}})
        self.assertEqual(problems, 1 if expects_ownership() else 0)
        self.assertIn("nothing holds UDP 9", out)

    def test_the_dhcp_ports_are_information_only(self):
        """67/68 belong to the DHCP client by design - crying wolf about them would be noise.

        Both shapes count as information: somebody (the DHCP client, pid 4) holds them, or nobody
        does at all on a machine with no DHCP lease.
        """
        problems, out = run_check({9: {28088}, 7: {28088}, 67: {4, 28088}, 68: {}, 45678: {28088}},
                                 ports=(9, 7, 67, 68))
        self.assertEqual(problems, 0)
        self.assertIn("the DHCP client - expected", out)

    def test_the_marker_port_counts_too(self):
        problems, out = run_check({9: {28088}, 7: {28088}})
        self.assertIn("nothing holds UDP 45678", out)
        self.assertEqual(problems, 1 if expects_ownership() else 0)


if __name__ == "__main__":
    unittest.main()