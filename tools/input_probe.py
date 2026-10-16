"""输入事件探针：抓 /dev/input 原始事件，验证键鼠能不能被 evdev 看到。

用法（root，因为 /dev/input 只有 root/input 组可读）：
    sudo python3 tools/input_probe.py [秒数，默认 60]

建议测试节奏（写进结论用）：
    1. 前 15 秒：什么都不碰（抓幽灵输入，比如 USB 接收器自己吐事件）
    2. 中间：正常敲键盘 + 拖鼠标 + 滚轮
    3. 最后 15 秒：再停住

逐条事件写 JSONL 到 .workbuddy/tmp/input-probe.jsonl，结束打印每设备汇总。
Ctrl-C 可随时提前结束，已写盘的不会丢。
"""
from __future__ import annotations

import json
import os
import select
import struct
import sys
import time
from pathlib import Path

EVENT = struct.Struct("llHHi")
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_LED, EV_SW, EV_REP = 0, 1, 2, 3, 4, 17, 5, 20
KIND = {EV_SYN: "SYN", EV_KEY: "KEY", EV_REL: "REL", EV_ABS: "ABS",
        EV_MSC: "MSC", EV_LED: "LED", EV_SW: "SW", EV_REP: "REP"}

KEY_NAME = {
    28: "ENTER", 14: "BACKSPACE", 57: "SPACE", 1: "ESC", 15: "TAB",
    16: "Q", 17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U",
    23: "I", 24: "O", 25: "P", 30: "A", 31: "S", 32: "D", 33: "F",
    34: "G", 35: "H", 36: "J", 37: "K", 38: "L", 44: "Z", 45: "X",
    46: "C", 47: "V", 48: "B", 49: "N", 50: "M",
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8",
    10: "9", 11: "0", 12: "MINUS", 13: "EQUAL",
    29: "LCTRL", 42: "LSHIFT", 56: "LALT", 97: "RCTRL", 54: "RSHIFT",
    100: "RALT", 125: "LWIN", 126: "RWIN", 111: "UP", 108: "DOWN",
    102: "HOME", 107: "END", 104: "PGUP", 109: "PGDN", 119: "PAUSE",
    58: "CAPSLOCK", 69: "NUMLOCK",
    0x110: "BTN_LEFT", 0x111: "BTN_RIGHT", 0x112: "BTN_MIDDLE",
}
REL_NAME = {0: "X", 1: "Y", 8: "WHEEL"}


def devices() -> dict:
    """{'event3': 'AT Translated Set 2 keyboard'}，来自 /proc/bus/input/devices。"""
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


def decode(etype: int, code: int, value: int) -> str:
    if etype == EV_KEY:
        n = KEY_NAME.get(code, "KEY_%03d" % code)
        v = {1: "down", 0: "up", 2: "repeat"}.get(value, str(value))
        return "%s %s" % (n, v)
    if etype == EV_REL:
        return "REL-%s %+d" % (REL_NAME.get(code, code), value)
    if etype == EV_ABS:
        return "ABS-%d %d" % (code, value)
    return "%s %d %d" % (KIND.get(etype, etype), code, value)


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    names = devices()
    fds = {}
    for node, dev in sorted(names.items()):
        try:
            fd = os.open("/dev/input/%s" % node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print("跳过 %s (%s): %s" % (node, dev, e))
            continue
        fds[fd] = (node, dev)
    print("监听 %d 个设备，共 %.0f 秒：" % (len(fds), duration))
    for fd, (node, dev) in sorted(fds.items()):
        print("  %-8s %s" % (node, dev))

    out_dir = Path(__file__).resolve().parent.parent / ".workbuddy" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "input-probe.jsonl"
    out_f = out_path.open("a", buffering=1)
    out_f.write(json.dumps({"_session_start": time.time()}) + "\n")
    print("逐条日志 -> %s" % out_path)
    print("节奏：先静置 15 秒 -> 敲键盘拖鼠标 -> 最后静置。开始！\n")

    counts: dict = {}
    t_end = time.monotonic() + duration
    try:
        while True:
            remain = t_end - time.monotonic()
            if remain <= 0:
                break
            r, _, _ = select.select(list(fds), [], [], min(remain, 1.0))
            for fd in r:
                node, dev = fds[fd]
                try:
                    data = os.read(fd, EVENT.size * 256)
                except BlockingIOError:
                    continue
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    sec, usec, etype, code, value = EVENT.unpack_from(data, off)
                    if etype == EV_SYN:
                        continue
                    counts[dev] = counts.get(dev, 0) + 1
                    row = {"wall": round(time.time(), 3),
                           "dev": dev,
                           "ev": decode(etype, code, value)}
                    out_f.write(json.dumps(row) + "\n")
    except KeyboardInterrupt:
        print("\n提前结束。")

    print("\n==== 每设备事件数 ====")
    for dev in sorted(names.values()):
        n = counts.get(dev, 0)
        if n:
            print("  %5d  %s" % (n, dev))
    quiet = [d for d in set(names.values()) if d not in counts]
    print("（静默设备 %d 个，略）" % len(quiet))
    print("日志在 %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
