"""Shared trading engine: signal -> chain -> risk-sized spread -> placement.

This is the single source of truth for building and placing a defined-risk
vertical from the current ORB signal. Both the execution MCP server (LLM-driven)
and the dashboard auto-trader import these functions so behaviour is identical.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.active_params import resolve_trading_config, strategy_payload
from core.config import get_settings
from core.db import Database
from core.ibkr_client import IBKRClient, IBKRUnavailable, atm_strike_grid, chain_is_usable
from core.journal import (
    _journal_close,
    _parse_plan,
    _structure_value,
    close_open_paper_trades,
    is_ibkr_backed,
    legs_from_plan,
)
from core.marketdata import fetch_bars_with_fallback, synthetic_chain
from core.models import Regime, SessionWindow, SpreadPlan, TradeRecord, TradeStatus
from core.risk import RiskManager
from core.sessions import active_window, bars_in_window, get_window_config
from core.strategy.orb import compute_orb_signal
from core.strategy.spreads import build_spread, per_contract_max_loss
from core.timeutils import utcnow


def resolve_window(window: str) -> SessionWindow:
    """Map a window name (or 'auto'/'active'/'current') to a SessionWindow."""
    if window.lower() in ("auto", "active", "current"):
        return active_window() or SessionWindow.NEW_YORK
    return SessionWindow(window.lower())


def days_to_expiry(expiry: str) -> float:
    if not expiry or len(expiry) < 8:
        return 7.0
    try:
        exp = datetime.strptime(expiry[:8], "%Y%m%d")
    except ValueError:
        return 7.0
    return max((exp - utcnow()).total_seconds() / 86400.0, 0.5)


async def build_trade_plan(
    symbol: str,
    window: str,
    *,
    use_synthetic: bool = False,
    target_r: float | None = None,
    synthetic_seed: int = 42,
    db: Database | None = None,
) -> dict[str, Any]:
    """Signal -> option chain -> spread -> risk sizing. Places no order."""
    win = resolve_window(window)
    db = db or Database()
    cfg, opt_run = resolve_trading_config(symbol, win, db)
    target_r = target_r if target_r is not None else cfg.target_r
    strategy = strategy_payload(cfg, opt_run)

    try:
        bars, source, warning = await fetch_bars_with_fallback(
            symbol, duration="3 D", bar_size="5 mins",
            use_synthetic=use_synthetic, synthetic_seed=synthetic_seed,
        )
    except IBKRUnavailable as exc:
        return {"ok": False, "error": str(exc), "hint": "Pass use_synthetic=true for a demo."}

    signal = compute_orb_signal(symbol, win, bars, cfg)
    if not signal.breakout:
        if signal.range_high <= 0 and signal.range_low <= 0:
            reason = (
                f"No opening-range bars for {win.value} "
                f"(as of {signal.as_of}). Waiting for session data."
            )
        else:
            reason = (
                f"No breakout in {win.value}: last {signal.last_price:.2f} "
                f"inside OR [{signal.range_low:.2f}, {signal.range_high:.2f}] "
                f"(need close beyond the range + buffer)."
            )
        return {
            "ok": False,
            "reason": reason,
            "signal": signal.model_dump(mode="json"),
            "data_source": source,
            "strategy": strategy,
        }

    chain: dict[str, Any]
    chain_source = source
    chain_warning = warning
    if source == "synthetic":
        chain = synthetic_chain(signal.last_price)
    else:
        try:
            async with IBKRClient() as ib:
                raw = await ib.option_chain(symbol, spot_hint=signal.last_price)
                expiry = raw["expiries"][0] if raw.get("expiries") else ""
                iv = await ib.atm_iv(symbol, expiry) if expiry else None
            spot = raw.get("spot") or signal.last_price
            strikes = list(raw.get("strikes") or [])
            if not chain_is_usable(spot, strikes):
                strikes = atm_strike_grid(spot)
                extra = (
                    f"IBKR chain {raw.get('trading_class')} strikes unusable "
                    f"near {spot:.2f}; using ATM $1 grid."
                )
                chain_warning = f"{chain_warning}; {extra}" if chain_warning else extra
            chain = {
                "spot": spot,
                "strikes": strikes,
                "expiry": expiry,
                "days_to_expiry": days_to_expiry(expiry),
                "iv": iv or 0.25,
                "multiplier": int(raw.get("multiplier") or 100),
            }
            chain_source = "ibkr"
        except IBKRUnavailable:
            chain = synthetic_chain(signal.last_price)
            chain_source = "synthetic"

    preview_one = build_spread(
        signal,
        spot=chain["spot"],
        expiry=chain["expiry"],
        days_to_expiry=chain["days_to_expiry"],
        iv=chain["iv"],
        strikes=chain["strikes"],
        target_r=target_r,
        stop_r=cfg.stop_r,
        contracts=1,
    )
    if preview_one is None:
        return {
            "ok": False,
            "reason": (
                "Could not construct a valid spread from the chain "
                f"(spot={chain.get('spot')}, expiry={chain.get('expiry')}, "
                f"strikes={chain.get('strikes')})."
            ),
            "signal": signal.model_dump(mode="json"),
            "strategy": strategy,
        }

    rm = RiskManager(db=db)
    per_loss = per_contract_max_loss(preview_one)
    decision = rm.pre_trade_checks(per_loss, window=win)

    plan = build_spread(
        signal,
        spot=chain["spot"],
        expiry=chain["expiry"],
        days_to_expiry=chain["days_to_expiry"],
        iv=chain["iv"],
        strikes=chain["strikes"],
        target_r=target_r,
        stop_r=cfg.stop_r,
        contracts=max(decision.contracts, 0),
    )

    return {
        "ok": True,
        "environment": rm.environment(),
        "data_source": chain_source,
        "warning": chain_warning,
        "signal": signal.model_dump(mode="json"),
        "plan": (plan or preview_one).model_dump(mode="json"),
        "per_contract_max_loss_usd": round(per_loss, 2),
        "risk": decision.as_dict(),
        "tradeable": decision.approved,
        "strategy": strategy,
        "iv": chain["iv"],
    }


async def place_trade_plan(
    preview: dict[str, Any],
    *,
    symbol: str,
    window: str,
    use_synthetic: bool = False,
    db: Database | None = None,
) -> dict[str, Any]:
    """Place a previously built (and approved) plan; record it to the journal.

    In synthetic mode - or when the data was synthetic - the fill is simulated.
    Otherwise the combo order is routed to IBKR (paper by default).
    """
    db = db or Database()
    if not preview.get("ok"):
        return preview
    if not preview.get("tradeable"):
        return {"ok": False, "error": "Risk checks failed.", **preview}

    plan_dict = preview["plan"]
    plan = SpreadPlan(**plan_dict)
    win = resolve_window(window)
    settings = get_settings()
    regime = Regime(preview["signal"]["regime"])

    simulate = preview["data_source"] == "synthetic" or use_synthetic
    if simulate:
        order_ref = f"SIM-{symbol}-{utcnow():%Y%m%d%H%M%S}"
        environment = f"{settings.trading_environment()}(sim)"
        placement: dict[str, Any] = {"simulated": True, "order_ref": order_ref}
    else:
        try:
            async with IBKRClient() as ib:
                placement = await ib.place_spread_order(
                    symbol,
                    plan.long_leg,
                    plan.short_leg,
                    plan.contracts,
                    limit_price=plan.net_debit,
                    take_profit_price=plan.take_profit_price,
                    stop_loss_price=plan.stop_loss_price,
                )
            order_ref = placement["order_ref"]
            environment = settings.trading_environment()
        except IBKRUnavailable as exc:
            # Paper journal should still record the fill so research has a sample.
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
    plan_payload["iv"] = preview.get("iv", 0.25)
    record = TradeRecord(
        environment=environment,
        symbol=symbol,
        window=win,
        regime=regime,
        spread_type=plan.spread_type,
        direction=plan.direction,
        contracts=plan.contracts,
        entry_price=plan.net_debit,
        max_loss=plan.max_loss,
        max_profit=plan.max_profit,
        target_r=plan.target_r,
        status=TradeStatus.OPEN,
        signal_strength=preview["signal"]["strength"],
        order_ref=order_ref,
        notes=plan.rationale + (
            f" | IBKR unavailable, simulated paper fill: {placement.get('ibkr_error')}"
            if placement.get("ibkr_error")
            else ""
        ),
        plan_json=json.dumps(plan_payload),
    )
    trade_id = db.insert_trade(record)

    return {
        "ok": True,
        "trade_id": trade_id,
        "environment": environment,
        "placement": placement,
        "plan": plan_dict,
    }


def _session_end_mark(trade: TradeRecord, bars: list) -> float:
    plan = _parse_plan(trade.plan_json)
    cfg = get_window_config(trade.window)
    path = bars_in_window(bars, cfg) or bars
    if not path:
        return trade.entry_price
    return _structure_value(trade, plan, path[-1].close)


async def settle_session_exits(
    db: Database,
    symbol: str,
    bars: list,
    *,
    now_window: SessionWindow | None = None,
    ib: Any = None,
) -> list[dict[str, Any]]:
    """Close journal-only trades and flatten IBKR combos whose session has ended.

    Take-profit / stop-loss on IBKR-backed trades stay with the broker OCA group.
    When that window is no longer active, this cancels remaining exits and
    market-sells the combo, then records the journal close. If IBKR rejects
    (market closed, disconnect), the row stays open and the next cycle retries.
    """
    closed = close_open_paper_trades(db, symbol, bars, now_window=now_window)
    live = now_window if now_window is not None else active_window()
    ibkr_due = [
        t
        for t in db.query_trades(status=TradeStatus.OPEN, limit=1000)
        if t.symbol.upper() == symbol.upper()
        and is_ibkr_backed(t)
        and live is not t.window
    ]
    if not ibkr_due:
        return closed

    owned_client = False
    client = ib
    try:
        if client is None:
            client = IBKRClient()
            await client.connect()
            owned_client = True
        for trade in ibkr_due:
            plan = _parse_plan(trade.plan_json)
            legs = legs_from_plan(plan)
            if legs is None or not trade.order_ref:
                continue
            long_leg, short_leg = legs
            try:
                result = await client.close_spread_order(
                    trade.symbol,
                    long_leg,
                    short_leg,
                    trade.contracts,
                    trade.order_ref,
                )
            except IBKRUnavailable:
                continue
            if not result.get("ok"):
                continue
            rec = _journal_close(db, trade, _session_end_mark(trade, bars), "session_end")
            rec["ibkr"] = True
            rec["ibkr_status"] = result.get("status")
            rec["already_flat"] = bool(result.get("already_flat"))
            closed.append(rec)
    finally:
        if owned_client and client is not None:
            await client.disconnect()
    return closed
