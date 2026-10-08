"""Where v2 keeps its state, per platform.

One function, three answers - so the Linux and macOS layouts cannot drift away from the
Windows one. `env` and `home` are injectable purely so this can be unit-tested from any
host (the tests run the macOS and Linux cases on Windows).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional

APP_DIR_WIN = "AutoReSleep"          # %LOCALAPPDATA%\AutoReSleep
APP_DIR_UNIX = "autoresleep"         # $XDG_STATE_HOME/autoresleep
APP_DIR_MAC = "AutoReSleep"          # ~/Library/Application Support/AutoReSleep


def data_dir(platform: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
             home: Optional[Path] = None) -> Path:
    """The single place v2 writes: config, logs, evidence, locks."""
    plat = platform or sys.platform
    e: Dict[str, str] = dict(os.environ if env is None else env)
    h = home if home is not None else Path.home()

    if plat.startswith("win"):
        base = e.get("LOCALAPPDATA") or str(h / "AppData" / "Local")
        return Path(base) / APP_DIR_WIN
    if plat == "darwin":
        return h / "Library" / "Application Support" / APP_DIR_MAC
    base = e.get("XDG_STATE_HOME") or str(h / ".local" / "state")
    return Path(base) / APP_DIR_UNIX


def config_file(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "config.json"


def log_file(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "journal.log"


def decision_log(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "decisions.log"


def heartbeat_file(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "heartbeat.txt"


def lock_file(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "listener.lock"


def packet_state(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "packet-last.txt"


def packet_log(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "packets-seen.log"


def input_last_file(platform: Optional[str] = None) -> Path:
    """The last REAL input event (evdev) seen by the privileged sensor, plus its heartbeat.

    The desktop idle clock is reset by the resume itself, so at the wake moment it cannot be
    trusted; this file is the hardware truth, and its heartbeat tells a reader whether the watcher
    is alive (a stale file must not read as "nobody touched it").
    """
    return data_dir(platform) / "input-last.json"


def session_cause_file(platform: Optional[str] = None) -> Path:
    """Why THIS wake session was wanted: 'asked-for' (a magic/marker packet) or 'input'.

    Keyed by the session stamp inside evidence-last.json, so the latch lives exactly as long as
    the session it belongs to - the next resume invalidates it without anyone clearing it.
    """
    return data_dir(platform) / "session-cause.json"


def link_snapshot_file(platform: Optional[str] = None) -> Path:
    """What the WLAN link looked like before sleeping (v1's os-wlanlink-presleep.txt)."""
    return data_dir(platform) / "link-presleep.json"


def wlan_event_log(platform: Optional[str] = None) -> Path:
    """Link moves as reported by nl80211 itself (`iw event`), recorded by the privileged sensor.

    The portable wifi-disconnect record: one uniform format for every driver, unlike a driver's
    private wake-reason string, which only some drivers print and each with its own field names.
    """
    return data_dir(platform) / "wlan-events.log"


def input_audit_log(platform: Optional[str] = None) -> Path:
    """Every evdev event that COUNTED as human input, with its device name.

    Exists because "who woke it" was unanswerable from the decision log alone: the log showed
    `input-at-wake(evdev)` without saying WHICH device or WHEN, so an unattended machine that woke
    itself could not be told apart from somebody pressing a key.
    """
    return data_dir(platform) / "input-events.log"


def link_event_log(platform: Optional[str] = None) -> Path:
    """Every link transition the LISTENER observed, with timestamps.

    The portable "did the wifi reconnect?" record: the listener samples the link while the
    machine is awake, so a drop-and-return is an observed event, not a driver's opinion.
    """
    return data_dir(platform) / "link-events.log"


def forced_sleep_marker(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "last-forced-sleep.txt"


def window_input_file(platform: Optional[str] = None) -> Path:
    """Did anybody touch the machine inside the re-sleep window? (`windowwatch`)

    Keyed by the wake the window belongs to, so the listener (which samples the platform input
    clock) and the judge (which decides) can be separate processes - the same file-based pattern
    the packet and hardware-input chains already use.
    """
    return data_dir(platform) / "window-input.json"


def ensure_data_dir(platform: Optional[str] = None) -> Path:
    d = data_dir(platform)
    d.mkdir(parents=True, exist_ok=True)
    return d