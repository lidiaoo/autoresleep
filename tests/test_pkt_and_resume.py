"""Slices 3+4: the two mechanisms that have to behave identically on all three OSes.

pkt   - magic packet / sender marker detection over plain UDP sockets (stdlib only, so it
        needs no privileges and no capture driver on Windows, Linux or macOS).
resume- the suspend/resume signal. v1 needed per-OS hooks (a Task Scheduler event trigger,
        a systemd-sleep script, a launchd job). A machine that just spent 8 hours asleep
        shows a big skew between the monotonic and the wall clock, and that skew is visible
        on every platform with no privileges at all - so the *transport* of that signal
        becomes portable even though the rest of each OS's plumbing is not.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.pkt import is_magic_packet, kind_of, marker_seen, parse_kind  # noqa: E402
from autoresleep.resume import ResumeDetector  # noqa: E402

MAC = "28d0ea27400c"
OTHER = "aabbccddeeff"


def magic(mac=MAC, repeats=16):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + raw * repeats


class TestMagicPacket(unittest.TestCase):
    def test_real_magic_packet_is_recognised(self):
        self.assertTrue(is_magic_packet(magic(), [MAC]))

    def test_mac_with_dashes_or_colons_matches_differently_formatted_detection(self):
        self.assertTrue(is_magic_packet(magic(), ["28:D0:EA:27:40:0C"]))
        self.assertTrue(is_magic_packet(magic(), ["28-D0-EA-27-40-0C"]))

    def test_multiple_candidate_macs_any_of_them_matches(self):
        self.assertTrue(is_magic_packet(magic(OTHER), [MAC, OTHER]))

    def test_short_magic_packet_is_rejected(self):
        # v1 accepted >=8 repeats because senders truncate; 4 is not a magic packet
        self.assertFalse(is_magic_packet(magic(repeats=4), [MAC]))

    def test_no_ff_prefix_is_rejected(self):
        self.assertFalse(is_magic_packet(b"\x00" * 6 + bytes.fromhex(MAC) * 16, [MAC]))

    def test_packet_for_another_mac_is_rejected(self):
        self.assertFalse(is_magic_packet(magic(OTHER), [MAC]))

    def test_marker_payload_is_recognised_as_an_asked_for_wake(self):
        self.assertTrue(marker_seen(b"HERMES-WOL-MARKER wake me", 45678, 45678))
        self.assertFalse(marker_seen(b"random datagram", 45678, 45678))


class TestKindParsing(unittest.TestCase):
    def test_kind_of_magic_and_marker(self):
        self.assertEqual(kind_of(magic(), [MAC], 9, 45678), "magic")
        self.assertEqual(kind_of(b"HERMES-WOL-MARKER", [MAC], 45678, 45678), "marker")
        self.assertIsNone(kind_of(b"dhcp-ish noise", [MAC], 68, 45678))

    def test_parse_kind_reads_back_the_state_file_format(self):
        self.assertEqual(parse_kind("magic|epoch=1759000000|src=192.168.1.9:9|len=102"), "magic")
        self.assertEqual(parse_kind("marker|epoch=1759000000|src=192.168.1.9:45678|len=16"), "marker")
        self.assertIsNone(parse_kind(""))
        self.assertIsNone(parse_kind("garbage"))


class TestPortValidation(unittest.TestCase):
    def test_an_out_of_range_port_is_rejected_before_the_os_is_touched(self):
        from autoresleep.pkt import Listener
        with self.assertRaises(ValueError):
            Listener(ports=[9], marker_port=154678).bind()

    def test_zero_is_rejected(self):
        from autoresleep.pkt import Listener
        with self.assertRaises(ValueError):
            Listener(ports=[0], marker_port=45678).bind()


class TestListenerOverRealSockets(unittest.TestCase):
    """The one part of the packet chain worth testing with real I/O.

    Uses high ports on purpose: v1's listener (and any other WoL listener) may already hold
    9/7/67/68/45678, and on Windows the socket that bound FIRST receives the unicast datagrams -
    measured here: v2's listener bound successfully alongside v1's and then never saw a packet
    that v1 logged. A real magic packet must still be recognised by THIS code.
    """

    def test_a_real_magic_packet_and_marker_are_detected(self):
        import socket
        import time as _time
        from autoresleep.pkt import Listener

        port, marker = 19009, 15467
        listener = Listener(ports=[port], marker_port=marker, macs=[MAC])
        listener.bind()
        self.assertIn(port, listener.sockets, "could not bind the test port")
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(magic(), ("127.0.0.1", port))
            sender.sendto(b"HERMES-WOL-MARKER hello", ("127.0.0.1", marker))
            seen = []
            deadline = _time.time() + 5
            while _time.time() < deadline and len(seen) < 2:
                seen.extend(listener.poll())
                _time.sleep(0.05)
            sender.close()
        finally:
            listener.close()

        kinds = sorted(s.kind for s in seen)
        self.assertEqual(kinds, ["magic", "marker"])
        self.assertTrue(all(s.src.startswith("127.0.0.1") for s in seen))
        self.assertIn("epoch=", seen[0].line())

    def test_a_packet_for_another_mac_is_not_reported(self):
        import socket
        import time as _time
        from autoresleep.pkt import Listener

        port = 19010
        listener = Listener(ports=[port], marker_port=15468, macs=[MAC])
        listener.bind()
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(magic(OTHER), ("127.0.0.1", port))
            _time.sleep(0.3)
            seen = listener.poll()
            sender.close()
        finally:
            listener.close()
        self.assertEqual(seen, [])

    def test_a_second_listener_cannot_share_the_port(self):
        """The measured bug this class used to work around with high ports.

        `bind()` set SO_REUSEADDR, which on Windows means the opposite of the Unix meaning - it
        PERMITS a second process to bind the same address, and then which socket receives a datagram
        is not ours to decide. Measured on this machine: three processes bound to UDP 9/7/67/68 at
        once (the current listener plus two stale ones whose kill had failed), so a magic packet
        could be delivered to a listener whose judge is not the running one - and nothing anywhere
        said so. Now the second bind must FAIL, visibly, so the operator is told the port is taken
        instead of living with a packet chain that silently stops seeing packets.
        """
        from autoresleep.pkt import Listener

        port, marker = 19011, 15469
        first = Listener(ports=[port], marker_port=None, macs=[MAC])
        first.bind()
        self.assertIn(port, first.sockets, "could not bind the test port")
        try:
            second = Listener(ports=[port], marker_port=None, macs=[MAC])
            second.bind()
            try:
                self.assertNotIn(port, second.sockets,
                                 "the second listener took a port that was already held - "
                                 "magic packets can now go to either process")
                self.assertIn(port, second.unavailable())
                # `advice()` is deliberately silent about a non-WoL port (it speaks up for 9/7 and
                # the marker only), so the thing to insist on here is the recorded failure itself.
                self.assertNotIn(port, second.unsupported_wol_ports())
            finally:
                second.close()
        finally:
            first.close()

    def test_the_shared_bind_flag_is_never_set(self):
        """`_exclusive` must ask for exclusivity, whatever the platform calls it."""
        import socket as _socket
        from autoresleep.pkt import _exclusive

        calls = []

        class FakeSock:
            def setsockopt(self, level, name, value):
                calls.append((level, name, value))

        _exclusive(FakeSock())
        self.assertTrue(calls, "no socket option was set at all")
        level, name, value = calls[0]
        self.assertEqual(level, _socket.SOL_SOCKET)
        if hasattr(_socket, "SO_EXCLUSIVEADDRUSE"):
            self.assertEqual(name, _socket.SO_EXCLUSIVEADDRUSE)
        else:
            self.assertEqual(name, _socket.SO_REUSEADDR)
            self.assertEqual(value, 0, "POSIX must NOT enable address reuse")


class TestResumeDetector(unittest.TestCase):
    def test_no_resume_while_the_clocks_run_together(self):
        d = ResumeDetector(threshold_s=90.0)
        self.assertFalse(d.observe(mono=1000.0, wall=1_000_000.0))
        self.assertFalse(d.observe(mono=1060.0, wall=1_000_060.0))   # 60 s later, no sleep

    def test_a_big_wall_clock_skew_is_a_resume(self):
        d = ResumeDetector(threshold_s=90.0)
        d.observe(mono=1000.0, wall=1_000_000.0)
        # machine slept for 2 hours: wall clock jumped, monotonic did not
        self.assertTrue(d.observe(mono=1002.0, wall=1_007_200.0))

    def test_small_ntp_corrections_are_not_resumes(self):
        d = ResumeDetector(threshold_s=90.0)
        d.observe(mono=1000.0, wall=1_000_000.0)
        self.assertFalse(d.observe(mono=1001.0, wall=1_000_030.0))   # +30 s drift, below threshold

    def test_monotonic_clock_restart_counts_as_a_resume(self):
        # Windows/Linux monotonic is boot-relative; after a resume some platforms restart it
        d = ResumeDetector(threshold_s=90.0)
        d.observe(mono=5000.0, wall=1_000_000.0)
        self.assertTrue(d.observe(mono=1.0, wall=1_000_005.0))


if __name__ == "__main__":
    unittest.main()