"""The packet chain (`pkt-` in v1): magic packet and sender marker detection.

Portable by design - plain UDP sockets, standard library only. No pcap, no capture driver,
no elevated privileges, identical code on Windows, Linux and macOS. The one unavoidable
platform difference is *listening*: the OS firewall has to allow inbound UDP to the process
holding the socket, which is why `autoresleep install` offers to open exactly those ports.
(On Windows that means a firewall rule; on Linux ufw/nftables; on macOS the application
firewall. `autoresleep doctor` reports which one applies.)

Honest limit inherited from v1: the packet that actually wakes a sleeping host is consumed
inside the NIC firmware and is never delivered to any host-side socket on any OS. What this
module sees is a trailing duplicate of a burst, or the sender's marker packet.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MARKER_PREFIX = b"HERMES-WOL-MARKER"
DEFAULT_PORTS = (9, 7, 67, 68)
DEFAULT_MARKER_PORT = 45678
MIN_REPEATS = 8          # senders truncate: v1 accepted >=8 repeats of the MAC


# --------------------------------------------------------------------------------------
# pure detection logic (this is what the tests pin down)
# --------------------------------------------------------------------------------------
def _mac_bytes(mac: str) -> Optional[bytes]:
    cleaned = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(cleaned) != 12:
        return None
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return None


def is_magic_packet(payload: bytes, macs: Iterable[str]) -> bool:
    """6 x 0xFF followed by >= MIN_REPEATS repetitions of one of our MACs."""
    if len(payload) < 6 + MIN_REPEATS * 6:
        return False
    if payload[:6] != b"\xff" * 6:
        return False
    for mac in macs:
        raw = _mac_bytes(mac)
        if raw is None:
            continue
        if payload[6:].startswith(raw * MIN_REPEATS):
            return True
    return False


def marker_seen(payload: bytes, port: int, marker_port: int) -> bool:
    return port == marker_port and payload.startswith(MARKER_PREFIX)


def kind_of(payload: bytes, macs: Iterable[str], port: int, marker_port: int) -> Optional[str]:
    if marker_seen(payload, port, marker_port):
        return "marker"
    if is_magic_packet(payload, macs):
        return "magic"
    return None


def parse_kind(text: str) -> Optional[str]:
    """Read back the state file written by the listener (`kind|epoch=..|src=..|len=..`).

    Kept deliberately primitive, like v1's `pkt-magicpacket-last.txt`: one short line that
    survives being written by a daemon and read by a judge running as another user.
    """
    if not text:
        return None
    head = text.strip().split("|", 1)[0].strip().lower()
    return head if head in ("magic", "marker") else None


# --------------------------------------------------------------------------------------
# MAC inventory - every platform answers the same question in its own way
# --------------------------------------------------------------------------------------
@dataclass
class Adapter:
    name: str
    mac: str
    wireless: bool
    label: str = ""


def _run(cmd: Sequence[str], timeout: float = 10.0) -> str:
    """Hidden + decoded. The listener is a console-less process, so a bare subprocess would make
    Windows pop a console window for every netsh / networksetup / ifconfig call."""
    try:
        from .system.base import run_hidden
        return run_hidden(cmd, timeout)
    except Exception:
        return ""


def adapters_windows() -> List[Adapter]:
    """IP Helper API via ctypes: no WMI, no PowerShell, works on a locked-down box."""
    import ctypes
    from ctypes import wintypes

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class IP_ADAPTER_ADDRESSES(ctypes.Structure):
        pass

    IP_ADAPTER_ADDRESSES._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", wintypes.LPWSTR),
        ("Description", wintypes.LPWSTR),
        ("FriendlyName", wintypes.LPWSTR),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("Mtu", wintypes.DWORD),
        ("IfType", wintypes.DWORD),
        ("OperStatus", ctypes.c_int),
        ("Ipv6IfIndex", wintypes.DWORD),
        ("ZoneIndices", wintypes.DWORD * 16),
        ("FirstPrefix", ctypes.c_void_p),
    ]

    GAA_FLAG_SKIP_ANYCAST = 0x0002
    GAA_FLAG_SKIP_MULTICAST = 0x0004
    GAA_FLAG_SKIP_DNS_SERVER = 0x0008
    IF_TYPE_IEEE80211 = 71
    AF_UNSPEC = 0
    ERROR_BUFFER_OVERFLOW = 111
    NO_ERROR = 0

    iphlpapi = ctypes.windll.iphlpapi
    size = wintypes.ULONG(15 * 1024)
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    buf = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf, ctypes.byref(size))
    if ret == ERROR_BUFFER_OVERFLOW:
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlpapi.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf, ctypes.byref(size))
    if ret != NO_ERROR:
        return []

    found: List[Adapter] = []
    node = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while node:
        a = node.contents
        n = int(a.PhysicalAddressLength)
        if n == 6:
            mac = "".join("%02x" % a.PhysicalAddress[i] for i in range(6))
            name = a.FriendlyName or ""
            desc = a.Description or ""
            found.append(Adapter(name=name, mac=mac, wireless=int(a.IfType) == IF_TYPE_IEEE80211,
                                 label=desc))
        node = a.Next
    return found


def adapters_linux() -> List[Adapter]:
    """sysfs: the `wireless` subdirectory is the kernel's own answer to 'is this Wi-Fi?'."""
    found: List[Adapter] = []
    base = Path("/sys/class/net")
    if not base.is_dir():
        return found
    for dev in sorted(base.iterdir()):
        try:
            mac = (dev / "address").read_text().strip().lower()
        except Exception:
            continue
        if not mac or mac == "00:00:00:00:00:00" or len(mac) != 17:
            continue
        wireless = (dev / "wireless").exists()
        found.append(Adapter(name=dev.name, mac=mac, wireless=wireless))
    return found


