"""Starlette web dashboard for the MES 5ORB futures system.

Serves a single-page UI plus a small JSON API over the trade journal and (when
IB Gateway is reachable) live IBKR positions and account values.

Run with:  python -m dashboard.app
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytz
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from core.active_params import (
    format_strategy,
    resolve_trading_config,
    strategy_by_window,
    strategy_payload,
)
from core.analytics import daily_revenue, monte_carlo, performance_metrics, summarize
from core.autotrade_state import load_state, save_state, should_autostart, start_kwargs
from core.backtest_mes import (
    evaluate_mes_signal_live,
    mes_live_config_snapshot,
    optimise_mes_5orb,
    run_mes_5orb_backtest,
    walk_forward_mes_7030,
)
from core.config import get_settings
from core.db import Database, is_tradable_orb_params
from core.engine import (
    build_trade_plan,
    candidate_mes_sessions_now,
    place_trade_plan,
    reconcile_open_futures_vs_ibkr,
    resolve_window,
    settle_session_exits,
)
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.journal import close_open_paper_trades, mark_open_trade
from core.marketdata import fetch_bars_with_fallback
from core.mes_active import resolve_mes_5orb_config
from core.models import Regime, SessionWindow, TradeStatus
from core.risk import RiskManager
from core.strategy.mes_5orb.asia_range import compute_asia_range, detect_asia_judas
from core.strategy.mes_5orb.markets import coerce_futures_symbol, is_supported_futures
from core.strategy.mes_5orb.opening_range import compute_opening_range, to_et
from core.strategy.mes_5orb.sessions import (
    apply_mes_opt_params,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
    resolve_session_exits,
    resolve_session_max_entries,
)
from core.sessions import describe_windows_gmt
from core.strategy.orb import compute_orb_signal
from core.timeutils import as_naive_utc, market_now, utcnow

_EASTERN = pytz.timezone("America/New_York")

_STATIC = Path(__file__).resolve().parent / "static"
_db = Database()
_log = logging.getLogger("orb.dashboard")
_SESSION_EXIT_INTERVAL = 30.0
# Dashboard is MES-only — hide legacy SPY / other symbols from every journal view.
_DASHBOARD_SYMBOL = "MES"


def _is_dashboard_symbol(symbol: str | None) -> bool:
    if not symbol:
        return False
    return str(symbol).strip().upper() == _DASHBOARD_SYMBOL


def _mes_trades(trades: list) -> list:
    return [t for t in trades if _is_dashboard_symbol(getattr(t, "symbol", None))]


def _mes_paper_snapshot(
    *,
    spots: dict[str, float],
    starting_capital: float,
    environment: str = "PAPER",
) -> dict[str, Any]:
    """Paper equity / daily PnL using MES journal rows only."""
    opens = _mes_trades(
        _db.query_trades(status=TradeStatus.OPEN, environment=environment, limit=1000)
    )
    closed = _mes_trades(
        _db.query_trades(status=TradeStatus.CLOSED, environment=environment, limit=100000)
    )
    unreal = 0.0
    for trade in opens:
        spot = spots.get(trade.symbol.upper())
        if spot is None:
            continue
        unreal += mark_open_trade(trade, spot)["unrealized_pnl"]
    lifetime = sum(float(t.pnl or 0.0) for t in closed if t.pnl is not None)
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_realised = 0.0
    for t in closed:
        if t.pnl is None:
            continue
        closed_at = t.closed_at or t.created_at
        if closed_at is None:
            continue
        if as_naive_utc(closed_at) >= start_of_day:
            daily_realised += float(t.pnl)
    unreal = round(unreal, 2)
    lifetime = round(lifetime, 2)
    daily_realised = round(daily_realised, 2)
    return {
        "lifetime_realised_pnl": lifetime,
        "open_unrealized_pnl": unreal,
        "daily_realised_pnl": daily_realised,
        "daily_pnl": round(daily_realised + unreal, 2),
        "paper_equity": round(starting_capital + lifetime + unreal, 2),
    }

# Short-lived cache for IBKR calls so the ~8s UI poll doesn't reconnect every
# time (and, when offline, doesn't retry the socket on every request).
_IBKR_CACHE_TTL = 20.0
_ibkr_cache: dict[str, tuple[float, tuple[bool, Any, str | None]]] = {}


async def _ibkr_fetch(
    key: str, fn: Callable[[IBKRClient], Awaitable[Any]]
) -> tuple[bool, Any, str | None]:
    """Fetch IBKR data with a TTL cache. Returns (connected, value, note)."""
    now = time.monotonic()
    cached = _ibkr_cache.get(key)
    if cached and now - cached[0] < _IBKR_CACHE_TTL:
        return cached[1]

    async def _inner() -> Any:
        async with IBKRClient() as ib:
            return await fn(ib)

    try:
        # Gateway login on a fresh host can stall connectAsync for many clientId
        # retries. Cap so the UI can render the journal instead of hanging.
        value = await asyncio.wait_for(_inner(), timeout=5.0)
        result: tuple[bool, Any, str | None] = (True, value, None)
    except IBKRUnavailable as exc:
        result = (False, None, str(exc))
    except TimeoutError:
        result = (
            False,
            None,
            "IBKR timed out (Gateway still starting, 2FA pending, or busy).",
        )
    _ibkr_cache[key] = (now, result)
    return result


async def index(_request: Request) -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


async def api_health(_request: Request) -> JSONResponse:
    """Tunnel / compose probe that does not touch IBKR."""
    return JSONResponse({"ok": True, "service": "dashboard"})


# --- backtesting / simulation helpers --------------------------------------

# Cache historical bars briefly so running a backtest then an optimisation on
# the same inputs doesn't refetch (and to keep IBKR historical requests low).
_BARS_CACHE_TTL = 60.0
_bars_cache: dict[tuple, tuple[float, tuple[list, str, str | None]]] = {}


def _resolve_window(window: str) -> SessionWindow:
    w = (window or "new_york").lower()
    if w in ("auto", "active", "current", "asia"):
        # Futures 5ORB has London + New York only; Asia maps to NY for UI compat.
        return SessionWindow.NEW_YORK
    if w in ("ny", "newyork", "new_york"):
        return SessionWindow.NEW_YORK
    if w == "london":
        return SessionWindow.LONDON
    return SessionWindow(w)


def _mes_params_payload(symbol: str, db: Database | None = None) -> dict[str, Any]:
    """JSON description of resolved 5ORB exit/retest knobs for the UI."""
    clear_mes_5orb_config_cache()
    base = load_mes_5orb_config(symbol)
    cfg, found = resolve_mes_5orb_config(symbol, db or _db)
    retest = cfg.sessions[0].retest if cfg.sessions else None
    base_rt = base.sessions[0].retest if base.sessions else None
    return {
        "symbol": cfg.symbol,
        "source": "selected" if found else "defaults",
        "label": (found or {}).get("label"),
        "params": {
            "entry_model": "mes_5orb",
            "symbol": cfg.symbol,
            "target_r": cfg.exits.target_r,
            "scale_fraction": cfg.exits.scale_fraction,
            "stop_buffer_ticks": cfg.exits.stop_buffer_ticks,
            "tolerance_ticks": retest.tolerance_ticks if retest else 3,
            "require_rejection_candle": (
                retest.require_rejection_candle if retest else False
            ),
            "use_hod_lod_target": cfg.exits.use_hod_lod_target,
            "move_stop_to_be": cfg.exits.move_stop_to_be,
            "runner_trail": cfg.exits.runner_trail,
        },
        "defaults": {
            "target_r": base.exits.target_r,
            "scale_fraction": base.exits.scale_fraction,
            "stop_buffer_ticks": base.exits.stop_buffer_ticks,
            "tolerance_ticks": base_rt.tolerance_ticks if base_rt else 3,
            "require_rejection_candle": (
                base_rt.require_rejection_candle if base_rt else False
            ),
        },
    }


def _parse_mes_custom_params(
    q, *, symbol: str
) -> dict[str, Any] | None:
    """Build tradable mes_5orb params from query knobs. None if no knobs present."""
    keys = (
        "target_r",
        "scale_fraction",
        "stop_buffer_ticks",
        "tolerance_ticks",
        "require_rejection_candle",
        "use_hod_lod_target",
        "move_stop_to_be",
        "runner_trail",
    )
    if not any(q.get(k) not in (None, "") for k in keys):
        return None
    params: dict[str, Any] = {
        "entry_model": "mes_5orb",
        "symbol": symbol,
        "source": "custom",
    }
    if q.get("target_r") not in (None, ""):
        params["target_r"] = float(q.get("target_r"))
    if q.get("scale_fraction") not in (None, ""):
        params["scale_fraction"] = min(max(float(q.get("scale_fraction")), 0.0), 1.0)
    if q.get("stop_buffer_ticks") not in (None, ""):
        params["stop_buffer_ticks"] = int(q.get("stop_buffer_ticks"))
    if q.get("tolerance_ticks") not in (None, ""):
        params["tolerance_ticks"] = int(q.get("tolerance_ticks"))
    for flag in ("require_rejection_candle", "use_hod_lod_target", "move_stop_to_be", "runner_trail"):
        raw = q.get(flag)
        if raw not in (None, ""):
            params[flag] = str(raw).lower() in ("1", "true", "yes", "on")
    return params


def _mes_score(metrics: dict[str, Any]) -> float:
    """Simple rank score for futures runs (expectancy + win rate − drawdown)."""
    if not metrics or int(metrics.get("trades") or 0) == 0:
        return -1e9
    exp = float(metrics.get("expectancy") or 0)
    wr = float(metrics.get("win_rate") or 0)
    dd = float(metrics.get("max_drawdown_pct") or 0)
    return exp * (0.5 + wr) - abs(dd) * 100


def _mes_trades_for_ui(trades: list) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in trades:
        d = t.as_dict() if hasattr(t, "as_dict") else dict(t)
        rows.append(
            {
                "session": d.get("session_name") or d.get("session"),
                "spread_type": "futures_5orb",
                "direction": d.get("direction"),
                "regime": "trend",
                "entry_value": d.get("entry_price"),
                "exit_value": d.get("exit_price"),
                "tp": None,
                "sl": d.get("stop_final") or d.get("stop_initial"),
                "exit_reason": d.get("exit_reason"),
                "pnl": d.get("pnl_usd"),
                "day": d.get("day"),
                "r_multiple": d.get("r_multiple"),
                "contracts": d.get("contracts"),
                "points": d.get("points"),
            }
        )
    return rows


def _filter_bars_for_window(bars: list, window: SessionWindow) -> list:
    """Optional session filter: keep full series (strategy is session-aware)."""
    _ = window
    return bars


async def _load_history(
    symbol: str, *, demo: bool, lookback_days: int, seed: int
) -> tuple[list, str, str | None]:
    """Fetch historical bars (IBKR live history, or synthetic in demo mode).

    For ~1 calendar month lookbacks, prefer IBKR ``1 M`` duration (same as the
    live month PnL reports) before falling back to ``N D``.
    """
    symbol = coerce_futures_symbol(symbol) if is_supported_futures(symbol) else symbol.upper()
    key = (symbol.upper(), demo, lookback_days, seed)
    now = time.monotonic()
    cached = _bars_cache.get(key)
    if cached and now - cached[0] < _BARS_CACHE_TTL:
        return cached[1]

    durations: list[str] = []
    if 28 <= lookback_days <= 35 and not demo:
        durations.append("1 M")
    durations.append(f"{lookback_days} D")

    bars: list = []
    source = "none"
    warning: str | None = None
    last_exc: Exception | None = None
    for dur in durations:
        try:
            bars, source, warning = await fetch_bars_with_fallback(
                symbol,
                duration=dur,
                bar_size="5 mins",
                use_synthetic=demo,
                allow_synthetic_fallback=demo,
                synthetic_days=lookback_days,
                synthetic_seed=seed,
            )
            if bars:
                break
        except IBKRUnavailable as exc:
            last_exc = exc
            if demo:
                raise
            continue
    if not bars and last_exc is not None and not demo:
        raise last_exc
    result = (bars, source, warning)
    _bars_cache[key] = (now, result)
    return result


async def _spot_by_symbol(symbols: set[str]) -> dict[str, float]:
    """Latest 5-min close per underlying, using the history cache."""
    spots: dict[str, float] = {}
    for symbol in symbols:
        try:
            bars, _, _ = await _load_history(
                symbol, demo=False, lookback_days=2, seed=1
            )
        except IBKRUnavailable:
            continue
        if bars:
            spots[symbol.upper()] = bars[-1].close
    return spots


def _with_mark(trade, spots: dict[str, float]) -> dict[str, Any]:
    payload = trade.model_dump(mode="json")
    spot = spots.get(trade.symbol.upper())
    if spot is None:
        return payload
    payload.update(mark_open_trade(trade, spot))
    return payload


def _parse_plan(plan_json: str | None) -> dict[str, Any]:
    try:
        raw = json.loads(plan_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _open_trade_levels(symbol: str) -> list[dict[str, Any]]:
    """Entry / stop / target for open journal trades on ``symbol`` (chart overlay)."""
    want = coerce_futures_symbol(symbol).upper()
    levels: list[dict[str, Any]] = []
    for trade in _db.query_trades(status=TradeStatus.OPEN, limit=200):
        if coerce_futures_symbol(trade.symbol).upper() != want:
            continue
        plan = _parse_plan(trade.plan_json)
        stop = plan.get("stop_loss_price")
        if stop is None:
            stop = plan.get("stop_price")
        target = plan.get("target_price")
        try:
            stop_f = float(stop) if stop is not None else None
        except (TypeError, ValueError):
            stop_f = None
        try:
            target_f = float(target) if target is not None else None
        except (TypeError, ValueError):
            target_f = None
        levels.append(
            {
                "trade_id": trade.id,
                "direction": trade.direction.value,
                "window": trade.window.value,
                "entry": float(trade.entry_price),
                "stop": stop_f,
                "target": target_f,
                "target_label": str(plan.get("target_label") or "TP"),
                "contracts": trade.contracts,
                "created_at": trade.created_at.isoformat() if trade.created_at else None,
            }
        )
    # Newest first so the chart can emphasise the latest fill.
    levels.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return levels


def _et_hhmm_gmt(t: dt_time, now: datetime | None = None) -> str:
    """Convert an America/New_York wall-clock time to GMT HH:MM (DST-aware)."""
    now = now or market_now()
    et_date = to_et(now).date()
    dt = _EASTERN.localize(datetime.combine(et_date, t))
    return dt.astimezone(pytz.UTC).strftime("%H:%M")


def _level(
    *,
    id: str,
    label: str,
    price: float | None,
    strategy: str,
    kind: str,
    color: str,
    desc: str,
    dash: list[int] | None = None,
    width: int = 1,
) -> dict[str, Any] | None:
    if price is None:
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    return {
        "id": id,
        "label": label,
        "price": round(px, 4),
        "strategy": strategy,
        "kind": kind,
        "color": color,
        "dash": dash or [5, 4],
        "width": width,
        "desc": desc,
    }


def _live_stack_overlay(symbol: str, bars: list) -> dict[str, Any]:
    """Active MES strategies + chart levels (Asia Judas / London / NY ORB)."""
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config(symbol)
    now = market_now()
    et = to_et(now)
    day = et.date()
    t = et.time()
    levels: list[dict[str, Any]] = []
    strategies: list[dict[str, Any]] = []

    # --- Asia Judas ---
    asia = cfg.asia_range
    asia_in_search = bool(
        asia.enabled and asia.allows_day(day) and asia.search_start <= t < asia.search_end
    )
    asia_in_range_build = False
    if asia.enabled:
        # Range build wraps midnight: 20:00 → 00:00 ET.
        if asia.range_start > asia.range_end:
            asia_in_range_build = t >= asia.range_start or t < asia.range_end
        else:
            asia_in_range_build = asia.range_start <= t < asia.range_end
    asia_status = "disabled"
    asia_reason = ""
    if asia.enabled:
        if not asia.allows_day(day):
            asia_status = "skipped_weekday"
            asia_reason = f"weekday {day.weekday()} not allowed"
        elif asia_in_range_build:
            asia_status = "building_range"
            asia_reason = "Asia range forming (20:00–00:00 ET)"
        elif asia_in_search:
            asia_status = "searching"
            asia_reason = "Judas search — waiting for sweep + reclaim"
        elif t < asia.search_start:
            asia_status = "waiting_search"
            asia_reason = f"search opens {asia.search_start.strftime('%H:%M')} ET"
        else:
            asia_status = "flat"
            asia_reason = "past Asia force flat / search end"

    ar = compute_asia_range(bars, day, asia) if asia.enabled and bars else None
    setup = None
    if ar is not None and not ar.skipped:
        for spec in (
            _level(
                id="asia_high",
                label="Asia high",
                price=ar.high,
                strategy="asia",
                kind="range",
                color="#a371f7",
                desc="Asia range high (20:00–00:00 ET) — BSL pool",
                width=2,
            ),
            _level(
                id="asia_low",
                label="Asia low",
                price=ar.low,
                strategy="asia",
                kind="range",
                color="#a371f7",
                desc="Asia range low (20:00–00:00 ET) — SSL pool",
                width=2,
            ),
            _level(
                id="asia_eq",
                label="Asia EQ",
                price=ar.eq,
                strategy="asia",
                kind="eq",
                color="#8b98a9",
                desc="Asia equilibrium (mid) — bias proxy",
                dash=[2, 4],
            ),
        ):
            if spec:
                levels.append(spec)
        if asia.use_ny_liquidity:
            for spec in (
                _level(
                    id="ny_high",
                    label="Prior NY high",
                    price=ar.ny_high,
                    strategy="asia",
                    kind="liquidity",
                    color="#39c5cf",
                    desc="Prior NY RTH high — extra BSL",
                    dash=[8, 4],
                ),
                _level(
                    id="ny_low",
                    label="Prior NY low",
                    price=ar.ny_low,
                    strategy="asia",
                    kind="liquidity",
                    color="#39c5cf",
                    desc="Prior NY RTH low — extra SSL",
                    dash=[8, 4],
                ),
            ):
                if spec:
                    levels.append(spec)
        if asia.use_pd_liquidity:
            for spec in (
                _level(
                    id="pd_high",
                    label="PD high",
                    price=ar.pd_high,
                    strategy="asia",
                    kind="liquidity",
                    color="#79c0ff",
                    desc="Previous day high — liquidity / target",
                    dash=[8, 4],
                ),
                _level(
                    id="pd_low",
                    label="PD low",
                    price=ar.pd_low,
                    strategy="asia",
                    kind="liquidity",
                    color="#79c0ff",
                    desc="Previous day low — liquidity / target",
                    dash=[8, 4],
                ),
            ):
                if spec:
                    levels.append(spec)
        setup = detect_asia_judas(bars, ar, asia, tick_size=cfg.tick_size)
        if setup is not None:
            asia_status = "setup"
            asia_reason = setup.notes or f"{setup.direction.value} Judas ready"
            for spec in (
                _level(
                    id="asia_entry",
                    label=f"Asia entry ({setup.direction.value})",
                    price=setup.entry_price,
                    strategy="asia",
                    kind="entry",
                    color="#4f8cff",
                    desc="Judas reclaim entry",
                    dash=[2, 3],
                    width=2,
                ),
                _level(
                    id="asia_stop",
                    label="Asia stop",
                    price=setup.initial_stop,
                    strategy="asia",
                    kind="stop",
                    color="#f85149",
                    desc="Beyond sweep extreme + buffer",
                    dash=[6, 4],
                    width=2,
                ),
                _level(
                    id="asia_target",
                    label=f"Asia TP ({setup.target_label})",
                    price=setup.target_price,
                    strategy="asia",
                    kind="target",
                    color="#3fb950",
                    desc=f"Target {setup.target_label}",
                    dash=[6, 4],
                    width=2,
                ),
                _level(
                    id="asia_sweep",
                    label="Sweep extreme",
                    price=setup.sweep_extreme,
                    strategy="asia",
                    kind="sweep",
                    color="#f85149",
                    desc="Judas sweep extreme",
                    dash=[1, 3],
                ),
            ):
                if spec:
                    levels.append(spec)
        elif ar.skipped:
            asia_reason = ar.skip_reason or "Asia range skipped"
            asia_status = "skipped_range"
    elif asia.enabled and ar is not None and ar.skipped:
        asia_status = "skipped_range"
        asia_reason = ar.skip_reason or "Asia range skipped"

    strategies.append(
        {
            "id": "asia",
            "name": "Asia Judas",
            "armed": bool(asia.enabled),
            "active": asia_in_search or asia_status == "setup",
            "status": asia_status,
            "reason": asia_reason,
            "gmt": (
                f"range {_et_hhmm_gmt(asia.range_start)}–{_et_hhmm_gmt(asia.range_end)} · "
                f"search {_et_hhmm_gmt(asia.search_start)}–{_et_hhmm_gmt(asia.search_end)} · "
                f"flat {_et_hhmm_gmt(asia.force_flat)} GMT"
            ),
            "desc": (
                "Not naked ORB: sweep SSL/BSL (Asia/NY/PD) then reclaim. "
                f"Target {asia.target_mode}, scale {int(asia.scale_fraction * 100)}%, "
                f"EQ bias {'on' if asia.require_eq_bias else 'off'}."
            ),
        }
    )

    # --- London / NY 5ORB ---
    for sess in cfg.sessions:
        if not sess.enabled:
            continue
        exits = resolve_session_exits(cfg, sess)
        max_e = resolve_session_max_entries(cfg, sess)
        in_window = sess.or_start <= t < sess.force_flat
        dirs = sess.entry.allowed_directions
        dir_txt = str(dirs or "both")
        status = "idle"
        reason = f"OR {sess.or_start.strftime('%H:%M')}–{sess.or_end.strftime('%H:%M')} ET"
        orb = compute_opening_range(bars, sess, day) if bars else None
        if in_window and orb is not None and not orb.skipped:
            status = "or_ready"
            reason = f"OR {orb.low:.2f}–{orb.high:.2f}"
            color = "#d29922" if sess.name == "london" else "#e3b341"
            for spec in (
                _level(
                    id=f"{sess.name}_or_high",
                    label=f"{sess.name.replace('_', ' ').title()} OR high",
                    price=orb.high,
                    strategy=sess.name,
                    kind="or",
                    color=color,
                    desc=f"{sess.name} 5m opening range high",
                    width=2,
                ),
                _level(
                    id=f"{sess.name}_or_low",
                    label=f"{sess.name.replace('_', ' ').title()} OR low",
                    price=orb.low,
                    strategy=sess.name,
                    kind="or",
                    color=color,
                    desc=f"{sess.name} 5m opening range low",
                    width=2,
                ),
            ):
                if spec:
                    levels.append(spec)
            live = evaluate_mes_signal_live(bars, cfg=cfg, session_name=sess.name, as_of=now)
            if live.get("ok"):
                status = "setup"
                plan = live.get("plan") or {}
                reason = str(plan.get("notes") or live.get("reason") or "break/retest ready")
                for spec in (
                    _level(
                        id=f"{sess.name}_entry",
                        label=f"{sess.name} entry",
                        price=plan.get("entry_price"),
                        strategy=sess.name,
                        kind="entry",
                        color="#4f8cff",
                        desc="5ORB retest entry",
                        dash=[2, 3],
                        width=2,
                    ),
                    _level(
                        id=f"{sess.name}_stop",
                        label=f"{sess.name} stop",
                        price=plan.get("stop_price"),
                        strategy=sess.name,
                        kind="stop",
                        color="#f85149",
                        desc="OR extreme stop",
                        dash=[6, 4],
                        width=2,
                    ),
                    _level(
                        id=f"{sess.name}_target",
                        label=f"{sess.name} TP",
                        price=plan.get("target_price"),
                        strategy=sess.name,
                        kind="target",
                        color="#3fb950",
                        desc=str(plan.get("target_label") or "target"),
                        dash=[6, 4],
                        width=2,
                    ),
                ):
                    if spec:
                        levels.append(spec)
            else:
                reason = str(live.get("reason") or reason)
                if "waiting" in reason.lower() or "no break" in reason.lower():
                    status = "searching"
        elif in_window and orb is not None and orb.skipped:
            status = "or_skipped"
            reason = orb.skip_reason or "OR skipped"
        elif in_window:
            status = "waiting_or"
            reason = "waiting for OR bars"
        elif t < sess.or_start:
            status = "waiting"
            reason = f"opens {sess.or_start.strftime('%H:%M')} ET"
        else:
            status = "flat"
            reason = "past force flat"

        strategies.append(
            {
                "id": sess.name,
                "name": f"{sess.name.replace('_', ' ').title()} 5ORB",
                "armed": True,
                "active": in_window,
                "status": status,
                "reason": reason,
                "gmt": (
                    f"OR {_et_hhmm_gmt(sess.or_start)}–{_et_hhmm_gmt(sess.or_end)} · "
                    f"search→{_et_hhmm_gmt(sess.search_end)} · "
                    f"flat {_et_hhmm_gmt(sess.force_flat)} GMT"
                ),
                "desc": (
                    f"Break/retest OR · dirs {dir_txt} · "
                    f"{exits.target_r}R · scale {int(exits.scale_fraction * 100)}% · "
                    f"max entries {max_e}."
                ),
            }
        )

    # Open trade levels (already journaled)
    for i, ot in enumerate(_open_trade_levels(symbol)[:3]):
        tag = f"#{ot['trade_id']}"
        for spec in (
            _level(
                id=f"open_entry_{ot['trade_id']}",
                label=f"Entry {tag}",
                price=ot.get("entry"),
                strategy=str(ot.get("window") or "open"),
                kind="entry",
                color="#4f8cff",
                desc=f"Open {ot.get('direction')} trade {tag}",
                dash=[2, 3],
                width=2 if i == 0 else 1,
            ),
            _level(
                id=f"open_stop_{ot['trade_id']}",
                label=f"SL {tag}",
                price=ot.get("stop"),
                strategy=str(ot.get("window") or "open"),
                kind="stop",
                color="#f85149",
                desc=f"Stop {tag}",
                dash=[6, 4],
                width=2 if i == 0 else 1,
            ),
            _level(
                id=f"open_tp_{ot['trade_id']}",
                label=f"{ot.get('target_label') or 'TP'} {tag}",
                price=ot.get("target"),
                strategy=str(ot.get("window") or "open"),
                kind="target",
                color="#3fb950",
                desc=f"Target {tag}",
                dash=[6, 4],
                width=2 if i == 0 else 1,
            ),
        ):
            if spec:
                levels.append(spec)

    active = [s for s in strategies if s.get("active")]
    schedule_parts = [f"{s['name']}: {s['gmt']}" for s in strategies if s.get("armed")]
    return {
        "now_et": et.strftime("%Y-%m-%d %H:%M %Z"),
        "now_gmt": et.astimezone(pytz.UTC).strftime("%H:%M GMT"),
        "strategies": strategies,
        "active_count": len(active),
        "levels": levels,
        "schedule_gmt": " · ".join(schedule_parts),
        "summary": (
            f"{len(active)} active now · "
            + "; ".join(f"{s['name']} ({s['status']})" for s in active)
            if active
            else "no strategy window open"
        ),
    }


def _equity_curve(pnls: list[float], starting: float = 0.0) -> list[dict]:
    equity = starting
    curve = [{"i": 0, "equity": round(equity, 2)}]
    for i, p in enumerate(pnls, start=1):
        equity += p
        curve.append({"i": i, "equity": round(equity, 2), "pnl": round(p, 2)})
    return curve


# --- auto-trading engine ---------------------------------------------------


class AutoTrader:
    """Background loop that runs MES 5ORB entry logic on an interval.

    Each cycle builds a risk-sized MES futures plan for the active London/NY
    window and, if a break/retest passes risk gates, places it (paper) via
    the shared engine.

    Safety: refuses LIVE autotrade unless LIVE_AUTOTRADE_CONFIRM is set.
    """

    MIN_INTERVAL = 10.0

    def __init__(self, db: Database) -> None:
        self._db = db
        self._task: asyncio.Task | None = None
        self.running = False
        self.symbol = "MES"
        self.window = "auto"
        self.demo = False
        self.interval = 60.0
        self.target_r: float | None = None
        self.risk_pct: float | None = None
        self.per_window_limit = 3
        self.started_at: str | None = None
        self.cycles = 0
        self.trades_placed = 0
        self.last_cycle_at: str | None = None
        self.log: list[dict] = []
        self._placed_counts: dict[tuple[str, str], int] = {}

    def _add_log(self, msg: str, level: str = "info") -> None:
        self.log.append({"t": utcnow().isoformat(), "level": level, "msg": msg})
        self.log = self.log[-120:]

    def status(self) -> dict:
        return {
            "running": self.running,
            "symbol": self.symbol,
            "window": self.window,
            "demo": self.demo,
            "interval": self.interval,
            "risk_pct": self.risk_pct,
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "cycles": self.cycles,
            "trades_placed": self.trades_placed,
            "log": list(reversed(self.log[-40:])),
            "strategy_by_window": strategy_by_window(self.symbol, self._db),
        }

    def set_risk_pct(self, risk_pct: float | None) -> dict:
        """Update risk % (1–5). Works while stopped or running; sizes next entries."""
        from core.risk import _as_risk_fraction

        settings = get_settings()
        frac = _as_risk_fraction(risk_pct)
        self.risk_pct = round(frac * 100) if frac is not None else round(
            settings.max_risk_per_trade * 100
        )
        self._add_log(f"Risk per trade set to {self.risk_pct:.0f}% of equity.", "info")
        try:
            st = load_state()
            st["risk_pct"] = self.risk_pct
            if self.running:
                st["enabled"] = True
                st["symbol"] = self.symbol
                st["window"] = self.window
                st["demo"] = self.demo
                st["interval"] = self.interval
                st["target_r"] = self.target_r
            save_state(st)
        except OSError as exc:
            _log.warning("could not persist risk_pct: %s", exc)
        return {"ok": True, **self.status()}

    def start(self, *, symbol: str, window: str, demo: bool, interval: float,
              target_r: float | None, risk_pct: float | None = None) -> dict:
        if self.running:
            return {"ok": False, "error": "Auto-trading is already running.", **self.status()}
        settings = get_settings()
        if settings.trading_environment() == "LIVE" and not settings.live_autotrade_enabled:
            return {
                "ok": False,
                "error": (
                    "LIVE auto-trading is locked. Set ACCOUNT_MODE=live, "
                    "LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK, and "
                    "LIVE_AUTOTRADE_CONFIRM=I_ENABLE_LIVE_AUTOTRADE — or use paper."
                ),
            }
        self.symbol = (symbol or get_settings().default_symbol or "MES").upper()
        from core.strategy.mes_5orb.markets import coerce_futures_symbol
        from core.risk import _as_risk_fraction

        self.symbol = coerce_futures_symbol(self.symbol)
        self.window = window or "auto"
        self.demo = demo
        self.interval = max(float(interval), self.MIN_INTERVAL)
        self.target_r = target_r
        frac = _as_risk_fraction(risk_pct)
        self.risk_pct = round(frac * 100) if frac is not None else round(
            settings.max_risk_per_trade * 100
        )
        self.running = True
        self.started_at = utcnow().isoformat()
        self.cycles = 0
        self.trades_placed = 0
        # Match live mes_5orb.json max entries per OR window (not the old 3/window SPY cap).
        clear_mes_5orb_config_cache()
        mes_cfg = load_mes_5orb_config(self.symbol)
        self.per_window_limit = max(int(mes_cfg.risk.max_entries_per_session), 1)
        self._placed_counts.clear()
        env_label = settings.trading_environment()
        data_label = (
            "demo data"
            if demo
            else ("live IBKR data" if env_label == "LIVE" else "live paper data")
        )
        self._add_log(
            f"Auto-trading started - {self.symbol} / {self.window}, "
            f"risk {self.risk_pct:.0f}%, {data_label}, every {self.interval:.0f}s "
            f"(up to {self.per_window_limit} per window from mes_5orb.json, "
            f"{settings.max_open_positions}/day"
            + (
                f", LIVE_MAX_CONTRACTS={settings.live_max_contracts}"
                if env_label == "LIVE"
                else ""
            )
            + ").",
            "start",
        )
        for w in SessionWindow:
            cfg, found = resolve_trading_config(self.symbol, w, self._db)
            payload = strategy_payload(cfg, found)
            self._add_log(f"{w.value}: {format_strategy(payload)}", "start")
        try:
            save_state(
                {
                    "enabled": True,
                    "symbol": self.symbol,
                    "window": self.window,
                    "demo": self.demo,
                    "interval": self.interval,
                    "target_r": self.target_r,
                    "risk_pct": self.risk_pct,
                }
            )
        except OSError as exc:
            _log.warning("could not persist autotrade state: %s", exc)
        self._task = asyncio.create_task(self._loop())
        return {"ok": True, **self.status()}

    async def stop(self) -> dict:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._add_log("Auto-trading stopped.", "stop")
        try:
            save_state(
                {
                    "enabled": False,
                    "symbol": self.symbol,
                    "window": self.window,
                    "demo": self.demo,
                    "interval": self.interval,
                    "target_r": self.target_r,
                }
            )
        except OSError as exc:
            _log.warning("could not persist autotrade stop: %s", exc)
        return {"ok": True, **self.status()}

    async def _loop(self) -> None:
        try:
            while self.running:
                await self._cycle()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # keep the server alive on unexpected errors
            self._add_log(f"Auto-trader loop error: {exc}", "error")
            self.running = False

    async def _cycle(self) -> None:
        self.cycles += 1
        self.last_cycle_at = utcnow().isoformat()
        seed = 100 + self.cycles if self.demo else 42

        try:
            bars, _, _ = await fetch_bars_with_fallback(
                self.symbol,
                duration="2 D",
                bar_size="5 mins",
                use_synthetic=self.demo,
                synthetic_seed=seed,
            )
            for closed in await settle_session_exits(self._db, self.symbol, bars):
                pnl = closed["pnl"]
                self._add_log(
                    f"CLOSED journal #{closed['trade_id']} {closed['reason']} "
                    f"pnl {pnl:+.2f} ({closed['window']})"
                    f"{' IBKR flattened' if closed.get('ibkr') else ''}.",
                    "trade" if pnl >= 0 else "warn",
                )
        except Exception as exc:
            self._add_log(f"Journal refresh error: {exc}", "error")

        # Live "auto": evaluate every MES session that is live right now.
        # Asia Judas and London overlap on purpose — both are checked each cycle.
        if not self.demo and self.window.lower() in ("auto", "active", "current"):
            candidates = candidate_mes_sessions_now()
            if not candidates:
                self._add_log(
                    f"No MES session is open ({describe_windows_gmt(market_now())}). Waiting.",
                    "muted",
                )
                return
        else:
            candidates = [self.window]

        for cycle_window in candidates:
            await self._try_session(cycle_window, seed=seed)

    async def _try_session(self, cycle_window: str, *, seed: int) -> None:
        try:
            preview = await build_trade_plan(
                self.symbol, cycle_window, use_synthetic=self.demo,
                target_r=self.target_r, synthetic_seed=seed, db=self._db,
                risk_pct=self.risk_pct,
            )
        except Exception as exc:
            self._add_log(f"Signal error ({cycle_window}): {exc}", "error")
            return

        if not preview.get("ok"):
            reason = preview.get("reason") or preview.get("error") or "no trade"
            self._add_log(f"No entry ({cycle_window}): {reason}", "muted")
            return

        plan_sess = str(
            (preview.get("plan") or {}).get("session_name") or cycle_window
        ).lower()
        try:
            win = resolve_window(plan_sess).value
        except ValueError:
            win = resolve_window(cycle_window).value
        # Count entries per window per real day (live) or per synthetic session
        # (demo, so each cycle can act on its own simulated day).
        bucket = f"seed{seed}" if self.demo else (self.last_cycle_at or "")[:10]
        key = (bucket, win)
        placed_here = self._placed_counts.get(key, 0)
        if placed_here >= self.per_window_limit:
            self._add_log(
                f"{win} already has {placed_here}/{self.per_window_limit} trades this session - holding.",
                "muted",
            )
            return

        if not preview.get("tradeable"):
            reasons = "; ".join(preview.get("risk", {}).get("reasons", [])) or "risk checks failed"
            self._add_log(f"Signal found ({win}) but blocked: {reasons}", "warn")
            return

        result = await place_trade_plan(
            preview, symbol=self.symbol, window=cycle_window,
            use_synthetic=self.demo, db=self._db,
        )
        if result.get("ok"):
            self._placed_counts[key] = placed_here + 1
            self.trades_placed += 1
            plan = result.get("plan", {})
            entry = plan.get("entry_price")
            stop = plan.get("stop_loss_price") or plan.get("stop_price")
            target = plan.get("target_price")
            tlabel = plan.get("target_label") or "TP"
            session = plan.get("session_name") or win
            self._add_log(
                f"PLACED trade #{result['trade_id']} {plan.get('symbol') or self.symbol} "
                f"{plan.get('direction','').upper()} x{plan.get('contracts','')} "
                f"{session} entry {entry} stop {stop} {tlabel} {target} "
                f"({result.get('environment','')}).",
                "trade",
            )
        else:
            self._add_log(f"Placement failed ({win}): {result.get('error','unknown')}", "error")


_autotrader = AutoTrader(_db)
try:
    _seed_rp = load_state().get("risk_pct")
    if _seed_rp is not None:
        _autotrader.risk_pct = float(_seed_rp)
except Exception:
    pass


async def _run_session_exits() -> None:
    """Flatten IBKR combos (and journal SIM rows) whose ORB window has ended.

    Runs even when auto-trade is off, so live accounts still close at each
    Asia / London / New York window_close while the dashboard is up.
    """
    opens = _db.query_trades(status=TradeStatus.OPEN, limit=1000)
    symbols = sorted({t.symbol.upper() for t in opens})
    for symbol in symbols:
        try:
            bars, _, _ = await fetch_bars_with_fallback(
                symbol, duration="2 D", bar_size="5 mins"
            )
        except IBKRUnavailable:
            bars = []
        except Exception as exc:
            _log.warning("session-exit bars %s: %s", symbol, exc)
            continue
        try:
            closed = await settle_session_exits(_db, symbol, bars)
        except Exception as exc:
            _log.warning("session-exit settle %s: %s", symbol, exc)
            continue
        for row in closed:
            pnl = row.get("pnl") or 0.0
            _autotrader._add_log(
                f"CLOSED journal #{row.get('trade_id')} {row.get('symbol') or ''} "
                f"{row.get('window')} {row.get('reason')} "
                f"exit {row.get('exit_price')} pnl {pnl:+.2f}"
                f"{' IBKR flattened' if row.get('ibkr') else ''}.",
                "trade" if pnl >= 0 else "warn",
            )

    # Live: flag journal vs broker qty mismatches (no auto-flatten).
    settings = get_settings()
    if settings.is_live and opens:
        try:
            report = await reconcile_open_futures_vs_ibkr(_db)
        except Exception as exc:  # noqa: BLE001
            _log.warning("futures reconcile: %s", exc)
            return
        for mismatch in report.get("mismatches") or []:
            if mismatch.get("error"):
                _autotrader._add_log(
                    f"RECONCILE {mismatch.get('symbol')}: {mismatch['error']}",
                    "warn",
                )
                continue
            _autotrader._add_log(
                f"RECONCILE MISMATCH {mismatch.get('symbol')}: "
                f"journal_net={mismatch.get('journal_net')} "
                f"broker={mismatch.get('broker_qty')} "
                f"delta={mismatch.get('delta')} "
                f"ids={mismatch.get('open_trade_ids')}",
                "error",
            )


async def _session_exit_loop() -> None:
    await asyncio.sleep(8)
    while True:
        try:
            await _run_session_exits()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("session-exit loop")
        await asyncio.sleep(_SESSION_EXIT_INTERVAL)


async def api_summary(_request: Request) -> JSONResponse:
    settings = get_settings()
    rm = RiskManager(db=_db)
    tripped, realised = rm.daily_loss_tripped()
    # Preferred risk%: running autotrader → persisted state → env default.
    preferred = _autotrader.risk_pct
    if preferred is None:
        preferred = load_state().get("risk_pct")
    if preferred is None:
        preferred = round(settings.max_risk_per_trade * 100)
    payload = {
        "environment": rm.environment(),
        "account_currency": settings.account_currency,
        "starting_capital": settings.starting_capital,
        "risk_pct_choices": [1, 2, 3, 4, 5],
        "active_risk_pct": preferred,
        "max_risk_per_trade_pct": float(preferred),
        "daily_loss_limit": round(rm.daily_loss_limit(), 2),
        "daily_realised_pnl": round(realised, 2),
        "daily_kill_switch_tripped": tripped,
        "open_positions": _db.open_position_count(),
        "max_open_positions": settings.max_open_positions,
        "max_open_positions_per_window": settings.max_open_positions_per_window,
        "open_by_window": {
            w.value: _db.open_position_count(window=w) for w in SessionWindow
        },
        "live_gate_ok": settings.is_live,
        "live_requested_but_unconfirmed": settings.live_requested_but_unconfirmed,
        "default_symbol": settings.default_symbol,
        "server_time": utcnow().isoformat() + "Z",
        "session_hours_gmt": describe_windows_gmt(),
        "strategy_by_window": strategy_by_window(settings.default_symbol, _db),
        "mes_params": _mes_params_payload(settings.default_symbol, _db),
        "live_stack": None,
    }
    # Lightweight live-stack schedule (no bars) for autotrade caption.
    try:
        clear_mes_5orb_config_cache()
        mes_cfg = load_mes_5orb_config(settings.default_symbol)
        asia = mes_cfg.asia_range
        parts: list[str] = []
        if asia.enabled:
            parts.append(
                f"Asia Judas range {_et_hhmm_gmt(asia.range_start)}–{_et_hhmm_gmt(asia.range_end)} "
                f"search {_et_hhmm_gmt(asia.search_start)}–{_et_hhmm_gmt(asia.search_end)} "
                f"flat {_et_hhmm_gmt(asia.force_flat)} GMT "
                f"(sweep+reclaim, not naked ORB; scale {int(asia.scale_fraction * 100)}%)"
            )
        for sess in mes_cfg.sessions:
            if not sess.enabled:
                continue
            exits = resolve_session_exits(mes_cfg, sess)
            dir_txt = str(sess.entry.allowed_directions or "both")
            parts.append(
                f"{sess.name.replace('_', ' ').title()} OR "
                f"{_et_hhmm_gmt(sess.or_start)}–{_et_hhmm_gmt(sess.or_end)} "
                f"flat {_et_hhmm_gmt(sess.force_flat)} GMT · {dir_txt} · "
                f"{exits.target_r}R/{int(exits.scale_fraction * 100)}%"
            )
        payload["live_stack"] = {
            "schedule_gmt": " · ".join(parts),
            "enabled_sessions": [s.name for s in mes_cfg.sessions if s.enabled],
            "asia_enabled": bool(asia.enabled),
        }
        if parts:
            payload["session_hours_gmt"] = " · ".join(parts)
    except Exception as exc:  # noqa: BLE001 — UI caption only
        _log.debug("live_stack caption failed: %s", exc)
    connected, acct, note = await _ibkr_fetch("account", lambda ib: ib.account_summary())
    payload["ibkr_connected"] = connected
    if connected:
        payload["ibkr"] = acct
    else:
        payload["ibkr_note"] = note
    opens = _mes_trades(_db.query_trades(status=TradeStatus.OPEN, limit=1000))
    try:
        spots = await asyncio.wait_for(
            _spot_by_symbol({t.symbol for t in opens}), timeout=4.0
        )
    except TimeoutError:
        spots = {}
    paper = _mes_paper_snapshot(
        spots=spots,
        starting_capital=settings.starting_capital,
        environment=rm.environment(),
    )
    payload.update(paper)
    equity = float(paper.get("paper_equity") or settings.starting_capital)
    payload["risk_budget_per_trade"] = round(
        rm.risk_budget_per_trade(preferred, equity=equity), 2
    )
    payload["equity_for_sizing"] = round(equity, 2)
    payload["dashboard_symbol"] = _DASHBOARD_SYMBOL
    payload["open_positions"] = len(opens)
    return JSONResponse(payload)


async def api_trades(request: Request) -> JSONResponse:
    limit = int(request.query_params.get("limit", "500"))
    status = request.query_params.get("status")
    open_paper = _mes_trades(
        _db.query_trades(status=TradeStatus.OPEN, environment="PAPER", limit=100)
    )
    seen: set[str] = set()
    for t in open_paper:
        if t.symbol in seen:
            continue
        seen.add(t.symbol)
        try:
            bars, _, _ = await _load_history(t.symbol, demo=False, lookback_days=2, seed=1)
            close_open_paper_trades(_db, t.symbol, bars)
        except IBKRUnavailable:
            continue
    trades = _mes_trades(
        _db.query_trades(
            status=TradeStatus(status.lower()) if status else None,
            limit=max(limit * 5, 500),
        )
    )[:limit]
    spots = await _spot_by_symbol(
        {t.symbol for t in trades if t.status is TradeStatus.OPEN}
    )
    return JSONResponse(
        {
            "count": len(trades),
            "symbol": _DASHBOARD_SYMBOL,
            "trades": [
                _with_mark(t, spots) if t.status is TradeStatus.OPEN else t.model_dump(mode="json")
                for t in trades
            ],
        }
    )


async def api_performance(_request: Request) -> JSONResponse:
    settings = get_settings()
    closed = _mes_trades(_db.query_trades(status=TradeStatus.CLOSED, limit=100000))
    closed_sorted = sorted(
        [t for t in closed if t.pnl is not None],
        key=lambda t: (t.closed_at or t.created_at),
    )
    pnls = [t.pnl for t in closed_sorted]

    # Equity curve starting from capital.
    equity = settings.starting_capital
    curve = [{"t": None, "equity": round(equity, 2)}]
    for t in closed_sorted:
        equity += t.pnl or 0.0
        curve.append(
            {
                "t": (t.closed_at or t.created_at).isoformat(),
                "equity": round(equity, 2),
                "pnl": round(t.pnl or 0.0, 2),
                "symbol": t.symbol,
                "spread_type": t.spread_type.value,
            }
        )

    opens = _mes_trades(
        _db.query_trades(status=TradeStatus.OPEN, environment="PAPER", limit=1000)
    )
    spots = await _spot_by_symbol({t.symbol for t in opens})
    paper = _mes_paper_snapshot(
        spots=spots,
        starting_capital=settings.starting_capital,
        environment=settings.trading_environment(),
    )
    if opens or paper["open_unrealized_pnl"]:
        curve.append(
            {
                "t": utcnow().isoformat(),
                "equity": paper["paper_equity"],
                "pnl": paper["open_unrealized_pnl"],
                "symbol": "OPEN",
                "spread_type": "mark",
                "live": True,
            }
        )

    by_window = {
        w.value: performance_metrics(
            [t.pnl for t in closed_sorted if t.window is w]
        ).as_dict()
        for w in SessionWindow
        if any(t.window is w for t in closed_sorted)
    }
    by_regime = {
        r.value: performance_metrics(
            [t.pnl for t in closed_sorted if t.regime is r]
        ).as_dict()
        for r in Regime
        if any(t.regime is r for t in closed_sorted)
    }
    by_strategy: dict[str, dict] = {}
    for st in {t.spread_type for t in closed_sorted}:
        by_strategy[st.value] = performance_metrics(
            [t.pnl for t in closed_sorted if t.spread_type is st]
        ).as_dict()

    return JSONResponse(
        {
            "closed_trades": len(pnls),
            "overall": summarize(pnls),
            "equity_curve": curve,
            "by_window": by_window,
            "by_regime": by_regime,
            "by_strategy": by_strategy,
        }
    )


async def api_revenue_daily(_request: Request) -> JSONResponse:
    """Realised P&L grouped by UTC calendar day (revenue tracker)."""
    settings = get_settings()
    closed = _mes_trades(_db.query_trades(status=TradeStatus.CLOSED, limit=100000))
    closed_sorted = sorted(
        [t for t in closed if t.pnl is not None],
        key=lambda t: (t.closed_at or t.created_at),
    )
    opens = _mes_trades(
        _db.query_trades(status=TradeStatus.OPEN, environment="PAPER", limit=1000)
    )
    spots = await _spot_by_symbol({t.symbol for t in opens})
    paper = _mes_paper_snapshot(
        spots=spots,
        starting_capital=settings.starting_capital,
        environment=settings.trading_environment(),
    )
    payload = daily_revenue(
        closed_sorted,
        today_unrealized=float(paper.get("open_unrealized_pnl") or 0.0),
    )
    payload["currency"] = settings.account_currency
    payload["starting_capital"] = settings.starting_capital
    payload["symbol"] = _DASHBOARD_SYMBOL
    return JSONResponse(payload)


async def api_positions(_request: Request) -> JSONResponse:
    open_trades = _mes_trades(_db.query_trades(status=TradeStatus.OPEN, limit=1000))
    spots = await _spot_by_symbol({t.symbol for t in open_trades})
    marked = [_with_mark(t, spots) for t in open_trades]
    paper = _mes_paper_snapshot(
        spots=spots,
        starting_capital=get_settings().starting_capital,
        environment=get_settings().trading_environment(),
    )
    payload: dict = {
        "open_trades": marked,
        "count": len(marked),
        "symbol": _DASHBOARD_SYMBOL,
        "open_unrealized_pnl": round(
            sum(t.get("unrealized_pnl") or 0.0 for t in marked), 2
        ),
        "paper_equity": paper["paper_equity"],
        "spots": spots,
    }
    connected, positions, note = await _ibkr_fetch("positions", lambda ib: ib.positions())
    payload["ibkr_connected"] = connected
    # Only show MES futures positions from IBKR when connected.
    if connected and isinstance(positions, list):
        payload["ibkr_positions"] = [
            p
            for p in positions
            if _is_dashboard_symbol(str((p or {}).get("symbol") or ""))
            or str((p or {}).get("symbol") or "").upper().startswith("MES")
        ]
    else:
        payload["ibkr_positions"] = []
    if not connected:
        payload["ibkr_note"] = note
    return JSONResponse(payload)


async def api_signals(request: Request) -> JSONResponse:
    """Live MES stack signals: Asia Judas + enabled London/NY 5ORB sessions."""
    settings = get_settings()
    symbol = coerce_futures_symbol(
        request.query_params.get("symbol", settings.default_symbol)
    )
    use_synthetic = request.query_params.get("use_synthetic", "false").lower() == "true"
    seed = int(request.query_params.get("seed", "1"))

    try:
        bars, source, warning = await fetch_bars_with_fallback(
            symbol,
            duration="3 D",
            bar_size="5 mins",
            use_synthetic=use_synthetic,
            allow_synthetic_fallback=use_synthetic,
            synthetic_seed=seed,
        )
    except IBKRUnavailable as exc:
        return JSONResponse(
            {"error": str(exc), "hint": "Toggle 'Demo data' on, or start IB Gateway."}
        )

    overlay = _live_stack_overlay(symbol, bars)
    signals: list[dict[str, Any]] = []
    for strat in overlay.get("strategies") or []:
        if not strat.get("armed"):
            continue
        sid = str(strat["id"])
        live = evaluate_mes_signal_live(bars, cfg=load_mes_5orb_config(symbol), session_name=sid)
        plan = live.get("plan") or {}
        signals.append(
            {
                "window": sid,
                "name": strat.get("name"),
                "direction": plan.get("direction") or live.get("direction") or "neutral",
                "breakout": bool(live.get("ok")),
                "strength": 1.0 if live.get("ok") else 0.0,
                "regime": "trend",
                "range_low": plan.get("or_low") or plan.get("asia_low"),
                "range_high": plan.get("or_high") or plan.get("asia_high"),
                "last_price": plan.get("entry_price"),
                "as_of": plan.get("as_of") or overlay.get("now_et"),
                "status": strat.get("status"),
                "reason": live.get("reason") or strat.get("reason"),
                "gmt": strat.get("gmt"),
                "desc": strat.get("desc"),
                "active": strat.get("active"),
                "strategy": {
                    "label": strat.get("name"),
                    "entry_model": "asia_judas" if sid == "asia" else "mes_5orb",
                },
            }
        )
    return JSONResponse(
        {
            "symbol": symbol,
            "data_source": source,
            "warning": warning,
            "signals": signals,
            "live_stack": overlay,
        }
    )


async def api_ticker(request: Request) -> JSONResponse:
    """Recent 5-minute OHLCV for a live ticker chart, plus strategy level overlays."""
    settings = get_settings()
    symbol = coerce_futures_symbol(
        request.query_params.get("symbol", settings.default_symbol)
    )
    demo = request.query_params.get("use_synthetic", "false").lower() == "true"
    try:
        hours = int(request.query_params.get("hours", "24") or 24)
    except ValueError:
        hours = 24
    hours = max(min(hours, 120), 1)
    seed = int(request.query_params.get("seed", "1") or 1)
    lookback_days = 2 if hours <= 24 else 5

    try:
        bars, source, warning = await _load_history(
            symbol, demo=demo, lookback_days=lookback_days, seed=seed
        )
    except IBKRUnavailable as exc:
        return JSONResponse(
            {"error": str(exc), "hint": "Toggle 'Demo data' on, or start IB Gateway."}
        )

    cutoff = utcnow() - timedelta(hours=hours)
    recent = [b for b in bars if as_naive_utc(b.ts) >= cutoff] or bars[-400:]
    recent = recent[-500:]

    overlay = _live_stack_overlay(symbol, bars)
    # Backward-compat single OR payload: prefer active ORB session range.
    range_payload = None
    for lvl_high, lvl_low, win in (
        ("london_or_high", "london_or_low", "london"),
        ("new_york_or_high", "new_york_or_low", "new_york"),
        ("asia_high", "asia_low", "asia"),
    ):
        by_id = {lv["id"]: lv for lv in overlay.get("levels") or []}
        hi = by_id.get(lvl_high)
        lo = by_id.get(lvl_low)
        if hi and lo:
            range_payload = {
                "window": win,
                "low": lo["price"],
                "high": hi["price"],
                "last": recent[-1].close if recent else None,
            }
            # Prefer currently active strategy for the legacy OR field.
            active_ids = {
                s["id"] for s in (overlay.get("strategies") or []) if s.get("active")
            }
            if win in active_ids or range_payload is not None:
                if win in active_ids:
                    break

    last = recent[-1] if recent else None
    prev = recent[-2] if len(recent) > 1 else last
    open_levels = _open_trade_levels(symbol)
    from core.timeutils import market_data_lag

    lag = market_data_lag()
    lag_minutes = round(lag.total_seconds() / 60.0, 1) if lag.total_seconds() > 0 else 0.0
    bar_lag_minutes = None
    if last is not None:
        bar_lag_minutes = round(
            (utcnow() - as_naive_utc(last.ts)).total_seconds() / 60.0, 1
        )
    return JSONResponse(
        {
            "symbol": symbol.upper(),
            "data_source": source,
            "warning": warning,
            "hours": hours,
            "market_data_lag_minutes": lag_minutes,
            "bar_lag_minutes": bar_lag_minutes,
            "last": None
            if last is None
            else {
                "ts": last.ts.isoformat(),
                "close": last.close,
                "change": round(last.close - prev.close, 4) if prev else 0.0,
            },
            "range": range_payload,
            "open_trades": open_levels,
            "live_stack": overlay,
            "levels": overlay.get("levels") or [],
            "strategies": overlay.get("strategies") or [],
            "bars": [
                {
                    "t": b.ts.isoformat(),
                    "o": b.open,
                    "h": b.high,
                    "l": b.low,
                    "c": b.close,
                }
                for b in recent
            ],
        }
    )


async def api_backtest(request: Request) -> JSONResponse:
    """Run a futures 5ORB break/retest backtest (optional 70/30 walk-forward)."""
    q = request.query_params
    settings = get_settings()
    symbol = coerce_futures_symbol(q.get("symbol", settings.default_symbol))
    window = _resolve_window(q.get("window", "new_york"))
    demo = q.get("demo", "false").lower() == "true"
    lookback_days = max(int(q.get("lookback_days", "30") or 30), 5)
    seed = int(q.get("seed", "3") or 3)
    walk_forward = q.get("walk_forward", "true").lower() != "false"

    try:
        bars, source, warning = await _load_history(
            symbol, demo=demo, lookback_days=lookback_days, seed=seed
        )
    except IBKRUnavailable as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "hint": "Tick 'Demo data' to run offline, or start IB Gateway.",
            }
        )

    clear_mes_5orb_config_cache()
    # Align with Live Month PnL: JSON defaults (opens-only / London shorts /
    # Asia Wed–Fri / 2.5R), not DB optimiser overlays. Optional query knobs
    # still apply for what-if runs.
    cfg = load_mes_5orb_config(symbol)
    custom = _parse_mes_custom_params(q, symbol=symbol)
    if custom:
        cfg = apply_mes_opt_params(cfg, custom)
    bars = _filter_bars_for_window(bars, window)

    # Match report_daily_pnl_opt: keep last ~lookback calendar days in ET.
    from core.strategy.mes_5orb.opening_range import to_et

    if bars and lookback_days <= 35:
        last_et = to_et(bars[-1].ts).date()
        start = last_et - timedelta(days=lookback_days - 1)
        clipped = [b for b in bars if to_et(b.ts).date() >= start]
        if clipped:
            bars = clipped

    def _run():
        bt = run_mes_5orb_backtest(bars, cfg=cfg)
        summary = bt.summary()
        combined = summary.get("combined") or {}
        trades_ui = _mes_trades_for_ui(bt.trades)
        pnls = [float(t["pnl"] or 0) for t in trades_ui]
        metrics = summarize(pnls) if pnls else combined
        if "monte_carlo" not in metrics and pnls:
            metrics = {**metrics, "monte_carlo": monte_carlo(pnls)}
        # Prefer backtest combined PF / expectancy when summarize differs.
        for k in (
            "profit_factor",
            "expectancy",
            "win_rate",
            "total_pnl",
            "max_drawdown",
            "wins",
            "losses",
        ):
            if combined.get(k) is not None:
                metrics[k] = combined[k]
        payload = {
            "window": window.value,
            "params": {
                "entry_model": "mes_5orb",
                "symbol": cfg.symbol,
                "point_value": cfg.point_value,
                "tick_size": cfg.tick_size,
                "risk_pct": cfg.risk.risk_pct,
                "source": "custom" if custom else "defaults",
            },
            "config": summary.get("config") or mes_live_config_snapshot(cfg),
            "num_trades": len(trades_ui),
            "metrics": metrics,
            "trades": trades_ui,
            "by_session": summary.get("by_session") or {},
            "by_day": summary.get("by_day") or [],
            "score": round(_mes_score(metrics), 4),
            "equity_curve": _equity_curve(pnls),
            "data_source": source,
            "warning": warning,
            "symbol": symbol,
            "lookback_days": lookback_days,
            "bars_analysed": len(bars),
            "aligns_with": "live_month_pnl_1_lot",
        }
        if walk_forward:
            wf = walk_forward_mes_7030(bars, cfg=cfg, is_fraction=0.70)
            payload["walk_forward_70_30"] = {
                "method": wf.get("method"),
                "is_fraction": wf.get("is_fraction"),
                "oos_fraction": wf.get("oos_fraction"),
                "total_trading_days": wf.get("total_trading_days"),
                "in_sample": wf.get("in_sample"),
                "out_of_sample": wf.get("out_of_sample"),
                "note": wf.get("walk_forward_note") or wf.get("error"),
            }
        return payload

    try:
        result = await anyio.to_thread.run_sync(_run)
    except Exception as exc:
        return JSONResponse({"error": f"Backtest failed: {exc}"})
    return JSONResponse(result)


async def api_optimise(request: Request) -> JSONResponse:
    """Grid-search 5ORB exit knobs with chronological 70/30 walk-forward ranking."""
    q = request.query_params
    settings = get_settings()
    symbol = coerce_futures_symbol(q.get("symbol", settings.default_symbol))
    window = _resolve_window(q.get("window", "new_york"))
    demo = q.get("demo", "false").lower() == "true"
    lookback_days = max(int(q.get("lookback_days", "60") or 60), 10)
    seed = int(q.get("seed", "3") or 3)
    top_n = max(min(int(q.get("top_n", "8") or 8), 20), 1)
    top_is = max(min(int(q.get("top_is", "8") or 8), 24), 1)

    try:
        bars, source, warning = await _load_history(
            symbol, demo=demo, lookback_days=lookback_days, seed=seed
        )
    except IBKRUnavailable as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "hint": "Tick 'Demo data' to run offline, or start IB Gateway.",
            }
        )

    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config(symbol)

    def _run():
        report = optimise_mes_5orb(
            bars,
            cfg=cfg,
            is_fraction=0.70,
            top_is=top_is,
            top_n=top_n,
        )
        wf_best = None
        top = report.get("top") or []
        if top and "error" not in report:
            from core.strategy.mes_5orb.sessions import apply_mes_opt_params

            best_cfg = apply_mes_opt_params(cfg, top[0]["params"])
            wf_best = walk_forward_mes_7030(bars, cfg=best_cfg, is_fraction=0.70)
        return report, wf_best

    try:
        report, wf_best = await anyio.to_thread.run_sync(_run)
    except Exception as exc:
        return JSONResponse({"error": f"Optimise failed: {exc}"})

    if report.get("error"):
        return JSONResponse({"error": report["error"], "hint": report.get("walk_forward_note")})

    top = report.get("top") or []
    best = top[0] if top else None
    metrics = (best or {}).get("metrics") or {}
    persisted_id = None
    if best and int(metrics.get("trades") or 0) > 0:
        persisted_id = _db.insert_backtest(
            label=(
                f"optimise 5ORB {symbol} "
                f"(best of {report.get('combinations_tested')})"
            ),
            symbol=symbol,
            window=window,
            params=best["params"],
            metrics=metrics,
        )

    return JSONResponse(
        {
            "symbol": symbol,
            "window": window.value,
            "data_source": source,
            "warning": warning,
            "lookback_days": lookback_days,
            "bars_analysed": len(bars),
            "combinations_tested": report.get("combinations_tested"),
            "top": top,
            "persisted_id": persisted_id,
            "walk_forward_70_30": wf_best,
            "baseline_params": report.get("baseline_params"),
            "grid": report.get("grid"),
            "note": report.get("walk_forward_note"),
            "method": report.get("method"),
            "is_days": report.get("is_days"),
            "oos_days": report.get("oos_days"),
        }
    )


async def api_optimisations(request: Request) -> JSONResponse:
    """List persisted backtest/optimisation runs (the optimisation history)."""
    limit = int(request.query_params.get("limit", "50") or 50)
    symbol = request.query_params.get("symbol") or None
    runs = _db.list_backtests(symbol=symbol, limit=limit)
    active_ids = _db.active_backtest_ids(symbol)
    for run in runs:
        run["is_active"] = run["id"] in active_ids and is_tradable_orb_params(run.get("params"))
    return JSONResponse({"count": len(runs), "runs": runs, "active_ids": sorted(active_ids)})


async def api_strategy_select(request: Request) -> JSONResponse:
    """Select an optimised or custom mes_5orb parameter set for trading."""
    q = request.query_params
    settings = get_settings()
    symbol = coerce_futures_symbol(q.get("symbol") or settings.default_symbol)
    bid_raw = q.get("backtest_id")
    try:
        if bid_raw not in (None, ""):
            run = _db.get_backtest(int(bid_raw))
            if run is None:
                return JSONResponse({"ok": False, "error": "No saved run with that id."})
            symbol = coerce_futures_symbol(run["symbol"] or symbol)
            window = SessionWindow(run["window"])
            params = run["params"] or {"entry_model": "mes_5orb", "symbol": symbol}
            label = run["label"]
            backtest_id = run["id"]
        else:
            window = _resolve_window(q.get("window", "new_york"))
            custom = _parse_mes_custom_params(q, symbol=symbol)
            if custom is not None:
                # Fill missing knobs from current JSON defaults so the set is complete.
                base = _mes_params_payload(symbol, _db)["defaults"]
                params = {
                    "entry_model": "mes_5orb",
                    "symbol": symbol,
                    "source": "custom",
                    "target_r": custom.get("target_r", base["target_r"]),
                    "scale_fraction": custom.get(
                        "scale_fraction", base["scale_fraction"]
                    ),
                    "stop_buffer_ticks": custom.get(
                        "stop_buffer_ticks", base["stop_buffer_ticks"]
                    ),
                    "tolerance_ticks": custom.get(
                        "tolerance_ticks", base["tolerance_ticks"]
                    ),
                    "require_rejection_candle": custom.get(
                        "require_rejection_candle",
                        base["require_rejection_candle"],
                    ),
                }
                for flag in ("use_hod_lod_target", "move_stop_to_be", "runner_trail"):
                    if flag in custom:
                        params[flag] = custom[flag]
                label = (
                    f"custom {symbol} {params['target_r']}R · "
                    f"{int(params['scale_fraction'] * 100)}% · "
                    f"buf {params['stop_buffer_ticks']} · "
                    f"tol {params['tolerance_ticks']}"
                )
                backtest_id = _db.insert_backtest(
                    label=label,
                    symbol=symbol,
                    window=window,
                    params=params,
                    metrics={"trades": 0, "note": "manual custom params"},
                )
            else:
                params = {"entry_model": "mes_5orb", "symbol": symbol}
                backtest_id = None
                label = f"selected {symbol} {window.value}"
        chosen = _db.set_active_strategy(
            symbol, window, params=params, backtest_id=backtest_id, label=label
        )
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    cfg, found = resolve_trading_config(symbol, window, _db)
    return JSONResponse(
        {
            "ok": True,
            "symbol": symbol,
            "window": window.value,
            "chosen": chosen,
            "strategy": strategy_payload(cfg, found),
            "strategy_by_window": strategy_by_window(symbol, _db),
            "mes_params": _mes_params_payload(symbol, _db),
        }
    )


async def api_mes_params(request: Request) -> JSONResponse:
    """Current / default 5ORB exit knobs for the custom-params panel."""
    q = request.query_params
    settings = get_settings()
    symbol = coerce_futures_symbol(q.get("symbol") or settings.default_symbol)
    return JSONResponse(_mes_params_payload(symbol, _db))


async def api_strategy_clear(request: Request) -> JSONResponse:
    """Revert a symbol (and optional window) to windows.json defaults."""
    q = request.query_params
    settings = get_settings()
    symbol = (q.get("symbol") or settings.default_symbol).upper()
    window_raw = q.get("window")
    window = None
    if window_raw and window_raw.lower() not in ("auto", "active", "current", "all", ""):
        window = _resolve_window(window_raw)
    cleared = _db.clear_active_strategy(symbol, window)
    return JSONResponse(
        {
            "ok": True,
            "cleared": cleared,
            "symbol": symbol,
            "window": window.value if window else "all",
            "strategy_by_window": strategy_by_window(symbol, _db),
            "mes_params": _mes_params_payload(symbol, _db),
        }
    )


async def api_preview(request: Request) -> JSONResponse:
    """Build a risk-sized plan for the current ORB signal. Does not place."""
    settings = get_settings()
    symbol = request.query_params.get("symbol", settings.default_symbol)
    window = request.query_params.get("window", "auto")
    demo = request.query_params.get("use_synthetic", "false").lower() == "true"
    seed = int(request.query_params.get("seed", "1") or 1)
    tr = request.query_params.get("target_r")
    target_r = float(tr) if tr not in (None, "") else None
    rp = request.query_params.get("risk_pct")
    risk_pct = float(rp) if rp not in (None, "") else None
    try:
        plan = await build_trade_plan(
            symbol,
            window,
            use_synthetic=demo,
            target_r=target_r,
            synthetic_seed=seed,
            db=_db,
            risk_pct=risk_pct,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse(plan)


async def api_autotrade_status(_request: Request) -> JSONResponse:
    return JSONResponse(_autotrader.status())


async def api_autotrade_start(request: Request) -> JSONResponse:
    q = request.query_params
    settings = get_settings()
    tr = q.get("target_r")
    rp = q.get("risk_pct")
    result = _autotrader.start(
        symbol=q.get("symbol", settings.default_symbol),
        window=q.get("window", "auto"),
        demo=q.get("demo", "false").lower() == "true",
        interval=float(q.get("interval", "60") or 60),
        target_r=float(tr) if tr not in (None, "") else None,
        risk_pct=float(rp) if rp not in (None, "") else None,
    )
    return JSONResponse(result)


async def api_autotrade_stop(_request: Request) -> JSONResponse:
    return JSONResponse(await _autotrader.stop())


async def api_autotrade_risk(request: Request) -> JSONResponse:
    """Set risk % (1–5) for the next sized entries; updates the summary card."""
    q = request.query_params
    rp = q.get("risk_pct")
    if rp in (None, ""):
        return JSONResponse({"ok": False, "error": "risk_pct required (1-5)"})
    try:
        return JSONResponse(_autotrader.set_risk_pct(float(rp)))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


routes = [
    Route("/", index),
    Route("/api/health", api_health),
    Route("/api/summary", api_summary),
    Route("/api/trades", api_trades),
    Route("/api/performance", api_performance),
    Route("/api/revenue/daily", api_revenue_daily),
    Route("/api/positions", api_positions),
    Route("/api/signals", api_signals),
    Route("/api/ticker", api_ticker),
    Route("/api/backtest", api_backtest),
    Route("/api/optimise", api_optimise),
    Route("/api/optimisations", api_optimisations),
    Route("/api/mes_params", api_mes_params),
    Route("/api/strategy/select", api_strategy_select, methods=["POST"]),
    Route("/api/strategy/clear", api_strategy_clear, methods=["POST"]),
    Route("/api/preview", api_preview),
    Route("/api/autotrade/status", api_autotrade_status),
    Route("/api/autotrade/start", api_autotrade_start, methods=["POST"]),
    Route("/api/autotrade/stop", api_autotrade_stop, methods=["POST"]),
    Route("/api/autotrade/risk", api_autotrade_risk, methods=["POST"]),
]


def _resume_autotrade_if_needed() -> None:
    """Start the paper auto-trader when last intent (or env) says keep running."""
    state = load_state()
    if not should_autostart(state):
        return
    kwargs = start_kwargs(state)
    result = _autotrader.start(**kwargs)
    if result.get("ok"):
        _autotrader._add_log("Resumed auto-trading after dashboard restart.", "start")
        _log.info(
            "autotrade resumed: %s / %s every %.0fs",
            kwargs["symbol"],
            kwargs["window"],
            kwargs["interval"],
        )
    else:
        _log.warning("autotrade resume skipped: %s", result.get("error"))


@asynccontextmanager
async def _lifespan(_app: Starlette):
    task = asyncio.create_task(_session_exit_loop(), name="orb-session-exits")
    try:
        # After the event loop is running so create_task in start() is valid.
        _resume_autotrade_if_needed()
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = Starlette(routes=routes, lifespan=_lifespan)


def main() -> None:
    settings = get_settings()
    host = settings.dashboard_host
    port = settings.dashboard_port
    print(f"Futures 5ORB dashboard -> http://{host}:{port}  (env: {settings.trading_environment()})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
