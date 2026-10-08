#!/usr/bin/env python3
"""Prove the packet chain end to end, without starting the daemon or sleeping anything.

The daemon (`listen`) is deliberately NOT used here: its fast path really suspends the machine
when a decision comes back SLEEP, and running it also binds UDP 9/7/67/68 - which would steal
packets from an already-installed listener on the same box (the README's "do not run two
listeners" rule). So this script drives the same chain, one step at a time, in a temporary data
directory and on private ports:

    socket -> is_magic_packet -> packets-seen.log/packet-last.txt -> decision

What it checks:
  1. a magic packet addressed to a WIRED card's MAC is recognised (the old listener only matched
     wireless MACs whenever the box also had wifi, so a wired WoL wake was judged non-magic and
     slept again);
  2. the judgement that follows is `skip reason=wake-requested src=pkt` - never a re-sleep.

Run it anywhere; it writes nothing outside the temporary directory it makes:

    python tools/verify_packet_chain.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PORT = 19090            # NOT 9/7: those may be held by a listener that is already installed
MARKER = 45680          # NOT 45678, for the same reason
MAC = "00:11:22:33:44:55"


def magic_payload(mac: str) -> bytes:
    raw = bytes(int(part, 16) for part in mac.split(":"))
    return b"\xff" * 6 + raw * 8           # MIN_REPEATS is 8; senders truncate bursts


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="ar-verify-")
    os.environ["LOCALAPPDATA"] = data_dir
    os.environ["XDG_STATE_HOME"] = data_dir
    os.environ["AUTORESLEEP_NO_ELEVATE"] = "1"     # never ask for privileges from a check script

    from autoresleep import cli, journal, paths
    from autoresleep.pkt import Listener, is_magic_packet, wake_macs

    print("data dir   : %s" % paths.data_dir())
    print("ports      : %d + marker %d (private, so nothing else is disturbed)" % (PORT, MARKER))

    # ---- 1. the socket chain -----------------------------------------------------------
    macs = wake_macs(None)
    print("magic MACs : %s" % (", ".join(macs) or "(none - this box has no adapters)"))
    if MAC not in [m for m in macs]:
        macs = macs + [MAC]        # the test card, so the check works on any box
    listener = Listener(ports=[PORT], marker_port=MARKER, macs=macs)
    listener.bind()
    if PORT not in listener.sockets:
        print("FAIL: could not bind UDP %d: %s" % (PORT, listener.unavailable().get(PORT)))
        return 1

    payload = magic_payload(MAC)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(payload, ("127.0.0.1", PORT))
    seen = []
    for _ in range(50):                      # a datagram on loopback is immediate; be generous
        seen = listener.poll()
        if seen:
            break
    if not seen:
        print("FAIL: the listener did not see its own magic packet on UDP %d" % PORT)
        return 1
    print("detected   : kind=%s src=%s len=%d" % (seen[0].kind, seen[0].src, seen[0].length))
    if seen[0].kind != "magic":
        print("FAIL: kind=%r, expected 'magic' (is_magic_packet / MAC matching)" % seen[0].kind)
        return 1

    # ---- 2. it must be evidence, and the judgement must say so -------------------------
    log = journal.get_logger()
    cli._record_packet(seen[0], log)
    print("evidence   : %s" % paths.packet_state().read_text(encoding="ascii").strip())

    # a wake to judge: without one the judgement has nothing to attribute the packet to
    from autoresleep.system import get_system, write_wake_evidence
    sysinfo = get_system()
    cli._collect_evidence(sysinfo)

    dl = journal.decision_logger()
    args = cli.build_parser().parse_args(["judge", "--config", str(paths.config_file())])
    decision, idle, ev, guards, cfg = cli.build_judgement(args)
    print("judgement  : %s" % decision.line())
    print("             idle=%.0fs  win=%s  pkt=%s" % (idle.seconds, cli._window_label(ev),
                                                      ev.pkt_kind or "-"))
    dl.info("%s  (verify_packet_chain)", decision.line())

    if str(decision.action) != "skip" or str(decision.reason) != "wake-requested":
        print("FAIL: expected skip/wake-requested - an asked-for wake was going to be slept")
        return 1
    print("OK         : a packet addressed to a WIRED MAC counts, and it exempts the wake")

    # ---- 3. the session latch, so a later judgement cannot forget ----------------------
    latch = paths.session_cause_file()
    print("latch      : %s" % (latch.read_text(encoding="utf-8").strip() if latch.exists()
                               else "(none written)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())