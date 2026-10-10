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

# The one MAC format everyone can agree on; the config module owns it because the config file is
# where a human types one (`wake_nics`), and pkt is where it gets compared.
from .config import normalize_mac

MARKER_PREFIX = b"HERMES-WOL-MARKER"

#: the WoL ethertype: a magic packet as a BARE ethernet frame, no IP and no UDP at all.
#: `etherwake`, most BIOS/switch tools and a fair number of phone apps send this instead, so a
#: listener watching only UDP 9/7 misses them completely - and this is the frame a NIC is most
#: often configured to wake on. Seeing it needs CAP_NET_RAW, hence the privileged sensor.
ETH_P_MAGIC = 0x0842
WOL_PORTS = (9, 7)            # where a magic packet is conventionally sent
DHCP_PORTS = (67, 68)         # watched only because some NICs wake on a DHCP renew
DEFAULT_PORTS = WOL_PORTS + DHCP_PORTS
DEFAULT_MARKER_PORT = 45678
MIN_REPEATS = 8          # senders truncate: v1 accepted >=8 repeats of the MAC

UNPRIVILEGED_PORT_FILE = "/proc/sys/net/ipv4/ip_unprivileged_port_start"


def _exclusive(sock: socket.socket) -> None:
    """Refuse to share this port with anybody, and let the OS say so if it is taken.

    NOT SO_REUSEADDR. On Windows that flag means the opposite of the Unix meaning: it *permits* a
    second process to bind the same address, and then which socket a datagram is delivered to is not
    ours to decide. Measured on a real machine: after a few installs, three processes were bound to
    UDP 9/7/67/68 at once - the current listener plus two stale ones whose kill had failed - so a
    magic packet could be handed to a listener whose judge is not the running one. That is the
    failure shape this project exists to prevent, and it fails *silently*: every process believes it
    owns the port, `advice()` never fires its "held by another process" branch, and the packet chain
    just stops seeing packets.

    SO_EXCLUSIVEADDRUSE is Windows' way to say "this port is mine alone" (it also stops a third
    party from stealing a port we already hold). On POSIX the default - no SO_REUSEADDR - already
    means that: a second bind fails with EADDRINUSE, which `bind()` records in `unavailable()` and
    `advice()` explains to the operator.
    """
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            return
        except OSError:
            pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    except OSError:
        pass


def udp_ports_in_use(ports: Iterable[int], path: str = "/proc/net/udp") -> set:
    """Which of these UDP ports some process is already listening on.

    Straight from the kernel (`/proc/net/udp`, port column in hex), no privileges and no `ss`
    parsing. It is how the unprivileged half tells "I cannot bind this" apart from "somebody is
    already watching it" - the second one is not a problem, it is the privileged sensor.
    """
    wanted = set(int(p) for p in ports)
    found = set()
    try:
        lines = Path(path).read_text().splitlines()
    except Exception:
        return found
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "07":      # 07 = listening; UDP keeps no other state
            continue
        try:
            port = int(parts[1].split(":")[1], 16)
        except Exception:
            continue
        if port in wanted:
            found.add(port)
    return found


def unprivileged_port_start(path: str = UNPRIVILEGED_PORT_FILE) -> Optional[int]:
    """Lowest port an ordinary user may bind; None where the kernel has no such boundary
    (i.e. not Linux). This number decides whether the WoL ports are watchable at all."""
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return None


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


def marker_seen(payload: bytes, port: int, marker_port: Optional[int]) -> bool:
    return port == marker_port and payload.startswith(MARKER_PREFIX)


def kind_of(payload: bytes, macs: Iterable[str], port: int,
            marker_port: Optional[int]) -> Optional[str]:
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


# --------------------------------------------------------------------------------------
# WHICH adapters are policed (`wake_nics` in the config)
# --------------------------------------------------------------------------------------
@dataclass
class NicSelection:
    """The adapters a wake is judged against, plus the config entries that matched nothing."""
    adapters: List[Adapter]
    unmatched: List[str]

    def macs(self) -> List[str]:
        return [a.mac for a in self.adapters if a.mac]

    def line(self) -> str:
        if not self.adapters:
            return "none"
        return ", ".join("%s %s%s" % (a.name, "wifi" if a.wireless else "wired",
                                      (" " + a.mac) if a.mac else "")
                         for a in self.adapters)


def _name_matches(entry: str, adapter: Adapter) -> bool:
    """Adapter names are whatever the platform calls them, so compare case-insensitively and
    accept the human label too (`netsh` shows the description, `iw` the interface name)."""
    want = entry.strip().lower()
    for candidate in (adapter.name, adapter.label):
        if candidate and candidate.strip().lower() == want:
            return True
    return False


