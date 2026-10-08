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

#: Key codes the ACPI/system layer emits by itself: they arrive around a wake that no person
#: caused. Measured on the real box: an `rtcwake -m mem -s 20` (a purely RTC-triggered wake,
#: nobody in the room) delivered KEY_POWER from the "Power Button" device two seconds before the
#: resume, the clock read it as a keypress, and the session latched "keyboard woke it" - so a
#: wifi-disconnect wake was exempted from re-sleeping. Nothing about a person is provable from a
#: power/sleep/wake key, so none of them may count as human input.
SYSTEM_KEY_CODES = frozenset((
    116,   # KEY_POWER
    142,   # KEY_SLEEP
    143,   # KEY_WAKEUP
    205,   # KEY_SUSPEND
))

#: How long after a resume MOTION is ignored. Measured on the real box across three wakes: a purely
#: RTC-triggered wake (nobody in the room, `rtcwake -m mem -s 20`) made the mouse emit REL events
#: in bursts at +7 s (80 events, value=-1), +11 s and +12 s - a HID device replaying its own state
#: after the xHCI controller re-initialised it, not a hand. Those events made an unattended wake
#: look like "keyboard/mouse woke it", latched a session-wide exemption and stopped the machine
#: from ever re-sleeping. The window covers the longest measured replay; KEY events are never
#: subject to it, because a real keypress at the wake is exactly what the exemption is for, and a
#: hand that keeps moving past the window is still counted.
MOTION_SETTLE_S = 15.0

#: Events taken from one device per poll. A person never needs more; a flooding device is drained
#: a slice at a time so one runaway mouse cannot hold the loop open.
READ_EVENTS = 64

#: Pause after a full read, because a full read means more is still waiting and `select` would
#: return immediately - the loop would spin and peg a core for as long as the device floods.
FLOOD_PAUSE_S = 0.002

#: The audit trail is diagnostic, so it is aggregated and capped. One line per event cost an
#: open/write/close per event and reached 71 MB in a few hours on the real box (~80 bytes an
#: event, a mouse that reports continuously) - a slow disk fill and wasted CPU.
AUDIT_FLUSH_S = 0.5
AUDIT_MAX_BYTES = 2 * 1024 * 1024

#: `input-last.json` only ever records the LAST event, so rewriting it per event (a fresh file
#: plus a rename, plus a read of the previous contents) bought nothing: the newest values are
#: kept in memory and written at this interval, and flushed at once when the stream goes quiet.
STATE_WRITE_S = 1.0

