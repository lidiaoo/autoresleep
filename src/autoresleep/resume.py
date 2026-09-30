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


# --------------------------------------------------------------------------------------
# The exact suspend ledger: how long has this machine been asleep, in total?
#
# The wall-vs-monotonic skew above needs a threshold (>90 s by default) and therefore CANNOT see
# a short sleep. Measured on the real box: a wifi-disconnect wake produced a 17-second sleep, the
# resume went unnoticed, the collector never ran, the session stamp never moved - and a stale
# "keyboard woke it" latch then held the machine awake for good.
#
# CLOCK_BOOTTIME counts suspend time, CLOCK_MONOTONIC does not. Their difference is the machine's
# accumulated sleep, exact to the millisecond and monotonic, so ANY sleep (one second included) is
# visible with no threshold to tune. Windows and macOS expose the same two quantities under other
# names; where neither half is available this returns None and the threshold heuristic still runs.
# --------------------------------------------------------------------------------------

#: CLOCK_BOOTTIME and CLOCK_MONOTONIC are two separate reads, and a timer tick can land between
#: them, so their difference wobbles by up to a tick. Measured on the real box: the ledger reported
#: a "sleep" four times in eleven seconds while the machine was wide awake, and each of those
#: rewrote the evidence and retired a legitimate latch. Anything smaller than this is that jitter,
#: not a sleep; a real suspend is orders of magnitude above it.
LEDGER_EPSILON_S = 0.5


def suspend_total() -> Optional[float]:
    """Seconds this machine has spent suspended, or None when the platform cannot say."""
    try:
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)        # includes suspend
        mono = time.clock_gettime(time.CLOCK_MONOTONIC)       # excludes suspend
        if boot > 0 and mono > 0:
            return max(0.0, boot - mono)
    except (AttributeError, OSError):
        pass
    try:                                                      # Windows
        import ctypes
        k32 = ctypes.windll.kernel32                           # type: ignore[attr-defined]
        tick = ctypes.c_ulonglong()
        k32.QueryUnbiasedInterruptTime(ctypes.byref(tick))
        unbiased = tick.value / 1e7                            # seconds, excludes suspend
        return max(0.0, k32.GetTickCount64() / 1000.0 - unbiased)
    except Exception:
        return None


class SuspendLedger:
    """Turns "the machine has slept since I last looked" into a fact, not a threshold guess.

    `changed()` is True when the ledger moved OR when the wall/monotonic skew fired - so a box
    with no CLOCK_BOOTTIME keeps exactly the old behaviour instead of losing resume detection.
    """

    def __init__(self, detector: Optional[ResumeDetector] = None):
        self.detector = detector or ResumeDetector()
        self._total: Optional[float] = None

    def observe(self, mono: Optional[float] = None,
                wall: Optional[float] = None) -> Optional[float]:
        """(seconds slept since the previous call) or None when nothing was noticed."""
        skew_fired = self.detector.observe(mono, wall)
        total = suspend_total()
        slept: Optional[float] = None
        if total is not None:
            if self._total is not None and total > self._total + LEDGER_EPSILON_S:
                slept = total - self._total
            self._total = total
        if slept:
            return slept
        if skew_fired:
            return self.detector.threshold_s
        return None

    def state(self) -> Optional[float]:
        """The current ledger reading, stored with evidence so a latch can be invalidated by it."""
        return self._total if self._total is not None else suspend_total()

    def seed(self, total: Optional[float]) -> None:
        if total is not None:
            self._total = total
