"""Resolve the ORB parameters actually used for live signals and trades.

Defaults come from ``configs/windows.json``. Trading overlays those defaults
only when the operator has explicitly chosen a parameter set (from an
optimiser ranking or the optimisation history).
"""

from __future__ import annotations

from typing import Any

from core.db import Database
from core.models import SessionWindow
from core.sessions import WindowConfig, get_window_config


def resolve_trading_config(
    symbol: str,
    window: SessionWindow,
    db: Database | None = None,
) -> tuple[WindowConfig, dict[str, Any] | None]:
    """Return ``(config, chosen_row)`` for ``symbol`` / ``window``.

    ``chosen_row`` is None when the operator has not selected a set and
    ``windows.json`` defaults are used as-is.
    """
    base = get_window_config(window)
    found = (db or Database()).get_active_strategy(symbol, window)
    if found is None:
        return base, None
    return base.overlay(found["params"]), found


def strategy_payload(
    cfg: WindowConfig, found: dict[str, Any] | None
) -> dict[str, Any]:
    """JSON-friendly description of the active strategy parameters."""
    out: dict[str, Any] = {
        "source": "selected" if found else "defaults",
        "params": cfg.strategy_dict(),
    }
    if found:
        out["run"] = {
            "id": found.get("id"),
            "label": found.get("label"),
            "created_at": found.get("created_at"),
        }
    return out


def format_strategy(payload: dict[str, Any]) -> str:
    p = payload.get("params") or {}
    src = payload.get("source") or "defaults"
    return (
        f"{src} OR {p.get('opening_range_minutes')}m "
        f"buf {p.get('breakout_buffer_atr')} str {p.get('min_strength')} "
        f"TR {p.get('target_r')}"
    )


def strategy_by_window(symbol: str, db: Database | None = None) -> dict[str, Any]:
    database = db or Database()
    out: dict[str, Any] = {}
    for window in SessionWindow:
        cfg, found = resolve_trading_config(symbol, window, database)
        out[window.value] = strategy_payload(cfg, found)
    return out