#: device names that carry a `B: EV` key bit but cannot be a person: virtual/ACPI/control sources.
#: The Video Bus exists on every box and would otherwise make every resume look like a keypress;
#: the ACPI buttons are the ones that actually fire on an unattended wake.
DENY = ("video bus", "pc speaker", "rfkill", "power button", "sleep button",
        "lid switch", "wmi hotkeys", "acpi")


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

    Non-blocking and silent: the newest qualifying event is kept in memory and written to the
    state file at most once a second (a release is ignored - a press always precedes it), plus a
    heartbeat every few seconds so a reader can tell "nothing has happened" apart from "nobody is
    watching". A device that reports continuously costs a bounded slice of CPU, not a file write
    per event. Closing the file handles on suspend makes no
    difference: the devices stay open, and a keypress that wakes the machine is delivered as soon
    as the readers run again.
    """

    def __init__(self, state_file: Path, devices_path: str = "/proc/bus/input/devices",
                 beat_seconds: float = 10.0, audit_file: Optional[Path] = None):
        self.state_file = Path(state_file)
        self.devices_path = devices_path
        self.beat_seconds = beat_seconds
        #: Every event that COUNTED as human input, with its device name. Without this, "who woke
        #: it" can only be inferred from a 45-second window, and an unattended machine that woke
        #: itself looks identical to somebody pressing a key - which is exactly the ambiguity that
        #: made a wifi-disconnect wake unanswerable on the real box.
        self.audit_file = Path(audit_file) if audit_file else Path(str(state_file) + ".audit")
        #: monotonic deadline before which motion events are treated as device re-init noise
        self.settle_until = 0.0
        #: aggregated audit rows awaiting a flush, keyed by (device, kind, note)
        self._audit_agg: Dict[tuple, dict] = {}
        self._audit_flushed = 0.0
        #: newest accepted event + when the state file was last allowed to be written
        self._pending_state: Optional[tuple] = None
        self._last_state_write = 0.0
        self._state_cache: dict = {}
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

    def _audit(self, dev: str, kind: str, code: int, value: int = 0,
               note: str = "") -> None:
        """Count an event; the line is written later by `flush_audit`, at most a few per second.

        A mouse coming back from a resume reports continuously, so one line per event meant
        thousands of open/write/close calls a second. Aggregating per (device, kind, note) keeps
        the same evidence ("this device, this kind, these codes, this many, ignored as re-init
        noise") at a fixed cost instead.
        """
        row = self._audit_agg.get((dev, kind, note))
        if row is None:
            row = self._audit_agg[(dev, kind, note)] = {"n": 0, "codes": {}, "first": time.time()}
        row["n"] += 1
        code = int(code)
        row["codes"][code] = row["codes"].get(code, 0) + 1

    def flush_audit(self, force: bool = False) -> None:
        if not self._audit_agg:
            return
        now = time.time()
        if not force and (now - self._audit_flushed) < AUDIT_FLUSH_S:
            return
        lines = []
        for (dev, kind, note), row in sorted(self._audit_agg.items()):
            codes = " ".join("%d:%d" % (c, n) for c, n in sorted(row["codes"].items())[:8])
            lines.append("%s  %-28s %-6s n=%-5d codes=%s%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["first"])),
                dev, kind, row["n"], codes, ("  " + note) if note else ""))
        self._audit_agg.clear()
        self._audit_flushed = now
        try:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            if self.audit_file.exists() and self.audit_file.stat().st_size > AUDIT_MAX_BYTES:
                old = self.audit_file.with_name(self.audit_file.name + ".1")
                try:
                    old.unlink()
                except Exception:
                    pass
                self.audit_file.rename(old)
            with open(self.audit_file, "a", encoding="utf-8") as fh:
                fh.writelines(lines)
        except Exception:
            pass

    def _note_event(self, epoch: Optional[float], beat: float, dev: str = "",
                    kind: str = "") -> None:
        """Remember the newest accepted event; the file is written by `flush_state`."""
        self._pending_state = (epoch, beat, dev, kind)

    def flush_state(self, force: bool = False) -> None:
        if self._pending_state is None:
            return
        now = time.time()
        if not force and (now - self._last_state_write) < STATE_WRITE_S:
            return
        epoch, beat, dev, kind = self._pending_state
        self._pending_state = None
        self._last_state_write = now
        self._write(epoch=epoch, beat=beat, dev=dev, kind=kind)

    def mark_resume(self, settle_s: float = MOTION_SETTLE_S) -> None:
        """Call the moment a resume is noticed: motion until `settle_s` later is not a person."""
        self.settle_until = time.monotonic() + settle_s

    def touch_beat(self, force: bool = False) -> None:
        """Refresh only the heartbeat, leaving the last event's timestamp in place."""
        now = time.time()
        if not force and (now - self.last_beat) < self.beat_seconds:
            return
        self.last_beat = now
        self._write(epoch=self._state_cache.get("epoch"), beat=now)

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return self._state_cache

    def _write(self, epoch: Optional[float], beat: float, dev: str = "",
               kind: str = "") -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = self._state_cache or self._read_state()
        payload = {"beat": beat, "epoch": epoch if epoch is not None else state.get("epoch"),
                   "dev": dev or state.get("dev") or "", "kind": kind or state.get("kind") or ""}
        self._state_cache = payload
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
        flooding = False
        for fd in ready:
            try:
                data = os.read(fd, EVENT.size * READ_EVENTS)
            except Exception:
                continue
            if len(data) >= EVENT.size * READ_EVENTS:
                flooding = True        # a full read means more is waiting on this device
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                sec, usec, etype, _code, ecode = EVENT.unpack_from(data, off)
                if etype == EV_KEY and ecode == 0:
                    continue                      # a release only ever follows a press
                if etype == EV_KEY and _code in SYSTEM_KEY_CODES:
                    continue                      # ACPI/system key: not evidence of a person
                if etype not in (EV_KEY, EV_REL, EV_ABS):
                    continue                      # sync/led/sound noise is not a person
                kind = {EV_KEY: "key", EV_REL: "motion", EV_ABS: "touch"}[etype]
                if etype in (EV_REL, EV_ABS) and time.monotonic() < self.settle_until:
                    # a device replaying its state as the bus comes back up
                    self._audit(self.fds.get(fd, ""), kind, _code, ecode, "settling:ignored")
                    continue
                self.events += 1
                seen += 1
                self._audit(self.fds.get(fd, ""), kind, _code, ecode)
                self._note_event(epoch=sec + usec / 1e6, beat=time.time(),
                                 dev=self.fds.get(fd, ""), kind=kind)
        # Quiet stream: write exactly what we have, so the file is never stale. Flooding stream:
        # the newest values are still in memory and go out within STATE_WRITE_S.
        self.flush_state(force=not seen)
        self.flush_audit(force=not seen)
        if flooding:
            time.sleep(FLOOD_PAUSE_S)
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