"""Daily trend / structure / volatility regimes for MES 5ORB.

Built from prior completed days only (no look-ahead). Used to gate entries
and to condition walk-forward optimisation on multi-year patterns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

import pytz

from core.models import Bar, Direction

_EASTERN = pytz.timezone("America/New_York")


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(_EASTERN)


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float

    @property
    def range(self) -> float:
        return max(self.high - self.low, 0.0)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


@dataclass(frozen=True)
class DayRegime:
    """Causal regime snapshot for a trading day (uses only prior closes)."""

    day: date
    bias: str  # up | down | flat
    structure: str  # trend | range | uncertain
    vol: str  # high | mid | low
    sma: float | None
    efficiency: float | None
    atr: float | None
    atr_pctile: float | None

    @property
    def label(self) -> str:
        return f"{self.structure}_{self.bias}_{self.vol}"


@dataclass(frozen=True)
class RegimeFilterConfig:
    """Gates which days / directions the opens stack may trade."""

    enabled: bool = False
    sma_period: int = 20
    efficiency_lookback: int = 10
    vol_lookback: int = 60
    high_vol_pctile: float = 0.70
    low_vol_pctile: float = 0.30
    trend_efficiency: float = 0.45
    range_efficiency: float = 0.22
    # None = all structures allowed.
    allowed_structures: tuple[str, ...] | None = None
    # None = all vol buckets allowed.
    allowed_vol: tuple[str, ...] | None = None
    # None = all biases allowed (up/down/flat).
    allowed_biases: tuple[str, ...] | None = None
    # Long only on up bias, short only on down; flat days skipped.
    require_trend_align: bool = False
    # Skip days whose bias is flat (even if structure allowed).
    skip_flat: bool = False


def regime_config_from_raw(raw: dict[str, Any] | None) -> RegimeFilterConfig:
    raw = raw or {}
    structs = raw.get("allowed_structures")
    vols = raw.get("allowed_vol")
    biases = raw.get("allowed_biases")
    return RegimeFilterConfig(
        enabled=bool(raw.get("enabled", False)),
        sma_period=max(int(raw.get("sma_period", 20)), 2),
        efficiency_lookback=max(int(raw.get("efficiency_lookback", 10)), 3),
        vol_lookback=max(int(raw.get("vol_lookback", 60)), 10),
        high_vol_pctile=float(raw.get("high_vol_pctile", 0.70)),
        low_vol_pctile=float(raw.get("low_vol_pctile", 0.30)),
        trend_efficiency=float(raw.get("trend_efficiency", 0.45)),
        range_efficiency=float(raw.get("range_efficiency", 0.22)),
        allowed_structures=(
            tuple(str(s).lower() for s in structs) if structs is not None else None
        ),
        allowed_vol=(tuple(str(v).lower() for v in vols) if vols is not None else None),
        allowed_biases=(
            tuple(str(b).lower() for b in biases) if biases is not None else None
        ),
        require_trend_align=bool(raw.get("require_trend_align", False)),
        skip_flat=bool(raw.get("skip_flat", False)),
    )


def build_daily_bars(bars: Iterable[Bar]) -> list[DailyBar]:
    """Aggregate 5m (or any intraday) bars to ET session days."""
    buckets: dict[date, list[Bar]] = {}
    for b in bars:
        d = _to_et(b.ts).date()
        buckets.setdefault(d, []).append(b)
    out: list[DailyBar] = []
    for d in sorted(buckets):
        day_bars = buckets[d]
        out.append(
            DailyBar(
                day=d,
                open=float(day_bars[0].open),
                high=max(float(b.high) for b in day_bars),
                low=min(float(b.low) for b in day_bars),
                close=float(day_bars[-1].close),
            )
        )
    return out


def build_regime_map(
    bars: list[Bar],
    *,
    cfg: RegimeFilterConfig | None = None,
) -> dict[date, DayRegime]:
    """Map each trading day → regime from prior days only (O(n) pass)."""
    cfg = cfg or RegimeFilterConfig()
    dailies = build_daily_bars(bars)
    if len(dailies) < 2:
        return {}

    closes = [d.close for d in dailies]
    # True range series (aligned to dailies[1:]).
    trs: list[float] = []
    for prev, cur in zip(dailies[:-1], dailies[1:]):
        trs.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )

    out: dict[date, DayRegime] = {}
    atr_hist: list[float] = []
    for i in range(1, len(dailies)):
        # History available for day i is dailies[:i] (completed before day i).
        hist_closes = closes[:i]
        sma = None
        if len(hist_closes) >= cfg.sma_period:
            sma = sum(hist_closes[-cfg.sma_period :]) / cfg.sma_period

        eff = None
        if i >= cfg.efficiency_lookback:
            window = dailies[i - cfg.efficiency_lookback : i]
            net = abs(window[-1].close - window[0].open)
            path = sum(d.body for d in window) + sum(
                abs(b.open - a.close) for a, b in zip(window[:-1], window[1:])
            )
            if path > 0:
                eff = net / path

        atr = None
        # TR ending at dailies[i-1] is trs[i-2] when i>=2.
        if i >= 2:
            period = min(cfg.sma_period, i - 1)
            atr = sum(trs[i - 1 - period : i - 1]) / period
            atr_hist.append(atr)

        atr_pctile = None
        if atr is not None and atr_hist:
            window = atr_hist[-cfg.vol_lookback :]
            atr_pctile = sum(1 for x in window if x <= atr) / len(window)

        last = hist_closes[-1]
        if sma is None:
            bias = "flat"
        elif last > sma * 1.001:
            bias = "up"
        elif last < sma * 0.999:
            bias = "down"
        else:
            bias = "flat"

        if eff is None:
            structure = "uncertain"
        elif eff >= cfg.trend_efficiency:
            structure = "trend"
        elif eff <= cfg.range_efficiency:
            structure = "range"
        else:
            structure = "uncertain"

        if atr_pctile is None:
            vol = "mid"
        elif atr_pctile >= cfg.high_vol_pctile:
            vol = "high"
        elif atr_pctile <= cfg.low_vol_pctile:
            vol = "low"
        else:
            vol = "mid"

        out[dailies[i].day] = DayRegime(
            day=dailies[i].day,
            bias=bias,
            structure=structure,
            vol=vol,
            sma=sma,
            efficiency=eff,
            atr=atr,
            atr_pctile=atr_pctile,
        )
    return out


def allows_regime(cfg: RegimeFilterConfig, ctx: DayRegime | None) -> bool:
    """True if the day may trade under ``cfg``."""
    if not cfg.enabled:
        return True
    if ctx is None:
        return False
    if cfg.skip_flat and ctx.bias == "flat":
        return False
    if cfg.allowed_structures is not None and ctx.structure not in cfg.allowed_structures:
        return False
    if cfg.allowed_vol is not None and ctx.vol not in cfg.allowed_vol:
        return False
    if cfg.allowed_biases is not None and ctx.bias not in cfg.allowed_biases:
        return False
    if cfg.require_trend_align and ctx.bias == "flat":
        return False
    return True


def aligned_directions(cfg: RegimeFilterConfig, ctx: DayRegime | None) -> str | None:
    """
    Return allowed_directions override for the day, or None to leave session default.

    When ``require_trend_align``: up→long, down→short, flat→skip (caller should
    treat None + require_trend_align as skip via ``allows_regime``).
    """
    if not cfg.enabled or not cfg.require_trend_align or ctx is None:
        return None
    if ctx.bias == "up":
        return "long"
    if ctx.bias == "down":
        return "short"
    return None


def direction_matches_bias(direction: Direction, bias: str) -> bool:
    if bias == "up":
        return direction is Direction.LONG
    if bias == "down":
        return direction is Direction.SHORT
    return False


def summarize_regimes(regime_map: Mapping[date, DayRegime]) -> dict[str, Any]:
    """Counts and crude forward-structure distribution over the map."""
    counts: dict[str, int] = {}
    by_struct: dict[str, int] = {}
    by_bias: dict[str, int] = {}
    by_vol: dict[str, int] = {}
    for ctx in regime_map.values():
        counts[ctx.label] = counts.get(ctx.label, 0) + 1
        by_struct[ctx.structure] = by_struct.get(ctx.structure, 0) + 1
        by_bias[ctx.bias] = by_bias.get(ctx.bias, 0) + 1
        by_vol[ctx.vol] = by_vol.get(ctx.vol, 0) + 1
    return {
        "days": len(regime_map),
        "by_structure": by_struct,
        "by_bias": by_bias,
        "by_vol": by_vol,
        "by_label": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:20]),
    }


def daily_forward_returns(
    bars: list[Bar],
    regime_map: Mapping[date, DayRegime],
) -> list[dict[str, Any]]:
    """Join next-day close-to-close return with the day's regime (pattern scan)."""
    dailies = build_daily_bars(bars)
    rows: list[dict[str, Any]] = []
    for i in range(len(dailies) - 1):
        d0 = dailies[i]
        d1 = dailies[i + 1]
        ctx = regime_map.get(d0.day)
        if ctx is None:
            continue
        ret = (d1.close - d0.close) / d0.close if d0.close else 0.0
        rows.append(
            {
                "day": d0.day.isoformat(),
                "weekday": d0.day.weekday(),
                "month": d0.day.month,
                "bias": ctx.bias,
                "structure": ctx.structure,
                "vol": ctx.vol,
                "label": ctx.label,
                "fwd_ret": ret,
                "aligned_ret": ret
                if ctx.bias == "up"
                else (-ret if ctx.bias == "down" else 0.0),
            }
        )
    return rows


