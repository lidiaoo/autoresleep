"""The wifi-event listener (`sudo iw event`), the portable answer to "did the WIFI wake us?".

`iw event` taps nl80211 and prints what the wireless subsystem itself does - disconnect,
associate, auth - for every driver, in one uniform format. That is the generality the user asked
for: a driver's private wake-reason string exists only when that driver feels like printing one,
but nl80211 events are the shared protocol layer underneath all of them.

The listener runs inside the privileged sensor and records events to small logs with timestamps;
the judgement never asks a driver anything, it only reads these files and asks "what did the
wireless subsystem say around the resume?".

Besides link moves it also taps the ONE event that answers the question the whole packet chain was
built around: the WoWLAN wakeup report. Measured on this box (2026-10-16, `sudo iw event -t -f`,
two real wakes):

    1792130691.782796: wlan0 (phy #0): WoWLAN wakeup
        * magic packet received
        * packet (might be truncated): ff:ff:ff:ff:ff:ff ... c0:a8:00:8c:c0:a8:00:ff ... ff:ff:ff:ff:ff:ff 28:d0:ea:27:40:0c 28:d0:ea:27:40:0c ...
    1792130720.783661: wlan0 (phy #0): WoWLAN wakeup
        wakeup not due to WoWLAN

The first was a real magic packet (source 192.168.0.140, destination broadcast, payload = the
magic signature), the second a wake the NIC itself says was NOT wireless. Both matter: the firmware
does consume the waking packet, but the driver still asks it WHY it woke and hands the answer to
cfg80211, which normalizes it (`struct cfg80211_wowlan_wakeup`: magic_pkt, disconnect,
gtk_rekey_failure, rfkill_release ...). That is a kernel struct, not a driver-private string - and
unlike the per-device wakeup counters it separates a magic packet from a spurious NIC wake.
"""
from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

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


#: The nl80211 event that names the wake CAUSE (see the module docstring for the measured lines).
WOWLAN_HEADER = re.compile(r"WoWLAN wakeup\b", re.I)
_WOWLAN_MAGIC = re.compile(r"magic packet", re.I)
_WOWLAN_NOT_WOWLAN = re.compile(r"not due to WoWLAN", re.I)
#: iw prints one bullet line per set cfg80211 trigger; the first word is enough to keep the log
#: readable without inventing a taxonomy for causes this project has no rule for.
_WOWLAN_OTHER = ("disconnect", "gtk", "rekey", "rfkill", "eap", "handshake", "pattern", "tcp")

_WOWLAN_ROW = re.compile(r"kind=(\S+)\s+epoch=([\d.]+)"
                         r"(?:\s+detail=(.*))?$")


def is_wowlan_header(line: str) -> bool:
    """Is this the header line of a WoWLAN wakeup report? (Timestamped, contains "WoWLAN wakeup".)"""
    return parse_event_line(line) is not None and bool(WOWLAN_HEADER.search(line))


def wowlan_tap_target(enabled: bool, log_file: Path) -> Optional[Path]:
    """Where the WoWLAN report tap writes, or None when the operator switched it off.

    One place decides whether the wake-cause channel runs at all (`config.wowlan_wake_reason`), so
    the sensor and every verifier agree on what "off" means: `None` makes `IwEventWatcher` write no
    `wowlan-events.log` row AND hand no report to the sensor - the channel disappears as a whole,
    instead of leaving a switch the judgement would have to remember to honour on every path.
    """
    return Path(log_file) if enabled else None


def classify_wowlan(continuation_lines: List[str]) -> str:
    """`iw`'s bullet lines -> the cause class, in this project's vocabulary. Pure.

    The continuation lines carry no timestamp of their own (that is how the reader tells them from
    events), so this reads them as one block of text:
      * `* magic packet received`      -> "magic"     (the claim a heard packet would have made)
      * `wakeup not due to WoWLAN`     -> "not-wowlan" (the NIC says this wake was NOT wireless)
      * anything else                  -> "other:<word>" - recorded, never exempts
    """
    text = " ".join(continuation_lines)
    if _WOWLAN_MAGIC.search(text):
        return "magic"
    if _WOWLAN_NOT_WOWLAN.search(text):
        return "not-wowlan"
    for word in _WOWLAN_OTHER:
        if re.search(word, text, re.I):
            return "other:" + word
    return "other"


def parse_wowlan_row(line: str) -> Optional[Dict[str, object]]:
    """One `wowlan-events.log` row -> {kind, epoch, detail}, or None for the hello/blank lines."""
    m = _WOWLAN_ROW.search(line)
    if not m:
        return None
    try:
        epoch = float(m.group(2))
    except ValueError:
        return None
    return {"kind": m.group(1), "epoch": epoch, "detail": (m.group(3) or "").strip()}


def read_wowlan_log(log_file: Path) -> List[Dict[str, object]]:
    """Every WoWLAN report in the file, oldest first. Missing file -> empty (not an error)."""
    try:
        lines = Path(log_file).read_text(errors="replace").splitlines()
    except Exception:
        return []
    return [row for row in (parse_wowlan_row(line) for line in lines) if row]


def last_wowlan(log_file: Path, max_age_s: Optional[float] = None,
                now: Optional[float] = None) -> Optional[Dict[str, object]]:
    """The newest report, optionally only if it is fresh. Used to contradict a counter reading."""
    rows = read_wowlan_log(log_file)
    if not rows:
        return None
    newest = rows[-1]
    if max_age_s is not None:
        age = (now if now is not None else time.time()) - float(newest["epoch"])
        if age < 0 or age > max_age_s:
            return None
    return newest


