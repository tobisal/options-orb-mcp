"""Activate weekly hunter playbook and simulate week-hit probability."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import BacktestParams, run_backtest, run_power_hour_backtest  # noqa: E402
from core.config import REPO_ROOT, get_settings  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Bar, SessionWindow  # noqa: E402
from core.weekly_hunter import (  # noqa: E402
    apply_hunter_params_to_db,
    clear_weekly_hunter_cache,
    load_weekly_hunter_config,
)
from datetime import datetime  # noqa: E402
import csv  # noqa: E402
from core.timeutils import as_naive_utc  # noqa: E402


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


def _trade_pnls_filtered(bars: list[Bar]) -> list[float]:
    cfg = load_weekly_hunter_config()
    p = cfg.new_york_params or {}
    params = BacktestParams(
        opening_range_minutes=int(p.get("opening_range_minutes", 15)),
        breakout_buffer_atr=float(p.get("breakout_buffer_atr", 0.02)),
        min_strength=float(p.get("min_strength", 0.4)),
        target_r=float(p.get("target_r", 2.0)),
        stop_r=float(p.get("stop_r", 1.0)),
        require_vwap_align=bool(p.get("require_vwap_align", True)),
        require_trend_regime=bool(p.get("require_trend_regime", True)),
        volume_confirm_mult=float(p.get("volume_confirm_mult", 1.0)),
    )
    orb = run_backtest(bars, SessionWindow.NEW_YORK, params)
    ph = run_power_hour_backtest(bars, symbol="SPY", require_negative_gamma=True)
    return list(orb.pnls) + list(ph.pnls)


def _iso_week_key(session: str) -> str:
    # session is YYYY-MM-DD
    d = datetime.fromisoformat(session).date()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def historical_weekly_returns(
    bars: list[Bar], *, capital: float, contracts_scale: float
) -> dict:
    """Group filtered NY+power-hour PnLs by ISO week; scale by contracts."""
    cfg = load_weekly_hunter_config()
    p = cfg.new_york_params or {}
    params = BacktestParams(
        opening_range_minutes=int(p.get("opening_range_minutes", 15)),
        breakout_buffer_atr=float(p.get("breakout_buffer_atr", 0.02)),
        min_strength=float(p.get("min_strength", 0.4)),
        target_r=float(p.get("target_r", 2.0)),
        stop_r=float(p.get("stop_r", 1.0)),
        require_vwap_align=True,
        require_trend_regime=True,
        volume_confirm_mult=1.0,
    )
    orb = run_backtest(bars, SessionWindow.NEW_YORK, params)
    ph = run_power_hour_backtest(bars, symbol="SPY", require_negative_gamma=True)

    by_week: dict[str, float] = {}
    for t in orb.trades + ph.trades:
        # MWF filter
        d = datetime.fromisoformat(t["session"]).date()
        if d.weekday() not in cfg.trade_weekdays:
            continue
        key = _iso_week_key(t["session"])
        by_week[key] = by_week.get(key, 0.0) + float(t["pnl"]) * contracts_scale

    weeks = sorted(by_week.items())
    rets = [pnl / capital for _, pnl in weeks]
    hit = sum(1 for r in rets if r >= cfg.weekly_return_target)
    return {
        "weeks": len(rets),
        "hit_5pct_weeks": hit,
        "hit_rate": round(hit / len(rets), 4) if rets else 0.0,
        "median_week_ret": round(sorted(rets)[len(rets) // 2], 4) if rets else 0.0,
        "mean_week_ret": round(sum(rets) / len(rets), 4) if rets else 0.0,
        "best_week_ret": round(max(rets), 4) if rets else 0.0,
        "worst_week_ret": round(min(rets), 4) if rets else 0.0,
        "contracts_scale": contracts_scale,
    }


def monte_carlo(
    pnls: list[float],
    *,
    capital: float,
    target: float,
    trades_per_week: float,
    scale: float,
    n_sims: int = 5000,
    seed: int = 42,
) -> dict:
    rng = random.Random(seed)
    if not pnls:
        return {"error": "no pnls"}
    hits = 0
    ruins = 0  # equity < 50% of start within a simulated year of weeks
    week_rets: list[float] = []
    for _ in range(n_sims):
        eq = capital
        ruined = False
        for _w in range(52):
            n = max(1, int(rng.gauss(trades_per_week, 0.8)))
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
    print("Applied active_strategy:", json.dumps(applied, indent=2))

    bars = load_barchart()
    # ~2 contracts if risk ~8-15% and max loss ~£40-55/contract
    scale = 2.0
    hist = historical_weekly_returns(bars, capital=capital, contracts_scale=scale)
    pnls = _trade_pnls_filtered(bars)
    # ~3 MWF trades/week across NY+PH when both fire
    mc = monte_carlo(
        pnls,
        capital=capital,
        target=target,
        trades_per_week=3.0,
        scale=scale,
    )

    # What scale would be needed for median week ≈ 5%? (often impossible)
    needed_scale = None
    if hist["median_week_ret"] and hist["median_week_ret"] > 0:
        needed_scale = round(target / hist["median_week_ret"] * scale, 2)

    report = {
        "day": datetime.utcnow().strftime("%Y-%m-%d"),
        "capital": capital,
        "weekly_target": target,
        "weekly_target_gbp": round(capital * target, 2),
        "applied": applied,
        "historical_mwf_weeks": hist,
        "monte_carlo": mc,
        "scale_for_median_5pct": needed_scale,
        "honest_note": (
            "5%/week sustained is not supported by Barchart edge at sane risk. "
            "Playbook maximises chance of occasional 5% weeks via filters + "
            "aggressive sizing; expect frequent sub-target weeks and material "
            "drawdown risk."
        ),
    }
    out = REPO_ROOT / "data" / "nightly" / f"weekly_hunter_{report['day']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("Wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
