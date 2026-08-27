"""Starlette web dashboard for the Options ORB MCP system.

Serves a single-page UI plus a small JSON API over the trade journal and (when
TWS/IB Gateway is reachable) live IBKR positions and account values. Read-only:
it never places or modifies trades - that stays with the execution agent.

Run with:  python -m dashboard.app   (or the ``orb-dashboard`` entry point)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
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
from core.analytics import performance_metrics, summarize
from core.backtest import (
    DEFAULT_ORB_GRID,
    BacktestParams,
    dedupe_by_outcome,
    grid_search,
    run_backtest,
)
from core.backtest import _score as score_metrics
from core.config import get_settings
from core.db import Database, is_tradable_orb_params
from core.engine import build_trade_plan, place_trade_plan, resolve_window, settle_session_exits
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.journal import close_open_paper_trades, mark_open_trade, paper_account_snapshot
from core.marketdata import fetch_bars_with_fallback
from core.models import Regime, SessionWindow, TradeStatus
from core.risk import RiskManager
from core.sessions import active_window, describe_windows_gmt
from core.strategy.orb import compute_orb_signal
from core.timeutils import as_naive_utc, utcnow

_STATIC = Path(__file__).resolve().parent / "static"
_db = Database()
_log = logging.getLogger("orb.dashboard")
_SESSION_EXIT_INTERVAL = 30.0

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
    try:
        async with IBKRClient() as ib:
            value = await fn(ib)
        result: tuple[bool, Any, str | None] = (True, value, None)
    except IBKRUnavailable as exc:
        result = (False, None, str(exc))
    _ibkr_cache[key] = (now, result)
    return result


async def index(_request: Request) -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


# --- backtesting / simulation helpers --------------------------------------

# Cache historical bars briefly so running a backtest then an optimisation on
# the same inputs doesn't refetch (and to keep IBKR historical requests low).
_BARS_CACHE_TTL = 60.0
_bars_cache: dict[tuple, tuple[float, tuple[list, str, str | None]]] = {}


def _resolve_window(window: str) -> SessionWindow:
    w = (window or "new_york").lower()
    if w in ("auto", "active", "current"):
        return SessionWindow.NEW_YORK
    return SessionWindow(w)


async def _load_history(
    symbol: str, *, demo: bool, lookback_days: int, seed: int
) -> tuple[list, str, str | None]:
    """Fetch historical bars (IBKR live history, or synthetic in demo mode)."""
    key = (symbol.upper(), demo, lookback_days, seed)
    now = time.monotonic()
    cached = _bars_cache.get(key)
    if cached and now - cached[0] < _BARS_CACHE_TTL:
        return cached[1]
    bars, source, warning = await fetch_bars_with_fallback(
        symbol,
        duration=f"{lookback_days} D",
        bar_size="5 mins",
        use_synthetic=demo,
        allow_synthetic_fallback=demo,
        synthetic_days=lookback_days,
        synthetic_seed=seed,
    )
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


def _params_from_query(q) -> BacktestParams:
    """Build BacktestParams from query params, falling back to defaults."""
    d = BacktestParams()

    def num(name: str, cast, default):
        raw = q.get(name)
        if raw is None or raw == "":
            return default
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return default

    return BacktestParams(
        opening_range_minutes=num("opening_range_minutes", int, d.opening_range_minutes),
        breakout_buffer_atr=num("breakout_buffer_atr", float, d.breakout_buffer_atr),
        min_strength=num("min_strength", float, d.min_strength),
        target_r=num("target_r", float, d.target_r),
        stop_r=num("stop_r", float, d.stop_r),
        iv=num("iv", float, d.iv),
        dte=num("dte", int, d.dte),
        cost_per_trade=num("cost_per_trade", float, d.cost_per_trade),
    )


def _equity_curve(pnls: list[float], starting: float = 0.0) -> list[dict]:
    equity = starting
    curve = [{"i": 0, "equity": round(equity, 2)}]
    for i, p in enumerate(pnls, start=1):
        equity += p
        curve.append({"i": i, "equity": round(equity, 2), "pnl": round(p, 2)})
    return curve


# --- auto-trading engine ---------------------------------------------------


class AutoTrader:
    """Background loop that runs the ORB entry logic on an interval.

    Each cycle it builds a risk-sized plan for the active session window and, if
    a qualifying breakout passes all risk gates, places it (paper/simulated) via
    the shared engine. It places up to 3 trades per window (max 9 per UTC day
    across Asia / London / New York).

    Safety: refuses to *start* against a LIVE account. Session-window
    flatten still runs from the dashboard loop so live IBKR combos close
    at each Asia / London / New York window_close.
    """

    MIN_INTERVAL = 10.0

    def __init__(self, db: Database) -> None:
        self._db = db
        self._task: asyncio.Task | None = None
        self.running = False
        self.symbol = "SPY"
        self.window = "auto"
        self.demo = False
        self.interval = 60.0
        self.target_r: float | None = None
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
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "cycles": self.cycles,
            "trades_placed": self.trades_placed,
            "log": list(reversed(self.log[-40:])),
            "strategy_by_window": strategy_by_window(self.symbol, self._db),
        }

    def start(self, *, symbol: str, window: str, demo: bool, interval: float,
              target_r: float | None) -> dict:
        if self.running:
            return {"ok": False, "error": "Auto-trading is already running.", **self.status()}
        settings = get_settings()
        if settings.trading_environment() == "LIVE":
            return {
                "ok": False,
                "error": "Auto-trading is disabled for LIVE accounts as a safety "
                         "measure. Set ACCOUNT_MODE=paper to use it.",
            }
        self.symbol = (symbol or "SPY").upper()
        self.window = window or "auto"
        self.demo = demo
        self.interval = max(float(interval), self.MIN_INTERVAL)
        self.target_r = target_r
        self.running = True
        self.started_at = utcnow().isoformat()
        self.cycles = 0
        self.trades_placed = 0
        self.per_window_limit = settings.max_open_positions_per_window
        self._placed_counts.clear()
        self._add_log(
            f"Auto-trading started - {self.symbol} / {self.window}, "
            f"{'demo data' if demo else 'live paper data'}, every {self.interval:.0f}s "
            f"(up to {self.per_window_limit} per window, "
            f"{settings.max_open_positions}/day).",
            "start",
        )
        for w in SessionWindow:
            cfg, found = resolve_trading_config(self.symbol, w, self._db)
            payload = strategy_payload(cfg, found)
            self._add_log(f"{w.value}: {format_strategy(payload)}", "start")
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

        # Live "auto" mode: only trade a window that is actually open now.
        # Falling back to New York outside its cash session would keep scoring
        # yesterday's bars (and never fill).
        if not self.demo and self.window.lower() in ("auto", "active", "current"):
            live = active_window()
            if live is None:
                self._add_log(
                    f"No session window is open ({describe_windows_gmt()}). Waiting.",
                    "muted",
                )
                return
            cycle_window = live.value
        else:
            cycle_window = self.window

        try:
            preview = await build_trade_plan(
                self.symbol, cycle_window, use_synthetic=self.demo,
                target_r=self.target_r, synthetic_seed=seed, db=self._db,
            )
        except Exception as exc:
            self._add_log(f"Signal error: {exc}", "error")
            return

        if not preview.get("ok"):
            reason = preview.get("reason") or preview.get("error") or "no trade"
            self._add_log(f"No entry: {reason}", "muted")
            return

        win = resolve_window(cycle_window).value
        # Count entries per window per real day (live) or per synthetic session
        # (demo, so each cycle can act on its own simulated day). Cap is 3.
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
            self._add_log(
                f"PLACED trade #{result['trade_id']} - {plan.get('spread_type','')} "
                f"{plan.get('direction','')} x{plan.get('contracts','')} "
                f"({result.get('environment','')}; "
                f"{format_strategy(preview.get('strategy') or {})}).",
                "trade",
            )
        else:
            self._add_log(f"Placement failed: {result.get('error','unknown')}", "error")


_autotrader = AutoTrader(_db)


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
                f"CLOSED journal #{row.get('trade_id')} {row.get('reason')} "
                f"pnl {pnl:+.2f} ({row.get('window')})"
                f"{' IBKR flattened' if row.get('ibkr') else ''}.",
                "trade" if pnl >= 0 else "warn",
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
    payload = {
        "environment": rm.environment(),
        "account_currency": settings.account_currency,
        "starting_capital": settings.starting_capital,
        "risk_budget_per_trade": round(rm.risk_budget_per_trade(), 2),
        "max_risk_per_trade_pct": round(settings.max_risk_per_trade * 100, 2),
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
    }
    connected, acct, note = await _ibkr_fetch("account", lambda ib: ib.account_summary())
    payload["ibkr_connected"] = connected
    if connected:
        payload["ibkr"] = acct
    else:
        payload["ibkr_note"] = note
    opens = _db.query_trades(status=TradeStatus.OPEN, limit=1000)
    spots = await _spot_by_symbol({t.symbol for t in opens})
    paper = paper_account_snapshot(
        _db,
        spots,
        starting_capital=settings.starting_capital,
        environment=rm.environment(),
    )
    payload.update(paper)
    return JSONResponse(payload)


async def api_trades(request: Request) -> JSONResponse:
    limit = int(request.query_params.get("limit", "500"))
    status = request.query_params.get("status")
    open_paper = _db.query_trades(status=TradeStatus.OPEN, environment="PAPER", limit=100)
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
    trades = _db.query_trades(
        status=TradeStatus(status.lower()) if status else None, limit=limit
    )
    spots = await _spot_by_symbol(
        {t.symbol for t in trades if t.status is TradeStatus.OPEN}
    )
    return JSONResponse(
        {
            "count": len(trades),
            "trades": [
                _with_mark(t, spots) if t.status is TradeStatus.OPEN else t.model_dump(mode="json")
                for t in trades
            ],
        }
    )


async def api_performance(_request: Request) -> JSONResponse:
    settings = get_settings()
    closed = _db.query_trades(status=TradeStatus.CLOSED, limit=100000)
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

    opens = _db.query_trades(status=TradeStatus.OPEN, environment="PAPER", limit=1000)
    spots = await _spot_by_symbol({t.symbol for t in opens})
    paper = paper_account_snapshot(
        _db,
        spots,
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


async def api_positions(_request: Request) -> JSONResponse:
    open_trades = _db.query_trades(status=TradeStatus.OPEN, limit=1000)
    spots = await _spot_by_symbol({t.symbol for t in open_trades})
    marked = [_with_mark(t, spots) for t in open_trades]
    payload: dict = {
        "open_trades": marked,
        "count": len(marked),
        "open_unrealized_pnl": round(
            sum(t.get("unrealized_pnl") or 0.0 for t in marked), 2
        ),
        "paper_equity": paper_account_snapshot(
            _db,
            spots,
            starting_capital=get_settings().starting_capital,
            environment=get_settings().trading_environment(),
        )["paper_equity"],
        "spots": spots,
    }
    connected, positions, note = await _ibkr_fetch("positions", lambda ib: ib.positions())
    payload["ibkr_connected"] = connected
    payload["ibkr_positions"] = positions if connected else []
    if not connected:
        payload["ibkr_note"] = note
    return JSONResponse(payload)


async def api_signals(request: Request) -> JSONResponse:
    settings = get_settings()
    symbol = request.query_params.get("symbol", settings.default_symbol)
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

    signals = []
    for w in SessionWindow:
        cfg, found = resolve_trading_config(symbol, w, _db)
        sig = compute_orb_signal(symbol, w, bars, cfg)
        payload = sig.model_dump(mode="json")
        payload["strategy"] = strategy_payload(cfg, found)
        signals.append(payload)
    return JSONResponse(
        {"symbol": symbol, "data_source": source, "warning": warning, "signals": signals}
    )


async def api_ticker(request: Request) -> JSONResponse:
    """Recent 5-minute OHLCV for a live ticker chart, plus the current OR overlay."""
    settings = get_settings()
    symbol = request.query_params.get("symbol", settings.default_symbol)
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

    win = active_window()
    range_payload = None
    if win is not None and bars:
        cfg, _found = resolve_trading_config(symbol, win, _db)
        sig = compute_orb_signal(symbol, win, bars, cfg)
        if sig.range_high > 0:
            range_payload = {
                "window": win.value,
                "low": sig.range_low,
                "high": sig.range_high,
                "last": sig.last_price,
            }

    last = recent[-1] if recent else None
    prev = recent[-2] if len(recent) > 1 else last
    return JSONResponse(
        {
            "symbol": symbol.upper(),
            "data_source": source,
            "warning": warning,
            "hours": hours,
            "last": None
            if last is None
            else {
                "ts": last.ts.isoformat(),
                "close": last.close,
                "change": round(last.close - prev.close, 4) if prev else 0.0,
            },
            "range": range_payload,
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
    """Run a single ORB spread backtest over historical (or demo) data.

    Returns the performance metrics, the simulated trades, and a simulated
    equity curve so the GUI can plot the run.
    """
    q = request.query_params
    settings = get_settings()
    symbol = q.get("symbol", settings.default_symbol)
    window = _resolve_window(q.get("window", "new_york"))
    demo = q.get("demo", "false").lower() == "true"
    lookback_days = max(int(q.get("lookback_days", "30") or 30), 5)
    seed = int(q.get("seed", "3") or 3)
    params = _params_from_query(q)

    try:
        bars, source, warning = await _load_history(
            symbol, demo=demo, lookback_days=lookback_days, seed=seed
        )
    except IBKRUnavailable as exc:
        return JSONResponse(
            {"error": str(exc), "hint": "Tick 'Demo data' to run offline, or start IB Gateway."}
        )

    result = await anyio.to_thread.run_sync(lambda: run_backtest(bars, window, params).summary())
    pnls = [t["pnl"] for t in result["trades"]]
    result["equity_curve"] = _equity_curve(pnls)
    result["score"] = round(score_metrics(result), 4)
    result["data_source"] = source
    result["warning"] = warning
    result["symbol"] = symbol
    result["lookback_days"] = lookback_days
    result["bars_analysed"] = len(bars)
    return JSONResponse(result)


async def api_optimise(request: Request) -> JSONResponse:
    """Grid-search the ORB parameter space and return the ranked top-N.

    The best run is persisted to the backtests table so it appears in the
    'Optimisations made' history.
    """
    q = request.query_params
    settings = get_settings()
    symbol = q.get("symbol", settings.default_symbol)
    window = _resolve_window(q.get("window", "new_york"))
    demo = q.get("demo", "false").lower() == "true"
    lookback_days = max(int(q.get("lookback_days", "60") or 60), 10)
    seed = int(q.get("seed", "3") or 3)
    top_n = max(min(int(q.get("top_n", "5") or 5), 25), 1)

    try:
        bars, source, warning = await _load_history(
            symbol, demo=demo, lookback_days=lookback_days, seed=seed
        )
    except IBKRUnavailable as exc:
        return JSONResponse(
            {"error": str(exc), "hint": "Tick 'Demo data' to run offline, or start IB Gateway."}
        )

    ranked = await anyio.to_thread.run_sync(
        lambda: grid_search(bars, window, DEFAULT_ORB_GRID)
    )
    distinct = dedupe_by_outcome(ranked)
    top = [
        {"params": r["params"], "score": r["score"], "metrics": r["metrics"]}
        for r in distinct[:top_n]
    ]

    persisted_id = None
    best = distinct[0] if distinct else None
    if best is not None and best["score"] > -1e8:
        persisted_id = _db.insert_backtest(
            label=f"optimise {symbol} {window.value} (best of {len(ranked)})",
            symbol=symbol,
            window=window,
            params=best["params"],
            metrics=best["metrics"],
        )

    return JSONResponse(
        {
            "symbol": symbol,
            "window": window.value,
            "data_source": source,
            "warning": warning,
            "lookback_days": lookback_days,
            "combinations_tested": len(ranked),
            "distinct_outcomes": len(distinct),
            "bars_analysed": len(bars),
            "top": top,
            "persisted_id": persisted_id,
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
    """Make an explicit parameter set the one used for live/paper entries."""
    q = request.query_params
    settings = get_settings()
    symbol = (q.get("symbol") or settings.default_symbol).upper()
    bid_raw = q.get("backtest_id")
    try:
        if bid_raw not in (None, ""):
            run = _db.get_backtest(int(bid_raw))
            if run is None:
                return JSONResponse({"ok": False, "error": "No saved run with that id."})
            if not is_tradable_orb_params(run["params"]):
                return JSONResponse(
                    {"ok": False, "error": "That run is a walk-forward grid, not a tradable set."}
                )
            symbol = (run["symbol"] or symbol).upper()
            window = SessionWindow(run["window"])
            params = run["params"]
            label = run["label"]
            backtest_id = run["id"]
        else:
            window = _resolve_window(q.get("window", "new_york"))
            params = _params_from_query(q).as_dict()
            if not is_tradable_orb_params(params):
                return JSONResponse({"ok": False, "error": "Missing ORB parameters."})
            backtest_id = None
            label = f"selected {symbol} {window.value}"
        chosen = _db.set_active_strategy(
            symbol, window, params=params, backtest_id=backtest_id, label=label
        )
    except (ValueError, KeyError) as exc:
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
        }
    )


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
    try:
        plan = await build_trade_plan(
            symbol,
            window,
            use_synthetic=demo,
            target_r=target_r,
            synthetic_seed=seed,
            db=_db,
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
    result = _autotrader.start(
        symbol=q.get("symbol", settings.default_symbol),
        window=q.get("window", "auto"),
        demo=q.get("demo", "false").lower() == "true",
        interval=float(q.get("interval", "60") or 60),
        target_r=float(tr) if tr not in (None, "") else None,
    )
    return JSONResponse(result)


async def api_autotrade_stop(_request: Request) -> JSONResponse:
    return JSONResponse(await _autotrader.stop())


routes = [
    Route("/", index),
    Route("/api/summary", api_summary),
    Route("/api/trades", api_trades),
    Route("/api/performance", api_performance),
    Route("/api/positions", api_positions),
    Route("/api/signals", api_signals),
    Route("/api/ticker", api_ticker),
    Route("/api/backtest", api_backtest),
    Route("/api/optimise", api_optimise),
    Route("/api/optimisations", api_optimisations),
    Route("/api/strategy/select", api_strategy_select, methods=["POST"]),
    Route("/api/strategy/clear", api_strategy_clear, methods=["POST"]),
    Route("/api/preview", api_preview),
    Route("/api/autotrade/status", api_autotrade_status),
    Route("/api/autotrade/start", api_autotrade_start, methods=["POST"]),
    Route("/api/autotrade/stop", api_autotrade_stop, methods=["POST"]),
]


@asynccontextmanager
async def _lifespan(_app: Starlette):
    task = asyncio.create_task(_session_exit_loop(), name="orb-session-exits")
    try:
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
    print(f"Options ORB dashboard -> http://{host}:{port}  (env: {settings.trading_environment()})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
