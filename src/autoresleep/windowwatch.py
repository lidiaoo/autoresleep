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
    therefore read "somebody touched it" for a wake nobody asked for - the conservative direction
    (the machine stays awake), and the operator's escape hatch is `resleep_break_on_input=false`,
    which ignores the window's input entirely.
  * macOS `HIDIdleTime` has the same shape and needs the same slow calibration on the target box.

The state is one small JSON file keyed by the wake it belongs to, so the listener (which samples)
and the judge (which decides) can be different processes - the same file-based pattern the packet
and input chains already use.
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

    def describe(self) -> str:
        if self.input is None:
            return "not sampled"
        return ("input-in-window" if self.input else "no-input-in-window") + \
               " via %s (%d samples, %s..%s)" % (self.source or "-", self.samples,
                                                 self.first_tick or "-", self.last_tick or "-")


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
            now: Optional[float] = None, source: str = "") -> WindowState:
    """Fold one sample of the platform input clock into this window's state. Pure, no I/O.

    Rules, in order:
      * a different anchor is a different wake: start over. A new session never inherits the last
        one's verdict - the same rule the session latch uses (`session-cause.json`).
      * a sample taken after `until` changes nothing but marks the window ended: input five minutes
        later is not input inside the window, and a stale `input=True` must never cancel a sleep it
        has nothing to do with.
      * otherwise a CHANGED tick means somebody touched the machine.

    The wake-causing input itself is deliberately not counted here: it happened at/before the first
    sample, and the wake-moment chain (`wake_by_input`) already answers for it.
    """
    now = time.time() if now is None else now
    if state is None or float(state.anchor or 0) != float(anchor):
        return WindowState(anchor=float(anchor), until=float(until), input=False,
                           first_tick=str(tick or ""), last_tick=str(tick or ""),
                           source=source, samples=1, updated_at=now, ended=False)
    if float(until) and now > float(until):
        state.ended = True
        state.updated_at = now
        return state
    if tick and state.last_tick and str(tick) != str(state.last_tick):
        state.input = True
    if tick:
        state.last_tick = str(tick)
    state.source = source or state.source
    state.samples += 1
    state.updated_at = now
    return state


def sample(path, anchor: float, until: float, tick: str, now: Optional[float] = None,
           source: str = "") -> Optional[WindowState]:
    """observe() + the file, so the listener and the judge share one window."""
    if not anchor:
        return None
    state = observe(read_state(path), anchor, until, tick, now=now, source=source)
    write_state(path, state)
    return state