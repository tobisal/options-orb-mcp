"""Diagnose London vs NY open trades under live MES config (~1 month)."""
from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta

from core.backtest_mes import run_mes_5orb_backtest
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.opening_range import compute_opening_range, to_et
from core.strategy.mes_5orb.sessions import clear_mes_5orb_config_cache, load_mes_5orb_config

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _metrics(trades) -> dict:
    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    return {
        "trades": len(trades),
        "pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "profit_factor": round(gp / gl, 4) if gl > 0 else (None if gp == 0 else 99.0),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
    }


def _bucket_or(width: float) -> str:
    if width < 2:
        return "<2"
    if width < 4:
        return "2-4"
    if width < 6:
        return "4-6"
    return "6+"


async def main(days: int = 30) -> int:
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
        print("no bars", flush=True)
        return 1

    last_et = to_et(bars[-1].ts).date()
    start = last_et - timedelta(days=min(days, 31) - 1)
    month = [b for b in bars if to_et(b.ts).date() >= start] or bars
    start = to_et(month[0].ts).date()

    # Full live stack for red-day attribution
    full = run_mes_5orb_backtest(month, cfg=cfg)

    # London-only / NY-only for clean session stats
    sessions_ldn = tuple(
        replace(s, enabled=(s.name == "london")) for s in cfg.sessions
    )
    sessions_ny = tuple(
        replace(s, enabled=(s.name == "new_york")) for s in cfg.sessions
    )
    ldn_cfg = replace(
        cfg, sessions=sessions_ldn, asia_range=replace(cfg.asia_range, enabled=False)
    )
    ny_cfg = replace(
        cfg, sessions=sessions_ny, asia_range=replace(cfg.asia_range, enabled=False)
    )
    ldn_bt = run_mes_5orb_backtest(month, cfg=ldn_cfg)
    ny_bt = run_mes_5orb_backtest(month, cfg=ny_cfg)

    def session_report(name: str, trades, session_cfg) -> dict:
        by_dir: dict[str, float] = defaultdict(float)
        by_exit: dict[str, float] = defaultdict(float)
        by_exit_n: Counter = Counter()
        by_dow: dict[str, float] = defaultdict(float)
        by_dow_n: Counter = Counter()
        by_or: dict[str, float] = defaultdict(float)
        by_or_n: Counter = Counter()
        sess = session_cfg.session(name)
        for t in trades:
            by_dir[t.direction] += t.pnl_usd
            by_exit[t.exit_reason] += t.pnl_usd
            by_exit_n[t.exit_reason] += 1
            dow = date.fromisoformat(t.day).weekday()
            by_dow[DOW[dow]] += t.pnl_usd
            by_dow_n[DOW[dow]] += 1
            if sess is not None:
                orb = compute_opening_range(month, sess, date.fromisoformat(t.day))
                if orb is not None and not orb.skipped:
                    bucket = _bucket_or(orb.width)
                    by_or[bucket] += t.pnl_usd
                    by_or_n[bucket] += 1
        return {
            **_metrics(trades),
            "by_direction": {k: round(v, 2) for k, v in by_dir.items()},
            "by_exit_pnl": {k: round(v, 2) for k, v in by_exit.items()},
            "by_exit_n": dict(by_exit_n),
            "by_dow_pnl": {k: round(v, 2) for k, v in by_dow.items()},
            "by_dow_n": dict(by_dow_n),
            "by_or_width_pnl": {k: round(v, 2) for k, v in by_or.items()},
            "by_or_width_n": dict(by_or_n),
        }

    # Red days on full stack: how much London/NY contributed
    by_day: dict[str, list] = defaultdict(list)
    for t in full.trades:
        by_day[t.day].append(t)
    red_detail = []
    for d, ts in sorted(by_day.items()):
        day_pnl = sum(t.pnl_usd for t in ts)
        if day_pnl >= -0.01:
            continue
        by_s = defaultdict(float)
        for t in ts:
            by_s[t.session_name] += t.pnl_usd
        red_detail.append(
            {
                "day": d,
                "dow": date.fromisoformat(d).strftime("%a"),
                "pnl": round(day_pnl, 2),
                "by_session": {k: round(v, 2) for k, v in by_s.items()},
            }
        )

    report = {
        "start": start.isoformat(),
        "end": last_et.isoformat(),
        "full_stack_pnl": round(sum(t.pnl_usd for t in full.trades), 2),
        "london": session_report("london", ldn_bt.trades, ldn_cfg),
        "new_york": session_report("new_york", ny_bt.trades, ny_cfg),
        "red_days": red_detail,
        "exits_global": {
            "target_r": cfg.exits.target_r,
            "scale_fraction": cfg.exits.scale_fraction,
            "london_override": cfg.session("london").exits is not None
            if cfg.session("london")
            else False,
            "ny_override": cfg.session("new_york").exits is not None
            if cfg.session("new_york")
            else False,
        },
    }
    path = "/tmp/london_ny_diag.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, indent=2, fp=fh)
    print(json.dumps(report, indent=2), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(30)))
