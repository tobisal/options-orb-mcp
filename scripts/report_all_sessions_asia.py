"""Month performance for live retest ORB — all sessions + Asia enabled."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import time, timedelta

from core.backtest_mes import run_mes_5orb_backtest
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import (
    EntryConfig,
    OpeningRangeFilter,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)


def _cfg(base):
    sessions = []
    for s in base.sessions:
        kw = {}
        if s.name == "new_york":
            kw = dict(
                or_start=time(9, 30),
                or_end=time(9, 35),
                opening_range=OpeningRangeFilter(0.75, 10.0),
            )
        sessions.append(
            replace(
                s,
                **kw,
                entry=EntryConfig(
                    mode="retest",
                    allowed_directions="both",
                    skip_if_opposite_first=False,
                ),
            )
        )
    asia = replace(base.asia_range, enabled=True)
    return replace(
        base,
        sessions=tuple(sessions),
        asia_range=asia,
        exits=replace(
            base.exits,
            max_stop_points=None,
            target_mode="r_multiple",
            target_r=2.0,
            use_hod_lod_target=True,
        ),
    )


async def main(days: int = 30) -> int:
    clear_mes_5orb_config_cache()
    base = load_mes_5orb_config("MES")
    bars = []
    async with IBKRClient() as ib:
        for dur in ("1 M", "30 D", "20 D"):
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

    cfg = _cfg(base)
    bt = run_mes_5orb_backtest(month, cfg=cfg)
    s = bt.summary()
    order = ["asia", "london", "london_mid", "new_york", "ny_mid", "ny_pm"]
    by = s.get("by_session") or {}
    rows = []
    for name in order:
        if name not in by:
            rows.append(
                {
                    "session": name,
                    "trades": 0,
                    "wins": 0,
                    "win_rate": None,
                    "profit_factor": None,
                    "total_pnl": 0.0,
                    "expectancy": None,
                    "max_drawdown": None,
                }
            )
            continue
        m = by[name]
        rows.append(
            {
                "session": name,
                "trades": m.get("trades"),
                "wins": m.get("wins"),
                "win_rate": m.get("win_rate"),
                "profit_factor": m.get("profit_factor"),
                "total_pnl": m.get("total_pnl"),
                "expectancy": m.get("expectancy"),
                "max_drawdown": m.get("max_drawdown"),
            }
        )
    # Any unexpected sessions
    for name, m in sorted(by.items()):
        if name not in order:
            rows.append(
                {
                    "session": name,
                    "trades": m.get("trades"),
                    "wins": m.get("wins"),
                    "win_rate": m.get("win_rate"),
                    "profit_factor": m.get("profit_factor"),
                    "total_pnl": m.get("total_pnl"),
                    "expectancy": m.get("expectancy"),
                    "max_drawdown": m.get("max_drawdown"),
                }
            )

    report = {
        "strategy": "retest (5m OR) + Asia Judas enabled for report only",
        "start": start.isoformat(),
        "end": last_et.isoformat(),
        "bar_count": len(month),
        "asia_enabled": True,
        "combined": s.get("combined"),
        "trade_count": s.get("trade_count"),
        "by_session": rows,
    }
    path = "/tmp/orb_all_sessions_asia.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(30)))
