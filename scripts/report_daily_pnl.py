"""Daily PnL for opens-only MES (Asia + London + NY)."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
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


def _opens_cfg(base):
    sessions = []
    for s in base.sessions:
        enabled = s.name in ("london", "new_york")
        kw = {"enabled": enabled}
        if s.name == "new_york":
            kw.update(
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
    return replace(
        base,
        sessions=tuple(sessions),
        asia_range=replace(base.asia_range, enabled=True),
        exits=replace(base.exits, max_stop_points=None, target_r=2.0),
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
        return 1

    last_et = to_et(bars[-1].ts).date()
    start = last_et - timedelta(days=min(days, 31) - 1)
    month = [b for b in bars if to_et(b.ts).date() >= start] or bars
    start = to_et(month[0].ts).date()

    bt = run_mes_5orb_backtest(month, cfg=_opens_cfg(base))
    by_day: dict[str, dict] = {}
    for t in bt.trades:
        d = t.day
        slot = by_day.setdefault(
            d,
            {"day": d, "pnl": 0.0, "trades": 0, "wins": 0, "by_session": defaultdict(float)},
        )
        slot["pnl"] += t.pnl_usd
        slot["trades"] += 1
        if t.pnl_usd > 0:
            slot["wins"] += 1
        slot["by_session"][t.session_name] += t.pnl_usd

    rows = []
    equity = 0.0
    for d in sorted(by_day):
        slot = by_day[d]
        equity += slot["pnl"]
        rows.append(
            {
                "day": d,
                "pnl": round(slot["pnl"], 2),
                "trades": slot["trades"],
                "wins": slot["wins"],
                "cum_pnl": round(equity, 2),
                "asia": round(slot["by_session"].get("asia", 0.0), 2),
                "london": round(slot["by_session"].get("london", 0.0), 2),
                "new_york": round(slot["by_session"].get("new_york", 0.0), 2),
            }
        )

    report = {
        "strategy": "opens only (asia + london + new_york)",
        "start": start.isoformat(),
        "end": last_et.isoformat(),
        "trade_count": len(bt.trades),
        "total_pnl": round(sum(r["pnl"] for r in rows), 2),
        "winning_days": sum(1 for r in rows if r["pnl"] > 0),
        "losing_days": sum(1 for r in rows if r["pnl"] < 0),
        "flat_days": sum(1 for r in rows if r["pnl"] == 0),
        "days": rows,
    }
    path = "/tmp/orb_daily_pnl.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(30)))
