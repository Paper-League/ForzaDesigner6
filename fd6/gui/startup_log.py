"""Tiny append-only startup logger to diagnose launch hangs in the frozen build.

Writes phase markers to %LOCALAPPDATA%\\FD6\\startup.log. Because the release is
built --windowed (no console), a hang or swallowed exception is otherwise
invisible; this file lets us see the LAST phase reached before a freeze.

Best-effort: any logging failure is ignored — logging must never itself break
or slow startup.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / "FD6"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "startup.log"


def log(phase: str) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {phase}\n")
    except Exception:
        pass


def reset() -> None:
    """Start a fresh log section for this launch."""
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n===== FD6 launch {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    except Exception:
        pass
