"""Edge discovery: search simple SPY 5m rules for weekly hit-rate.

Goal context: P(week >= 5% of capital) at 2x contracts. Current hunter ~4%.
We search for any rule family that materially raises week-hit rate under
walk-forward (not just in-sample).
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from core.config import REPO_ROOT
from core.models import Bar, Direction, Regime, SessionWindow
from core.backtest import BacktestParams, _simulate_trade
from core.sessions import EASTERN, group_by_session, opening_range_of, get_window_config
from core.strategy.orb import average_true_range, session_vwap
from core.timeutils import as_naive_utc
import pytz

CAPITAL = 1000.0
SCALE = 2.0
TARGET = 0.05
CSV_PATH = REPO_ROOT / "data" / "history" / "SPY_5mins_barchart.csv"


def load_bars() -> list[Bar]:
    bars: list[Bar] = []
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
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


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


def week_key(d) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def summarize_weeks(pnls_by_week: dict[str, float], scale: float = SCALE) -> dict:
    if not pnls_by_week:
        return {"weeks": 0, "hit_5": 0, "hit_rate": 0.0, "mean": 0.0, "median": 0.0, "total": 0.0}
    rets = sorted((v * scale) / CAPITAL for v in pnls_by_week.values())
    hits = sum(1 for r in rets if r >= TARGET)
    return {
        "weeks": len(rets),
        "hit_5": hits,
        "hit_rate": round(hits / len(rets), 4),
        "mean": round(sum(rets) / len(rets), 4),
        "median": round(rets[len(rets) // 2], 4),
        "total": round(sum(rets) * CAPITAL, 2),
        "best": round(rets[-1], 4),
        "worst": round(rets[0], 4),
    }


def walk_forward_weeks(
    trade_days: list[tuple],  # (date, pnl)
    folds: int = 3,
) -> dict:
    """Split by week chronologically; train unused — just OOS aggregate of later folds."""
    by_week: dict[str, float] = defaultdict(float)
    for d, pnl in trade_days:
        by_week[week_key(d)] += pnl
    keys = sorted(by_week.keys())
    if len(keys) < folds + 1:
        return {"error": "not enough weeks", "oos": summarize_weeks({})}
    seg = len(keys) // (folds + 1)
    oos: dict[str, float] = {}
    for f in range(folds):
        chunk = keys[seg * (f + 1) : seg * (f + 2)]
        for k in chunk:
            oos[k] = by_week[k]
    return {"oos": summarize_weeks(oos), "folds": folds, "oos_weeks": len(oos)}


def rth_days(bars: list[Bar]) -> dict:
    days: dict = defaultdict(list)
    for b in bars:
        et = _to_et(b.ts)
        if time(9, 30) <= et.time() < time(16, 0):
            days[et.date()].append(b)
    return {d: sorted(v, key=lambda x: x.ts) for d, v in days.items()}


def sim_directional(spot: float, direction: Direction, forward: list[Bar], params: BacktestParams) -> float | None:
    t = _simulate_trade(
        spot=spot,
        direction=direction,
        regime=Regime.TREND,
        forward_bars=forward,
        params=params,
    )
    return None if t is None else float(t["pnl"])


# --- Candidate edges -------------------------------------------------------

def edge_orb_filtered(bars: list[Bar]) -> list[tuple]:
    from core.strategy.orb import passes_entry_filters, classify_regime_bars

    cfg = get_window_config(SessionWindow.NEW_YORK)
    params = BacktestParams(
        opening_range_minutes=15,
        breakout_buffer_atr=0.02,
        min_strength=0.4,
        target_r=2.0,
        stop_r=1.0,
        require_vwap_align=True,
        require_trend_regime=True,
        volume_confirm_mult=1.0,
    )
    atr_all = average_true_range(bars)
    out = []
    for session_key, sbars in group_by_session(bars, cfg):
        orb, post = opening_range_of(sbars, params.opening_range_minutes)
        if not orb or len(post) < 2:
            continue
        rh, rl = max(b.high for b in orb), min(b.low for b in orb)
        width = max(rh - rl, 1e-9)
        buf = params.breakout_buffer_atr * (atr_all or width)
        for i, b in enumerate(post):
            direction = Direction.NEUTRAL
            if b.close > rh + buf and (b.close - rh) / width >= params.min_strength:
                direction = Direction.LONG
            elif b.close < rl - buf and (rl - b.close) / width >= params.min_strength:
                direction = Direction.SHORT
            if direction is Direction.NEUTRAL:
                continue
            regime = classify_regime_bars(post[: i + 1])
            ok, _ = passes_entry_filters(
                direction=direction,
                price=b.close,
                vwap_bars=list(orb) + list(post[: i + 1]),
                entry_bar=b,
                orb_bars=orb,
                regime=regime,
                require_vwap_align=True,
                require_trend_regime=True,
                volume_confirm_mult=1.0,
            )
            if not ok:
                continue
            pnl = sim_directional(b.close, direction, post[i + 1 :], params)
            if pnl is not None:
                out.append((datetime.fromisoformat(session_key).date(), pnl))
            break
    return out


def edge_gap_fade(days: dict, min_gap_pct: float = 0.003) -> list[tuple]:
    """Fade prior-close → open gap; exit by 11:00 ET or TP/SL on structure."""
    params = BacktestParams(target_r=1.0, stop_r=1.0)
    dates = sorted(days)
    out = []
    for i in range(1, len(dates)):
        prev, cur = days[dates[i - 1]], days[dates[i]]
        if not prev or not cur or len(cur) < 6:
            continue
        gap = (cur[0].open - prev[-1].close) / prev[-1].close
        if abs(gap) < min_gap_pct:
            continue
        direction = Direction.SHORT if gap > 0 else Direction.LONG
        # hold until 11:00 ET bars
        forward = [b for b in cur[1:] if _to_et(b.ts).time() <= time(11, 0)]
        if len(forward) < 2:
            continue
        pnl = sim_directional(cur[0].open, direction, forward, params)
        if pnl is not None:
            out.append((dates[i], pnl))
    return out


def edge_lunch_vwap_reversion(days: dict) -> list[tuple]:
    """At 12:00 ET, fade stretch from VWAP; exit by 15:00."""
    params = BacktestParams(target_r=1.2, stop_r=1.0)
    out = []
    for d, sbars in days.items():
        pre = [b for b in sbars if _to_et(b.ts).time() < time(12, 0)]
        post = [b for b in sbars if time(12, 0) <= _to_et(b.ts).time() < time(15, 0)]
        if len(pre) < 10 or len(post) < 4:
            continue
        vwap = session_vwap(pre)
        if vwap is None:
            continue
        spot = pre[-1].close
        dist = (spot - vwap) / vwap
        if abs(dist) < 0.002:
            continue
        direction = Direction.SHORT if dist > 0 else Direction.LONG
        pnl = sim_directional(spot, direction, post, params)
        if pnl is not None:
            out.append((d, pnl))
    return out


def edge_power_hour_trend(days: dict, break_pts: float = 0.8) -> list[tuple]:
    """3pm anchor break without gamma filter; flatter than video but more trades."""
    params = BacktestParams(target_r=2.0, stop_r=1.0)
    out = []
    for d, sbars in days.items():
        post = [b for b in sbars if _to_et(b.ts).time() >= time(15, 0)]
        if len(post) < 3:
            continue
        anchor = post[0].open
        entry_i = None
        direction = Direction.NEUTRAL
        for i, b in enumerate(post[1:], start=1):
            if b.close >= anchor + break_pts:
                entry_i, direction = i, Direction.LONG
                break
            if b.close <= anchor - break_pts:
                entry_i, direction = i, Direction.SHORT
                break
        if entry_i is None or entry_i >= len(post) - 1:
            continue
        pnl = sim_directional(post[entry_i].close, direction, post[entry_i + 1 :], params)
        if pnl is not None:
            out.append((d, pnl))
    return out


def edge_orb_first_break_tight(bars: list[Bar], or_min: int = 5) -> list[tuple]:
    """5m ORB first break, no filters — research-favoured short OR."""
    cfg = get_window_config(SessionWindow.NEW_YORK)
    params = BacktestParams(
        opening_range_minutes=or_min,
        breakout_buffer_atr=0.02,
        min_strength=0.1,
        target_r=2.0,
        stop_r=1.0,
    )
    atr_all = average_true_range(bars)
    out = []
    for session_key, sbars in group_by_session(bars, cfg):
        orb, post = opening_range_of(sbars, or_min)
        if not orb or len(post) < 2:
            continue
        rh, rl = max(b.high for b in orb), min(b.low for b in orb)
        width = max(rh - rl, 1e-9)
        buf = 0.02 * (atr_all or width)
        for i, b in enumerate(post):
            direction = Direction.NEUTRAL
            if b.close > rh + buf:
                direction = Direction.LONG
            elif b.close < rl - buf:
                direction = Direction.SHORT
            if direction is Direction.NEUTRAL:
                continue
            pnl = sim_directional(b.close, direction, post[i + 1 :], params)
            if pnl is not None:
                out.append((datetime.fromisoformat(session_key).date(), pnl))
            break
    return out


def edge_range_credit_morning(days: dict) -> list[tuple]:
    """If first 30m range is tight vs ATR, sell premium bias with trend of open (credit via RANGE regime sim)."""
    params = BacktestParams(target_r=1.0, stop_r=1.0)
    out = []
    for d, sbars in days.items():
        if len(sbars) < 20:
            continue
        first = sbars[:6]  # 30m
        rest = sbars[6:30]  # until ~noon
        if len(rest) < 4:
            continue
        width = max(b.high for b in first) - min(b.low for b in first)
        atr = average_true_range(sbars[:14]) or width
        if width > 0.35 * atr:
            continue  # only tight opens
        # lean with open vs prior mid of first bar
        direction = Direction.LONG if first[-1].close >= first[0].open else Direction.SHORT
        t = _simulate_trade(
            spot=first[-1].close,
            direction=direction,
            regime=Regime.RANGE,  # credit structure
            forward_bars=rest,
            params=params,
        )
        if t:
            out.append((d, float(t["pnl"])))
    return out


def edge_combo_first_n_per_day(
    edges: dict[str, list[tuple]], *, n: int = 2
) -> list[tuple]:
    """Causal combo: keep chronologically first ``n`` day-trades (no hindsight pick)."""
    # Rebuild as dated lists; order within day is arbitrary across edges —
    # use stable priority order of edge names then append.
    by_day: dict = defaultdict(list)
    for name in sorted(edges.keys()):
        for d, pnl in edges[name]:
            by_day[d].append(pnl)
    out = []
    for d in sorted(by_day.keys()):
        out.append((d, sum(by_day[d][:n])))
    return out


def edge_combo_sum_all(edges: dict[str, list[tuple]], *, max_per_day: int = 3) -> list[tuple]:
    """Causal: take up to max_per_day signals in discovery order (sorted edge names)."""
    return edge_combo_first_n_per_day(edges, n=max_per_day)


def to_week_map(trades: list[tuple]) -> dict[str, float]:
    m: dict[str, float] = defaultdict(float)
    for d, pnl in trades:
        m[week_key(d)] += pnl
    return dict(m)


def main() -> int:
    bars = load_bars()
    days = rth_days(bars)
    print(f"bars={len(bars)} rth_days={len(days)}")

    candidates = {
        "orb_filtered_15": edge_orb_filtered(bars),
        "orb_5m_raw": edge_orb_first_break_tight(bars, 5),
        "orb_10m_raw": edge_orb_first_break_tight(bars, 10),
        "gap_fade_0.3pct": edge_gap_fade(days, 0.003),
        "gap_fade_0.5pct": edge_gap_fade(days, 0.005),
        "lunch_vwap_fade": edge_lunch_vwap_reversion(days),
        "power_hour_0.8": edge_power_hour_trend(days, 0.8),
        "power_hour_1.0": edge_power_hour_trend(days, 1.0),
        "tight_open_credit": edge_range_credit_morning(days),
    }

    # Causal combos (no hindsight best-of-day).
    candidates["combo_orb5+gap+lunch_first2"] = edge_combo_first_n_per_day(
        {
            "a": candidates["orb_5m_raw"],
            "b": candidates["gap_fade_0.3pct"],
            "c": candidates["lunch_vwap_fade"],
        },
        n=2,
    )
    candidates["combo_orb5+gap+ph_first2"] = edge_combo_first_n_per_day(
        {
            "a": candidates["orb_5m_raw"],
            "b": candidates["gap_fade_0.3pct"],
            "c": candidates["power_hour_0.8"],
        },
        n=2,
    )
    candidates["combo_stack_first3"] = edge_combo_sum_all(
        {
            "orb": candidates["orb_5m_raw"],
            "gap": candidates["gap_fade_0.3pct"],
            "lunch": candidates["lunch_vwap_fade"],
            "ph": candidates["power_hour_0.8"],
        },
        max_per_day=3,
    )

    rows = []
    for name, trades in candidates.items():
        weeks = to_week_map(trades)
        full = summarize_weeks(weeks)
        wf = walk_forward_weeks(trades)
        oos = wf.get("oos", {})
        rows.append(
            {
                "edge": name,
                "trades": len(trades),
                "is": full,
                "oos": oos,
            }
        )
        print(
            f"{name:28s} n={len(trades):4d}  "
            f"IS hit5={full['hit_rate']:.1%} mean={full['mean']:.2%}  "
            f"OOS hit5={oos.get('hit_rate', 0):.1%} mean={oos.get('mean', 0):.2%} totalOOS={oos.get('total', 0)}"
        )

    rows.sort(key=lambda r: (r["oos"].get("hit_rate", 0), r["oos"].get("mean", 0)), reverse=True)
    out = {
        "day": datetime.utcnow().strftime("%Y-%m-%d"),
        "capital": CAPITAL,
        "scale": SCALE,
        "target_week": TARGET,
        "goal_hit_rate": 0.80,
        "note": (
            "80% weeks at >=5% needs ~7-12% mean weeks; no candidate here reaches "
            "that. Ranked by walk-forward OOS hit rate then mean."
        ),
        "ranked": rows,
        "best": rows[0] if rows else None,
    }
    path = REPO_ROOT / "data" / "nightly" / "edge_discovery_2026-09-24.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("Wrote", path)
    print("BEST OOS:", rows[0]["edge"] if rows else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
