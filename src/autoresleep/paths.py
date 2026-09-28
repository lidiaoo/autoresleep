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


def link_snapshot_file(platform: Optional[str] = None) -> Path:
    """What the WLAN link looked like before sleeping (v1's os-wlanlink-presleep.txt)."""
    return data_dir(platform) / "link-presleep.json"


def forced_sleep_marker(platform: Optional[str] = None) -> Path:
    return data_dir(platform) / "last-forced-sleep.txt"


def ensure_data_dir(platform: Optional[str] = None) -> Path:
    d = data_dir(platform)
    d.mkdir(parents=True, exist_ok=True)
    return d