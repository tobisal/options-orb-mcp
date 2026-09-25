"""Mark-to-market and close paper journal trades.

The auto-trader places entries; Interactive Brokers may also attach TP/SL
orders. This module is what actually *finishes* a paper trade in SQLite so the
research journal gets realised P&L (take-profit, stop-loss, or session end).
IBKR-backed live/paper fills are flattened at the broker in ``settle_session_exits``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.db import Database
from core.models import Bar, OptionLeg, OptionRight, SessionWindow, TradeRecord, TradeStatus
from core.pricing import vertical_spread_value
from core.sessions import active_window, bars_in_window, get_window_config
from core.strategy.spreads import CONTRACT_MULTIPLIER
from core.timeutils import utcnow


def mark_open_trade(trade: TradeRecord, spot: float) -> dict[str, Any]:
    """Mark an open trade vs the current underlying print."""
    plan = _parse_plan(trade.plan_json)
    if plan.get("instrument") == "future" or plan.get("entry_model") == "mes_5orb":
        point_value = float(plan.get("point_value") or 5.0)
        if trade.direction.value == "long":
            points = spot - trade.entry_price
        else:
            points = trade.entry_price - spot
        pnl = round(points * point_value * trade.contracts, 2)
        mark = spot
    else:
        mark = _structure_value(trade, plan, spot)
        pnl = round(
            (mark - trade.entry_price) * CONTRACT_MULTIPLIER * trade.contracts, 2
        )
    tp = _num(plan.get("take_profit_price"))
    sl = _num(plan.get("stop_loss_price"))
    progress: float | None = None
    if tp is not None and sl is not None and tp != sl:
        progress = max(0.0, min(1.0, (mark - sl) / (tp - sl)))
    return {
        "spot": round(spot, 4),
        "mark": round(mark, 4),
        "unrealized_pnl": pnl,
        "take_profit_price": tp,
        "stop_loss_price": sl,
        "progress_to_tp": round(progress, 3) if progress is not None else None,
    }


def paper_account_snapshot(
    db: Database,
    spots: dict[str, float],
    *,
    starting_capital: float,
    environment: str = "PAPER",
) -> dict[str, Any]:
    """Paper equity: starting capital + closed P&L + open mark-to-market."""
    opens = db.query_trades(status=TradeStatus.OPEN, environment=environment, limit=1000)
    unreal = 0.0
    for trade in opens:
        spot = spots.get(trade.symbol.upper())
        if spot is None:
            continue
        unreal += mark_open_trade(trade, spot)["unrealized_pnl"]
    lifetime = db.realised_pnl(environment=environment)
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_realised = db.realised_pnl_since(start_of_day, environment=environment)
    unreal = round(unreal, 2)
    lifetime = round(lifetime, 2)
    daily_realised = round(daily_realised, 2)
    return {
        "lifetime_realised_pnl": lifetime,
        "open_unrealized_pnl": unreal,
        "daily_realised_pnl": daily_realised,
        "daily_pnl": round(daily_realised + unreal, 2),
        "paper_equity": round(starting_capital + lifetime + unreal, 2),
        "open_count": len(opens),
    }


def evaluate_exit(
    trade: TradeRecord,
    bars: list[Bar],
    *,
    window_live: bool,
) -> tuple[float, str] | None:
    """Return ``(exit_price, reason)`` if this open trade should close now."""
    if not bars:
        return None
    plan = _parse_plan(trade.plan_json)
    spot = bars[-1].close
    value = _structure_value(trade, plan, spot)
    tp = _num(plan.get("take_profit_price"))
    sl = _num(plan.get("stop_loss_price"))
    if tp is not None and value >= tp:
        return tp, "take_profit"
    if sl is not None and value <= sl:
        return sl, "stop_loss"
    if not window_live:
        return value, "session_end"
    return None


def is_ibkr_backed(trade: TradeRecord) -> bool:
    """True when this journal row was routed to IBKR (not a SIM- fill)."""
    ref = (trade.order_ref or "").strip()
    return bool(ref) and not ref.upper().startswith("SIM-")


def legs_from_plan(plan: dict[str, Any]) -> tuple[OptionLeg, OptionLeg] | None:
    """Rebuild long/short legs from a stored spread plan, or None if incomplete."""
    expiry = str(plan.get("expiry") or "")
    long_raw = plan.get("long_leg") or {}
    short_raw = plan.get("short_leg") or {}
    try:
        long_leg = OptionLeg(
            right=OptionRight(str(long_raw.get("right") or "C")),
            strike=float(long_raw["strike"]),
            expiry=str(long_raw.get("expiry") or expiry),
            action=str(long_raw.get("action") or "BUY"),
            quantity=int(long_raw.get("quantity") or 1),
        )
        short_leg = OptionLeg(
            right=OptionRight(str(short_raw.get("right") or "C")),
            strike=float(short_raw["strike"]),
            expiry=str(short_raw.get("expiry") or expiry),
            action=str(short_raw.get("action") or "SELL"),
            quantity=int(short_raw.get("quantity") or 1),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not long_leg.expiry or not short_leg.expiry:
        return None
    return long_leg, short_leg


def _journal_close(
    db: Database, trade: TradeRecord, exit_price: float, reason: str
) -> dict[str, Any]:
    plan = _parse_plan(trade.plan_json)
    if plan.get("instrument") == "future" or plan.get("entry_model") == "mes_5orb":
        point_value = float(plan.get("point_value") or 5.0)
        if trade.direction.value == "long":
            points = exit_price - trade.entry_price
        else:
            points = trade.entry_price - exit_price
        pnl = round(points * point_value * trade.contracts, 2)
    else:
        pnl = round(
            (exit_price - trade.entry_price) * CONTRACT_MULTIPLIER * trade.contracts, 2
        )
    db.close_trade(trade.id or 0, exit_price=exit_price, pnl=pnl)
    notes = (trade.notes or "").strip()
    extra = f"exit={reason}"
    if trade.id:
        db_update_notes = f"{notes} | {extra}" if notes else extra
        _append_exit_note(db, trade.id, db_update_notes)
    return {
        "trade_id": trade.id,
        "reason": reason,
        "exit_price": round(exit_price, 4),
        "pnl": pnl,
        "window": trade.window.value,
        "ibkr": False,
    }


def close_open_paper_trades(
    db: Database,
    symbol: str,
    bars: list[Bar],
    *,
    now_window: SessionWindow | None = None,
) -> list[dict[str, Any]]:
    """Close qualifying open journal trades that were never sent to IBKR.

    IBKR-backed rows (live or paper fills with an ``ORB-`` order ref) are left
    alone here; ``settle_session_exits`` flattens those at the broker first.
    """
    live = now_window if now_window is not None else active_window()
    closed: list[dict[str, Any]] = []
    opens = [
        t
        for t in db.query_trades(status=TradeStatus.OPEN, limit=1000)
        if t.symbol.upper() == symbol.upper() and not is_ibkr_backed(t)
    ]
    for trade in opens:
        window_live = live is trade.window
        cfg = get_window_config(trade.window)
        in_win = bars_in_window(bars, cfg)
        path = in_win or bars
        decision = evaluate_exit(trade, path, window_live=window_live)
        if decision is None:
            continue
        exit_price, reason = decision
        closed.append(_journal_close(db, trade, exit_price, reason))
    return closed


def _append_exit_note(db: Database, trade_id: int, notes: str) -> None:
    with db._conn() as conn:
        conn.execute("UPDATE trades SET notes = ? WHERE id = ?", (notes, trade_id))


def _parse_plan(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _structure_value(trade: TradeRecord, plan: dict[str, Any], spot: float) -> float:
    long_leg = plan.get("long_leg") or {}
    short_leg = plan.get("short_leg") or {}
    try:
        long_k = float(long_leg["strike"])
        short_k = float(short_leg["strike"])
        right = OptionRight(str(long_leg.get("right") or "C"))
    except (KeyError, TypeError, ValueError):
        return trade.entry_price
    expiry = str(plan.get("expiry") or long_leg.get("expiry") or "")
    t = _days_to_expiry(expiry) / 365.0
    iv = _num(plan.get("iv")) or 0.25
    try:
        return vertical_spread_value(spot, long_k, short_k, t, iv, right)
    except (ValueError, ZeroDivisionError):
        return trade.entry_price


def _days_to_expiry(expiry: str) -> float:
    if not expiry or len(expiry) < 8:
        return 7.0
    try:
        exp = datetime.strptime(expiry[:8], "%Y%m%d")
    except ValueError:
        return 7.0
    return max((exp - utcnow()).total_seconds() / 86400.0, 0.5)
