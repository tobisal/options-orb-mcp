"""Analyze losing days under live opens-opt config; test loss-minimising filters."""
from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta

from core.backtest_mes import run_mes_5orb_backtest
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.opening_range import to_et
from core.strategy.mes_5orb.sessions import clear_mes_5orb_config_cache, load_mes_5orb_config
from dataclasses import replace as dc_replace  # noqa: F401 — used via replace


def _by_day(trades):
    d: dict[str, list] = defaultdict(list)
    for t in trades:
        d[t.day].append(t)
    return d


def _day_pnl(trades) -> float:
    return sum(t.pnl_usd for t in trades)


async def main() -> int:
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config("MES")
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
    start = last_et - timedelta(days=30)
    month = [b for b in bars if to_et(b.ts).date() >= start] or bars

    base = run_mes_5orb_backtest(month, cfg=cfg)
    by = _by_day(base.trades)
    lose_days = sorted(d for d, ts in by.items() if _day_pnl(ts) < -0.01)

    lose_trades = []
    for d in lose_days:
        for t in by[d]:
            lose_trades.append(t)

    # Patterns on losing-day trades only
    sess_pnl = defaultdict(float)
    sess_n = Counter()
    dir_pnl = defaultdict(float)
    reason_pnl = defaultdict(float)
    reason_n = Counter()
    dow_pnl = defaultdict(float)  # Mon=0
    for t in lose_trades:
        sess_pnl[t.session_name] += t.pnl_usd
        sess_n[t.session_name] += 1
        dir_pnl[t.direction] += t.pnl_usd
        reason_pnl[t.exit_reason] += t.pnl_usd
        reason_n[t.exit_reason] += 1
        dow = date.fromisoformat(t.day).weekday()
        dow_pnl[dow] += t.pnl_usd

    # Also: which sessions drive red days (net contrib on red days)
    red_day_detail = []
    for d in lose_days:
        ts = by[d]
        by_s = defaultdict(float)
        for t in ts:
            by_s[t.session_name] += t.pnl_usd
        red_day_detail.append(
            {
                "day": d,
                "dow": date.fromisoformat(d).strftime("%a"),
                "pnl": round(_day_pnl(ts), 2),
                "trades": [
                    {
                        "session": t.session_name,
                        "dir": t.direction,
                        "pnl": round(t.pnl_usd, 2),
                        "exit": t.exit_reason,
                        "r": round(t.r_multiple, 2),
                    }
                    for t in ts
                ],
                "by_session": {k: round(v, 2) for k, v in by_s.items()},
            }
        )

    # --- Mitigation backtests ---
    def run_variant(label: str, mut) -> dict:
        c = mut(cfg)
        bt = run_mes_5orb_backtest(month, cfg=c)
        pnls = [t.pnl_usd for t in bt.trades]
        by2 = _by_day(bt.trades)
        red = [d for d, ts in by2.items() if _day_pnl(ts) < -0.01]
        red_loss = sum(_day_pnl(by2[d]) for d in red)
        return {
            "label": label,
            "trades": len(bt.trades),
            "total_pnl": round(sum(pnls), 2),
            "pf": round(bt.summary()["combined"].get("profit_factor") or 0, 3),
            "winning_days": sum(1 for d, ts in by2.items() if _day_pnl(ts) > 0.01),
            "losing_days": len(red),
            "sum_red_day_pnl": round(red_loss, 2),
            "worst_day": round(min((_day_pnl(ts) for ts in by2.values()), default=0), 2),
        }

    variants = []
    variants.append(run_variant("baseline (live)", lambda c: c))

    # 1) No Asia
    variants.append(
        run_variant(
            "no Asia",
            lambda c: replace(c, asia_range=replace(c.asia_range, enabled=False)),
        )
    )

    # 2) Asia only long (skip shorts) — need entry filter; approximate via allowed on sessions only.
    # Asia is separate path — skip by disabling require and using a patched approach:
    # disable asia entirely for short days is hard without code; use asia off on Mon/Tue via weekday on London/NY
    # instead: max 1 entry, no reentry
    variants.append(
        run_variant(
            "no reentry, max 1 entry",
            lambda c: replace(
                c,
                risk=replace(c.risk, allow_reentry=False, max_entries_per_session=1),
            ),
        )
    )

    # 3) Cap daily loss: simulate post-filter on baseline trades
    def cap_daily(trades, limit: float):
        kept = []
        day_pnl = 0.0
        cur = None
        for t in sorted(trades, key=lambda x: x.entry_time):
            if t.day != cur:
                cur = t.day
                day_pnl = 0.0
            if day_pnl <= -limit:
                continue
            kept.append(t)
            day_pnl += t.pnl_usd
        return kept

    for lim in (75.0, 100.0, 125.0):
        kept = cap_daily(base.trades, lim)
        by2 = _by_day(kept)
        red = [d for d, ts in by2.items() if _day_pnl(ts) < -0.01]
        variants.append(
            {
                "label": f"daily loss cap ${lim:.0f} (sim)",
                "trades": len(kept),
                "total_pnl": round(sum(t.pnl_usd for t in kept), 2),
                "pf": None,
                "winning_days": sum(1 for d, ts in by2.items() if _day_pnl(ts) > 0.01),
                "losing_days": len(red),
                "sum_red_day_pnl": round(sum(_day_pnl(by2[d]) for d in red), 2),
                "worst_day": round(min((_day_pnl(ts) for ts in by2.values()), default=0), 2),
            }
        )

    # 4) Skip Asia after first Asia loss that day (sim)
    def skip_asia_after_loss(trades):
        kept = []
        asia_lost = set()
        for t in sorted(trades, key=lambda x: x.entry_time):
            if t.session_name == "asia" and t.day in asia_lost:
                continue
            kept.append(t)
            if t.session_name == "asia" and t.pnl_usd < 0:
                asia_lost.add(t.day)
        return kept

    kept = skip_asia_after_loss(base.trades)
    by2 = _by_day(kept)
    red = [d for d, ts in by2.items() if _day_pnl(ts) < -0.01]
    variants.append(
        {
            "label": "no 2nd Asia trade after Asia loss (sim)",
            "trades": len(kept),
            "total_pnl": round(sum(t.pnl_usd for t in kept), 2),
            "pf": None,
            "winning_days": sum(1 for d, ts in by2.items() if _day_pnl(ts) > 0.01),
            "losing_days": len(red),
            "sum_red_day_pnl": round(sum(_day_pnl(by2[d]) for d in red), 2),
            "worst_day": round(min((_day_pnl(ts) for ts in by2.values()), default=0), 2),
        }
    )

    # 5) Skip trading after any session loss that day (hard stop)
    def stop_after_any_loss(trades):
        kept = []
        stopped = set()
        for t in sorted(trades, key=lambda x: x.entry_time):
            if t.day in stopped:
                continue
            kept.append(t)
            if t.pnl_usd < 0:
                stopped.add(t.day)
        return kept

    kept = stop_after_any_loss(base.trades)
    by2 = _by_day(kept)
    red = [d for d, ts in by2.items() if _day_pnl(ts) < -0.01]
    variants.append(
        {
            "label": "stop day after first losing trade (sim)",
            "trades": len(kept),
            "total_pnl": round(sum(t.pnl_usd for t in kept), 2),
            "pf": None,
            "winning_days": sum(1 for d, ts in by2.items() if _day_pnl(ts) > 0.01),
            "losing_days": len(red),
            "sum_red_day_pnl": round(sum(_day_pnl(by2[d]) for d in red), 2),
            "worst_day": round(min((_day_pnl(ts) for ts in by2.values()), default=0), 2),
        }
    )

    # 6) Skip Mon/Tue Asia only — filter asia trades on those DOWs
    def skip_asia_dow(trades, bad_dows: set[int]):
        kept = []
        for t in trades:
            if t.session_name == "asia" and date.fromisoformat(t.day).weekday() in bad_dows:
                continue
            kept.append(t)
        return kept

    for name, dows in [("skip Asia Mon", {0}), ("skip Asia Mon+Tue", {0, 1}), ("skip Asia Fri", {4})]:
        kept = skip_asia_dow(base.trades, dows)
        by2 = _by_day(kept)
        red = [d for d, ts in by2.items() if _day_pnl(ts) < -0.01]
        variants.append(
            {
                "label": name,
                "trades": len(kept),
                "total_pnl": round(sum(t.pnl_usd for t in kept), 2),
                "pf": None,
                "winning_days": sum(1 for d, ts in by2.items() if _day_pnl(ts) > 0.01),
                "losing_days": len(red),
                "sum_red_day_pnl": round(sum(_day_pnl(by2[d]) for d in red), 2),
                "worst_day": round(min((_day_pnl(ts) for ts in by2.values()), default=0), 2),
            }
        )

    # Combined best heuristic: daily cap $100 + no 2nd asia after loss
    def combo(trades):
        return cap_daily(skip_asia_after_loss(trades), 100.0)

    kept = combo(base.trades)
    by2 = _by_day(kept)
    red = [d for d, ts in by2.items() if _day_pnl(ts) < -0.01]
    variants.append(
        {
            "label": "Asia no-reentry-after-loss + $100 day cap",
            "trades": len(kept),
            "total_pnl": round(sum(t.pnl_usd for t in kept), 2),
            "pf": None,
            "winning_days": sum(1 for d, ts in by2.items() if _day_pnl(ts) > 0.01),
            "losing_days": len(red),
            "sum_red_day_pnl": round(sum(_day_pnl(by2[d]) for d in red), 2),
            "worst_day": round(min((_day_pnl(ts) for ts in by2.values()), default=0), 2),
        }
    )

    report = {
        "baseline_pnl": round(sum(t.pnl_usd for t in base.trades), 2),
        "losing_days": lose_days,
        "red_day_detail": red_day_detail,
        "patterns": {
            "session_pnl_on_red_days": {k: round(v, 2) for k, v in sess_pnl.items()},
            "session_trades_on_red_days": dict(sess_n),
            "direction_pnl_on_red_days": {k: round(v, 2) for k, v in dir_pnl.items()},
            "exit_reason_pnl": {k: round(v, 2) for k, v in reason_pnl.items()},
            "exit_reason_n": dict(reason_n),
            "dow_pnl_on_red_day_trades": {
                ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][k]: round(v, 2)
                for k, v in sorted(dow_pnl.items())
            },
        },
        "mitigations": sorted(variants, key=lambda x: x["total_pnl"], reverse=True),
    }
    path = "/tmp/loss_day_analysis.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
