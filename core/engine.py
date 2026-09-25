"""Shared trading engine: futures 5ORB break/retest -> bracket -> journal.

Supports CME/CBOT equity-index futures (MES, MNQ, MYM, M2K, ES, NQ).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.backtest_mes import evaluate_mes_signal_live
from core.config import get_settings
from core.db import Database
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.journal import (
    _journal_close,
    _parse_plan,
    is_ibkr_backed,
)
from core.marketdata import fetch_bars_with_fallback
from core.models import Direction, Regime, SessionWindow, SpreadType, TradeRecord, TradeStatus
from core.risk import RiskManager
from core.sessions import active_window
from core.strategy.mes_5orb.markets import (
    DEFAULT_FUTURES_SYMBOL,
    coerce_futures_symbol,
)
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import load_mes_5orb_config
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop
from core.timeutils import utcnow


def resolve_window(window: str) -> SessionWindow:
    """Map a window name (or 'auto'/'active'/'current') to a SessionWindow."""
    if window.lower() in ("auto", "active", "current"):
        return active_window() or SessionWindow.NEW_YORK
    key = window.lower().replace(" ", "_")
    if key in ("ny", "newyork", "new_york"):
        return SessionWindow.NEW_YORK
    if key == "london":
        return SessionWindow.LONDON
    return SessionWindow(key)


def _mes_session_to_window(session_name: str) -> SessionWindow:
    if session_name == "london":
        return SessionWindow.LONDON
    return SessionWindow.NEW_YORK


async def build_mes_trade_plan(
    symbol: str = DEFAULT_FUTURES_SYMBOL,
    window: str = "auto",
    *,
    use_synthetic: bool = False,
    synthetic_seed: int = 42,
    db: Database | None = None,
    risk_pct: float | None = None,
) -> dict[str, Any]:
    """Futures 5ORB signal -> plan -> risk sizing. Places no order."""
    db = db or Database()
    symbol = coerce_futures_symbol(symbol)
    cfg = load_mes_5orb_config(symbol)
    # Chosen risk% (1–5) wins; else config; else Settings.MAX_RISK_PER_TRADE.
    effective_risk = risk_pct if risk_pct is not None else cfg.risk.risk_pct

    try:
        bars, source, warning = await fetch_bars_with_fallback(
            symbol,
            duration="3 D",
            bar_size="5 mins",
            use_synthetic=use_synthetic,
            synthetic_seed=synthetic_seed,
            allow_synthetic_fallback=use_synthetic,
        )
    except IBKRUnavailable as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": (
                f"Pass use_synthetic=true for a demo, or place {symbol}_5mins.csv "
                "in data/history/."
            ),
        }

    session_name = None
    if window.lower() not in ("auto", "active", "current"):
        session_name = (
            "london"
            if "london" in window.lower()
            else (
                "new_york"
                if "new" in window.lower() or window.lower() in ("ny", "new_york")
                else None
            )
        )

    signal = evaluate_mes_signal_live(bars, session_name=session_name, cfg=cfg)
    strategy = {
        "entry_model": "mes_5orb",
        "symbol": symbol,
        "point_value": cfg.point_value,
        "tick_size": cfg.tick_size,
        "exchange": cfg.exchange,
        "contracts_config": cfg.risk.contracts,
        "max_concurrent": cfg.risk.max_concurrent,
        "risk_pct": effective_risk,
    }

    if not signal.get("ok"):
        return {
            "ok": False,
            "reason": signal.get("reason", f"No {symbol} 5ORB setup"),
            "signal": {k: v for k, v in signal.items() if k != "mes_plan"},
            "data_source": source,
            "warning": warning,
            "strategy": strategy,
        }

    mes_plan = signal.get("mes_plan")
    plan_dict = signal.get("plan") or (mes_plan.as_dict() if mes_plan else {})
    plan_dict["symbol"] = symbol
    plan_dict["point_value"] = cfg.point_value
    plan_dict["tick_size"] = cfg.tick_size
    stop_points = float(plan_dict.get("stop_points") or 0)
    rm = RiskManager(db=db)
    win = _mes_session_to_window(str(plan_dict.get("session_name") or "new_york"))
    decision = rm.pre_trade_checks_futures(
        stop_points,
        point_value=cfg.point_value,
        requested_contracts=cfg.risk.contracts,
        max_concurrent=cfg.risk.max_concurrent,
        window=win,
        risk_pct=effective_risk,
    )
    plan_dict["contracts"] = decision.contracts
    plan_dict["risk_pct"] = effective_risk
    plan_dict["max_loss_usd"] = round(
        stop_points * cfg.point_value * max(decision.contracts, 0), 2
    )

    return {
        "ok": True,
        "environment": rm.environment(),
        "data_source": source,
        "warning": warning,
        "signal": {
            "session": signal.get("session"),
            "or_high": signal.get("or_high"),
            "or_low": signal.get("or_low"),
            "state": signal.get("state"),
            "direction": plan_dict.get("direction"),
            "breakout": True,
            "regime": Regime.TREND.value,
            "strength": 1.0,
            "symbol": symbol,
            "notes": plan_dict.get("notes", ""),
        },
        "plan": plan_dict,
        "per_contract_max_loss_usd": round(stop_points * cfg.point_value, 2),
        "risk": decision.as_dict(),
        "tradeable": decision.approved,
        "strategy": strategy,
        "instrument": "future",
        "entry_model": "mes_5orb",
    }


async def build_trade_plan(
    symbol: str,
    window: str,
    *,
    use_synthetic: bool = False,
    target_r: float | None = None,
    synthetic_seed: int = 42,
    db: Database | None = None,
    entry_strategy: str | None = None,
    risk_pct: float | None = None,
) -> dict[str, Any]:
    """Futures 5ORB entry point for any supported symbol."""
    _ = target_r, entry_strategy
    return await build_mes_trade_plan(
        symbol=symbol,
        window=window,
        use_synthetic=use_synthetic,
        synthetic_seed=synthetic_seed,
        db=db,
        risk_pct=risk_pct,
    )


async def place_trade_plan(
    preview: dict[str, Any],
    *,
    symbol: str,
    window: str,
    use_synthetic: bool = False,
    db: Database | None = None,
) -> dict[str, Any]:
    """Place an approved futures plan; record it to the journal."""
    db = db or Database()
    if not preview.get("ok"):
        return preview
    if not preview.get("tradeable"):
        return {"ok": False, "error": "Risk checks failed.", **preview}

    plan_dict = dict(preview["plan"])
    settings = get_settings()
    symbol = coerce_futures_symbol(
        plan_dict.get("symbol") or symbol or settings.default_symbol
    )
    direction = Direction(plan_dict.get("direction") or "long")
    session_name = str(plan_dict.get("session_name") or "new_york")
    win = _mes_session_to_window(session_name)
    contracts = int(plan_dict.get("contracts") or 1)
    entry_price = float(plan_dict.get("entry_price") or 0)
    stop_price = float(plan_dict.get("stop_price") or 0)
    point_value = float(plan_dict.get("point_value") or load_mes_5orb_config(symbol).point_value)
    stop_points = abs(entry_price - stop_price)
    max_loss = stop_points * point_value * contracts

    simulate = preview.get("data_source") == "synthetic" or use_synthetic
    side = "BUY" if direction is Direction.LONG else "SELL"

    if simulate:
        order_ref = f"SIM-{symbol}-{utcnow():%Y%m%d%H%M%S}"
        environment = f"{settings.trading_environment()}(sim)"
        placement: dict[str, Any] = {"simulated": True, "order_ref": order_ref}
    else:
        try:
            async with IBKRClient(readonly=False) as ib:
                placement = await ib.place_future_bracket(
                    symbol,
                    side=side,
                    contracts=contracts,
                    stop_price=stop_price,
                    entry_limit=None,
                )
            if not placement.get("ok"):
                return {
                    "ok": False,
                    "error": placement.get("error", f"{symbol} place failed"),
                    **preview,
                }
            order_ref = placement["order_ref"]
            environment = settings.trading_environment()
        except IBKRUnavailable as exc:
            if settings.trading_environment() != "PAPER":
                return {"ok": False, "error": f"Order placement failed: {exc}"}
            order_ref = f"SIM-{symbol}-{utcnow():%Y%m%d%H%M%S}"
            environment = "PAPER(sim)"
            placement = {
                "simulated": True,
                "order_ref": order_ref,
                "ibkr_error": str(exc),
            }

    plan_payload = dict(plan_dict)
    plan_payload["entry_model"] = "mes_5orb"
    plan_payload["instrument"] = "future"
    plan_payload["symbol"] = symbol
    plan_payload["point_value"] = point_value
    plan_payload["side"] = side
    plan_payload["use_trailing_stop"] = True
    plan_payload["trail_active"] = False
    plan_payload["original_stop_loss_price"] = stop_price
    plan_payload["stop_loss_price"] = stop_price

    notes = str(plan_dict.get("notes") or f"{symbol} mes_5orb")
    if placement.get("ibkr_error"):
        notes += f" | IBKR unavailable, simulated paper fill: {placement['ibkr_error']}"

    record = TradeRecord(
        environment=environment,
        symbol=symbol,
        window=win,
        regime=Regime.TREND,
        spread_type=SpreadType.BULL_CALL if direction is Direction.LONG else SpreadType.BEAR_PUT,
        direction=direction,
        contracts=contracts,
        entry_price=entry_price,
        max_loss=max_loss,
        max_profit=0.0,
        target_r=0.0,
        status=TradeStatus.OPEN,
        signal_strength=1.0,
        order_ref=order_ref,
        notes=notes,
        plan_json=json.dumps(plan_payload),
    )
    trade_id = db.insert_trade(record)

    return {
        "ok": True,
        "trade_id": trade_id,
        "environment": environment,
        "placement": placement,
        "plan": plan_payload,
        "entry_model": "mes_5orb",
        "instrument": "future",
    }


def _mes_mark(trade: TradeRecord, bars: list) -> float:
    if not bars:
        return float(trade.entry_price)
    return float(bars[-1].close)


def _mes_force_flat_due(trade: TradeRecord, cfg, now: datetime | None = None) -> bool:
    """True when ET clock is at/after this trade's session force_flat."""
    plan = _parse_plan(trade.plan_json)
    session_name = str(plan.get("session_name") or "")
    if trade.window is SessionWindow.LONDON:
        session_name = session_name or "london"
    elif trade.window is SessionWindow.NEW_YORK:
        session_name = session_name or "new_york"
    sess = cfg.session(session_name) if session_name else None
    if sess is None:
        if trade.window is SessionWindow.LONDON:
            sess = cfg.session("london")
        else:
            sess = cfg.session("new_york")
    if sess is None:
        return False
    et = to_et(now or utcnow())
    return et.time() >= sess.force_flat


