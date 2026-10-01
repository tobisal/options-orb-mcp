"""Simulate live opens stack over last N days (same engine as Live Month PnL).

Uses ``run_mes_5orb_backtest`` + ``configs/mes_5orb.json`` defaults:
opens-only, London shorts, Asia Wed–Fri, 2.5R full scale, 1 MES lot.

Optional ``--risk-pct`` re-sizes each trade from stop distance vs equity
(starting capital) for a counterfactual auto-size run; default is 1-lot
historical PnL matching the dashboard / canvas month report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta

from core.backtest_mes import run_mes_5orb_backtest
from core.config import get_settings
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import clear_mes_5orb_config_cache, load_mes_5orb_config


def _size_contracts(
    *,
    stop_points: float,
    point_value: float,
    equity: float,
    risk_pct: float,
    max_contracts: int,
) -> int:
    if stop_points <= 0 or equity <= 0 or risk_pct <= 0:
        return 0
    budget = equity * (risk_pct / 100.0)
    per = stop_points * point_value
    if per <= 0:
        return 0
    n = int(budget // per)
    return max(0, min(n, max_contracts))


async def main(days: int = 30, risk_pct: float | None = None) -> int:
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config("MES")

    bars = []
    async with IBKRClient() as ib:
        for dur in ("1 M", f"{days} D", "20 D"):
            bars = await ib.historical_bars_future(
                "MES", duration=dur, bar_size="5 mins"
            )
            if bars:
                break
    if not bars:
        print("no bars", flush=True)
        return 1

    last_et = to_et(bars[-1].ts).date()
    start = last_et - timedelta(days=min(days, 31) - 1)
    month = [b for b in bars if to_et(b.ts).date() >= start] or bars
    start = to_et(month[0].ts).date()

    bt = run_mes_5orb_backtest(month, cfg=cfg)
    summary = bt.summary()
    combined = summary["combined"]

    trades_out = []
    equity = float(get_settings().starting_capital)
    total_sized = 0.0
    for t in bt.trades:
        stop_pts = abs(t.entry_price - t.stop_initial)
        contracts = 1
        if risk_pct is not None:
            contracts = _size_contracts(
                stop_points=stop_pts,
                point_value=cfg.point_value,
                equity=equity,
                risk_pct=risk_pct,
                max_contracts=cfg.risk.contracts,
            )
            if contracts <= 0:
                contracts = 1
        pnl = round(t.points * cfg.point_value * contracts, 2)
        if risk_pct is not None:
            equity += pnl
            total_sized += pnl
        trades_out.append(
            {
                "day": t.day,
                "session": t.session_name,
                "direction": t.direction,
                "entry": t.entry_price,
                "exit": t.exit_price,
                "exit_reason": t.exit_reason,
                "points": round(t.points, 4),
                "r_multiple": round(t.r_multiple, 4),
                "contracts": contracts,
                "pnl": pnl if risk_pct is not None else round(t.pnl_usd, 2),
            }
        )

    report = {
        "aligns_with": "live_month_pnl",
        "start": start.isoformat(),
        "end": last_et.isoformat(),
        "bars": len(month),
        "config": summary.get("config"),
        "sizing": (
            f"risk_pct={risk_pct} on starting_capital"
            if risk_pct is not None
            else "1_lot_historical"
        ),
        "trade_count": len(bt.trades),
        "total_pnl": (
            round(total_sized, 2)
            if risk_pct is not None
            else round(combined.get("total_pnl") or 0, 2)
        ),
        "win_rate": combined.get("win_rate"),
        "profit_factor": combined.get("profit_factor"),
        "expectancy": combined.get("expectancy"),
        "by_session": summary.get("by_session"),
        "by_day": summary.get("by_day"),
        "trades": trades_out,
    }
    path = "/tmp/mes_sim_live_month.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, indent=2, fp=fh)
    print(json.dumps(report, indent=2), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=30)
    p.add_argument(
        "--risk-pct",
        type=float,
        default=None,
        help="If set, size each trade at this %% of equity (else 1 lot)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.days, args.risk_pct)))
