"""Resolve active MES/futures 5ORB config (JSON defaults + optional optimiser overlay)."""

from __future__ import annotations

from typing import Any

from core.db import Database
from core.models import SessionWindow
from core.strategy.mes_5orb.sessions import (
    Mes5OrbConfig,
    apply_mes_opt_params,
    load_mes_5orb_config,
)


def resolve_mes_5orb_config(
    symbol: str | None = None,
    db: Database | None = None,
    *,
    window: SessionWindow | None = None,
) -> tuple[Mes5OrbConfig, dict[str, Any] | None]:
    """Return ``(cfg, chosen_row)`` with optimiser knobs applied when selected."""
    cfg = load_mes_5orb_config(symbol)
    database = db or Database()
    windows = (
        [window]
        if window is not None
        else [SessionWindow.NEW_YORK, SessionWindow.LONDON]
    )
    for win in windows:
        if win is None:
            continue
        found = database.get_active_strategy(cfg.symbol, win)
        if found and isinstance(found.get("params"), dict):
            params = found["params"]
            # Only overlay when the saved set carries exit/retest knobs.
            if any(
                k in params
                for k in (
                    "target_r",
                    "scale_fraction",
                    "stop_buffer_ticks",
                    "tolerance_ticks",
                    "require_rejection_candle",
                )
            ):
                return apply_mes_opt_params(cfg, params), found
    return cfg, None
