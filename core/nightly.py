"""Nightly grid-search: pick tomorrow's ORB parameters per session window.

Runs after the US cash close and before Asia, so the next Asia / London /
New York sessions all use a freshly ranked set. The job writes the winner
into ``active_strategy`` (same table as the dashboard "Use for trading"
button) and does not place orders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.backtest import DEFAULT_ORB_GRID, dedupe_by_outcome, grid_search
from core.config import get_settings
from core.db import Database, is_tradable_orb_params
from core.ibkr_client import IBKRUnavailable
from core.marketdata import (
    cached_bars_for_lookback,
    fetch_bars_range,
    refresh_recent_bars,
)
from core.models import Bar, SessionWindow
from core.timeutils import utcnow

MIN_TRADES = 5


def select_best_for_window(
    bars: list[Bar],
    window: SessionWindow,
    *,
    db: Database,
    symbol: str,
    apply: bool = True,
    min_trades: int = MIN_TRADES,
    as_of: datetime | None = None,
    grid: dict[str, list] | None = None,
) -> dict[str, Any]:
    """Grid-search one window and optionally apply the #1 tradable set."""
    as_of = as_of or utcnow()
    ranked = grid_search(bars, window, grid or DEFAULT_ORB_GRID)
    distinct = dedupe_by_outcome(ranked)
    best = distinct[0] if distinct else None
    incumbent = db.get_active_strategy(symbol, window)
    day = as_of.strftime("%Y-%m-%d")
    payload: dict[str, Any] = {
        "window": window.value,
        "symbol": symbol.upper(),
        "combinations_tested": len(ranked),
        "distinct_outcomes": len(distinct),
        "applied": False,
        "kept_incumbent": False,
        "best": None,
        "incumbent": (
            {"label": incumbent.get("label"), "params": incumbent.get("params")}
            if incumbent
            else None
        ),
    }
    if best is None or best["score"] <= -1e8:
        payload["reason"] = "No ranked parameter set."
        payload["kept_incumbent"] = incumbent is not None
        return payload

    metrics = best.get("metrics") or {}
    trades = int(metrics.get("trades") or 0)
    params = dict(best["params"])
    payload["best"] = {
        "params": params,
        "score": best["score"],
        "metrics": {
            k: metrics.get(k)
            for k in ("trades", "win_rate", "expectancy", "profit_factor", "sharpe")
            if k in metrics
        },
    }
    if trades < min_trades:
        payload["reason"] = (
            f"Best set has only {trades} trades (need {min_trades}); "
            "keeping the current selection."
        )
        payload["kept_incumbent"] = incumbent is not None
        return payload
    if not is_tradable_orb_params(params):
        payload["reason"] = "Best row is not a tradable ORB set."
        payload["kept_incumbent"] = incumbent is not None
        return payload

    label = (
        f"nightly {day} {symbol.upper()} {window.value} "
        f"(best of {len(ranked)})"
    )
    run_id = db.insert_backtest(
        label=label,
        symbol=symbol.upper(),
        window=window,
        params=params,
        metrics=metrics,
    )
    payload["backtest_id"] = run_id
    payload["label"] = label
    if apply:
        db.set_active_strategy(
            symbol.upper(),
            window,
            params=params,
            backtest_id=run_id,
            label=label,
        )
        payload["applied"] = True
        payload["reason"] = "Applied #1 ranked set for the next session."
    else:
        payload["reason"] = "Dry run: ranked and saved, not applied."
    return payload


async def run_nightly_optimise(
    *,
    symbol: str | None = None,
    lookback_days: int = 60,
    apply: bool = True,
    min_trades: int = MIN_TRADES,
    refresh_days: int = 5,
    progress: Any | None = None,
    db: Database | None = None,
    bars: list[Bar] | None = None,
    grid: dict[str, list] | None = None,
) -> dict[str, Any]:
    """Refresh history, rank each window, apply the winners."""
    log = progress or (lambda _msg: None)
    settings = get_settings()
    symbol = (symbol or settings.default_symbol).upper()
    database = db or Database()
    warning: str | None = None

    if bars is None:
        try:
            await refresh_recent_bars(symbol, days=refresh_days, progress=log)
        except IBKRUnavailable as exc:
            warning = f"IBKR refresh failed, using cache: {exc}"
            log(warning)
        have = cached_bars_for_lookback(symbol, lookback_days)
        if have is None:
            log(f"Cache short of {lookback_days}d; fetching range")
            bars = await fetch_bars_range(symbol, days=lookback_days, progress=log)
        else:
            bars = have
    if not bars:
        return {"ok": False, "error": f"No bars for {symbol}.", "windows": []}

    log(f"Ranking {symbol} on {len(bars)} bars ({lookback_days}d lookback)")
    windows: list[dict[str, Any]] = []
    for window in SessionWindow:
        log(f"  {window.value}...")
        windows.append(
            select_best_for_window(
                bars,
                window,
                db=database,
                symbol=symbol,
                apply=apply,
                min_trades=min_trades,
                grid=grid,
            )
        )
    applied = sum(1 for w in windows if w.get("applied"))
    return {
        "ok": True,
        "symbol": symbol,
        "as_of": utcnow().isoformat() + "Z",
        "lookback_days": lookback_days,
        "bars": len(bars),
        "apply": apply,
        "warning": warning,
        "applied_windows": applied,
        "windows": windows,
    }
