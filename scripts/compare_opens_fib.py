"""Compare opens-only optimised retest vs Fib+MACD on same windows."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from core.backtest_mes import run_mes_5orb_backtest
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.sessions import (
    EntryConfig,
    apply_mes_opt_params,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)


OPT = {
    "target_r": 2.5,
    "scale_fraction": 1.0,
    "stop_buffer_ticks": 1,
    "tolerance_ticks": 3,
    "require_rejection_candle": False,
    "use_hod_lod_target": True,
    "runner_trail": True,
    "allow_reentry": True,
    "max_entries_per_session": 2,
    "asia_target_r": 1.5,
    "asia_scale_fraction": 0.5,
}


def _opens(cfg, *, mode: str):
    sessions = []
    for s in cfg.sessions:
        sessions.append(
            replace(
                s,
                enabled=s.name in ("london", "new_york"),
                entry=EntryConfig(
                    mode=mode,
                    allowed_directions="both",
                    skip_if_opposite_first=False,
                ),
            )
        )
    base = replace(
        cfg,
        sessions=tuple(sessions),
        asia_range=replace(cfg.asia_range, enabled=True),
    )
    return apply_mes_opt_params(base, OPT)


async def main() -> int:
    clear_mes_5orb_config_cache()
    raw = load_mes_5orb_config("MES")
    bars = []
    async with IBKRClient() as ib:
        for dur in ("1 M", "30 D", "20 D"):
            bars = await ib.historical_bars_future(
                "MES", duration=dur, bar_size="5 mins"
            )
            if bars:
                break
    if not bars:
        return 1

    retest = _opens(raw, mode="retest")
    # Fib on London/NY; Asia stays Judas (asia_range path, not session entry).
    fib = _opens(raw, mode="fib_macd")
    # Optional: 15m NY OR for fib (author style) while keeping retest 5m
    from datetime import time
    from core.strategy.mes_5orb.sessions import OpeningRangeFilter

    fib_sessions = []
    for s in fib.sessions:
        if s.name == "new_york":
            fib_sessions.append(
                replace(
                    s,
                    or_start=time(9, 30),
                    or_end=time(9, 45),
                    opening_range=OpeningRangeFilter(1.0, 20.0),
                )
            )
        else:
            fib_sessions.append(s)
    fib = replace(fib, sessions=tuple(fib_sessions))

    r = run_mes_5orb_backtest(bars, cfg=retest)
    f = run_mes_5orb_backtest(bars, cfg=fib)
    rs, fs = r.summary(), f.summary()

    def rows(summary):
        by = summary.get("by_session") or {}
        out = []
        for name in ("asia", "london", "new_york"):
            m = by.get(name) or {}
            out.append(
                {
                    "session": name,
                    "trades": m.get("trades", 0),
                    "win_rate": m.get("win_rate"),
                    "profit_factor": m.get("profit_factor"),
                    "total_pnl": m.get("total_pnl", 0),
                }
            )
        return out

    report = {
        "bars": len(bars),
        "retest_opt": {
            "label": "opens retest (live opt)",
            "combined": rs["combined"],
            "trades": rs["trade_count"],
            "by_session": rows(rs),
        },
        "fib_macd": {
            "label": "opens Fib 50/61.8+MACD (15m NY OR) + Asia Judas",
            "combined": fs["combined"],
            "trades": fs["trade_count"],
            "by_session": rows(fs),
        },
        "winner": (
            "fib_macd"
            if float(fs["combined"]["total_pnl"])
            > float(rs["combined"]["total_pnl"])
            else "retest_opt"
        ),
    }
    path = "/tmp/opens_fib_compare.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
