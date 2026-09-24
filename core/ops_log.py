"""In-memory ops event ring for Discord / dashboard consumers.

Auto-trade, session exits, and trailing-stop events all emit here so the
Discord bot can pump a single endpoint even when auto-trade is stopped.
"""

from __future__ import annotations

import threading
from typing import Any

from core.timeutils import utcnow

_LOCK = threading.Lock()
_ENTRIES: list[dict[str, Any]] = []
_MAX = 200


def emit(msg: str, *, level: str = "info", source: str = "ops") -> dict[str, Any]:
    entry = {
        "t": utcnow().isoformat(),
        "level": level,
        "source": source,
        "msg": msg,
    }
    with _LOCK:
        _ENTRIES.append(entry)
        if len(_ENTRIES) > _MAX:
            del _ENTRIES[: len(_ENTRIES) - _MAX]
    return entry


def recent(*, limit: int = 80) -> list[dict[str, Any]]:
    with _LOCK:
        rows = list(_ENTRIES)
    if limit <= 0:
        return rows
    return rows[-limit:]


def clear() -> None:
    """Test helper."""
    with _LOCK:
        _ENTRIES.clear()