def select_wake_nics(nics: Optional[Sequence[str]] = None,
                     ads: Optional[Sequence[Adapter]] = None) -> NicSelection:
    """Which adapters the re-sleep logic acts on.

    Empty/None = EVERY adapter, wired and wireless (`[]` in the config, docs/resleep-window-plan.md).
    Entries match by MAC first - normalised, because every platform tool writes them differently -
    then by adapter name. `unmatched` is returned rather than swallowed: an entry that matches no
    adapter is a NIC nobody is policing, and the visible symptom would be a machine that keeps
    waking itself, which is the failure mode this whole project exists to remove.
    """
    ads = list(ads if ads is not None else adapters())
    entries = [str(n).strip() for n in (nics or []) if str(n).strip()]
    if not entries:
        return NicSelection(adapters=ads, unmatched=[])
    chosen: List[Adapter] = []
    unmatched: List[str] = []
    for entry in entries:
        mac = normalize_mac(entry)
        hit: Optional[Adapter] = None
        if mac:
            for a in ads:
                if a.mac and normalize_mac(a.mac) == mac:
                    hit = a
                    break
        if hit is None:
            for a in ads:
                if _name_matches(entry, a):
                    hit = a
                    break
        if hit is None:
            unmatched.append(entry)
        elif hit not in chosen:
            chosen.append(hit)
    return NicSelection(adapters=chosen, unmatched=unmatched)


def wake_macs(nics: Optional[Sequence[str]] = None) -> List[str]:
    """The MACs to match magic packets against: the configured NICs, else ALL of them.

    This replaced a `wireless_macs()` that - on a box that has wifi - quietly ignored a magic
    packet addressed to the ETHERNET card's MAC. No packet evidence, so a wired WoL wake was
    judged non-magic and slept again: the exact opposite of what the operator asked for.
    v1's rule still holds: never hardcode a NIC model or interface name; rebuild it every run.
    """
    return select_wake_nics(nics).macs()


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


class RawMagicSocket:
    """A bare-ethertype listener for `0x0842` magic frames (Linux; needs CAP_NET_RAW).

    Bound to the ethertype itself, so the kernel filters: this socket sees the WoL frames and
    almost nothing else, which keeps it cheap enough to run next to the UDP listener.
    """

    def __init__(self, macs: Optional[Sequence[str]] = None, iface: Optional[str] = None):
        self.macs = list(macs or [])
        self.iface = iface
        self.sock = None
        self.error: Optional[str] = None

    def bind(self) -> bool:
        try:
            import socket as _socket
            sock = _socket.socket(_socket.AF_PACKET, _socket.SOCK_RAW,
                                  _socket.htons(ETH_P_MAGIC))
            if self.iface:
                sock.bind((self.iface, 0))
            sock.setblocking(False)
            self.sock = sock
            return True
        except Exception as exc:
            self.error = str(exc)
            self.sock = None
            return False

    @staticmethod
    def parse_frame(frame: bytes, macs: Sequence[str]) -> Optional["Seen"]:
        """A received 0x0842 frame -> Seen, or None when it is somebody else's packet.

        Separated from the socket so the ethernet layout (dst 6 / src 6 / ethertype 2, then the
        magic payload) is testable without privileges or a live interface.
        """
        if len(frame) <= 14:
            return None
        payload = frame[14:]
        if not is_magic_packet(payload, macs):
            return None       # somebody else's magic frame, or a different payload shape
        src_mac = ":".join("%02x" % b for b in frame[6:12])
        return Seen(kind="magic", epoch=time.time(),
                    src="raw:0x0842 from %s" % src_mac, length=len(payload))

    def poll(self) -> List["Seen"]:
        if self.sock is None:
            return []
        out: List[Seen] = []
        while True:
            try:
                frame, _addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                break
            except Exception:
                break
            seen = self.parse_frame(frame, self.macs)
            if seen is not None:
                out.append(seen)
        return out

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def advice(self) -> Optional[str]:
        if self.sock is not None or not self.error:
            return None
        return ("bare ethernet magic frames (ethertype 0x0842) will NOT be seen: %s. They need "
                "CAP_NET_RAW, which the installed wol-sensor unit grants - re-run "
                "`autoresleep install` or check `systemctl show -p AmbientCapabilities "
                "autoresleep-wol-sensor`." % self.error)


