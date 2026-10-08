"""Configuration: a JSON file in the platform data dir, with v1's shipped defaults.

Deliberately dependency-free (no pydantic, no yaml) - v1's whole value was that it ran on a
machine with nothing installed, and a cross-platform tool that needs a package manager first
is a cross-platform tool nobody installs on the machine they only touch twice a year.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, List, Optional

from . import paths
from .decide import Params

VALID_POLICIES = ("skip", "resleep-if-unattended")

#: Keys whose value is a list of STRINGS. `ports` is a list of ints, so `config --set` cannot
#: infer the element type from "it is a list" - coercing a NIC name with int() would raise.
#: Kept next to the fields it describes rather than in the CLI.
LIST_OF_STR_KEYS = frozenset(("wake_nics",))

#: A MAC however a human writes it: 00:11:22:33:44:55, 00-11-22-33-44-55, 0011.2233.4455,
#: 001122334455. Adapter NAMES are never matched here - they are whatever the platform calls them.
_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$"
                     r"|^[0-9a-f]{12}$"
                     r"|^(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}$", re.I)


def normalize_mac(text: str) -> Optional[str]:
    """'00-11-22-33-44-55' -> '00:11:22:33:44:55'; None when it is not a MAC at all.

    One canonical form, because the same card is written differently by every platform tool
    (netsh lowercases with dashes, `ip link` colon-separates, ifconfig does not pad) and a
    comparison that silently misses is the failure this project keeps guarding against.
    """
    raw = str(text).strip()
    if not _MAC_RE.match(raw):
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


@dataclass
class Config:
    #: DEPRECATED as a *wait*: the re-sleep wait is now `resleep_delay_seconds`, measured from the
    #: wake moment (docs/resleep-window-plan.md). The field stays because old config files, the
    #: pre-suspend grace check and several tests still speak of it - but it no longer decides how
    #: long a NIC-caused wake waits before it is slept again.
    idle_minutes: float = 2.0
    unknown_idle_minutes: float = 15.0      # unattributable wake: demand a long idle
    unattributed_wake_policy: str = "skip"  # 'skip' | 'resleep-if-unattended'
    repeat_minutes: int = 2                 # how often the judge runs
    min_gap_minutes: float = 5.0            # debounce between two forced sleeps ...
    min_gap_unattended_minutes: float = 1.0  # ... but only when a human might be there
    pkt_window_s: float = 180.0             # how recent a packet must be to count
    resume_threshold_s: float = 90.0        # wall/monotonic skew that means "we slept"
    fast_path_seconds: int = 30             # listener re-judges this often once idle >= threshold
    link_sample_seconds: int = 15            # listener samples the link this often (driver-free)
    grace_seconds: int = 60                 # last look before suspending (v1's tier-A grace)
    grace_only_for_guesses: bool = True     # grace protects guesses, not evidence-backed wakes
    marker_port: int = 45678
    ports: List[int] = field(default_factory=lambda: [9, 7, 67, 68])
    arm_idle_seconds: int = 45              # arm the wake-reason trace once this idle
    disarm_idle_seconds: int = 20           # disarm it when the user is definitely back
    log_max_bytes: int = 512 * 1024
    log_backups: int = 3
    firewall_rule: bool = True              # open the listener's UDP ports at install
    # --- the re-sleep window (docs/resleep-window-plan.md) ---------------------------------
    wake_nics: List[str] = field(default_factory=list)  # [] = every wired + every wireless NIC
    magic_only: bool = False                # only a WoL magic packet may wake this machine
    resleep_delay_seconds: float = 10.0     # observation window, measured from the WAKE moment
    resleep_break_on_input: bool = True     # hands on the machine inside the window cancel it
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ validation
    def validate(self) -> "Config":
        if self.unattributed_wake_policy not in VALID_POLICIES:
            raise ValueError("unattributed_wake_policy must be one of %r" % (VALID_POLICIES,))
        for name in ("idle_minutes", "unknown_idle_minutes", "min_gap_minutes",
                     "pkt_window_s", "resume_threshold_s", "resleep_delay_seconds"):
            if float(getattr(self, name)) < 0:
                raise ValueError("%s must not be negative" % name)
        if int(self.repeat_minutes) <= 0:
            raise ValueError("repeat_minutes must be positive")
        if not self.ports or any(int(p) <= 0 or int(p) > 65535 for p in self.ports):
            raise ValueError("ports must be valid UDP port numbers")
        if not 0 < int(self.marker_port) <= 65535:
            raise ValueError("marker_port must be a valid UDP port number")
        # A typo in wake_nics must be loud: an entry that matches no adapter is a NIC nobody is
        # policing, and the failure mode is a machine that keeps waking itself (plan section 4.4).
        if not isinstance(self.wake_nics, (list, tuple)):
            raise ValueError("wake_nics must be a list")
        cleaned: List[str] = []
        for item in self.wake_nics:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("wake_nics entries must be non-empty strings "
                                 "(a MAC or an adapter name)")
            cleaned.append(normalize_mac(item) or item.strip())
        self.wake_nics = cleaned
        # Coerced rather than rejected: a hand-edited config.json that says 1 instead of true must
        # not crash a judge whose only job is to decide whether to sleep the machine.
        self.magic_only = bool(self.magic_only)
        self.resleep_break_on_input = bool(self.resleep_break_on_input)
        return self

    # ------------------------------------------------------------------ i/o
    def save(self, path: Optional[Path] = None) -> Path:
        self.validate()
        target = Path(path) if path else paths.config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8")
        # atomic replace: a judge run must never read a half-written config
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        target = Path(path) if path else paths.config_file()
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        # unknown keys are ignored on purpose: a v1 or future-version file must not crash
        # a judge that is only trying to decide whether to sleep the machine
        data: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        return cls(**data)

    # ------------------------------------------------------------------ bridge to the engine
    def params(self) -> Params:
        return Params(pkt_window_s=float(self.pkt_window_s),
                      idle_minutes=float(self.idle_minutes),
                      unknown_idle_minutes=float(self.unknown_idle_minutes),
                      min_gap_minutes=float(self.min_gap_minutes),
                      min_gap_unattended_minutes=float(self.min_gap_unattended_minutes),
                      unattributed_wake_policy=self.unattributed_wake_policy)