"""The only input clock that cannot lie: real events on the kernel's input devices.

The X idle counter (and every desktop's idle API) is reset by the resume itself, so at the moment
we need it most - just after waking - it reports "somebody just typed" even for a wake caused by a
wifi disconnect. Measured on the real box: every resume produced `wake-by-input idle=0s`, and once
that was latched for the session the machine never slept again.

evdev is the hardware truth: a keypress that woke the machine is delivered as a real input event,
while a wifi wake produces none at all. `/dev/input/event*` is root:input, so this runs inside the
privileged sensor (system unit, `SupplementaryGroups=input`) and leaves its finding in one small
file the judgement reads - the same file-based pattern as the packet evidence.
"""
from __future__ import annotations

import json
import os
import re
import select
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: struct input_event: struct timeval (2 longs on 64-bit) + type (u16) + code (u16) + value (s32)
EVENT = struct.Struct("llHHi")

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3

#: device names that carry a `B: EV` key bit but cannot be a human: virtual/non-input sources
#: (the Video Bus exists on every box and would otherwise make every resume look like a keypress)
DENY = ("video bus", "pc speaker", "rfkill")


def discover_devices(path: str = "/proc/bus/input/devices") -> Dict[str, str]:
    """{'event3': 'AT Translated Set 2 keyboard', ...} for devices a human can drive.

    Parsed from the kernel's own device list rather than guessed: a section is kept when its
    capability bitmap has EV_KEY/EV_REL/EV_ABS (keys, motion or absolute axes) - that excludes
    switches such as headphone jacks, which report only EV_SW and are not people.
    """
    out: Dict[str, str] = {}
    name, handlers, bits = None, None, None
    try:
        text = Path(path).read_text(errors="replace")
    except Exception:
        return out
    for line in text.splitlines() + [""]:
        if line.startswith("N: Name="):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            handlers = line.split("=", 1)[1].split()
        elif line.startswith("B: EV="):
            bits = line.split("=", 1)[1].strip()
        elif line.startswith("I:") or not line.strip():
            if name and handlers and bits:
                try:
                    value = int(bits, 16)
                except ValueError:
                    value = 0
                human = bool(value & 0b1110)          # keys / relative / absolute
                if human and not any(d in name.lower() for d in DENY):
                    for handler in handlers:
                        if handler.startswith("event"):
                            out[handler] = name
            name, handlers, bits = None, None, None
    return out


class InputWatcher:
    """Reads every human-capable input device and records the last real event.

    Non-blocking and silent: one small JSON write per qualifying event (a release is ignored - a
    press always precedes it), plus a heartbeat every few seconds so a reader can tell "nothing
    has happened" apart from "nobody is watching". Closing the file handles on suspend makes no
    difference: the devices stay open, and a keypress that wakes the machine is delivered as soon
    as the readers run again.
    """

    def __init__(self, state_file: Path, devices_path: str = "/proc/bus/input/devices",
                 beat_seconds: float = 10.0):
        self.state_file = Path(state_file)
        self.devices_path = devices_path
        self.beat_seconds = beat_seconds
        self.fds: Dict[int, str] = {}
        self.device_names: List[str] = []
        self.last_beat = 0.0
        self.events = 0

    def bind(self) -> int:
        """Open what we may open; a device we cannot read is reported, not assumed."""
        for name, dev in discover_devices(self.devices_path).items():
            path = "/dev/input/%s" % name
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except Exception:
                continue
            self.fds[fd] = dev
            self.device_names.append(dev)
        self.touch_beat(force=True)
        return len(self.fds)

    def close(self) -> None:
        for fd in list(self.fds):
            try:
                os.close(fd)
            except Exception:
                pass
        self.fds.clear()

    def touch_beat(self, force: bool = False) -> None:
        """Refresh only the heartbeat, leaving the last event's timestamp in place."""
        now = time.time()
        if not force and (now - self.last_beat) < self.beat_seconds:
            return
        self.last_beat = now
        self._write(epoch=self._read_state().get("epoch"), beat=now)

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, epoch: Optional[float], beat: float, dev: str = "",
               kind: str = "") -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        payload = {"beat": beat, "epoch": epoch if epoch is not None else state.get("epoch"),
                   "dev": dev or state.get("dev") or "", "kind": kind or state.get("kind") or ""}
        tmp = self.state_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception:
            pass

    def poll(self, timeout: float = 1.0) -> int:
        """Wait up to `timeout` for input; return how many qualifying events were seen."""
        if not self.fds:
            time.sleep(min(timeout, 0.2))
            self.touch_beat()
            return 0
        try:
            ready, _, _ = select.select(list(self.fds), [], [], timeout)
        except Exception:
            return 0
        seen = 0
        for fd in ready:
            try:
                data = os.read(fd, EVENT.size * 64)
            except Exception:
                continue
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                sec, usec, etype, _code, ecode = EVENT.unpack_from(data, off)
                if etype == EV_KEY and ecode == 0:
                    continue                      # a release only ever follows a press
                if etype not in (EV_KEY, EV_REL, EV_ABS):
                    continue                      # sync/led/sound noise is not a person
                self.events += 1
                seen += 1
                self._write(epoch=sec + usec / 1e6, beat=time.time(),
                            dev=self.fds.get(fd, ""),
                            kind={EV_KEY: "key", EV_REL: "motion", EV_ABS: "touch"}[etype])
        self.touch_beat()
        return seen


def read_last_input(state_file: Path, live_within_s: float = 90.0,
                    now: Optional[float] = None) -> Tuple[Optional[float], Optional[str], bool]:
    """(last real event epoch, device, is-anyone-watching) from the watcher's file.

    `is-anyone-watching` is what keeps a dead watcher from silently claiming "no input": without a
    fresh heartbeat the caller must fall back to the desktop's own (weaker) idle clock instead of
    treating a stale file as proof that nobody touched the machine.
    """
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except Exception:
        return None, None, False
    beat = float(data.get("beat") or 0)
    epoch = data.get("epoch")
    return (float(epoch) if epoch else None), (data.get("dev") or None), (now - beat) <= live_within_s