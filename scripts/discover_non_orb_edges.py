"""Non-ORB edge discovery on SPY 5m Barchart.

No opening-range breakout rules. Candidates are mean-reversion, momentum,
overnight, and session-timing effects. Ranked by walk-forward OOS P(week>=5%)
at 2x contracts on £1000.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytz

from core.backtest import BacktestParams, _simulate_trade
from core.config import REPO_ROOT
from core.models import Bar, Direction, Regime
from core.sessions import EASTERN
from core.strategy.orb import average_true_range, session_vwap
from core.timeutils import as_naive_utc

CAPITAL = 1000.0
SCALE = 2.0
TARGET = 0.05
CSV = REPO_ROOT / "data" / "history" / "SPY_5mins_barchart.csv"


def load_bars() -> list[Bar]:
    bars: list[Bar] = []
    with CSV.open(encoding="utf-8", newline="") as fh:
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


def to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


def rth_days(bars: list[Bar]) -> dict:
    days: dict = defaultdict(list)
    for b in bars:
        et = to_et(b.ts)
        if time(9, 30) <= et.time() < time(16, 0):
            days[et.date()].append(b)
    return {d: sorted(v, key=lambda x: x.ts) for d, v in days.items()}


def week_key(d) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def sim(spot: float, direction: Direction, forward: list[Bar], *, tr: float = 1.5, sr: float = 1.0, regime: Regime = Regime.TREND) -> float | None:
    if len(forward) < 2:
        return None
    t = _simulate_trade(
        spot=spot,
        direction=direction,
        regime=regime,
        forward_bars=forward,
        params=BacktestParams(target_r=tr, stop_r=sr),
    )
    return None if t is None else float(t["pnl"])


def summarize(trades: list[tuple]) -> dict:
    by: dict[str, float] = defaultdict(float)
    for d, pnl in trades:
        by[week_key(d)] += pnl
    if not by:
        return {"weeks": 0, "hit_rate": 0.0, "mean": 0.0, "median": 0.0, "total": 0.0, "trades": 0}
    rets = sorted((v * SCALE) / CAPITAL for v in by.values())
    hits = sum(1 for r in rets if r >= TARGET)
    return {
        "weeks": len(rets),
        "hit_rate": round(hits / len(rets), 4),
        "mean": round(sum(rets) / len(rets), 4),
        "median": round(rets[len(rets) // 2], 4),
        "total": round(sum(rets) * CAPITAL, 2),
        "trades": len(trades),
        "best": round(rets[-1], 4),
        "worst": round(rets[0], 4),
    }


def walk_forward(trades: list[tuple], folds: int = 3) -> dict:
    by: dict[str, float] = defaultdict(float)
    for d, pnl in trades:
        by[week_key(d)] += pnl
    keys = sorted(by)
    if len(keys) < folds + 1:
        return summarize([])
    seg = len(keys) // (folds + 1)
    oos: dict[str, float] = {}
    for f in range(folds):
        for k in keys[seg * (f + 1) : seg * (f + 2)]:
            oos[k] = by[k]
    # reuse summarize via fake trades
    fake = []
    for k, v in oos.items():
        # one synthetic day per week
        y, w = k.split("-W")
        fake.append((datetime.fromisocalendar(int(y), int(w), 1).date(), v / SCALE))
    return summarize(fake)


# ---- Non-ORB edges --------------------------------------------------------

def edge_overnight_long(days: dict) -> list[tuple]:
    """Buy close, 'exit' next open via first bar — modeled as long into overnight gap then flat at open+30m."""
    dates = sorted(days)
    out = []
    params_tr, params_sr = 1.0, 1.0
    for i in range(len(dates) - 1):
        a, b = days[dates[i]], days[dates[i + 1]]
        if len(a) < 3 or len(b) < 4:
            continue
        entry = a[-1].close
        # forward = next morning first hour
        forward = [x for x in b if to_et(x.ts).time() <= time(10, 30)]
        pnl = sim(entry, Direction.LONG, forward, tr=params_tr, sr=params_sr)
        if pnl is not None:
            out.append((dates[i + 1], pnl))
    return out


def edge_overnight_fade_gap(days: dict, min_gap: float = 0.002) -> list[tuple]:
    """If overnight gap >= min, fade it from the open until 11:00."""
    dates = sorted(days)
    out = []
    for i in range(1, len(dates)):
        prev, cur = days[dates[i - 1]], days[dates[i]]
        gap = (cur[0].open - prev[-1].close) / prev[-1].close
        if abs(gap) < min_gap:
            continue
        direction = Direction.SHORT if gap > 0 else Direction.LONG
        forward = [x for x in cur[1:] if to_et(x.ts).time() <= time(11, 0)]
        pnl = sim(cur[0].open, direction, forward, tr=1.2, sr=1.0)
        if pnl is not None:
            out.append((dates[i], pnl))
    return out


def edge_vwap_snap(days: dict, z: float = 0.004, start: time = time(10, 0), end: time = time(15, 0)) -> list[tuple]:
    """When price is stretched from session VWAP, fade back; exit by 15:55."""
    out = []
    for d, sbars in days.items():
        window = [b for b in sbars if start <= to_et(b.ts).time() < end]
        if len(window) < 8:
            continue
        # scan for first stretch
        for i in range(6, len(window) - 3):
            vwap = session_vwap(window[: i + 1])
            if vwap is None or vwap <= 0:
                continue
            dist = (window[i].close - vwap) / vwap
            if abs(dist) < z:
                continue
            direction = Direction.SHORT if dist > 0 else Direction.LONG
            forward = window[i + 1 :]
            # also allow to end of day
            rest = [b for b in sbars if b.ts > window[i].ts]
            pnl = sim(window[i].close, direction, rest[:24], tr=1.5, sr=1.0)
            if pnl is not None:
                out.append((d, pnl))
            break
    return out


def edge_rsi_reversion(days: dict, period: int = 14, lo: float = 25, hi: float = 75) -> list[tuple]:
    """Intraday RSI(close) extreme → fade for next 12 bars."""
    out = []
    for d, sbars in days.items():
        if len(sbars) < period + 15:
            continue
        closes = [b.close for b in sbars]
        # wilder-ish simple RSI
        for i in range(period + 1, len(sbars) - 12):
            changes = [closes[j] - closes[j - 1] for j in range(i - period + 1, i + 1)]
            gains = sum(max(c, 0) for c in changes) / period
            losses = sum(max(-c, 0) for c in changes) / period
            if losses <= 1e-12:
                rsi = 100.0
            else:
                rs = gains / losses
                rsi = 100 - (100 / (1 + rs))
            if lo < rsi < hi:
                continue
            direction = Direction.LONG if rsi <= lo else Direction.SHORT
            # only during liquid hours
            if not (time(10, 0) <= to_et(sbars[i].ts).time() <= time(14, 30)):
                continue
            pnl = sim(sbars[i].close, direction, sbars[i + 1 : i + 13], tr=1.5, sr=1.0)
            if pnl is not None:
                out.append((d, pnl))
            break
    return out


def edge_prior_day_break(days: dict) -> list[tuple]:
    """Break of prior day high/low after 10:00, hold to 15:30."""
    dates = sorted(days)
    out = []
    for i in range(1, len(dates)):
        prev, cur = days[dates[i - 1]], days[dates[i]]
        phd = max(b.high for b in prev)
        pld = min(b.low for b in prev)
        post = [b for b in cur if to_et(b.ts).time() >= time(10, 0)]
        if len(post) < 5:
            continue
        entry_i = None
        direction = Direction.NEUTRAL
        for j, b in enumerate(post):
            if b.close > phd:
                entry_i, direction = j, Direction.LONG
                break
            if b.close < pld:
                entry_i, direction = j, Direction.SHORT
                break
        if entry_i is None or entry_i >= len(post) - 2:
            continue
        forward = [b for b in post[entry_i + 1 :] if to_et(b.ts).time() <= time(15, 30)]
        pnl = sim(post[entry_i].close, direction, forward, tr=2.0, sr=1.0)
        if pnl is not None:
            out.append((dates[i], pnl))
    return out


def edge_open_drive(days: dict) -> list[tuple]:
    """First 15m close vs open direction; enter 09:45 continuation until 11:30."""
    out = []
    for d, sbars in days.items():
        first = [b for b in sbars if to_et(b.ts).time() < time(9, 45)]
        rest = [b for b in sbars if time(9, 45) <= to_et(b.ts).time() <= time(11, 30)]
        if len(first) < 2 or len(rest) < 3:
            continue
        direction = Direction.LONG if first[-1].close >= first[0].open else Direction.SHORT
        pnl = sim(rest[0].open, direction, rest[1:], tr=1.5, sr=1.0)
        if pnl is not None:
            out.append((d, pnl))
    return out


def edge_atr_expansion_break(days: dict) -> list[tuple]:
    """After 11:00, if 30m range > 1.2x morning ATR, trade break of that 30m box to 15:00."""
    out = []
    for d, sbars in days.items():
        morning = [b for b in sbars if to_et(b.ts).time() < time(11, 0)]
        box = [b for b in sbars if time(11, 0) <= to_et(b.ts).time() < time(11, 30)]
        after = [b for b in sbars if to_et(b.ts).time() >= time(11, 30)]
        if len(morning) < 10 or len(box) < 3 or len(after) < 5:
            continue
        atr = average_true_range(morning) or 0.5
        bh, bl = max(b.high for b in box), min(b.low for b in box)
        if (bh - bl) < 1.2 * atr:
            continue
        entry_i = None
        direction = Direction.NEUTRAL
        for j, b in enumerate(after):
            if b.close > bh:
                entry_i, direction = j, Direction.LONG
                break
            if b.close < bl:
                entry_i, direction = j, Direction.SHORT
                break
        if entry_i is None:
            continue
        forward = [b for b in after[entry_i + 1 :] if to_et(b.ts).time() <= time(15, 0)]
        pnl = sim(after[entry_i].close, direction, forward, tr=1.8, sr=1.0)
        if pnl is not None:
            out.append((d, pnl))
    return out


def edge_climax_volume_fade(days: dict) -> list[tuple]:
    """Bar volume >= 2.5x prior 20-bar avg and range wide → fade next 8 bars."""
    out = []
    for d, sbars in days.items():
        if len(sbars) < 30:
            continue
        for i in range(20, len(sbars) - 8):
            if not (time(10, 0) <= to_et(sbars[i].ts).time() <= time(14, 0)):
                continue
            avg = sum(b.volume for b in sbars[i - 20 : i]) / 20
            if avg <= 0 or sbars[i].volume < 2.5 * avg:
                continue
            rng = sbars[i].high - sbars[i].low
            atr = average_true_range(sbars[i - 14 : i + 1]) or rng
            if rng < 1.2 * atr:
                continue
            # fade the close direction of climax bar
            direction = Direction.SHORT if sbars[i].close >= sbars[i].open else Direction.LONG
            pnl = sim(sbars[i].close, direction, sbars[i + 1 : i + 9], tr=1.2, sr=1.0)
            if pnl is not None:
                out.append((d, pnl))
            break
    return out


def edge_ib_break_midday(days: dict) -> list[tuple]:
    """Initial balance = first hour; break after 11:00 (classic IB, not ORB minutes param)."""
    out = []
    for d, sbars in days.items():
        ib = [b for b in sbars if to_et(b.ts).time() < time(10, 30)]
        after = [b for b in sbars if to_et(b.ts).time() >= time(11, 0)]
        if len(ib) < 5 or len(after) < 5:
            continue
        ih, il = max(b.high for b in ib), min(b.low for b in ib)
        entry_i = None
        direction = Direction.NEUTRAL
        for j, b in enumerate(after):
            if b.close > ih:
                entry_i, direction = j, Direction.LONG
                break
            if b.close < il:
                entry_i, direction = j, Direction.SHORT
                break
        if entry_i is None:
            continue
        forward = [b for b in after[entry_i + 1 :] if to_et(b.ts).time() <= time(15, 30)]
        pnl = sim(after[entry_i].close, direction, forward, tr=2.0, sr=1.0)
        if pnl is not None:
            out.append((d, pnl))
    return out


def edge_credit_chop(days: dict) -> list[tuple]:
    """Tight morning (low ATR expansion): sell premium (RANGE regime) with open bias until 14:00."""
    out = []
    for d, sbars in days.items():
        morn = [b for b in sbars if to_et(b.ts).time() < time(11, 0)]
        rest = [b for b in sbars if time(11, 0) <= to_et(b.ts).time() <= time(14, 0)]
        if len(morn) < 10 or len(rest) < 5:
            continue
        atr = average_true_range(morn) or 1.0
        width = max(b.high for b in morn) - min(b.low for b in morn)
        if width > 0.8 * atr:
            continue
        direction = Direction.LONG if morn[-1].close >= morn[0].open else Direction.SHORT
        pnl = sim(morn[-1].close, direction, rest, tr=1.0, sr=1.0, regime=Regime.RANGE)
        if pnl is not None:
            out.append((d, pnl))
    return out


def combo_first_n(edges: dict[str, list[tuple]], n: int = 2) -> list[tuple]:
    by: dict = defaultdict(list)
    for name in sorted(edges):
        for d, pnl in edges[name]:
            by[d].append(pnl)
    return [(d, sum(by[d][:n])) for d in sorted(by)]


def main() -> int:
    bars = load_bars()
    days = rth_days(bars)
    print(f"bars={len(bars)} days={len(days)}")

    cands = {
        "overnight_long": edge_overnight_long(days),
        "gap_fade_0.2": edge_overnight_fade_gap(days, 0.002),
        "gap_fade_0.4": edge_overnight_fade_gap(days, 0.004),
        "vwap_snap_0.4pct": edge_vwap_snap(days, 0.004),
        "vwap_snap_0.6pct": edge_vwap_snap(days, 0.006),
        "rsi_25_75": edge_rsi_reversion(days, lo=25, hi=75),
        "rsi_20_80": edge_rsi_reversion(days, lo=20, hi=80),
        "prior_day_break": edge_prior_day_break(days),
        "open_drive": edge_open_drive(days),
        "atr_expansion_break": edge_atr_expansion_break(days),
        "volume_climax_fade": edge_climax_volume_fade(days),
        "initial_balance_break": edge_ib_break_midday(days),
        "credit_chop_morning": edge_credit_chop(days),
    }
    # best singles for combo
    cands["combo_gap+vwap+rsi"] = combo_first_n(
        {
            "g": cands["gap_fade_0.2"],
            "v": cands["vwap_snap_0.4pct"],
            "r": cands["rsi_25_75"],
        },
        n=2,
    )
    cands["combo_ib+gap+credit"] = combo_first_n(
        {
            "i": cands["initial_balance_break"],
            "g": cands["gap_fade_0.2"],
            "c": cands["credit_chop_morning"],
        },
        n=2,
    )
    cands["combo_meanrev_stack"] = combo_first_n(
        {
            "g": cands["gap_fade_0.2"],
            "v": cands["vwap_snap_0.4pct"],
            "r": cands["rsi_25_75"],
            "x": cands["volume_climax_fade"],
        },
        n=3,
    )

    rows = []
    for name, trades in cands.items():
        is_ = summarize(trades)
        oos = walk_forward(trades)
        rows.append({"edge": name, "is": is_, "oos": oos})
        print(
            f"{name:28s} n={is_['trades']:4d}  "
            f"IS hit={is_['hit_rate']:.1%} mean={is_['mean']:.2%}  "
            f"OOS hit={oos['hit_rate']:.1%} mean={oos['mean']:.2%} £={oos['total']}"
        )

    rows.sort(key=lambda r: (r["oos"]["hit_rate"], r["oos"]["mean"], r["oos"]["total"]), reverse=True)
    out = {
        "day": datetime.utcnow().strftime("%Y-%m-%d"),
        "family": "non_orb",
        "scale": SCALE,
        "target": TARGET,
        "ranked": rows,
        "best": rows[0] if rows else None,
        "note": "No ORB rules. 80% hit at 5%/week still blocked by SPY weekly distribution.",
    }
    path = REPO_ROOT / "data" / "nightly" / "non_orb_discovery_2026-09-24.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("BEST:", rows[0]["edge"] if rows else None)
    print("Wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
