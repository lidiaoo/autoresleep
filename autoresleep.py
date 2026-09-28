#!/usr/bin/env python3
"""Run v2 straight from a checkout: `python autoresleep.py doctor`.

No venv, no pip install, no dependencies - which is the point: this has to work on a machine
you log into twice a year, and on a Linux box with no pip at all. `pyproject.toml` exists for
people who would rather have a real `autoresleep` command on their PATH.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from autoresleep.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())