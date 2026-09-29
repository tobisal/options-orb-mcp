"""Compare old MES 5ORB vs Edgeful-inspired config over recent bars (IBKR)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import time, timedelta

from core.backtest_mes import run_mes_5orb_backtest
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import (
    EntryConfig,
    OpeningRangeFilter,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)


def _old_cfg(base):
    """Pre-Edgeful MES config: 5m NY OR, no stop cap, no opposite-first skip."""
    sessions = []
    for s in base.sessions:
        if s.name == "new_york":
            sessions.append(
                replace(
                    s,
                    or_start=time(9, 30),
                    or_end=time(9, 35),
                    opening_range=OpeningRangeFilter(0.75, 10.0),
                    entry=EntryConfig(
                        mode="retest",
                        allowed_directions="both",
                        skip_if_opposite_first=False,
                    ),
                )
            )
        else:
            sessions.append(
                replace(
                    s,
                    entry=EntryConfig(
                        mode="retest",
                        allowed_directions="both",
                        skip_if_opposite_first=False,
                    ),
                )
            )
    return replace(
        base,
        sessions=tuple(sessions),
        exits=replace(
            base.exits,
            max_stop_points=None,
            target_mode="r_multiple",
            target_r=2.0,
        ),
    )


def _new_cfg(base):
    """Current Edgeful-inspired live config (as loaded)."""
    return base


def _score(summary: dict) -> tuple[float, float, float, int]:
    c = summary["combined"]
    pnl = float(c.get("total_pnl") or 0)
    pf = float(c.get("profit_factor") or 0)
    if pf == float("inf"):
        pf = 99.0
    wr = float(c.get("win_rate") or 0)
    n = int(summary.get("trade_count") or 0)
    return pnl, pf, wr, n


def _pick_winner(old_s: dict, new_s: dict) -> str:
    """Prefer higher total PnL; tie-break profit factor, then win rate."""
    o = _score(old_s)
    n = _score(new_s)
    if n[0] > o[0] + 1e-6:
        return "new"
    if o[0] > n[0] + 1e-6:
        return "old"
    if n[1] > o[1] + 1e-6:
        return "new"
    if o[1] > n[1] + 1e-6:
        return "old"
    if n[2] >= o[2]:
        return "new"
    return "old"


async def main(days: int = 30) -> int:
    clear_mes_5orb_config_cache()
    base = load_mes_5orb_config("MES")
    # ContFuture rejects endDateTime chunks (returns empty); use a single
    # lookback. IBKR "1 M" / "30 D" when available, else "20 D".
    from core.ibkr_client import IBKRClient

    bars: list = []
    source = "ibkr"
    async with IBKRClient() as ib:
        for dur in ("1 M", "30 D", "20 D"):
            print(f"requesting {dur}…", flush=True)
            bars = await ib.historical_bars_future(
                "MES", duration=dur, bar_size="5 mins"
            )
            print(f"  got {len(bars)} bars", flush=True)
            if bars:
                break
    if not bars:
        print("no bars from IBKR", flush=True)
        return 1

    last_et = to_et(bars[-1].ts).date()
    # Keep last ~calendar month of ET dates present in the series.
    start = last_et - timedelta(days=min(days, 31) - 1)
    month_bars = [b for b in bars if to_et(b.ts).date() >= start]
    if not month_bars:
        month_bars = bars
        start = to_et(bars[0].ts).date()
    print(
        f"range {to_et(month_bars[0].ts)} -> {to_et(month_bars[-1].ts)} "
        f"({len(month_bars)} bars, start={start})",
        flush=True,
    )

    old = _old_cfg(base)
    new = _new_cfg(base)
    old_bt = run_mes_5orb_backtest(month_bars, cfg=old)
    new_bt = run_mes_5orb_backtest(month_bars, cfg=new)
    old_s = old_bt.summary()
    new_s = new_bt.summary()
    winner = _pick_winner(old_s, new_s)

    report = {
        "days_requested": days,
        "bar_count": len(month_bars),
        "source": source,
        "start": start.isoformat(),
        "end": last_et.isoformat(),
        "old": {
            "label": "5m NY OR, no max_stop, no opposite-first",
            "ny_or_end": "09:35",
            "max_stop_points": None,
            "skip_if_opposite_first": False,
            "summary": old_s,
            "trade_count": len(old_bt.trades),
        },
        "new": {
            "label": "15m NY OR, max_stop=15, skip_if_opposite_first",
            "ny_or_end": "09:45",
            "max_stop_points": 15.0,
            "skip_if_opposite_first": True,
            "summary": new_s,
            "trade_count": len(new_bt.trades),
        },
        "winner": winner,
        "decision_rule": "max total_pnl, then profit_factor, then win_rate",
    }
    out_path = "/tmp/orb_month_compare.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str), flush=True)
    print(f"wrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(30)))