def adapters_macos() -> List[Adapter]:
    """networksetup knows which hardware port is Wi-Fi; ifconfig has the MACs."""
    found: List[Adapter] = []
    ifaces: List[Tuple[str, bool]] = []
    current_port = ""
    for line in _run(["networksetup", "-listallhardwareports"]).splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            current_port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:"):
            dev = line.split(":", 1)[1].strip()
            ifaces.append((dev, "wi-fi" in current_port.lower() or "airport" in current_port.lower()))
    for dev, wireless in ifaces:
        for line in _run(["ifconfig", dev]).splitlines():
            if "ether" in line.lower():
                mac = line.split()[1].strip().lower()
                found.append(Adapter(name=dev, mac=mac, wireless=wireless, label=dev))
                break
    return found


def adapters() -> List[Adapter]:
    if os.name == "nt":
        return adapters_windows()
    if os.sys.platform == "darwin":
        return adapters_macos()
    return adapters_linux()


def wireless_macs() -> List[str]:
    """The MACs to match magic packets against - rebuilt from THIS machine every run.

    v1's hardest-won rule: never hardcode a NIC model or interface name. They change when
    the card is swapped or the driver is reinstalled, and the failure mode is silent.
    """
    ads = adapters()
    wifi = [a.mac for a in ads if a.wireless]
    return wifi or [a.mac for a in ads]


# --------------------------------------------------------------------------------------
# the resident part
# --------------------------------------------------------------------------------------
@dataclass
class Seen:
    kind: str
    epoch: float
    src: str
    length: int

    def line(self) -> str:
        return "%s|epoch=%d|src=%s|len=%d" % (self.kind, int(self.epoch), self.src, self.length)


class Listener:
    """Binds the WoL ports and reports every datagram that looks asked-for.

    One socket per port (no raw sockets, no privileges). `poll()` does one non-blocking
    sweep and returns what it saw, so the caller decides what to do with it - which keeps
    this class trivially testable and keeps the sleep decision in one place.
    """

    def __init__(self, ports: Sequence[int] = DEFAULT_PORTS, marker_port: int = DEFAULT_MARKER_PORT,
                 macs: Optional[Iterable[str]] = None):
        self.ports = list(ports)
        self.marker_port = marker_port
        self.macs = list(macs) if macs is not None else wireless_macs()
        self.sockets: Dict[int, socket.socket] = {}
        self._unavailable: Dict[int, str] = {}

    def bind(self) -> None:
        # Validate before touching the OS: `bind()` on a port above 65535 raises OverflowError,
        # and a daemon that dies on start-up because of a config typo looks exactly like a
        # daemon that is running and simply never sees a packet - the failure mode this whole
        # project exists to avoid.
        for port in list(self.ports) + [self.marker_port]:
            if not isinstance(port, int) or isinstance(port, bool) or not 0 < int(port) <= 65535:
                raise ValueError("UDP port out of range: %r (must be 1-65535)" % (port,))
        for port in list(self.ports) + [self.marker_port]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                s.setblocking(False)
                self.sockets[port] = s
            except OSError as exc:
                # ports 67/68 are held by the system's DHCP client on every platform -
                # that is normal, not a failure, and only the marker/magic ports matter
                self._unavailable[port] = str(exc)

    def unavailable(self) -> Dict[int, str]:
        return dict(self._unavailable)

    def poll(self) -> List[Seen]:
        out: List[Seen] = []
        for port, s in self.sockets.items():
            while True:
                try:
                    payload, addr = s.recvfrom(2048)
                except BlockingIOError:
                    break
                except OSError:
                    break
                kind = kind_of(payload, self.macs, port, self.marker_port)
                if kind:
                    out.append(Seen(kind=kind, epoch=time.time(),
                                    src="%s:%d" % (addr[0], addr[1]), length=len(payload)))
        return out

    def close(self) -> None:
        for s in self.sockets.values():
            try:
                s.close()
            except Exception:
                pass
        self.sockets.clear()