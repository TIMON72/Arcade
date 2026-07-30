"""Paid play session — shared wall-clock deadline across processes."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_PATH = Path(__file__).resolve().parent / "session_until.txt"


def _read_until() -> float:
    try:
        return float(SESSION_PATH.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0.0


def _write_until(until: float) -> None:
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_text(str(until), encoding="utf-8")
    os.replace(tmp, SESSION_PATH)


def remaining_sec() -> float:
    return max(0.0, _read_until() - time.time())


def add_minutes(minutes: int) -> float:
    """Extend session by `minutes` (stacks on remaining time). Returns new remaining sec."""
    minutes = max(0, int(minutes))
    now = time.time()
    base = max(now, _read_until())
    until = base + minutes * 60.0
    _write_until(until)
    rem = until - now
    logger.info("Session +%s min → %.0fs left (until=%s)", minutes, rem, until)
    return rem


def clear() -> None:
    _write_until(0.0)
    logger.info("Session cleared")