def aggregate_pattern_expectancy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean forward return by structure / bias / weekday / month."""

    def _bucket(key: str) -> dict[str, dict[str, float]]:
        groups: dict[str, list[float]] = {}
        aligned: dict[str, list[float]] = {}
        for r in rows:
            k = str(r[key])
            groups.setdefault(k, []).append(float(r["fwd_ret"]))
            aligned.setdefault(k, []).append(float(r["aligned_ret"]))
        out: dict[str, dict[str, float]] = {}
        for k, vals in groups.items():
            a = aligned[k]
            out[k] = {
                "n": len(vals),
                "mean_fwd_ret": sum(vals) / len(vals),
                "mean_aligned_ret": sum(a) / len(a) if a else 0.0,
            }
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["mean_aligned_ret"]))

    return {
        "by_structure": _bucket("structure"),
        "by_bias": _bucket("bias"),
        "by_vol": _bucket("vol"),
        "by_weekday": _bucket("weekday"),
        "by_month": _bucket("month"),
        "by_label": _bucket("label"),
    }


def suggest_from_trade_regimes(
    ranked: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build filter candidates from ORB trade PnL by structure|bias|vol."""
    if not ranked:
        return []
    by_struct: dict[str, list[float]] = {}
    by_bias: dict[str, list[float]] = {}
    by_vol: dict[str, list[float]] = {}
    for r in ranked:
        parts = str(r["key"]).split("|")
        if len(parts) != 3:
            continue
        s, b, v = parts
        avg = float(r["avg"])
        n = int(r["trades"])
        by_struct.setdefault(s, []).extend([avg] * n)
        by_bias.setdefault(b, []).extend([avg] * n)
        by_vol.setdefault(v, []).extend([avg] * n)

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    cands: list[dict[str, Any]] = []
    struct_means = {k: _mean(v) for k, v in by_struct.items()}
    bias_means = {k: _mean(v) for k, v in by_bias.items()}
    vol_means = {k: _mean(v) for k, v in by_vol.items()}

    good_structs = tuple(k for k, m in struct_means.items() if m > 0)
    bad_structs = tuple(k for k, m in struct_means.items() if m <= 0)
    if good_structs and bad_structs:
        cands.append(
            {
                "label": "trade_keep_pos_structs",
                "allowed_structures": good_structs,
            }
        )
    if struct_means.get("range", 0) > struct_means.get("trend", 0):
        cands.append(
            {
                "label": "trade_range_uncertain",
                "allowed_structures": ("range", "uncertain"),
            }
        )

    good_bias = tuple(k for k, m in bias_means.items() if m > 0 and k != "flat")
    if good_bias:
        cands.append(
            {
                "label": "trade_pos_bias",
                "allowed_biases": good_bias,
                "skip_flat": True,
            }
        )
    if bias_means.get("down", 0) > bias_means.get("up", 0):
        cands.append(
            {
                "label": "trade_down_bias",
                "allowed_biases": ("down",),
            }
        )
        cands.append(
            {
                "label": "trade_range_down",
                "allowed_structures": ("range", "uncertain"),
                "allowed_biases": ("down",),
            }
        )

    good_vol = tuple(k for k, m in vol_means.items() if m > 0)
    if good_vol and len(good_vol) < 3:
        cands.append({"label": "trade_pos_vol", "allowed_vol": good_vol})
    if vol_means.get("high", 0) > vol_means.get("low", 0):
        cands.append(
            {
                "label": "trade_high_vol",
                "allowed_vol": ("high", "mid"),
            }
        )
        cands.append(
            {
                "label": "trade_range_down_high",
                "allowed_structures": ("range", "uncertain"),
                "allowed_biases": ("down",),
                "allowed_vol": ("high", "mid"),
            }
        )
    return cands


