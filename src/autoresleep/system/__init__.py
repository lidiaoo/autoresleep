"""Platform dispatch + the evidence the judge reads.

The judge must not care which OS it runs on, so the platform layer is chosen once here and
everything else talks to the `System` interface. `collect()` writes the evidence the next
judge run will read - the v2 equivalent of v1's `drv-`/`os-` files, with `chain` recorded so a
reader can tell WHICH chain produced a line.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ..decide import Evidence
from .. import paths
from .base import FirewallStatus, IdleInfo, System, WakeSource

__all__ = ["get_system", "System", "IdleInfo", "WakeSource", "FirewallStatus",
           "write_wake_evidence", "read_evidence"]


def get_system(platform: Optional[str] = None) -> System:
    plat = (platform or sys.platform).lower()
    if plat.startswith("win"):
        from .windows import WindowsSystem
        return WindowsSystem()
    if plat == "darwin":
        from .macos import MacSystem
        return MacSystem()
    if plat.startswith("linux") or plat.startswith("freebsd"):
        from .linux import LinuxSystem
        return LinuxSystem()
    raise ValueError("unsupported platform: %r" % platform)


def write_wake_evidence(source: WakeSource, link: Optional[str] = None,
                        link_why: str = "", chain: str = "os",
                        session: Optional[float] = None,
                        observed_at: Optional[float] = None) -> Path:
    """One JSON file per resume, written by the collector, read by the judge.

    Deliberately a separate FILE with a timestamp inside: v1 learned that a judge and a
    collector running as different users must not race on a shared log line, and that a
    reader has to be able to tell "no evidence" from "evidence that says nothing".

    `session`/`observed_at` exist so a re-write of the SAME resume (the judge probing the link
    because that resume's evidence had none) keeps the session stamp: moving it would look like a
    new wake and silently drop the "this wake was asked for" latch.
    """
    paths.ensure_data_dir()
    target = paths.data_dir() / "evidence-last.json"
    stamp = float(observed_at or time.time())
    payload = {
        "observed_at": stamp,
        #: which wake session this describes. Everything that is "true about this wake but only
        #: provable near the resume" is latched against this stamp (see write_session_cause).
        "session": float(session or stamp),
        # WHICH wake this describes. v1 validated its driver reason against the wake it was
        # judging; without that, an attribution from six hours ago gets reused as if it were
        # about the wake happening now (measured on this box: every decision for an evening was
        # based on a 16:11 attribution while the machine kept waking at 20:4x).
        "wake_time": source.wake_time,
        "wake_epoch": source.wake_epoch,
        "chain": source.chain or chain,
        "kind": source.kind,
        "who": source.who,
        "raw": source.raw,
        "timer": source.timer,
        "method": source.method,
        "link": link,
        "link_why": link_why,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)          # atomic: the judge never reads a half-written file
    return target


#: the causes that exempt a WHOLE wake session from re-sleeping
SESSION_CAUSES = ("asked-for", "input")


def write_session_cause(cause: str, session: Optional[float]) -> None:
    """Latch WHY this wake session was wanted, so the exemption outlives the evidence window.

    "A magic packet woke us" and "somebody's hands were on it" are both facts about an INSTANT
    near the resume, so they can only be established inside a window. The consequence - do not put
    this machine back to sleep - has to last the whole session, and a window cannot express that:
    measured on this box, a magic-packet wake was re-slept two minutes after the window expired,
    and a keyboard wake was re-slept as soon as the idle clock passed the threshold.

    Keyed by the session stamp, so the latch cannot leak into a later wake: a new resume writes a
    new stamp and the old latch stops matching. Nothing has to remember to clear it.
    """
    if not cause or session is None:
        return
    paths.ensure_data_dir()
    target = paths.session_cause_file()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"cause": cause, "session": float(session)}), encoding="utf-8")
    tmp.replace(target)


def read_session_cause(session: Optional[float]) -> Optional[str]:
    """The latched cause belonging to `session`, or None (no latch / another session / unreadable)."""
    target = paths.session_cause_file()
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    if session is None or float(data.get("session") or 0) != float(session):
        return None   # a different resume: its latch is history, not evidence about this one
    cause = data.get("cause")
    return cause if cause in SESSION_CAUSES else None


def read_evidence(max_age_s: float = 12 * 3600,
                  current_wake_epoch: Optional[float] = None) -> Evidence:
    """Assemble what the judge needs. Missing/stale/mismatched evidence returns empty chains.

    `current_wake_epoch` is the wake the OS says is the latest one. If the stored evidence
    describes a different wake, it is not evidence about this one - return nothing and let the
    caller re-query, instead of acting on a conclusion that belongs to another night.
    """
    target = paths.data_dir() / "evidence-last.json"
    if not target.exists():
        return Evidence()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return Evidence()
    age = time.time() - float(data.get("observed_at") or 0)
    if age > max_age_s:
        return Evidence()
    stored_wake = data.get("wake_epoch")
    if current_wake_epoch:
        if not stored_wake:
            # Older evidence has no wake stamp, so it cannot be checked against this wake.
            # "Cannot be verified" must not read as "verified": refuse it and re-query live.
            return Evidence()
        if abs(float(stored_wake) - float(current_wake_epoch)) > 120:
            return Evidence()  # belongs to another wake: history, not evidence

    pkt_kind, pkt_age = None, None
    state = paths.packet_state()
    if state.exists():
        try:
            head = state.read_text(encoding="utf-8").strip()
            body = dict(kv.split("=", 1) for kv in head.split("|") if "=" in kv)
            if head.startswith("magic") or head.startswith("marker"):
                pkt_kind = head.split("|", 1)[0]
                pkt_age = time.time() - float(body.get("epoch", 0))
        except Exception:
            pass

    chain = data.get("chain") or "os"
    kind = data.get("kind") or None
    return Evidence(pkt_kind=pkt_kind, pkt_age_s=pkt_age,
                    # a driver report is a different claim from an OS attribution, so it lands
                    # in its own field (and wins in the engine)
                    drv_reason=(kind if chain == "drv" else None),
                    drv_age_s=age,
                    os_link=data.get("link"), os_link_why=data.get("link_why"),
                    wake_kind=(kind if chain != "drv" else None),
                    wake_who=data.get("who") or None,
                    # when the evidence was collected - i.e. the resume moment. Used as the
                    # wake epoch when the OS cannot name one (Linux has no portable stamp).
                    observed_at=float(data.get("observed_at") or 0) or None,
                    session=(float(data.get("session") or data.get("observed_at") or 0) or None))