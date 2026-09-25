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
    apply_trailing_to_trade,
    close_open_paper_trades,
    evaluate_exit,
    is_ibkr_backed,
    legs_from_plan,
)
from core.marketdata import fetch_bars_with_fallback, synthetic_chain
from core.models import Regime, SessionWindow, SpreadPlan, TradeRecord, TradeStatus
from core.ops_log import emit as ops_emit
from core.risk import RiskManager
from core.sessions import active_window, bars_in_window, get_window_config
from core.strategy.orb import compute_orb_signal
from core.strategy.overnight import (
    compute_overnight_signal,
    load_overnight_config,
    overnight_entry_window_active,
)
from core.strategy.power_hour_gamma import (
    compute_power_hour_signal,
    load_power_hour_config,
    power_hour_active_now,
)
from core.strategy.tokyo_range import (
    compute_tokyo_range_signal,
    load_tokyo_range_config,
    tokyo_entry_window_active,
)
from core.strategy.multi_pack import MultiPackConfig, compute_multi_pack_signal
from core.strategy.spreads import build_spread, per_contract_max_loss
from core.timeutils import utcnow
from core.trail import plan_uses_trailing, risk_share_from_plan


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
    entry_strategy: str | None = None,
) -> dict[str, Any]:
    """Signal -> option chain -> spread -> risk sizing. Places no order.

    ``entry_strategy``: ``auto`` / ``overnight`` / ``orb`` / ``tokyo_range`` /
    ``power_hour_gamma`` / ``multi_pack``.
    """
    win = resolve_window(window)
    db = db or Database()
    cfg, opt_run = resolve_trading_config(symbol, win, db)
    settings = get_settings()
    mode = (entry_strategy or settings.entry_strategy or "auto").lower().strip()
    strategy = strategy_payload(cfg, opt_run)

    try:
        bars, source, warning = await fetch_bars_with_fallback(
            symbol, duration="3 D", bar_size="5 mins",
            use_synthetic=use_synthetic, synthetic_seed=synthetic_seed,
        )
    except IBKRUnavailable as exc:
        return {"ok": False, "error": str(exc), "hint": "Pass use_synthetic=true for a demo."}

    ph_cfg = load_power_hour_config(symbol)
    tk_cfg = load_tokyo_range_config(symbol)
    on_cfg = load_overnight_config()
    try:
        from core.weekly_hunter import load_weekly_hunter_config

        wh = load_weekly_hunter_config()
        wh_on = settings.weekly_hunter_enabled and wh.enabled
        use_multi = bool(getattr(wh, "use_multi_pack", False)) if wh_on else False
        use_overnight = bool(getattr(wh, "use_overnight", False)) if wh_on else False
    except Exception:
        wh_on, wh, use_multi, use_overnight = False, None, False, False

    if mode == "multi_pack":
        use_multi = True
    if mode == "overnight":
        use_overnight = True

    # Overnight-only playbook: never fall through to ORB / multi_pack.
    overnight_only = use_overnight and mode in ("auto", "overnight")

    signal = None
    entry_model = "orb"
    stop_r = cfg.stop_r
    resolved_target_r = target_r if target_r is not None else cfg.target_r
    range_width = 0.0
    pack_cfg = MultiPackConfig()

    use_on = (
        signal is None
        and mode in ("auto", "overnight")
        and settings.overnight_enabled
        and on_cfg.enabled
        and (overnight_only or overnight_entry_window_active(cfg=on_cfg) or mode == "overnight")
    )
    in_overnight = overnight_entry_window_active(cfg=on_cfg)
    if use_on and (mode == "overnight" or overnight_only or in_overnight):
        on_signal = compute_overnight_signal(symbol, bars, cfg=on_cfg)
        if on_signal.breakout:
            signal = on_signal
            entry_model = "overnight"
            resolved_target_r = on_cfg.target_r if target_r is None else target_r
            stop_r = on_cfg.stop_r
            win = SessionWindow.NEW_YORK
        elif mode == "overnight" or overnight_only or in_overnight:
            return {
                "ok": False,
                "reason": on_signal.notes or "overnight idle",
                "signal": on_signal.model_dump(mode="json"),
                "data_source": source,
                "strategy": {**strategy, "entry_model": "overnight"},
            }

    if overnight_only:
        # Already handled (or returned idle). Do not try ORB / packs.
        if signal is None:
            return {
                "ok": False,
                "reason": "overnight playbook: no entry outside 15:45-16:00 ET",
                "signal": None,
                "data_source": source,
                "strategy": {**strategy, "entry_model": "overnight"},
            }
    elif use_multi and mode in ("auto", "multi_pack"):
        mp = compute_multi_pack_signal(symbol, bars, cfg=pack_cfg)
        if mp.breakout:
            signal = mp
            entry_model = "multi_pack"
            resolved_target_r = pack_cfg.target_r if target_r is None else target_r
            stop_r = pack_cfg.stop_r
            win = SessionWindow.NEW_YORK
        elif mode == "multi_pack":
            return {
                "ok": False,
                "reason": mp.notes or "multi_pack idle",
                "signal": mp.model_dump(mode="json"),
                "data_source": source,
                "strategy": {**strategy, "entry_model": "multi_pack"},
            }

    use_ph = (
        signal is None
        and not overnight_only
        and mode in ("auto", "power_hour_gamma")
        and settings.power_hour_gamma_enabled
        and ph_cfg.enabled
        and (not wh_on or wh.enable_power_hour)
        and not use_multi  # multi_pack already includes power-hour leg
    )
    in_power_hour = power_hour_active_now(cfg=ph_cfg)
    if use_ph and (mode == "power_hour_gamma" or in_power_hour):
        ph_signal = compute_power_hour_signal(symbol, bars, cfg=ph_cfg, window=win)
        if ph_signal.breakout:
            signal = ph_signal
            entry_model = "power_hour_gamma"
            resolved_target_r = ph_cfg.target_r if target_r is None else target_r
            stop_r = ph_cfg.stop_r
        elif mode == "power_hour_gamma" or in_power_hour:
            return {
                "ok": False,
                "reason": ph_signal.notes or "No power-hour gamma entry yet.",
                "signal": ph_signal.model_dump(mode="json"),
                "data_source": source,
                "strategy": {**strategy, "entry_model": "power_hour_gamma"},
            }

    use_tk = (
        signal is None
        and not overnight_only
        and mode in ("auto", "tokyo_range")
        and settings.tokyo_range_enabled
        and tk_cfg.enabled
        and (not wh_on or wh.enable_tokyo_range)
    )
    in_tokyo = tokyo_entry_window_active(cfg=tk_cfg)
    if use_tk and (mode == "tokyo_range" or in_tokyo):
        tk_signal = compute_tokyo_range_signal(symbol, bars, cfg=tk_cfg, window=SessionWindow.LONDON)
        if tk_signal.breakout:
            signal = tk_signal
            entry_model = "tokyo_range"
            # Un-buffered raw width for R maths.
            range_width = max(
                (tk_signal.range_high - tk_cfg.buffer) - (tk_signal.range_low + tk_cfg.buffer),
                1e-9,
            )
            resolved_target_r = tk_cfg.target_r(range_width) if target_r is None else target_r
            stop_r = tk_cfg.stop_r(range_width)
            win = SessionWindow.LONDON
        elif mode == "tokyo_range" or in_tokyo:
            return {
                "ok": False,
                "reason": tk_signal.notes or "No Tokyo range breakout yet.",
                "signal": tk_signal.model_dump(mode="json"),
                "data_source": source,
                "strategy": {**strategy, "entry_model": "tokyo_range"},
            }

    if signal is None and not overnight_only and mode in ("auto", "orb"):
        signal = compute_orb_signal(symbol, win, bars, cfg)
        entry_model = "orb"
        resolved_target_r = target_r if target_r is not None else cfg.target_r
        stop_r = cfg.stop_r

    if signal is None or not signal.breakout:
        if signal is None:
            reason = f"Entry strategy '{mode}' produced no signal."
            sig_dump = None
        elif signal.range_high <= 0 and signal.range_low <= 0:
            reason = (
                f"No opening-range bars for {win.value} "
                f"(as of {signal.as_of}). Waiting for session data."
            )
            sig_dump = signal.model_dump(mode="json")
        else:
            reason = (
                f"No breakout in {win.value}: last {signal.last_price:.2f} "
                f"inside OR [{signal.range_low:.2f}, {signal.range_high:.2f}] "
                f"(need close beyond the range + buffer)."
            )
            sig_dump = signal.model_dump(mode="json")
        return {
            "ok": False,
            "reason": reason,
            "signal": sig_dump,
            "data_source": source,
            "strategy": {**strategy, "entry_model": entry_model},
        }

    strategy = {**strategy, "entry_model": entry_model}
    # Trailing is on for every entry model (window defaults + global kill-switch).
    use_trail = cfg.use_trailing_stop
    trail_act = cfg.trail_activate_r
    trail_dist = cfg.trail_distance_r
    if entry_model == "overnight":
        use_trail = on_cfg.use_trailing_stop
        trail_act = on_cfg.trail_activate_r
        trail_dist = on_cfg.trail_distance_r
        strategy = {
            **strategy,
            "entry_after_et": on_cfg.entry_after_et.strftime("%H:%M"),
            "flatten_after_et": on_cfg.flatten_after_et.strftime("%H:%M"),
            "target_r": resolved_target_r,
            "stop_r": stop_r,
            "use_trailing_stop": use_trail,
            "trail_activate_r": trail_act,
            "trail_distance_r": trail_dist,
        }
    elif entry_model == "power_hour_gamma":
        strategy = {
            **strategy,
            "break_points": ph_cfg.break_points,
            "stop_points": ph_cfg.stop_points,
            "target_points": ph_cfg.target_points,
            "require_negative_gamma": ph_cfg.require_negative_gamma,
            "use_trailing_stop": use_trail,
            "trail_activate_r": trail_act,
            "trail_distance_r": trail_dist,
        }
    elif entry_model == "tokyo_range":
        strategy = {
            **strategy,
            "buffer": tk_cfg.buffer,
            "target_range_mult": tk_cfg.target_range_mult,
            "range_width": round(range_width, 4),
            "entry_until_gmt": tk_cfg.entry_until_gmt.strftime("%H:%M"),
            "flatten_gmt": tk_cfg.flatten_gmt.strftime("%H:%M"),
            "use_trailing_stop": use_trail,
            "trail_activate_r": trail_act,
            "trail_distance_r": trail_dist,
        }
    elif entry_model == "multi_pack":
        strategy = {
            **strategy,
            "pack": "gap_fade+orb_5m+power_hour",
            "target_r": resolved_target_r,
            "stop_r": stop_r,
            "use_trailing_stop": use_trail,
            "trail_activate_r": trail_act,
            "trail_distance_r": trail_dist,
        }
    else:
        strategy = {
            **strategy,
            "use_trailing_stop": use_trail,
            "trail_activate_r": trail_act,
            "trail_distance_r": trail_dist,
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
        target_r=resolved_target_r,
        stop_r=stop_r,
        contracts=1,
        use_trailing_stop=use_trail,
        trail_activate_r=trail_act,
        trail_distance_r=trail_dist,
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
        target_r=resolved_target_r,
        stop_r=stop_r,
        contracts=max(decision.contracts, 0),
        use_trailing_stop=use_trail,
        trail_activate_r=trail_act,
        trail_distance_r=trail_dist,
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
        "entry_model": entry_model,
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
            async with IBKRClient(readonly=False) as ib:
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


def _infer_flat_reason(plan: dict[str, Any], mark: float, entry: float) -> str:
    if plan.get("trail_active"):
        return "trailing_stop"
    tp = plan.get("take_profit_price")
    sl = plan.get("stop_loss_price")
    try:
        if tp is not None and mark >= float(tp) - 1e-6:
            return "take_profit"
        if sl is not None and mark <= float(sl) + 1e-6:
            return "stop_loss"
    except (TypeError, ValueError):
        pass
    if mark >= entry:
        return "take_profit"
    return "already_flat"


async def manage_open_ibkr_exits(
    db: Database,
    symbol: str,
    bars: list,
    *,
    client: IBKRClient,
    now_window: SessionWindow | None = None,
) -> list[dict[str, Any]]:
    """Mid-session sync + trailing activation for IBKR-backed opens."""
    closed: list[dict[str, Any]] = []
    live = now_window if now_window is not None else active_window()
    opens = [
        t
        for t in db.query_trades(status=TradeStatus.OPEN, limit=1000)
        if t.symbol.upper() == symbol.upper() and is_ibkr_backed(t)
    ]
    for trade in opens:
        plan = _parse_plan(trade.plan_json)
        legs = legs_from_plan(plan)
        if legs is None or not trade.order_ref:
            continue
        long_leg, short_leg = legs
        try:
            still_open = await client.position_open_for_spread(
                trade.symbol, long_leg, short_leg
            )
        except Exception:
            continue

        mark = _session_end_mark(trade, bars)
        if not still_open:
            reason = _infer_flat_reason(plan, mark, float(trade.entry_price))
            rec = _journal_close(db, trade, mark, reason)
            rec["ibkr"] = True
            rec["already_flat"] = True
            closed.append(rec)
            ops_emit(
                f"CLOSED journal #{trade.id} {reason} pnl {rec['pnl']:+.2f} "
                f"(mid-session sync {trade.symbol})",
                level="trade",
                source="trail",
            )
            continue

        # Trailing applies to every entry model; keep managing even when the
        # trade's session window has ended (e.g. overnight hold into next day).
        if live is not trade.window and not plan_uses_trailing(plan):
            continue

        new_plan, changed, just_activated = apply_trailing_to_trade(db, trade, mark)
        if just_activated and plan_uses_trailing(new_plan):
            if new_plan.get("ibkr_trail_placed"):
                continue
            rs = risk_share_from_plan(new_plan, float(trade.entry_price))
            dist_r = float(new_plan.get("trail_distance_r") or 0.3)
            trail_amount = max(dist_r * rs, 0.01)
            try:
                result = await client.place_trailing_stop(
                    trade.symbol,
                    long_leg,
                    short_leg,
                    trade.contracts,
                    trade.order_ref,
                    trail_amount=trail_amount,
                    stop_loss_price=new_plan.get("stop_loss_price"),
                )
            except Exception as exc:
                ops_emit(
                    f"TRAIL place failed #{trade.id}: {exc}",
                    level="error",
                    source="trail",
                )
                continue
            if result.get("ok"):
                new_plan["ibkr_trail_placed"] = True
                if trade.id:
                    db.update_plan_json(trade.id, new_plan)
                mode = result.get("mode")
                level = "warn" if mode == "trail_fallback_modify" else "trade"
                ops_emit(
                    f"TRAIL activated {trade.symbol} #{trade.id} mode={mode} "
                    f"amount={trail_amount:.2f} stop={new_plan.get('stop_loss_price')}",
                    level=level,
                    source="trail",
                )
            else:
                ops_emit(
                    f"TRAIL failed #{trade.id}: {result.get('error')}",
                    level="error",
                    source="trail",
                )
        elif changed and new_plan.get("trail_active") and not new_plan.get("ibkr_trail_placed"):
            # Software trail raised SL before native place — keep plan only.
            pass

        # Software-side exit if mark hit raised SL (covers fallback static stop).
        decision = evaluate_exit(
            trade, bars, window_live=True, plan=new_plan
        )
        if decision and decision[1] in {"trailing_stop", "stop_loss", "take_profit"}:
            exit_price, reason = decision
            try:
                flat = await client.close_spread_order(
                    trade.symbol,
                    long_leg,
                    short_leg,
                    trade.contracts,
                    trade.order_ref,
                )
            except IBKRUnavailable:
                continue
            if not flat.get("ok"):
                continue
            rec = _journal_close(db, trade, exit_price, reason)
            rec["ibkr"] = True
            closed.append(rec)
            ops_emit(
                f"CLOSED journal #{trade.id} {reason} pnl {rec['pnl']:+.2f}",
                level="trade",
                source="trail",
            )
    return closed


async def settle_session_exits(
    db: Database,
    symbol: str,
    bars: list,
    *,
    now_window: SessionWindow | None = None,
    ib: Any = None,
) -> list[dict[str, Any]]:
    """Close journal-only trades, manage IBKR trails/sync, flatten ended windows.

    Take-profit / stop-loss on IBKR-backed trades stay with the broker OCA group
    until trailing activates (native TRAIL) or the session ends. Mid-session
    sync closes the journal when the broker is already flat.
    """
    closed = close_open_paper_trades(db, symbol, bars, now_window=now_window)
    for row in closed:
        ops_emit(
            f"CLOSED journal #{row.get('trade_id')} {row.get('reason')} "
            f"pnl {row.get('pnl', 0):+.2f}",
            level="trade",
            source="journal",
        )

    live = now_window if now_window is not None else active_window()
    ibkr_opens = [
        t
        for t in db.query_trades(status=TradeStatus.OPEN, limit=1000)
        if t.symbol.upper() == symbol.upper() and is_ibkr_backed(t)
    ]
    if not ibkr_opens:
        return closed

    owned_client = False
    client = ib
    try:
        if client is None:
            client = IBKRClient(readonly=False)
            await client.connect()
            owned_client = True

        closed.extend(
            await manage_open_ibkr_exits(
                db, symbol, bars, client=client, now_window=live
            )
        )

        ibkr_due = [
            t
            for t in db.query_trades(status=TradeStatus.OPEN, limit=1000)
            if t.symbol.upper() == symbol.upper()
            and is_ibkr_backed(t)
            and live is not t.window
        ]
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
            reason = "session_end"
            if result.get("already_flat"):
                reason = _infer_flat_reason(
                    plan, _session_end_mark(trade, bars), float(trade.entry_price)
                )
            rec = _journal_close(db, trade, _session_end_mark(trade, bars), reason)
            rec["ibkr"] = True
            rec["ibkr_status"] = result.get("status")
            rec["already_flat"] = bool(result.get("already_flat"))
            closed.append(rec)
            ops_emit(
                f"CLOSED journal #{trade.id} {reason} pnl {rec['pnl']:+.2f} "
                f"(session flatten)",
                level="trade",
                source="session",
            )
    finally:
        if owned_client and client is not None:
            await client.disconnect()
    return closed
