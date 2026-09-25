"""Activate non-ORB overnight playbook and report week-hit vs 5% goal."""

from __future__ import annotations

import csv
import json
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import run_overnight_backtest  # noqa: E402
from core.config import REPO_ROOT, get_settings  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Bar  # noqa: E402
from core.timeutils import as_naive_utc  # noqa: E402
from core.weekly_hunter import (  # noqa: E402
    apply_hunter_params_to_db,
    clear_weekly_hunter_cache,
    load_weekly_hunter_config,
)


def load_barchart() -> list[Bar]:
    path = REPO_ROOT / "data" / "history" / "SPY_5mins_barchart.csv"
    bars: list[Bar] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                Bar(
                    ts=as_naive_utc(datetime.fromisoformat(row["ts"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def _iso_week_key(session: str) -> str:
    d = datetime.fromisoformat(session).date()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weekly_stats(
    trades: list[dict],
    *,
    capital: float,
    scale: float,
    weekdays: tuple[int, ...] | None,
    oos_folds: int | None = None,
) -> dict:
    by_week: dict[str, float] = defaultdict(float)
    for t in trades:
        d = datetime.fromisoformat(t["session"]).date()
        if weekdays is not None and d.weekday() not in weekdays:
            continue
        by_week[_iso_week_key(t["session"])] += float(t["pnl"]) * scale

    keys = sorted(by_week)
    if oos_folds is not None and len(keys) >= oos_folds + 1:
        seg = len(keys) // (oos_folds + 1)
        keys = []
        for f in range(oos_folds):
            keys.extend(sorted(by_week)[seg * (f + 1) : seg * (f + 2)])

    rets = [by_week[k] / capital for k in keys]
    if not rets:
        return {
            "weeks": 0,
            "hit_5pct_weeks": 0,
            "hit_rate": 0.0,
            "median_week_ret": 0.0,
            "mean_week_ret": 0.0,
            "best_week_ret": 0.0,
            "worst_week_ret": 0.0,
            "total_pnl": 0.0,
            "contracts_scale": scale,
        }
    hit = sum(1 for r in rets if r >= 0.05)
    ordered = sorted(rets)
    return {
        "weeks": len(rets),
        "hit_5pct_weeks": hit,
        "hit_rate": round(hit / len(rets), 4),
        "median_week_ret": round(ordered[len(ordered) // 2], 4),
        "mean_week_ret": round(sum(rets) / len(rets), 4),
        "best_week_ret": round(ordered[-1], 4),
        "worst_week_ret": round(ordered[0], 4),
        "total_pnl": round(sum(by_week[k] for k in keys), 2),
        "contracts_scale": scale,
    }


def monte_carlo(
    pnls: list[float],
    *,
    capital: float,
    target: float,
    trades_per_week: float,
    scale: float,
    n_sims: int = 3000,
    seed: int = 42,
) -> dict:
    rng = random.Random(seed)
    if not pnls:
        return {"error": "no pnls"}
    hits = 0
    ruins = 0
    week_rets: list[float] = []
    for _ in range(n_sims):
        eq = capital
        ruined = False
        for _w in range(52):
            n = max(1, int(rng.gauss(trades_per_week, 0.5)))
            week = sum(rng.choice(pnls) * scale for _ in range(n))
            eq += week
            week_rets.append(week / capital)
            if week / capital >= target:
                hits += 1
            if eq < 0.5 * capital:
                ruined = True
                break
        if ruined:
            ruins += 1
    return {
        "sims": n_sims,
        "p_week_ge_target": round(hits / (n_sims * 52), 4),
        "p_ruin_50pct_in_year": round(ruins / n_sims, 4),
        "median_sim_week_ret": round(sorted(week_rets)[len(week_rets) // 2], 4),
    }


def main() -> int:
    clear_weekly_hunter_cache()
    cfg = load_weekly_hunter_config()
    settings = get_settings()
    capital = settings.starting_capital
    target = cfg.weekly_return_target

    applied = apply_hunter_params_to_db(Database())
    print("Applied:", json.dumps(applied, indent=2))

    print("Loading bars…")
    bars = load_barchart()
    print(f"Running overnight backtest on {len(bars)} bars…")
    bt = run_overnight_backtest(bars, symbol="SPY")
    print(f"Trades: {len(bt.trades)}")

    scale = float(cfg.contracts_scale)
    hist_all = weekly_stats(bt.trades, capital=capital, scale=scale, weekdays=tuple(range(5)))
    hist_mwf = weekly_stats(bt.trades, capital=capital, scale=scale, weekdays=cfg.trade_weekdays)
    oos = weekly_stats(bt.trades, capital=capital, scale=scale, weekdays=None, oos_folds=3)
    scale_stress = {
        "2x": weekly_stats(bt.trades, capital=capital, scale=2.0, weekdays=None, oos_folds=3),
        "4x": weekly_stats(bt.trades, capital=capital, scale=4.0, weekdays=None, oos_folds=3),
        "6x": weekly_stats(bt.trades, capital=capital, scale=6.0, weekdays=None, oos_folds=3),
    }
    mc = monte_carlo(
        list(bt.pnls),
        capital=capital,
        target=target,
        trades_per_week=3.0,
        scale=scale,
    )

    report = {
        "day": datetime.utcnow().strftime("%Y-%m-%d"),
        "strategy": "overnight_long",
        "capital": capital,
        "weekly_target": target,
        "applied": applied,
        "trade_count": len(bt.trades),
        "historical_all_weekdays": hist_all,
        "historical_mwf": hist_mwf,
        "walk_forward_oos": oos,
        "oos_by_scale": scale_stress,
        "monte_carlo_2x": mc,
        "goal_80pct_week_ge_5pct": False,
        "honest_note": (
            "Overnight long is the best non-ORB edge found on this SPY 5m set. "
            "At 2× contracts OOS P(week≥5%) stays low. Scaling to 4–6× raises "
            "hit rate but is leverage, not new alpha, and raises ruin risk. "
            "80% weeks ≥5% remains unreachable at sane risk."
        ),
    }
    out = REPO_ROOT / "data" / "nightly" / f"overnight_{report['day']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
