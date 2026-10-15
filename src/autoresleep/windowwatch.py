"""The re-sleep window: what "hands on the machine" means between a wake and the judgement.

The window starts at the resume (`evidence.observed_at`) and lasts `resleep_delay_seconds`. At the
end of it the judgement needs exactly one fact - did anybody touch the machine in those seconds -
and on two of the three platforms the only source for it is the platform's own input clock, judged
by its DELTA. Never by an absolute reading: a resume resets the desktop idle counter, so `idle=0s`
right after a wifi wake says nothing at all. That is the bug that made every wake look like a
keypress, latched a session-wide exemption and kept a machine awake for a day.

Linux has something better and does not use this module: real evdev events, written by the
privileged sensor, with the device name and its motion-settle window. This is the fallback the
other two platforms get, and it is honest about what it cannot see:

  * Windows' `GetLastInputInfo` cannot distinguish a keypress from the HID re-play a resume
    triggers (measured: a mouse replaying its state +7 s after an RTC wake). A box that replays can
    therefore read "somebody touched it" for a wake nobody asked for.
  * macOS `HIDIdleTime` has the same shape and needs the same slow calibration on the target box.

`settle_s` is the portable answer to the first bullet, and it is the same mechanism Linux already
has. `inputwatch` ignores MOTION for `MOTION_SETTLE_S` (15 s) after a resume, because a bus coming
back up makes a HID device replay its state and that replay has nothing to do with a person. The
other two platforms have no device names to filter on, so the only signal left is the clock - and
the settle window is applied to that instead: a tick CHANGE inside `settle_s` of the wake is
recorded, audited, and NOT counted as input.

Measured on the real box (2026-10-15, a 9-minute Wi-Fi wake, nobody at the machine): the window's
first sample still read the PRE-SLEEP input tick with `idle=603 s` - proof that no hand was on the
machine - and the tick then jumped 604.6 s at anchor+1.6 s. That single change was the resume's
replay; before `settle_s` existed it latched `session-cause=input` and the machine never re-slept
for the rest of the session. The change is folded into `last_tick` even when it is ignored, so a
settled replay can never come back as "input" once the settle window is over; only a FURTHER
change after it counts.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WindowState:
    anchor: float = 0.0            # the wake moment this window belongs to
    until: float = 0.0             # anchor + resleep_delay_seconds
    ended: bool = False            # the window is over: later input is NOT this window's input
    input: Optional[bool] = None   # was there input inside it (None = nothing sampled yet)
    first_tick: str = ""
    last_tick: str = ""
    source: str = ""
    samples: int = 0
    updated_at: float = 0.0
    #: Changes before this moment are the resume's own HID replay, not a person (`settle_s`).
    #: Kept in the file so the listener's samples and the judge's reading agree on the same rule,
    #: and so an operator reading `window-input.json` can see which rule produced the verdict.
    settle_until: float = 0.0
    #: How many changes were ignored as settling. Forensics: "nobody touched it" and "something
    #: touched it and we chose not to believe it" are different answers, and the next morning's
    #: question is exactly which one happened.
    settled: int = 0

    def describe(self) -> str:
        if self.input is None:
            return "not sampled"
        out = ("input-in-window" if self.input else "no-input-in-window") + \
            " via %s (%d samples, %s..%s)" % (self.source or "-", self.samples,
                                              self.first_tick or "-", self.last_tick or "-")
        if self.settled:
            out += "; %d change(s) ignored as resume settle" % self.settled
        return out


def read_state(path) -> Optional[WindowState]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    known = set(WindowState.__dataclass_fields__)
    return WindowState(**{k: v for k, v in data.items() if k in known})


def write_state(path, state: WindowState) -> None:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(state)), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        pass


def observe(state: Optional[WindowState], anchor: float, until: float, tick: str,
            now: Optional[float] = None, source: str = "", settle_s: float = 0.0) -> WindowState:
    """Fold one sample of the platform input clock into this window's state. Pure, no I/O.

    Rules, in order:
      * a different anchor is a different wake: start over. A new session never inherits the last
        one's verdict - the same rule the session latch uses (`session-cause.json`).
      * a sample taken after `until` changes nothing but marks the window ended: input five minutes
        later is not input inside the window, and a stale `input=True` must never cancel a sleep it
        has nothing to do with.
      * a CHANGED tick means the platform's input clock moved. Inside the settle window
        (`anchor + settle_s`) that is the resume's own HID replay and is only counted in `settled`;
        after it, somebody touched the machine.
      * the new tick is folded into `last_tick` either way, which is what stops an absorbed replay
        from being re-detected as "input" for the rest of the window.

    The wake-causing input itself is deliberately not counted here: it happened at/before the first
    sample, and the wake-moment chain (`wake_by_input`) already answers for it.
    """
    now = time.time() if now is None else now
    if state is None or float(state.anchor or 0) != float(anchor):
        return WindowState(anchor=float(anchor), until=float(until), input=False,
                           first_tick=str(tick or ""), last_tick=str(tick or ""),
                           source=source, samples=1, updated_at=now, ended=False,
                           settle_until=float(anchor) + max(0.0, float(settle_s or 0.0)))
    if float(until) and now > float(until):
        state.ended = True
        state.updated_at = now
        return state
    # A state written before this rule existed carries no `settle_until`: derive it from the anchor
    # rather than reading 0.0 as "no settle". Reading it as "no settle" would make the listener and
    # the judge disagree about the one window that straddles the upgrade - and two clocks that
    # disagree at the wake is this module's whole failure mode.
    settle_until = float(state.settle_until or 0.0) or \
        (float(anchor) + max(0.0, float(settle_s or 0.0)))
    if tick and state.last_tick and str(tick) != str(state.last_tick):
        if now < settle_until:
            state.settled += 1              # the bus coming back up, not a hand
        else:
            state.input = True
    if tick:
        state.last_tick = str(tick)
    state.settle_until = settle_until
    state.source = source or state.source
    state.samples += 1
    state.updated_at = now
    return state


def sample(path, anchor: float, until: float, tick: str, now: Optional[float] = None,
           source: str = "", settle_s: float = 0.0) -> Optional[WindowState]:
    """observe() + the file, so the listener and the judge share one window."""
    if not anchor:
        return None
    state = observe(read_state(path), anchor, until, tick, now=now, source=source,
                    settle_s=settle_s)
    write_state(path, state)
    return state