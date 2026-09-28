"""The resume signal, portable.

v1 had to hear about a resume from the OS: a Task Scheduler event trigger on Windows, a
`systemd-sleep` hook on Linux, a launchd job on macOS. Each of those is a separate
integration with its own failure mode, and the Windows one was the weakest link in the whole
tool (the arm task was frozen mid-suspend, so the wake-reason trace often started too late).

A machine coming back from sleep is visible without any of that: the wall clock advanced by
hours while the monotonic clock barely moved. That skew is readable on every platform, by any
process, with no privileges. So v2 gets one portable resume detector instead of three hooks -
and the per-OS plumbing is then only needed for SUSPENDING and for collecting evidence.

Known limits, stated up front:
  * an NTP correction of more than `threshold_s` looks like a resume (the threshold is 90 s
    by default; corrections are normally seconds);
  * the very first sample after start-up never counts as a resume (no baseline);
  * a suspend shorter than the threshold is invisible here - tier-B cases still work, because
    the wall-clock idle check does not depend on this detector at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class Resume:
    at: float                 # wall-clock time the resume was observed
    slept_s: float            # how long the machine was away, in seconds


class ResumeDetector:
    """Feed it (monotonic, wall) pairs; it tells you when the machine came back."""

    def __init__(self, threshold_s: float = 90.0):
        self.threshold_s = threshold_s
        self._last_mono: Optional[float] = None
        self._last_wall: Optional[float] = None

    def observe(self, mono: Optional[float] = None, wall: Optional[float] = None) -> bool:
        if mono is None:
            mono = time.monotonic()
        if wall is None:
            wall = time.time()

        first = self._last_mono is None
        prev_mono, prev_wall = self._last_mono, self._last_wall
        self._last_mono, self._last_wall = mono, wall
        if first:
            return False

        mono_delta = mono - float(prev_mono)
        wall_delta = wall - float(prev_wall)

        # some platforms restart the boot-relative monotonic clock across a resume
        if mono_delta < 0:
            return True
        # the machine was away (wall clock moved) but our process was not running
        return (wall_delta - mono_delta) > self.threshold_s

    def last(self) -> Optional[Resume]:
        if self._last_mono is None or self._last_wall is None:
            return None
        return Resume(at=self._last_wall, slept_s=0.0)


def wait_for_resume(detector: ResumeDetector, interval_s: float = 5.0, on_resume=None):
    """Blocking loop for the listener daemon. Kept trivial so the daemon stays readable."""
    while True:
        try:
            if detector.observe():
                if on_resume is not None:
                    on_resume()
        except Exception:
            pass
        time.sleep(interval_s)