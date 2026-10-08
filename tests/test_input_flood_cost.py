"""The watcher must stay cheap when a device floods.

Measured on the real box: after a resume the mouse reported continuously at roughly a thousand
events a second. The first version wrote one audit line and rewrote `input-last.json` for every
one of those events - an open/write/close per event plus a fresh file and a rename - and because
`select` returns immediately while data is pending, the reader loop spun as fast as it could. That
is the 100% CPU and the 71 MB log. These tests pin the cost per event at zero file writes and the
cost per flood at a bounded number of lines.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoresleep.inputwatch import (                        # noqa: E402
    AUDIT_MAX_BYTES, EVENT, FLOOD_PAUSE_S, READ_EVENTS, STATE_WRITE_S, InputWatcher,
)


class FloodCostTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="iw-flood-"))
        self.state = self.dir / "input-last.json"
        self.audit = self.dir / "input-events.log"
        self.watcher = InputWatcher(self.state, audit_file=self.audit)

    def _lines(self) -> list:
        if not self.audit.exists():
            return []
        return [ln for ln in self.audit.read_text(encoding="utf-8").splitlines() if ln]

    def test_a_flood_of_events_costs_one_audit_line(self):
        for _ in range(1000):
            self.watcher._audit("Mouse", "motion", 0, -1)
        self.assertEqual(self._lines(), [])            # nothing written per event
        self.watcher.flush_audit(force=True)
        lines = self._lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("Mouse", lines[0])
        self.assertIn("n=1000", lines[0])
        self.assertIn("0:1000", lines[0])

    def test_discarded_motion_is_counted_apart_from_real_input(self):
        for _ in range(80):
            self.watcher._audit("Mouse", "motion", 0, -1, "settling:ignored")
        self.watcher._audit("Keyboard", "key", 30, 1)
        self.watcher.flush_audit(force=True)
        text = "\n".join(self._lines())
        self.assertIn("settling:ignored", text)
        self.assertIn("Keyboard", text)

    def test_flush_is_rate_limited_unless_forced(self):
        self.watcher._audit("Mouse", "motion", 0, -1)
        self.watcher.flush_audit()                     # first flush always lands
        self.assertEqual(len(self._lines()), 1)
        self.watcher._audit("Mouse", "motion", 1, 1)
        self.watcher.flush_audit()                     # too soon: deferred
        self.assertEqual(len(self._lines()), 1)
        self.watcher.flush_audit(force=True)
        self.assertEqual(len(self._lines()), 2)

    def test_audit_file_is_capped(self):
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        self.audit.write_text("x" * (AUDIT_MAX_BYTES + 10), encoding="utf-8")
        self.watcher._audit("Mouse", "motion", 0, -1)
        self.watcher.flush_audit(force=True)
        rotated = self.audit.with_name(self.audit.name + ".1")
        self.assertTrue(rotated.exists(), "the oversized log must be rolled away")
        self.assertLess(self.audit.stat().st_size, AUDIT_MAX_BYTES)

    def test_state_file_is_not_rewritten_per_event(self):
        self.watcher._note_event(epoch=11.0, beat=100.0, dev="Mouse", kind="motion")
        self.watcher.flush_state(force=False)
        self.assertTrue(self.state.exists(), "the first event must reach the file")
        first = self.state.read_text(encoding="utf-8")
        # 400 more events inside the interval: the file must not be touched again
        before = self.state.stat().st_mtime_ns
        for i in range(400):
            self.watcher._note_event(epoch=12.0 + i, beat=100.0, dev="Mouse", kind="motion")
            self.watcher.flush_state(force=False)
        self.assertEqual(self.state.read_text(encoding="utf-8"), first)
        self.assertEqual(self.state.stat().st_mtime_ns, before)

    def test_quiet_stream_flushes_the_newest_event_at_once(self):
        self.watcher._note_event(epoch=None, beat=1.0, dev="Mouse", kind="motion")
        self.watcher.flush_state(force=True)           # as poll() does when nothing was seen
        self.watcher._note_event(epoch=4321.0, beat=2.0, dev="Keyboard", kind="key")
        self.watcher.flush_state(force=True)
        got = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(got["epoch"], 4321.0)
        self.assertEqual(got["dev"], "Keyboard")

    def test_the_last_accepted_event_wins_not_the_first(self):
        self.watcher._note_event(epoch=1.0, beat=1.0, dev="Mouse", kind="motion")
        self.watcher._note_event(epoch=2.0, beat=2.0, dev="Keyboard", kind="key")
        self.watcher.flush_state(force=True)
        got = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((got["epoch"], got["dev"]), (2.0, "Keyboard"))

    def test_read_budget_is_bounded(self):
        self.assertGreaterEqual(READ_EVENTS, 16)
        self.assertLessEqual(READ_EVENTS, 256)
        self.assertGreater(FLOOD_PAUSE_S, 0.0)
        self.assertLessEqual(FLOOD_PAUSE_S, 0.05)
        self.assertLessEqual(STATE_WRITE_S, 5.0)

    #: Windows' select() cannot wait on an os.pipe() handle (sockets only), so poll() there
    #: returns 0 events immediately - which would make BOTH of these assertions meaningless
    #: (the "no pause" one would pass without ever reading an event). POSIX only.
    @unittest.skipUnless(os.name == "posix", "drives poll() through an os.pipe()")
    def test_poll_pauses_on_a_full_read_instead_of_spinning(self):
        """A full read means more is waiting, so the loop would spin without the pause."""
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        blob = b"".join(EVENT.pack(i, 0, 1, 30, 1) for i in range(READ_EVENTS))
        self.watcher.fds = {r: "Keyboard"}
        self.watcher.settle_until = 0.0
        os.write(w, blob + blob)                  # more waiting than one read takes
        t0 = time.monotonic()
        self.watcher.poll(timeout=0.01)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, FLOOD_PAUSE_S * 0.5)

    @unittest.skipUnless(os.name == "posix", "drives poll() through an os.pipe()")
    def test_poll_does_not_pause_on_a_partial_read(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        os.write(w, EVENT.pack(1, 0, 1, 30, 1))
        self.watcher.fds = {r: "Keyboard"}
        self.watcher.settle_until = 0.0
        t0 = time.monotonic()
        self.watcher.poll(timeout=0.01)
        self.assertLess(time.monotonic() - t0, FLOOD_PAUSE_S)


if __name__ == "__main__":
    unittest.main()
