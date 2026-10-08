"""Logging, with the rotation bug fixed by not writing it by hand.

v1 rotated its logs by reading a file and writing it back through the same path, which
truncates first and collapses the file to whatever the reader had buffered - measured: a
decision log came back with 16 lines out of hundreds, a trace log from 1191 lines to 4 KB.
`RotatingFileHandler` does the rename dance correctly, so v2 uses it and there is no custom
rotation left to get wrong.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Optional

from . import paths

_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_CONFIGURED: set = set()


class _ReopenFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler that lets go of the file after every write.

    On Windows an open handle makes the file undeletable ("another program is using this file"),
    and this project does all three things that trip over it: it rotates its own logs, it runs its
    tests against temporary data directories, and it can be pointed at a different data directory
    by `--config`. Nothing about a power tool needs a log file held open between two lines, and the
    noise this produced in the test suite (a teardown crash printed after a green run) is exactly
    how a real failure gets missed.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        finally:
            try:
                self.close()
            except Exception:
                pass


def _handler_for(path, max_bytes: int, backups: int) -> logging.Handler:
    paths.ensure_data_dir()
    handler = _ReopenFileHandler(
        str(path), maxBytes=int(max_bytes), backupCount=int(backups), encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    return handler


def get_logger(name: str = "autoresleep", max_bytes: int = 512 * 1024, backups: int = 3,
               also_stderr: bool = False) -> logging.Logger:
    log = logging.getLogger(name)
    key = (name, str(paths.data_dir()))
    if key in _CONFIGURED:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(_handler_for(paths.log_file(), max_bytes, backups))
    if also_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        log.addHandler(stream)
    _CONFIGURED.add(key)
    return log


def decision_logger(max_bytes: int = 512 * 1024, backups: int = 3) -> logging.Logger:
    """The file a human reads the next morning: one line per judgement."""
    log = logging.getLogger("autoresleep.decisions")
    key = ("autoresleep.decisions", str(paths.data_dir()))
    if key in _CONFIGURED:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = _handler_for(paths.decision_log(), max_bytes, backups)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    _CONFIGURED.add(key)
    return log


def heartbeat(text: str) -> None:
    """Single line, overwritten every run: looking at it answers 'is it alive?' in one second."""
    paths.ensure_data_dir()
    try:
        paths.heartbeat_file().write_text(text, encoding="utf-8")
    except Exception:
        pass


def read_heartbeat() -> Optional[str]:
    target = paths.heartbeat_file()
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8").strip()
    except Exception:
        return None