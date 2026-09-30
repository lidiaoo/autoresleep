"""Ground truth: what does the kernel actually deliver on /dev/input across a resume?

Run: python3 capture_resume_events.py <seconds-to-sleep>
Prints every event that arrived before the sleep, during the resume, and just after, with the
kernel's own timestamp, so a bus reset can be told apart from a human keypress by evidence.
"""
from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

EVENT = struct.Struct("llHHi")
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_LED, EV_SW = 0, 1, 2, 3, 4, 17, 5

KIND = {EV_SYN: "SYN", EV_KEY: "KEY", EV_REL: "REL", EV_ABS: "ABS",
        EV_MSC: "MSC", EV_LED: "LED", EV_SW: "SW"}


def devices() -> dict:
    """{'event3': 'AT Translated Set 2 keyboard'} for every readable event node."""
    out, name, handlers = {}, None, None
    text = Path("/proc/bus/input/devices").read_text(errors="replace")
    for line in text.splitlines() + [""]:
        if line.startswith("N: Name="):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            handlers = line.split("=", 1)[1].split()
        elif line.startswith("I:") or not line.strip():
            if name and handlers:
                for h in handlers:
                    if h.startswith("event"):
                        out[h] = name
            name, handlers = None, None
    return out


def main() -> int:
    sleep_s = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    names = devices()
    fds = {}
    for node, dev in names.items():
        try:
            fd = os.open("/dev/input/%s" % node, os.O_RDONLY | os.O_NONBLOCK)
        except Exception:
            continue
        fds[fd] = dev

    def drain(tag: str, rows: list) -> None:
        for fd in list(fds):
            try:
                data = os.read(fd, EVENT.size * 256)
            except Exception:
                continue
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                sec, usec, etype, code, value = EVENT.unpack_from(data, off)
                rows.append({"tag": tag, "t": round(sec + usec / 1e6, 6),
                             "dev": fds[fd], "type": KIND.get(etype, etype),
                             "code": code, "value": value})

    rows: list = []
    drain("pre", rows)
    time.sleep(1)
    drain("pre", rows)
    print("设备 %d 个，先读干净" % len(fds))

    print("rtcwake -m mem -s %d（无人按键，纯 RTC 唤醒）..." % sleep_s)
    t0 = time.time()
    subprocess.run(["rtcwake", "-m", "mem", "-s", str(sleep_s)],
                   capture_output=True, timeout=sleep_s + 180)
    t1 = time.time()
    print("恢复于 %.3f（离开 %.1f 秒）" % (t1, t1 - t0))

    # After a resume the xHCI controller re-enumerates: the nodes we opened before the suspend can
    # be gone and NEW event nodes appear. Re-scan and open them, or the very events we are hunting
    # are the ones we cannot see.
    for _ in range(6):
        drain("after", rows)
        time.sleep(0.25)
    fresh = devices()
    for node, dev in fresh.items():
        if any(fds.get(fd) == dev for fd in fds):
            continue
        try:
            fd = os.open("/dev/input/%s" % node, os.O_RDONLY | os.O_NONBLOCK)
        except Exception:
            continue
        fds[fd] = dev
        print("  恢复后新出现的设备: %s (%s)" % (dev, node))
    for _ in range(20):
        drain("after-new", rows)
        time.sleep(0.25)

    print(json.dumps({"resume": t1, "rows": rows}, ensure_ascii=False))
    for fd in list(fds):
        try:
            os.close(fd)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
