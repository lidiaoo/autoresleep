"""The wifi-event listener (`sudo iw event`), the portable answer to "did the WIFI wake us?".

`iw event` taps nl80211 and prints what the wireless subsystem itself does - disconnect,
associate, auth - for every driver, in one uniform format. That is the generality the user asked
for: a driver's private wake-reason string exists only when that driver feels like printing one,
but nl80211 events are the shared protocol layer underneath all of them.

The listener runs inside the privileged sensor and records events to a small log with timestamps;
the judgement never asks a driver anything, it only reads this file and asks "did the wifi move
around the time the machine resumed?".
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional

#: the nl80211 lines that mean "the link moved": a drop, a (re)association, or a roam.
#: Roams matter as much as drops - measured on the real box, the wifi changes BSSID while
#: `iw dev wlan0 link` keeps answering "Connected".
#: Written against the lines the kernel ACTUALLY prints, captured on the real box with
#: `sudo iw event -t | stdbuf -oL` while scanning and reconnecting:
#:   1790738340.015097: wlp5s0 (phy #0): mgmt TX status (cookie 135): acked
#:   1790738412.501000: wlp5s0 (phy #0): disconnected (by AP) reason: 3 (DEAUTH_LEAVING)
#:   1790738412.777000: wlp5s0 (phy #0): new 44:df:65:cd:00:00 station became authorized
#: The MAC sits BETWEEN "new" and "station", so a literal "new station" never matches a real
#: association - which is how a re-association went unnoticed while the tool called the link
#: "intact". Match the real shapes, not the shape it looked like it should have.
MOVE_PATTERNS = (
    # No trailing \b on the stems: the kernel writes "DEAUTH_LEAVING" and "deauthenticated",
    # where a word boundary after "deauth" does not exist, and a pattern that only matched the
    # noun "deauth" silently missed every real line.
    re.compile(r"disconnect", re.I),
    re.compile(r"deauth", re.I),
    re.compile(r"disassoc", re.I),
    re.compile(r"\bassociat", re.I),
    re.compile(r"\bauthenticat", re.I),
    re.compile(r"became (authorized|authenticated)", re.I),
    re.compile(r"connect(ed|ing) to\b", re.I),
    # the kernel puts the MAC between the two words; accept either order so a driver that
    # phrases it differently still counts
    re.compile(r"\bnew\s+(?:[0-9a-f:]{17}\s+)?station\b", re.I),
    re.compile(r"\broam(ed|ing)?\b", re.I),
)

_TS = re.compile(r"^\[?(\d+(?:\.\d+)?)\]?\s*(.*)$")


def parse_event_line(line: str) -> Optional[float]:
    """`iw event` prints `[seconds.microseconds] text`; return the epoch seconds, or None."""
    m = _TS.match(line.strip())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def is_link_move(line: str) -> bool:
    m = _TS.match(line.strip())
    text = m.group(2) if m else line
    return any(p.search(text) for p in MOVE_PATTERNS)


class IwEventWatcher:
    """Runs `iw event -t` (one per interface, or all if `iface` is None) and appends link moves.

    A dead child is restarted by the sensor's main loop (`alive()` says when it is not); a world
    without `iw` simply yields no events, and the judgement treats that as "no wifi evidence"
    rather than as proof of anything.
    """

    def __init__(self, log_file: Path, iface: Optional[str] = None):
        self.log_file = Path(log_file)
        self.iface = iface
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._written = 0

    def start(self) -> bool:
        cmd = ["iw", "event", "-t"] + (["-i", self.iface] if self.iface else [])
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception:
            self.proc = None
            return False
        # Say hello in the log even when nothing has moved yet. A wifi link that never drops
        # produces no events, so without this the file simply does not exist and a reader cannot
        # tell "the tap is up and quiet" apart from "the tap is broken" - the same trap the input
        # watcher avoids with its heartbeat. The line has no parseable timestamp, so every reader
        # skips it.
        self._append("# iw-event tap started %s%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "" if self.iface is None else " iface=%s" % self.iface))
        # Read on our own thread, NEVER on the caller's. `iw event` prints nothing until the link
        # actually moves, and a pipe whose writer is silent blocks `readline()` forever: the
        # sensor's main loop sat in anon_pipe_read and stopped polling the magic-packet sockets
        # altogether, so a real magic packet would have gone unseen. Measured on the real box with
        # /proc/<pid>/stack while a magic-packet wake was supposed to be covered.
        self._start_reader()
        return True

    def _start_reader(self) -> None:
        """Seam for tests: the reader thread, started once a proc exists."""
        if self._reader is not None and self._reader.is_alive():
            return
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            if self.proc is None or self.proc.stdout is None:
                return
            for line in self.proc.stdout:                 # blocking here is fine: it is our thread
                if is_link_move(line):
                    self._append(self._row(line))
                    self._written += 1
        except Exception:
            pass

    @staticmethod
    def _row(line: str) -> str:
        epoch = parse_event_line(line)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch or time.time()))
        return "%s  wlan-event  %s" % (stamp, line.strip())

    def _append(self, text: str) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(text.rstrip("\n") + "\n")
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None

    def drain(self) -> int:
        """How many link-move events the reader thread has written since the last call.

        Deliberately non-blocking: the reading happens on the reader thread, so this can be called
        from the sensor's main loop, which must keep polling the packet sockets.
        """
        written, self._written = self._written, 0
        return written


def wifi_moves_around_resume(log_file: Path, wake_epoch: float, slack_before: float = 5.0,
                             slack_after: float = 30.0) -> Optional[str]:
    """The wifi-event answer to "why did we wake": did the link move at the resume?

    Events land slightly AFTER the kernel resumes (userspace starts a beat late), so the window
    is asymmetric: a drop seconds before the resume is the wake cause, and an event up to
    `slack_after` later is the same story being told by userspace. Returns the matched line for
    `who=`, or None when the log says nothing about this wake.
    """
    try:
        lines = Path(log_file).read_text(errors="replace").splitlines()
    except Exception:
        return None
    best: Optional[str] = None
    for line in lines:
        epoch = parse_event_line(line.split("wlan-event", 1)[-1])
        if epoch is None:
            continue
        if (wake_epoch - slack_before) <= epoch <= (wake_epoch + slack_after):
            best = line.split("wlan-event", 1)[-1].strip()
    return best