def suggest_regime_filters(expectancy: dict[str, Any]) -> list[dict[str, Any]]:
    """Heuristic filter candidates from multi-year forward-return patterns."""
    cands: list[dict[str, Any]] = []
    by_struct = expectancy.get("by_structure") or {}
    by_vol = expectancy.get("by_vol") or {}
    by_bias = expectancy.get("by_bias") or {}

    trend = by_struct.get("trend") or {}
    range_ = by_struct.get("range") or {}
    uncertain = by_struct.get("uncertain") or {}
    if (range_.get("mean_aligned_ret") or 0) > (trend.get("mean_aligned_ret") or 0):
        cands.append(
            {
                "label": "fwd_range_uncertain",
                "allowed_structures": ("range", "uncertain"),
            }
        )
    if (uncertain.get("mean_aligned_ret") or 0) > (trend.get("mean_aligned_ret") or 0):
        cands.append(
            {
                "label": "fwd_skip_trend",
                "allowed_structures": ("range", "uncertain"),
            }
        )
    if (
        trend.get("mean_aligned_ret", 0) > 0
        and (range_.get("mean_aligned_ret") or 0) < (trend.get("mean_aligned_ret") or 0)
    ):
        cands.append(
            {
                "label": "trend_only",
                "allowed_structures": ("trend",),
            }
        )
        cands.append(
            {
                "label": "trend_align",
                "allowed_structures": ("trend", "uncertain"),
                "require_trend_align": True,
            }
        )

    high = by_vol.get("high") or {}
    mid = by_vol.get("mid") or {}
    low = by_vol.get("low") or {}
    if (low.get("mean_aligned_ret") or 0) > (high.get("mean_aligned_ret") or 0):
        cands.append({"label": "skip_high_vol", "allowed_vol": ("mid", "low")})
    if (high.get("mean_fwd_ret") or 0) > (low.get("mean_fwd_ret") or 0):
        cands.append({"label": "fwd_prefer_high_vol", "allowed_vol": ("high", "mid")})

    up = by_bias.get("up") or {}
    down = by_bias.get("down") or {}
    if (up.get("mean_aligned_ret") or 0) > 0 or (down.get("mean_aligned_ret") or 0) > 0:
        cands.append(
            {
                "label": "align_bias",
                "require_trend_align": True,
                "skip_flat": True,
            }
        )

    cands.insert(0, {"label": "regime_off", "enabled": False})
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for c in cands:
        if c["label"] in seen:
            continue
        seen.add(c["label"])
        uniq.append(c)
    return uniq