async def settle_session_exits(
    db: Database,
    symbol: str,
    bars: list,
    *,
    now_window: SessionWindow | None = None,
    ib: Any = None,
) -> list[dict[str, Any]]:
    """Manage futures trail stops and force-flat at session force_flat times."""
    _ = now_window
    symbol = coerce_futures_symbol(symbol)
    closed: list[dict[str, Any]] = []

    opens = [
        t
        for t in db.query_trades(status=TradeStatus.OPEN, limit=1000)
        if t.symbol.upper() == symbol
    ]
    if not opens:
        return closed

    owned_client = False
    client = ib
    try:
        need_ib = any(is_ibkr_backed(t) for t in opens)
        if need_ib and client is None:
            try:
                client = IBKRClient(readonly=False)
                await client.connect()
                owned_client = True
            except IBKRUnavailable:
                client = None

        for trade in opens:
            plan = _parse_plan(trade.plan_json)
            trade_symbol = coerce_futures_symbol(trade.symbol)
            trade_cfg = load_mes_5orb_config(trade_symbol)

            direction = trade.direction
            stop = float(plan.get("stop_loss_price") or trade.entry_price)
            lag = int(plan.get("trail_pivot_lag") or trade_cfg.trailing_stop.pivot_lag_bars)
            buffer = float(trade_cfg.trailing_stop.buffer_ticks) * float(trade_cfg.tick_size)

            trail = SwingTrailingStop(
                direction=direction,
                stop=stop,
                pivot_lag=lag,
                buffer=buffer,
            )
            mark = _mes_mark(trade, bars)
            if bars:
                for b in bars[-40:]:
                    changed = trail.update(b)
                    if changed is not None:
                        plan["stop_loss_price"] = trail.stop
                        plan["trail_active"] = True
                        if trade.id:
                            db.update_plan_json(trade.id, plan)

            hit = bool(bars) and trail.hit(bars[-1])
            due_flat = _mes_force_flat_due(trade, trade_cfg)

            if not hit and not due_flat:
                if (
                    client is not None
                    and is_ibkr_backed(trade)
                    and trade.order_ref
                    and plan.get("trail_active")
                    and abs(float(plan.get("stop_loss_price") or 0) - stop) > 1e-9
                ):
                    side = "BUY" if direction is Direction.LONG else "SELL"
                    try:
                        await client.modify_future_stop(
                            trade_symbol,
                            trade.order_ref,
                            stop_price=float(plan["stop_loss_price"]),
                            contracts=trade.contracts,
                            side=side,
                        )
                    except Exception:
                        pass
                continue

            reason = "trailing_stop" if hit else "force_flat"
            exit_px = trail.stop if hit else mark

            if is_ibkr_backed(trade) and client is not None and trade.order_ref:
                side = "BUY" if direction is Direction.LONG else "SELL"
                try:
                    result = await client.close_future_position(
                        trade_symbol,
                        contracts=trade.contracts,
                        side=side,
                        order_ref=trade.order_ref,
                    )
                except IBKRUnavailable:
                    continue
                if not result.get("ok"):
                    continue
                rec = _journal_close(db, trade, exit_px, reason)
                rec["ibkr"] = True
                rec["ibkr_status"] = result.get("status")
                closed.append(rec)
            elif not is_ibkr_backed(trade):
                rec = _journal_close(db, trade, exit_px, reason)
                closed.append(rec)
    finally:
        if owned_client and client is not None:
            await client.disconnect()
    return closed
