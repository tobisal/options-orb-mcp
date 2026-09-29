"""Counterfactual: auto MES at 5% risk over last N days. Run inside orb-dashboard."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytz

from core.backtest_mes import _manage_scaled_exit, evaluate_mes_signal_live
from core.config import get_settings
from core.db import Database
from core.engine import _mes_session_to_window
from core.journal import paper_account_snapshot
from core.marketdata import fetch_bars_with_fallback
from core.models import Bar, Direction
from core.risk import RiskManager
from core.sessions import active_window
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import clear_mes_5orb_config_cache, load_mes_5orb_config


async def main(days: int = 15) -> int:
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config("MES")
    bars, source, warning = await fetch_bars_with_fallback(
        "MES",
        duration=f"{days} D",
        bar_size="5 mins",
        use_synthetic=False,
        allow_synthetic_fallback=False,
    )
    print(f"bars={len(bars)} source={source} warn={warning}", flush=True)
    if not bars:
        return 1
    print(f"range {bars[0].ts} -> {bars[-1].ts}", flush=True)

    db = Database()
    rm = RiskManager(db=db)
    equity = float(
        paper_account_snapshot(
            db,
            {},
            starting_capital=get_settings().starting_capital,
            environment=rm.environment(),
        ).get("paper_equity")
        or get_settings().starting_capital
    )
    risk_pct = 5.0
    tick = cfg.tick_size
    exits = cfg.exits
    lag = cfg.trailing_stop.pivot_lag_bars
    trail_buf = cfg.trailing_stop.buffer_ticks * tick
    pv = cfg.point_value
    max_conc = cfg.risk.max_concurrent
    per_window_limit = 3

    last_et = to_et(bars[-1].ts).date()
    start_et_date = last_et - timedelta(days=days - 1)

    def mes_sess(wname: str) -> str | None:
        return {"london": "london", "new_york": "new_york", "asia": "asia"}.get(wname)

    def force_flat_time(sess: str) -> time:
        if sess == "asia":
            return time(11, 55)
        s = cfg.session(sess)
        return s.force_flat if s else time(16, 0)

    fills: list[dict] = []
    open_trades: list[dict] = []

    for i, b in enumerate(bars):
        et = to_et(b.ts)
        if et.date() < start_et_date:
            continue

        live_open = sum(1 for t in open_trades if not t.get("closed"))
        # Mark closed if past force flat (approx — full exit sim later)
        for t in open_trades:
            if t.get("closed"):
                continue
            if et.date() > t["manage_day"] or (
                et.date() == t["manage_day"] and et.time() >= t["force_flat"]
            ):
                t["closed"] = True  # provisional; PnL filled below
        live_open = sum(1 for t in open_trades if not t.get("closed"))

        aw = active_window(
            pytz.utc.localize(b.ts) if b.ts.tzinfo is None else b.ts
        )
        if aw is None:
            continue
        wname = aw.value
        sess = mes_sess(wname)
        if sess is None:
            continue

        day_key = et.date().isoformat()
        placed_here = sum(
            1 for f in fills if f["day"] == day_key and f["spy_window"] == wname
        )
        if placed_here >= per_window_limit or live_open >= max_conc:
            continue

        hist = bars[: i + 1]
        sig = evaluate_mes_signal_live(
            hist, session_name=sess, cfg=cfg, as_of=b.ts
        )
        if not sig.get("ok"):
            continue
        plan = sig.get("plan") or {}
        if plan.get("session_name") != sess:
            continue

        entry_px = float(bars[i].close)
        plan_entry = float(plan["entry_price"])
        plan_stop = float(plan["stop_price"])
        plan_target = float(plan["target_price"])
        if plan.get("direction") == "long":
            stop = entry_px - (plan_entry - plan_stop)
            target = entry_px + (plan_target - plan_entry)
            direction = Direction.LONG
        else:
            stop = entry_px + (plan_stop - plan_entry)
            target = entry_px - (plan_entry - plan_target)
            direction = Direction.SHORT
        stop_pts = abs(entry_px - stop)
        if stop_pts <= 0:
            continue

        fp = (
            day_key,
            wname,
            plan.get("direction"),
            round(plan_entry, 2),
            round(plan_stop, 2),
        )
        if any((not t.get("closed")) and t.get("fp") == fp for t in open_trades):
            continue

        decision = rm.pre_trade_checks_futures(
            stop_pts,
            point_value=pv,
            requested_contracts=cfg.risk.contracts,
            max_concurrent=max_conc,
            window=_mes_session_to_window(sess),
            risk_pct=risk_pct,
            equity=equity,
        )
        # Size from budget; ignore stale DB open-count blocks
        contracts = decision.contracts
        if contracts <= 0:
            continue
        if any("Stop risk" in r or "budget" in r.lower() for r in decision.reasons):
            continue
        if live_open >= max_conc:
            continue

        ff = force_flat_time(sess)
        manage_day = (
            et.date() + timedelta(days=1)
            if sess == "asia" and et.time() >= time(20, 0)
            else et.date()
        )
        fill = {
            "day": day_key,
            "et": et.strftime("%Y-%m-%d %H:%M"),
            "spy_window": wname,
            "mes_session": sess,
            "direction": direction.value,
            "entry_i": i,
            "entry": round(entry_px, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "target_label": plan.get("target_label"),
            "contracts": contracts,
            "notes": plan.get("notes"),
            "fp": fp,
            "force_flat": ff,
            "manage_day": manage_day,
            "closed": False,
            "scale": (
                cfg.asia_range.scale_fraction
                if sess == "asia"
                else exits.scale_fraction
            ),
            "trail": False if sess == "asia" else exits.runner_trail,
        }
        fills.append(fill)
        open_trades.append(fill)

    for t in fills:
        entry_i = t["entry_i"]
        direction = Direction.LONG if t["direction"] == "long" else Direction.SHORT
        manage: list[Bar] = []
        for b in bars[entry_i + 1 :]:
            et = to_et(b.ts)
            if t["mes_session"] == "asia" and et.date() < t["manage_day"]:
                manage.append(b)
                continue
            if et.date() == t["manage_day"] and et.time() <= t["force_flat"]:
                manage.append(b)
            elif et.date() > t["manage_day"]:
                break
            elif et.date() == t["manage_day"] and et.time() > t["force_flat"]:
                break
        ex = replace(
            exits,
            scale_fraction=t["scale"],
            runner_trail=t["trail"],
            move_stop_to_be=True,
        )
        exit_px, exit_ts, reason, _, _ = _manage_scaled_exit(
            direction=direction,
            entry_px=t["entry"],
            init_stop=t["stop"],
            target_px=t["target"],
            target_label=t.get("target_label") or "target",
            manage=manage,
            entry_i=entry_i,
            entry_ts=bars[entry_i].ts,
            exits=ex,
            lag=lag,
            trail_buffer=trail_buf,
            tick=tick,
            scale_frac=t["scale"],
        )
        pts = (
            (exit_px - t["entry"])
            if direction is Direction.LONG
            else (t["entry"] - exit_px)
        )
        t["exit"] = round(float(exit_px), 2)
        t["exit_et"] = to_et(exit_ts).strftime("%Y-%m-%d %H:%M")
        t["reason"] = reason
        t["points"] = round(pts, 2)
        t["pnl"] = round(pts * pv * t["contracts"], 2)
        t["closed"] = True
        t.pop("fp", None)
        t.pop("entry_i", None)
        t["manage_day"] = t["manage_day"].isoformat()
        t["force_flat"] = t["force_flat"].strftime("%H:%M")

    total = sum(t["pnl"] for t in fills)
    wins = sum(1 for t in fills if t["pnl"] > 0)
    by_sess: dict[str, dict] = {}
    by_day: dict[str, float] = {}
    for t in fills:
        by_sess.setdefault(t["mes_session"], {"n": 0, "pnl": 0.0})
        by_sess[t["mes_session"]]["n"] += 1
        by_sess[t["mes_session"]]["pnl"] += t["pnl"]
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["pnl"]

    out = {
        "assumptions": {
            "lookback_days": days,
            "source": source,
            "warning": warning,
            "equity": round(equity, 2),
            "risk_pct": risk_pct,
            "mode": (
                "auto windows asia/london/new_york only; market fill; "
                f"max_concurrent={max_conc}; {per_window_limit}/window"
            ),
            "bar_start": bars[0].ts.isoformat(),
            "bar_end": bars[-1].ts.isoformat(),
            "sim_from_et": start_et_date.isoformat(),
            "sim_to_et": last_et.isoformat(),
        },
        "summary": {
            "trades": len(fills),
            "wins": wins,
            "losses": len(fills) - wins,
            "win_rate": round(wins / len(fills), 4) if fills else 0,
            "total_pnl": round(total, 2),
            "avg_trade": round(total / len(fills), 2) if fills else 0,
            "by_session": {
                k: {"trades": v["n"], "pnl": round(v["pnl"], 2)}
                for k, v in sorted(by_sess.items())
            },
            "by_day": {k: round(v, 2) for k, v in sorted(by_day.items())},
        },
        "trades": fills,
    }
    path = "/app/data/nightly/mes_auto_5pct_15d.json"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        print("wrote", path, flush=True)
    except OSError:
        path = "data/nightly/mes_auto_5pct_15d.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        print("wrote", path, flush=True)

    print(json.dumps(out["assumptions"], indent=2), flush=True)
    print(json.dumps(out["summary"], indent=2), flush=True)
    for t in fills:
        print(
            f"{t['et']} {t['mes_session']:7s} {t['direction']:5s} "
            f"entry={t['entry']} exit={t['exit']} {t['reason']:24s} "
            f"{t['points']:+7.2f}pt x{t['contracts']} = {t['pnl']:+8.2f}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    raise SystemExit(asyncio.run(main(d)))