class IwEventWatcher:
    """Runs `iw event -t` (one per interface, or all if `iface` is None), appends link moves to
    `log_file` and WoWLAN wakeup reports to `wowlan_file`.

    A dead child is restarted by the sensor's main loop (`alive()` says when it is not); a world
    without `iw` simply yields no events, and the judgement treats that as "no wifi evidence"
    rather than as proof of anything.
    """

    #: How long to wait for a report's bullet lines after its header. They are printed in the same
    #: write burst, so this only ever bites when the block is genuinely incomplete.
    block_timeout_s = 0.5

    def __init__(self, log_file: Path, iface: Optional[str] = None,
                 wowlan_file: Optional[Path] = None):
        self.log_file = Path(log_file)
        self.iface = iface
        self.wowlan_file = Path(wowlan_file) if wowlan_file is not None else None
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._written = 0
        #: our own byte buffer: the text wrapper reads ahead and hides a burst from `select`
        self._buf = ""
        self._eof = False
        #: an open WoWLAN report: [header line, bullet lines] - see `_read_loop`
        self._pending: Optional[List[object]] = None
        self._wowlan: List[Dict[str, object]] = []

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
        if self.wowlan_file is not None:
            self._append("# wowlan tap started %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                         self.wowlan_file)
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
        """Line loop with a deadline-bounded read; blocking here is fine, it is our own thread.

        Findings from this machine, each of which decided the shape of this loop:
          * the pipe must not be read through the text wrapper (see `_fill`) - a burst already in
            Python's buffer is invisible to `select` on the fd, which is exactly how the bullets
            below a WoWLAN header went missing in the first version of this code;
          * a WoWLAN report is a BLOCK: the header carries the timestamp, the bullets that name the
            cause carry none. So the reader keeps one report open until either a line arrives that
            has a timestamp (the report is over) or the burst stops (`block_timeout_s`);
          * and it must never block forever waiting for that: `iw event` is silent for hours.
        """
        try:
            if self.proc is None or self.proc.stdout is None:
                return
            while not self._eof:
                line = self._take_line()
                if line is None:
                    # Wait forever when idle; wait only briefly when a report is still open, so its
                    # bullets get a chance without ever stalling on a silent pipe.
                    if not self._fill(self.block_timeout_s if self._pending is not None else None):
                        if self._pending is not None:
                            self._flush_pending()
                        if self._eof or self.proc.poll() is not None:
                            break
                    continue
                self._handle_line(line)
            self._flush_pending()
        except Exception:
            pass

    def _handle_line(self, line: str) -> None:
        if self._pending is not None:
            if parse_event_line(line) is None:
                self._pending[1].append(line.strip())   # a bullet: belongs to the open report
                return
            self._flush_pending()
        if is_wowlan_header(line):
            self._pending = [line, []]                  # its bullets arrive right after
            return
        if is_link_move(line):
            self._append(self._row(line))
            self._written += 1

    def _flush_pending(self) -> None:
        if self._pending is None:
            return
        header, conts = self._pending
        self._pending = None
        # The header keeps its timestamp out of `detail`: the row already carries `epoch=`, and the
        # bullets are the part worth reading back.
        head_text = re.sub(r"^\[?[\d.]+\]?\s*:?\s*", "", header.strip())
        self._record_wowlan(classify_wowlan(conts),
                            (parse_event_line(header) or time.time()),
                            (head_text + (" | " + " | ".join(conts) if conts else ""))[:200])

    def _take_line(self) -> Optional[str]:
        """One complete line from the buffer, or None when nothing complete is buffered yet."""
        if "\n" not in self._buf:
            return None
        line, self._buf = self._buf.split("\n", 1)
        return line + "\n"

    def _fill(self, timeout: Optional[float]) -> bool:
        """Read one chunk off the child's pipe into our own buffer. False = timed out or EOF.

        Reads the FD directly, NOT `self.proc.stdout`: the text wrapper reads ahead, so bytes can
        sit in its buffer where `select` cannot see them. Measured here: a `printf` burst of
        "WoWLAN wakeup" + "* magic packet received" arrived together, `select` on the fd said
        "nothing", and the cause line was lost - the report came out as `kind=other`.
        """
        if self._eof or self.proc is None or self.proc.stdout is None:
            return False
        try:
            fd = self.proc.stdout.fileno()
        except Exception:
            return False
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except Exception:
            return False
        if not ready:
            return False
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return False
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk.decode("utf-8", "replace")
        return True

    def _record_wowlan(self, kind: str, epoch: float, detail: str) -> None:
        """Write the report to the wowlan log and queue it for the sensor's main loop."""
        self._wowlan.append({"kind": kind, "epoch": float(epoch), "detail": detail})
        if self.wowlan_file is None:
            return
        self._append("%s  wowlan  kind=%s epoch=%.6f detail=%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)), kind, epoch, detail),
            self.wowlan_file)

    def take_wowlan(self) -> List[Dict[str, object]]:
        """Drain the reports classified since the last call. Non-blocking, like `drain()`."""
        out, self._wowlan = self._wowlan, []
        return out

    @staticmethod
    def _row(line: str) -> str:
        epoch = parse_event_line(line)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch or time.time()))
        return "%s  wlan-event  %s" % (stamp, line.strip())

    def _append(self, text: str, target: Optional[Path] = None) -> None:
        try:
            path = Path(target) if target is not None else self.log_file
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
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