class Listener:
    """Binds the WoL ports and reports every datagram that looks asked-for.

    One socket per port (no raw sockets, no privileges). `poll()` does one non-blocking
    sweep and returns what it saw, so the caller decides what to do with it - which keeps
    this class trivially testable and keeps the sleep decision in one place.
    """

    def __init__(self, ports: Sequence[int] = DEFAULT_PORTS,
                 marker_port: Optional[int] = DEFAULT_MARKER_PORT,
                 macs: Optional[Iterable[str]] = None):
        self.ports = list(ports)
        self.marker_port = marker_port
        #: `marker_port=None` means "do not bind a marker socket" - the privileged sensor watches
        #: only the magic ports, and it must NOT steal the marker port from the user listener
        self._bind_ports = list(self.ports)
        if marker_port:
            self._bind_ports.append(marker_port)
        self.macs = list(macs) if macs is not None else wake_macs(None)
        self.sockets: Dict[int, socket.socket] = {}
        self._unavailable: Dict[int, str] = {}
        #: errno per port, kept separately because the MESSAGE is localized while the number is not:
        #: EADDRINUSE is 98 on POSIX and 10048 (WSAEADDRINUSE) on Windows, and a Chinese Windows
        #: says "通常每个套接字地址(协议/网络地址/端口)只允许使用一次" - so matching on the English
        #: text "in use" decided "not in use" and turned the healthy state (our own listener holding
        #: the ports) into "nothing can tell an asked-for wake from a spurious one until this is
        #: fixed". doctor printed exactly that on a machine where everything was fine.
        self._errno: Dict[int, Optional[int]] = {}

    def bind(self) -> None:
        # Validate before touching the OS: `bind()` on a port above 65535 raises OverflowError,
        # and a daemon that dies on start-up because of a config typo looks exactly like a
        # daemon that is running and simply never sees a packet - the failure mode this whole
        # project exists to avoid.
        for port in self._bind_ports:
            if not isinstance(port, int) or isinstance(port, bool) or not 0 < int(port) <= 65535:
                raise ValueError("UDP port out of range: %r (must be 1-65535)" % (port,))
        for port in self._bind_ports:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _exclusive(s)
                s.bind(("0.0.0.0", port))
                s.setblocking(False)
                self.sockets[port] = s
            except OSError as exc:
                # ports 67/68 are held by the system's DHCP client on every platform -
                # that is normal, not a failure, and only the marker/magic ports matter
                self._unavailable[port] = str(exc)
                self._errno[port] = getattr(exc, "errno", None)
                # Close the socket that failed to bind (it exists unless socket() itself failed):
                # a listener that keeps retrying would leak one descriptor per attempt.
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    def unavailable(self) -> Dict[int, str]:
        return dict(self._unavailable)

    def unsupported_wol_ports(self) -> Dict[int, str]:
        """WoL ports we could not watch, with the reason.

        A partial bind must never be quiet. On Linux these ports are privileged, this daemon runs
        as an ordinary user (`--user` unit), and the visible symptom is not an error: it is a
        magic-packet wake re-slept two minutes later because the packet was never seen.
        """
        return {p: why for p, why in self._unavailable.items() if p in WOL_PORTS}

    def advice(self) -> Optional[str]:
        """What the operator can do about a partial bind (None when there is nothing to say).

        Failing to bind 67/68 is routine - the DHCP client owns those - so only the magic ports,
        and the marker port (the fallback that works unprivileged), are worth a word.
        """
        reasons = " ".join(self._unavailable.values()).lower()
        # EADDRINUSE by NUMBER as well as by text: 98 on POSIX, 10048 on Windows, and the message
        # is localized (a Chinese Windows says "只允许使用一次", which no English match will catch)
        errnos = set(e for e in self._errno.values() if e is not None)
        in_use = bool(errnos & {98, 10048}) or ("in use" in reasons) or ("98" in reasons) \
            or ("只允许使用一次" in reasons)
        covered = udp_ports_in_use(self._unavailable.keys())
        if self.marker_port in self._unavailable and not self.sockets:
            why = self._unavailable[self.marker_port]
            if in_use:
                return ("marker port %d is held by another process (another listener?): this "
                        "probe could not bind anything, which is expected while the daemon runs"
                        % self.marker_port)
            return ("no UDP port could be bound at all (marker %d: %s) - nothing can tell an "
                    "asked-for wake from a spurious one until this is fixed"
                    % (self.marker_port, why))
        missing = self.unsupported_wol_ports()
        if not missing:
            return None
        ports = "/".join(str(p) for p in sorted(missing))
        if covered and len(covered) == len(missing):
            # somebody with the privilege already watches them: the wol-sensor system unit
            return ("magic packets to UDP port%s %s are watched by another process already (the "
                    "privileged wol-sensor unit); this listener covers the marker port only, "
                    "which is the intended split" % ("s" if len(missing) > 1 else "", ports))
        if any(k in reasons for k in ("denied", "permission", "not permitted", "不允许")):
            start = unprivileged_port_start()
            # marker_port is None on the privileged sensor, which is *supposed* to hold 9/7, so
            # the "send the marker elsewhere instead" advice only fits the user listener.
            marker = ("" if self.marker_port is None else
                      " - or send the marker packet to port %d instead (README section 7, "
                      "'marker')" % self.marker_port)
            return ("magic packets to UDP port%s %s will NOT be seen: binding them needs root "
                    "(%s). Either raise the boundary - `sudo sysctl -w "
                    "net.ipv4.ip_unprivileged_port_start=%d` and persist it in "
                    "/etc/sysctl.d/99-autoresleep.conf%s"
                    % ("s" if len(missing) > 1 else "", ports,
                       ("unprivileged_port_start=%d" % start) if start is not None
                       else "the kernel reserves them",
                       min(list(missing) + [9]), marker))
        return ("magic packets to UDP port%s %s will NOT be seen (%s)"
                % ("s" if len(missing) > 1 else "", ports,
                   "; ".join(sorted(set(missing.values())))))

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