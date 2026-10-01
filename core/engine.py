"""Shared trading engine: futures 5ORB break/retest -> bracket -> journal.

Supports CME/CBOT equity-index futures (MES, MNQ, MYM, M2K, ES, NQ).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from core.backtest_mes import evaluate_mes_signal_live
from core.config import get_settings
from core.db import Database
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.journal import (
    _journal_close,
    _parse_plan,
    is_ibkr_backed,
    paper_account_snapshot,
)
from core.marketdata import fetch_bars_with_fallback
from core.models import Direction, Regime, SessionWindow, SpreadType, TradeRecord, TradeStatus
from core.risk import RiskManager
from core.sessions import active_window
from core.mes_active import resolve_mes_5orb_config
from core.strategy.mes_5orb.exits import (
    apply_profit_lock_stop,
    hit_stop,
    hit_target,
    tighten_stop_to_be,
)
from core.strategy.mes_5orb.markets import (
    DEFAULT_FUTURES_SYMBOL,
    coerce_futures_symbol,
)
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import load_mes_5orb_config
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop
from core.timeutils import market_now, utcnow


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
    name = (session_name or "").lower()
    if name in ("asia", "asian", "asia_range"):
        return SessionWindow.ASIA
    if name.startswith("london"):
        return SessionWindow.LONDON
    return SessionWindow.NEW_YORK


def candidate_mes_sessions_now(
    *,
    cfg: Any | None = None,
    now: datetime | None = None,
) -> list[str]:
    """MES sessions the live autotrader should evaluate this cycle.

    Asia Judas and London (or NY) can overlap — both are returned so they run
    in the same cycle rather than one blocking the other.
    """
    from core.timeutils import market_now

    cfg = cfg or load_mes_5orb_config()
    et = to_et(now or market_now())
    t = et.time()
    day = et.date()
    out: list[str] = []

    asia = cfg.asia_range
    if (
        asia.enabled
        and asia.allows_day(day)
        and asia.search_start <= t < asia.force_flat
    ):
        out.append("asia")

    for sess in cfg.active_sessions_at(t):
        if sess.name not in out:
            out.append(sess.name)
    return out


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
    cfg, _chosen = resolve_mes_5orb_config(symbol, db)
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
        w = window.lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "asia": "asia",
            "asian": "asia",
            "asia_range": "asia",
            "london": "london",
            "london_mid": "london_mid",
            "londonmid": "london_mid",
            "new_york": "new_york",
            "newyork": "new_york",
            "ny": "new_york",
            "ny_mid": "ny_mid",
            "nymid": "ny_mid",
            "ny_pm": "ny_pm",
            "nypm": "ny_pm",
            "afternoon": "ny_pm",
        }
        if w in aliases:
            session_name = aliases[w]
        elif "london_mid" in w or w.endswith("_mid") and "london" in w:
            session_name = "london_mid"
        elif "ny_pm" in w or "nypm" in w:
            session_name = "ny_pm"
        elif "ny_mid" in w or "nymid" in w:
            session_name = "ny_mid"
        elif "london" in w:
            session_name = "london"
        elif "new" in w or w.startswith("ny"):
            session_name = "new_york"

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
    settings = get_settings()
    # Live: size off IBKR NetLiquidation. Paper: journal equity curve.
    equity: float
    equity_source = "paper_journal"
    if settings.is_live and not use_synthetic:
        try:
            async with IBKRClient(readonly=True) as ib:
                acct = await ib.account_summary()
            net = acct.get("NetLiquidation")
            if net is not None and float(net) > 0:
                equity = float(net)
                equity_source = "ibkr_net_liquidation"
            else:
                paper = paper_account_snapshot(
                    db,
                    {},
                    starting_capital=settings.starting_capital,
                    environment=rm.environment(),
                )
                equity = float(paper.get("paper_equity") or settings.starting_capital)
                equity_source = "paper_journal_fallback"
        except IBKRUnavailable:
            return {
                "ok": False,
                "reason": "LIVE sizing requires IBKR NetLiquidation; Gateway offline.",
                "signal": {k: v for k, v in signal.items() if k != "mes_plan"},
                "data_source": source,
                "warning": warning,
                "strategy": strategy,
            }
    else:
        paper = paper_account_snapshot(
            db,
            {},
            starting_capital=settings.starting_capital,
            environment=rm.environment(),
        )
        equity = float(paper.get("paper_equity") or settings.starting_capital)

    requested = int(cfg.risk.contracts)
    if settings.is_live:
        requested = min(requested, max(int(settings.live_max_contracts), 1))

    decision = rm.pre_trade_checks_futures(
        stop_points,
        point_value=cfg.point_value,
        requested_contracts=requested,
        max_concurrent=cfg.risk.max_concurrent,
        window=win,
        risk_pct=effective_risk,
        equity=equity,
    )
    if settings.is_live and decision.contracts > settings.live_max_contracts:
        decision.contracts = max(int(settings.live_max_contracts), 0)
        if decision.contracts <= 0:
            decision.approved = False
            decision.reasons.append(
                f"LIVE_MAX_CONTRACTS={settings.live_max_contracts} blocks sizing."
            )
    plan_dict["contracts"] = decision.contracts
    plan_dict["risk_pct"] = effective_risk
    plan_dict["equity_for_sizing"] = round(equity, 2)
    plan_dict["equity_source"] = equity_source
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
        "equity_source": equity_source,
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
    target_price = (
        float(plan_dict["target_price"])
        if plan_dict.get("target_price") is not None
        else None
    )
    # Full-scale targets can sit on the broker as OCA TP; half-scale keeps TP
    # in software so the runner can be managed after the scale.
    scale_frac = float(plan_dict.get("scale_fraction") or 0.5)
    broker_tp = target_price if (target_price is not None and scale_frac >= 0.999) else None

    if simulate:
        if settings.is_live:
            return {"ok": False, "error": "Refusing simulated fills while ACCOUNT_MODE=live."}
        order_ref = f"SIM-{symbol}-{utcnow():%Y%m%d%H%M%S}"
        environment = f"{settings.trading_environment()}(sim)"
        placement: dict[str, Any] = {
            "simulated": True,
            "order_ref": order_ref,
            "avg_fill_price": entry_price,
            "filled_qty": float(contracts),
        }
    else:
        try:
            async with IBKRClient(readonly=False) as ib:
                placement = await ib.place_future_bracket(
                    symbol,
                    side=side,
                    contracts=contracts,
                    stop_price=stop_price,
                    entry_limit=None,
                    take_profit_price=broker_tp,
                    fill_timeout=float(settings.live_fill_timeout_seconds or 15.0),
                )
            if not placement.get("ok"):
                return {
                    "ok": False,
                    "error": placement.get("error", f"{symbol} place failed"),
                    **preview,
                }
            order_ref = placement["order_ref"]
            environment = settings.trading_environment()
            fill_px = placement.get("avg_fill_price")
            if fill_px is not None and float(fill_px) > 0:
                entry_price = float(fill_px)
            fill_qty = placement.get("filled_qty")
            if fill_qty is not None and float(fill_qty) > 0:
                contracts = max(int(round(float(fill_qty))), 1)
                plan_dict["contracts"] = contracts
                stop_points = abs(entry_price - stop_price)
                max_loss = stop_points * point_value * contracts
        except IBKRUnavailable as exc:
            if settings.is_live or settings.trading_environment() != "PAPER":
                return {"ok": False, "error": f"Order placement failed: {exc}"}
            order_ref = f"SIM-{symbol}-{utcnow():%Y%m%d%H%M%S}"
            environment = "PAPER(sim)"
            placement = {
                "simulated": True,
                "order_ref": order_ref,
                "ibkr_error": str(exc),
                "avg_fill_price": entry_price,
                "filled_qty": float(contracts),
            }

    plan_payload = dict(plan_dict)
    plan_payload["entry_model"] = "mes_5orb"
    plan_payload["instrument"] = "future"
    plan_payload["symbol"] = symbol
    plan_payload["point_value"] = point_value
    plan_payload["side"] = side
    plan_payload["stop_mode"] = plan_dict.get("stop_mode") or "or_extreme"
    plan_payload["use_trailing_stop"] = bool(plan_dict.get("use_trailing_stop", True))
    plan_payload["trail_active"] = False
    plan_payload["scaled_out"] = False
    plan_payload["original_stop_loss_price"] = stop_price
    plan_payload["stop_loss_price"] = stop_price
    plan_payload["broker_take_profit"] = broker_tp is not None
    plan_payload["avg_fill_price"] = placement.get("avg_fill_price")
    plan_payload["filled_qty"] = placement.get("filled_qty")
    if plan_dict.get("target_price") is not None:
        plan_payload["target_price"] = float(plan_dict["target_price"])
        plan_payload["target_label"] = str(plan_dict.get("target_label") or "2R")
        plan_payload["scale_fraction"] = float(plan_dict.get("scale_fraction") or 0.5)

    notes = str(plan_dict.get("notes") or f"{symbol} mes_5orb")
    if placement.get("ibkr_error"):
        notes += f" | IBKR unavailable, simulated paper fill: {placement['ibkr_error']}"

    target_r = float(plan_dict.get("target_r") or 2.0)
    max_profit = (
        abs(float(plan_dict["target_price"]) - entry_price) * point_value * contracts
        if plan_dict.get("target_price") is not None
        else 0.0
    )

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
        max_profit=max_profit,
        target_r=target_r,
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
    et = to_et(now or market_now())
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
            trade_cfg, _ = resolve_mes_5orb_config(trade_symbol, db)

            direction = trade.direction
            stop = float(plan.get("stop_loss_price") or trade.entry_price)
            entry_px = float(trade.entry_price)
            target_px = plan.get("target_price")
            target_px_f = float(target_px) if target_px is not None else None
            scale_frac = float(plan.get("scale_fraction") if plan.get("scale_fraction") is not None else trade_cfg.exits.scale_fraction)
            scaled = bool(plan.get("scaled_out"))
            lag = int(plan.get("trail_pivot_lag") or trade_cfg.trailing_stop.pivot_lag_bars)
            buffer = float(trade_cfg.trailing_stop.buffer_ticks) * float(trade_cfg.tick_size)
            use_trail = bool(plan.get("use_trailing_stop", trade_cfg.exits.runner_trail))

            mark = _mes_mark(trade, bars)
            last = bars[-1] if bars else None
            due_flat = _mes_force_flat_due(trade, trade_cfg)
            full_exit_at_target = scale_frac >= 1.0 - 1e-9
            pending_full = bool(plan.get("pending_full_target")) or (
                scaled and full_exit_at_target
            )

            # Soft profit lock tiers (e.g. 75%→35%, 80%→50%, …).
            exits_cfg = trade_cfg.exits
            if target_px_f is not None and last is not None:
                lock_mark = float(last.close)
                tiers = None
                plan_tiers = plan.get("profit_lock_tiers")
                if isinstance(plan_tiers, list) and plan_tiers:
                    parsed: list[tuple[float, float]] = []
                    for item in plan_tiers:
                        try:
                            if isinstance(item, dict):
                                parsed.append(
                                    (
                                        float(item.get("arm")),
                                        float(
                                            item.get(
                                                "lock",
                                                item.get("fraction"),
                                            )
                                        ),
                                    )
                                )
                            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                                parsed.append((float(item[0]), float(item[1])))
                        except (TypeError, ValueError):
                            continue
                    if parsed:
                        tiers = parsed
                new_stop = apply_profit_lock_stop(
                    direction,
                    entry_px,
                    target_px_f,
                    lock_mark,
                    stop,
                    arm_fraction=float(
                        plan.get("profit_lock_arm", exits_cfg.profit_lock_arm)
                    ),
                    lock_fraction=float(
                        plan.get(
                            "profit_lock_fraction", exits_cfg.profit_lock_fraction
                        )
                    ),
                    tiers=tiers if tiers else exits_cfg.profit_lock_tiers,
                )
                if abs(new_stop - stop) > 1e-9:
                    stop = new_stop
                    plan["stop_loss_price"] = stop
                    plan["profit_lock_armed"] = True
                    if trade.id:
                        db.update_plan_json(trade.id, plan)

            async def _close_full_target(exit_px: float, reason: str) -> bool:
                """Close at target price. Returns True if journaled closed."""
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
                        return False
                    if not result.get("ok"):
                        return False
                    rec = _journal_close(db, trade, exit_px, reason)
                    rec["ibkr"] = True
                    rec["ibkr_status"] = result.get("status")
                    closed.append(rec)
                    return True
                if not is_ibkr_backed(trade):
                    closed.append(
                        _journal_close(db, trade, exit_px, reason)
                    )
                    return True
                return False

            def _mark_pending_target(fill: float) -> None:
                plan["pending_full_target"] = True
                plan["scaled_out"] = True
                plan["scale_fill_price"] = fill
                # Never park a full-TP trade on breakeven — keep protective OR stop.
                if plan.get("original_stop_loss_price") is not None:
                    plan["stop_loss_price"] = float(plan["original_stop_loss_price"])
                if trade.id:
                    db.update_plan_json(trade.id, plan)

            # Recovery / pending full TP: always finish at target fill, never BE.
            if pending_full:
                fill = plan.get("scale_fill_price")
                if fill is None and target_px_f is not None:
                    fill = target_px_f
                if fill is not None:
                    reason = f"target_{plan.get('target_label') or '2R'}"
                    ok = await _close_full_target(float(fill), reason)
                    if ok:
                        continue
                    _mark_pending_target(float(fill))
                    continue

            # Primary 2R / HOD hit.
            if (
                last is not None
                and not scaled
                and target_px_f is not None
                and hit_target(direction, last, target_px_f)
            ):
                if full_exit_at_target:
                    reason = f"target_{plan.get('target_label') or '2R'}"
                    exit_px = target_px_f
                    ok = await _close_full_target(exit_px, reason)
                    if ok:
                        plan["scaled_out"] = True
                        plan["scale_fill_price"] = target_px_f
                        plan["pending_full_target"] = False
                        if trade.id:
                            db.update_plan_json(trade.id, plan)
                        continue
                    _mark_pending_target(target_px_f)
                    continue

                # Partial scale: take marker, move stop to BE (never loosen), trail runner.
                plan["scaled_out"] = True
                plan["scale_fill_price"] = target_px_f
                if trade_cfg.exits.move_stop_to_be:
                    stop = tighten_stop_to_be(direction, entry_px, stop)
                    plan["stop_loss_price"] = stop
                plan["trail_active"] = use_trail
                if trade.id:
                    db.update_plan_json(trade.id, plan)
                scaled = True

            # Full-TP trades must not be managed via BE / trail after a target tag.
            if full_exit_at_target and (scaled or plan.get("pending_full_target")):
                continue

            stop = float(plan.get("stop_loss_price") or stop)
            trail = SwingTrailingStop(
                direction=direction,
                stop=stop,
                pivot_lag=lag,
                buffer=buffer,
            )
            if bars and scaled and use_trail:
                for b in bars[-40:]:
                    changed = trail.update(b)
                    if changed is not None:
                        plan["stop_loss_price"] = trail.stop
                        plan["trail_active"] = True
                        if trade.id:
                            db.update_plan_json(trade.id, plan)
                stop = trail.stop

            hit = bool(last) and (
                hit_stop(direction, last, stop)
                or (scaled and use_trail and trail.hit(last))
            )

            if not hit and not due_flat:
                if (
                    client is not None
                    and is_ibkr_backed(trade)
                    and trade.order_ref
                    and plan.get("trail_active")
                    and abs(
                        float(plan.get("stop_loss_price") or 0)
                        - float(plan.get("original_stop_loss_price") or stop)
                    )
                    > 1e-9
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

            if hit:
                if not scaled:
                    reason = "or_stop"
                elif abs(stop - entry_px) < float(trade_cfg.tick_size):
                    reason = "breakeven_stop"
                else:
                    reason = "trailing_stop"
                exit_px = stop
            else:
                reason = "force_flat"
                exit_px = mark

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


async def reconcile_open_futures_vs_ibkr(
    db: Database,
    *,
    client: Optional[IBKRClient] = None,
) -> dict[str, Any]:
    """Compare open futures trades in the journal vs IBKR positions.

    Does not auto-close or flatten. Returns a report for ops / Discord.
    """
    owned_client = client is None
    if client is None:
        client = IBKRClient()
    try:
        await client.connect()
    except IBKRUnavailable as exc:
        return {"ok": False, "error": str(exc), "mismatches": []}

    mismatches: list[dict[str, Any]] = []
    try:
        open_trades = [
            t
            for t in db.query_trades(status=TradeStatus.OPEN, limit=200)
            if is_ibkr_backed(t)
        ]
        by_symbol: dict[str, list[TradeRecord]] = {}
        for trade in open_trades:
            if not trade.contracts:
                continue
            plan = trade.plan_json or {}
            if str(plan.get("instrument") or "").lower() != "future":
                continue
            sym = coerce_futures_symbol(str(plan.get("symbol") or trade.symbol or ""))
            by_symbol.setdefault(sym, []).append(trade)

        for symbol, trades in by_symbol.items():
            try:
                broker_qty = await client.futures_position_qty(symbol)
            except Exception as exc:  # noqa: BLE001
                mismatches.append(
                    {
                        "symbol": symbol,
                        "error": str(exc),
                        "journal_net": None,
                        "broker_qty": None,
                    }
                )
                continue

            journal_net = 0
            for trade in trades:
                side = str((trade.plan_json or {}).get("side") or "").upper()
                qty = int(trade.contracts or 0)
                if side == "SELL":
                    journal_net -= qty
                else:
                    journal_net += qty

            if int(broker_qty) != int(journal_net):
                mismatches.append(
                    {
                        "symbol": symbol,
                        "journal_net": journal_net,
                        "broker_qty": int(broker_qty),
                        "open_trade_ids": [t.id for t in trades if t.id],
                        "delta": int(broker_qty) - int(journal_net),
                    }
                )
    finally:
        if owned_client:
            await client.disconnect()

    return {
        "ok": True,
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
    }
