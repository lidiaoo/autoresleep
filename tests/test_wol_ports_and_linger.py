"""What the listener can and cannot see, and whether the units survive a logout.

Both are "looks fine, protects nothing" failures: a listener that silently watches fewer ports
than it was configured with, and a `--user` unit that is torn down at logout (`Linger=no`) on a
machine nobody logs into. Neither shows up as an error, so both have to be said out loud.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# the repo root holds the entry script `autoresleep.py`, which shadows the package of the same
# name - every test file bootstraps `src` itself so a single file can be run on its own
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.pkt import DHCP_PORTS, Listener, WOL_PORTS, unprivileged_port_start  # noqa: E402


class _FakeSockets(object):
    """A socket factory where `bind()` fails for whichever ports the test names."""

    def __init__(self, denied=(), in_use=()):
        self.denied = set(denied)
        self.in_use = set(in_use)
        self.bound = []

    def __call__(self, *args, **kwargs):
        outer = self

        class _Sock(object):
            def setsockopt(self, *a):
                pass

            def bind(self, addr):
                port = addr[1]
                if port in outer.denied:
                    raise PermissionError(13, "Permission denied")
                if port in outer.in_use:
                    raise OSError(98, "Address already in use")
                outer.bound.append(port)

            def setblocking(self, flag):
                pass

            def recvfrom(self, n):
                raise BlockingIOError

            def close(self):
                pass

        return _Sock()


def _listener(denied=(), in_use=()):
    fake = _FakeSockets(denied, in_use)
    with mock.patch("autoresleep.pkt.socket.socket", fake):
        lsn = Listener()
        lsn.bind()
    return lsn, fake


class TestUnprivilegedPortStart(unittest.TestCase):
    def test_the_kernel_value_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "start"
            p.write_text("9\n", encoding="utf-8")
            self.assertEqual(unprivileged_port_start(str(p)), 9)

    def test_a_missing_or_garbled_file_is_none_not_a_crash(self):
        self.assertIsNone(unprivileged_port_start("/nonexistent/start"))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "start"
            p.write_text("banana", encoding="utf-8")
            self.assertIsNone(unprivileged_port_start(str(p)))


class TestAWolPortThatCannotBeBoundIsNeverSilent(unittest.TestCase):
    """On Linux the WoL ports are privileged and this daemon is an ordinary user. The symptom of
    a partial bind is not an error message - it is a magic-packet wake re-slept two minutes later
    because the packet was never seen."""

    def test_privileged_ports_that_refuse_say_exactly_what_to_do(self):
        # `udp_ports_in_use` reads the REAL /proc/net/udp, and on a machine where the privileged
        # sensor already runs these ports really are held - so pin the "nobody holds them" case
        lsn, _ = _listener(denied=(7, 9, 67, 68))
        self.assertEqual(sorted(lsn.unsupported_wol_ports()), [7, 9])
        with mock.patch("autoresleep.pkt.udp_ports_in_use", return_value=set()):
            hint = lsn.advice() or ""
        self.assertIn("7/9", hint)
        self.assertIn("sudo sysctl -w net.ipv4.ip_unprivileged_port_start=7", hint)
        self.assertIn("45678", hint)

    def test_the_boundary_value_is_quoted_from_the_kernel(self):
        lsn, _ = _listener(denied=(9,))
        with mock.patch("autoresleep.pkt.unprivileged_port_start", return_value=1024), \
             mock.patch("autoresleep.pkt.udp_ports_in_use", return_value=set()):
            self.assertIn("unprivileged_port_start=1024", lsn.advice() or "")

    def test_dhcp_ports_held_by_the_client_are_not_worth_a_word(self):
        # routine: the DHCP client owns 67/68 and no magic packet is sent there
        lsn, fake = _listener(denied=(67, 68))
        self.assertEqual(lsn.unsupported_wol_ports(), {})
        self.assertIsNone(lsn.advice())
        self.assertEqual(sorted(lsn.sockets), [7, 9, 45678])

    def test_another_process_holding_the_ports_is_not_a_privilege_alarm(self):
        # `doctor`/`install` probe the ports while the real listener already holds them
        lsn, _ = _listener(in_use=tuple(WOL_PORTS) + DHCP_PORTS + (45678,))
        hint = lsn.advice() or ""
        self.assertIn("held by another process", hint)
        self.assertNotIn("sysctl", hint)

    def test_nothing_bound_and_not_a_conflict_is_the_severe_case(self):
        lsn, _ = _listener(denied=tuple(WOL_PORTS) + DHCP_PORTS + (45678,))
        with mock.patch("autoresleep.pkt.udp_ports_in_use", return_value=set()):
            hint = lsn.advice() or ""
        self.assertIn("no UDP port could be bound", hint)
        self.assertIn("45678", hint)

    def test_a_healthy_listener_needs_no_advice(self):
        lsn, fake = _listener()
        self.assertEqual(sorted(lsn.sockets), sorted(list(WOL_PORTS) + list(DHCP_PORTS) + [45678]))
        with mock.patch("autoresleep.pkt.udp_ports_in_use", return_value=set()):
            self.assertIsNone(lsn.advice())

    def test_the_sensor_does_not_steal_the_marker_port(self):
        # marker_port=None: the privileged half watches UDP 9/7 only, so the user listener keeps
        # the marker socket (and with it the only port it can bind without privileges)
        lsn, fake = _listener()
        self.assertIn(45678, lsn._bind_ports)
        sensor = Listener(ports=WOL_PORTS, marker_port=None)
        self.assertEqual(sensor._bind_ports, [9, 7])
        self.assertNotIn(45678, sensor._bind_ports)

    def test_the_privileged_sensor_advises_without_a_marker_port(self):
        # The sensor builds `Listener(marker_port=None)`. When it cannot bind 9/7 (the capability
        # is missing) advice() must not raise on the absent marker port - that is the exact crash
        # a plain `autoresleep wol-sensor` run hit: "%d format: a real number is required, not
        # NoneType". It must still say what to do about the privileged ports.
        fake = _FakeSockets(denied=(7, 9, 67, 68))
        with mock.patch("autoresleep.pkt.socket.socket", fake):
            sensor = Listener(ports=WOL_PORTS, marker_port=None)
            sensor.bind()
        self.assertEqual(sorted(sensor.unsupported_wol_ports()), [7, 9])
        with mock.patch("autoresleep.pkt.udp_ports_in_use", return_value=set()):
            hint = sensor.advice() or ""
        self.assertIn("7/9", hint)
        self.assertIn("ip_unprivileged_port_start", hint)
        self.assertNotIn("marker packet to port", hint)   # there is no marker port to suggest


class TestLingerIsReported(unittest.TestCase):
    """A `--user` unit with `Linger=no` dies at logout: after a reboot without a login there is
    nothing watching the machine at all."""

    def _linger(self, out, user="tester"):
        from autoresleep.scheduling.linux import linger_enabled
        with mock.patch.dict(os.environ, {"USER": user, "LOGNAME": user}):
            with mock.patch("autoresleep.system.linux._run", return_value=out):
                return linger_enabled()

    def test_no_is_false(self):
        self.assertIs(self._linger("no\n"), False)

    def test_yes_is_true(self):
        self.assertIs(self._linger("yes\n"), True)

    def test_an_unknown_answer_is_none_not_a_guess(self):
        self.assertIsNone(self._linger(""))
        self.assertIsNone(self._linger("maybe"))


if __name__ == "__main__":
    unittest.main()


class TestWhoHoldsThePortIsReadFromTheKernel(unittest.TestCase):
    """`/proc/net/udp` (port column in hex, state 07 = listening) is how the unprivileged half
    tells "I may not bind this" apart from "somebody already watches it" - the second is the
    privileged sensor doing its job, not a problem to warn about."""

    HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
              "   uid  timeout inode ref pointer drops\n")

    def _proc(self, entries):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "udp"
        path.write_text(self.HEADER + "".join(entries), encoding="utf-8")
        return td, path

    def test_listening_ports_are_recognised_from_hex(self):
        from autoresleep.pkt import udp_ports_in_use
        td, path = self._proc([
            "  10: 00000000:0009 00000000:0000 07 00000000:00000000 00:00000000 00000000"
            "  1000        0 12345 1 0000000000000000 0\n",
            "  11: 00000000:B26E 00000000:0000 07 00000000:00000000 00:00000000 00000000"
            "  1000        0 12346 1 0000000000000000 0\n",
        ])
        with td:
            self.assertEqual(udp_ports_in_use({9, 45678, 7}, str(path)), {9, 45678})

    def test_a_connected_socket_is_not_a_listener(self):
        from autoresleep.pkt import udp_ports_in_use
        td, path = self._proc([
            "  12: 0100007F:0035 00000000:0000 01 00000000:00000000 00:00000000 00000000"
            "  1000        0 12347 1 0000000000000000 0\n",
        ])
        with td:
            self.assertEqual(udp_ports_in_use({53}, str(path)), set())

    def test_an_unreadable_proc_file_is_an_empty_set_not_a_crash(self):
        from autoresleep.pkt import udp_ports_in_use
        self.assertEqual(udp_ports_in_use({9}, "/nonexistent/udp"), set())

    def test_a_listener_that_cannot_bind_but_is_covered_does_not_warn(self):
        lsn, _ = _listener(denied=(7, 9))
        with mock.patch("autoresleep.pkt.udp_ports_in_use", return_value={7, 9}):
            hint = lsn.advice() or ""
        self.assertIn("watched by another process", hint)
        self.assertNotIn("sudo sysctl", hint)

    def test_only_a_partial_cover_still_warns(self):
        lsn, _ = _listener(denied=(7, 9))
        with mock.patch("autoresleep.pkt.udp_ports_in_use", return_value={9}):
            hint = lsn.advice() or ""
        self.assertIn("sudo sysctl", hint)

