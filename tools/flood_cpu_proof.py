"""End-to-end CPU proof: create a real uinput mouse that floods, and measure the sensor's CPU.

Run as root (`/dev/uinput` is root-only). Creates a genuine input device, restarts the sensor so
it binds the new node, floods for FLOOD_S seconds, then reports the sensor's own CPU and what
landed in the audit trail. Destroys the device afterwards so no phantom mouse is left behind.
"""

import fcntl
import os
import pathlib
import struct
import subprocess
import sys
import time

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y = 0, 1
BTN_LEFT = 0x110

FLOOD_S = 20.0
AUDIT = pathlib.Path("/home/jianghh/.local/state/autoresleep/input-events.log")
SENSOR = "autoresleep-wol-sensor.service"
HZ = os.sysconf("SC_CLK_TCK")


def ev(etype, code, value):
    # struct input_event: two 64-bit time fields, u16 type, u16 code, s32 value = 24 bytes
    return struct.pack("qqHHi", 0, 0, etype, code, value)


def main() -> int:
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for code in (REL_X, REL_Y):
        fcntl.ioctl(fd, UI_SET_RELBIT, code)
    fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_LEFT)
    setup = struct.pack("4H80sI", 0x03, 0x1234, 0x5678, 1,
                        b"fs-test-flood-mouse".ljust(80, b"\0"), 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    print("  created uinput device 'fs-test-flood-mouse'")
    time.sleep(1.5)

    subprocess.run(["systemctl", "restart", SENSOR], check=False)
    time.sleep(4.0)

    node = subprocess.run(["bash", "-lc",
                           "grep -l fs-test-flood-mouse /sys/class/input/event*/device/name 2>/dev/null | head -1"],
                          capture_output=True, text=True).stdout.strip()
    print("  sensor bound it: %s" % (node or "(node not found by name)"))

    pid = int(subprocess.run(["pgrep", "-f", "autoresleep.py wol-sensor"],
                             capture_output=True, text=True).stdout.split()[0])
    before_lines = len(AUDIT.read_text(encoding="utf-8").splitlines()) if AUDIT.exists() else 0
    with open("/proc/%d/stat" % pid) as fh:
        f = fh.read().split()
    u0, s0 = int(f[13]), int(f[14])
    t0 = time.monotonic()
    blob = (ev(EV_REL, REL_X, 1) + ev(EV_REL, REL_Y, -1)) * 16 + ev(EV_SYN, 0, 0)
    sent = 0
    end = t0 + FLOOD_S
    while time.monotonic() < end:
        for _ in range(64):                    # 64 writes of 33 events = ~2100 events per burst
            os.write(fd, blob)
            sent += 32
    elapsed = time.monotonic() - t0
    with open("/proc/%d/stat" % pid) as fh:
        f = fh.read().split()
    u1, s1 = int(f[13]), int(f[14])
    cpu = ((u1 - u0) + (s1 - s0)) / HZ
    after_lines = len(AUDIT.read_text(encoding="utf-8").splitlines()) if AUDIT.exists() else 0

    print("  flooded ~%d events in %.1fs  (~%.0f events/s)" % (sent, elapsed, sent / elapsed))
    print("  sensor CPU over that window: %.2fs  => %.1f%% of one core" % (cpu, cpu * 100 / elapsed))
    print("  audit lines added: %d   (one line per event would be ~%d)" % (after_lines - before_lines, sent))
    added = AUDIT.read_text(encoding="utf-8").splitlines()[before_lines:]
    for ln in added[:6]:
        print("     " + ln)

    fcntl.ioctl(fd, UI_DEV_DESTROY)
    os.close(fd)
    subprocess.run(["systemctl", "restart", SENSOR], check=False)
    print("  device destroyed, sensor restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
