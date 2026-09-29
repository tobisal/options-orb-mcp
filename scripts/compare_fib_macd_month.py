"""Compare current MES retest ORB vs ORB+Fib+MACD pullback over ~1 month."""
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


def _baseline_cfg(base):
    """Live winner: 5m OR retest, no stop cap."""
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
    return replace(
        base,
        sessions=tuple(sessions),
        exits=replace(
            base.exits,
            max_stop_points=None,
            target_mode="r_multiple",
            target_r=2.0,
            use_hod_lod_target=True,
        ),
    )


def _fib_cfg(base):
    """ORB break → Fib 50/61.8 + MACD; 15m NY OR."""
    sessions = []
    for s in base.sessions:
        kw = {}
        if s.name == "new_york":
            kw = dict(
                or_start=time(9, 30),
                or_end=time(9, 45),
                opening_range=OpeningRangeFilter(1.0, 20.0),
            )
        sessions.append(
            replace(
                s,
                **kw,
                entry=EntryConfig(
                    mode="fib_macd",
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
            use_hod_lod_target=True,
            scale_fraction=0.5,
        ),
    )


def _score(summary: dict) -> tuple[float, float, float]:
    c = summary["combined"]
    pnl = float(c.get("total_pnl") or 0)
    pf = float(c.get("profit_factor") or 0)
    if pf == float("inf"):
        pf = 99.0
    wr = float(c.get("win_rate") or 0)
    return pnl, pf, wr


def _pick(a: dict, b: dict) -> str:
    sa, sb = _score(a), _score(b)
    if sb[0] > sa[0] + 1e-6:
        return "fib_macd"
    if sa[0] > sb[0] + 1e-6:
        return "retest"
    if sb[1] > sa[1] + 1e-6:
        return "fib_macd"
    if sa[1] > sb[1] + 1e-6:
        return "retest"
    return "fib_macd" if sb[2] >= sa[2] else "retest"


async def main(days: int = 30) -> int:
    clear_mes_5orb_config_cache()
    base = load_mes_5orb_config("MES")
    bars = []
    async with IBKRClient() as ib:
        for dur in ("1 M", "30 D", "20 D"):
            print(f"requesting {dur}…", flush=True)
            bars = await ib.historical_bars_future(
                "MES", duration=dur, bar_size="5 mins"
            )
            print(f"  got {len(bars)}", flush=True)
            if bars:
                break
    if not bars:
        return 1

    last_et = to_et(bars[-1].ts).date()
    start = last_et - timedelta(days=min(days, 31) - 1)
    month = [b for b in bars if to_et(b.ts).date() >= start] or bars
    start = to_et(month[0].ts).date()
    print(f"range {start} -> {last_et} ({len(month)} bars)", flush=True)

    retest = _baseline_cfg(base)
    fib = _fib_cfg(base)
    r_bt = run_mes_5orb_backtest(month, cfg=retest)
    f_bt = run_mes_5orb_backtest(month, cfg=fib)
    r_s, f_s = r_bt.summary(), f_bt.summary()
    winner = _pick(r_s, f_s)

    report = {
        "start": start.isoformat(),
        "end": last_et.isoformat(),
        "bar_count": len(month),
        "retest": {"summary": r_s, "trades": len(r_bt.trades)},
        "fib_macd": {"summary": f_s, "trades": len(f_bt.trades)},
        "winner": winner,
        "decision_rule": "max total_pnl, then profit_factor, then win_rate",
    }
    path = "/tmp/orb_fib_compare.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(30)))
