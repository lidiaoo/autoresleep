"""Configuration: a JSON file in the platform data dir, with v1's shipped defaults.

Deliberately dependency-free (no pydantic, no yaml) - v1's whole value was that it ran on a
machine with nothing installed, and a cross-platform tool that needs a package manager first
is a cross-platform tool nobody installs on the machine they only touch twice a year.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, List, Optional

from . import paths
from .decide import Params

VALID_POLICIES = ("skip", "resleep-if-unattended")


@dataclass
class Config:
    idle_minutes: float = 2.0               # tier B: input idle needed for a Wi-Fi-NIC wake
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
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ validation
    def validate(self) -> "Config":
        if self.unattributed_wake_policy not in VALID_POLICIES:
            raise ValueError("unattributed_wake_policy must be one of %r" % (VALID_POLICIES,))
        for name in ("idle_minutes", "unknown_idle_minutes", "min_gap_minutes",
                     "pkt_window_s", "resume_threshold_s"):
            if float(getattr(self, name)) < 0:
                raise ValueError("%s must not be negative" % name)
        if int(self.repeat_minutes) <= 0:
            raise ValueError("repeat_minutes must be positive")
        if not self.ports or any(int(p) <= 0 or int(p) > 65535 for p in self.ports):
            raise ValueError("ports must be valid UDP port numbers")
        if not 0 < int(self.marker_port) <= 65535:
            raise ValueError("marker_port must be a valid UDP port number")
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