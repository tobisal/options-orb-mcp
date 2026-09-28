"""ICT Asian Range (Market Maker Primer) — Judas sweep + reclaim.

Source model (ICT Forex Market Maker Primer — Implementing The Asian Range):
- Mark Asia high/low from 20:00–00:00 America/New_York (modern ICT; primer once
  said 19:00, later mentorship uses 20:00).
- Bias proxy from price vs Asia EQ at London search open.
- Bullish: sweep Asia low (SSL), reclaim close back above → long.
- Bearish: sweep Asia high (BSL), reclaim close back below → short.
- Target: opposite Asia extreme (or 2R). Prefer partials over aggressive trails.

Refs:
- https://forum.ictsharks.com/t/ict-forex-market-maker-primer-course-implementing-the-asian-range/234
- https://innercircletrader.net/tutorials/ict-asian-range/
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

import pytz

from core.models import Bar, Direction

EASTERN = pytz.timezone("America/New_York")


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


class AsiaSetupState(str, Enum):
    IDLE = "idle"
    SWEPT = "swept"
    RECLAIMED = "reclaimed"
    INVALID = "invalid"


@dataclass(frozen=True)
class AsiaRangeConfig:
    enabled: bool = False
    # Range build window (America/New_York wall clock).
    range_start: time = time(20, 0)
    range_end: time = time(0, 0)  # midnight — ends at 00:00 of the trade day
    # Judas search / manage (London → early NY).
    search_start: time = time(2, 0)
    search_end: time = time(11, 0)
    force_flat: time = time(11, 55)
    min_width_points: float = 2.0
    max_width_points: float = 40.0
    stop_buffer_ticks: int = 2
    # "opposite_extreme" = ARH for longs / ARL for shorts; "2R" = risk multiple.
    target_mode: str = "opposite_extreme"
    target_r: float = 2.0
    scale_fraction: float = 0.5
    # If True, only take Judas on the EQ-implied side (above EQ → bearish sweep).
    require_eq_bias: bool = True


@dataclass(frozen=True)
class AsiaRange:
    """Asia range that feeds London/NY — ``trade_day`` is the calendar day after range end."""

    trade_day: date
    high: float
    low: float
    eq: float
    midnight_open: float | None
    bar_count: int
    skipped: bool = False
    skip_reason: str = ""

    @property
    def width(self) -> float:
        return self.high - self.low


@dataclass
class AsiaJudasSetup:
    trade_day: date
    direction: Direction
    asia_high: float
    asia_low: float
    asia_eq: float
    sweep_extreme: float
    entry_bar_index: int | None = None
    entry_price: float | None = None
    initial_stop: float | None = None
    target_price: float | None = None
    target_label: str = "asia_opposite"
    state: AsiaSetupState = AsiaSetupState.IDLE
    notes: str = ""


def compute_asia_range(
    bars: list[Bar],
    trade_day: date,
    cfg: AsiaRangeConfig,
) -> AsiaRange | None:
    """Build Asia H/L for the window ending at midnight of ``trade_day``.

    Default window: previous calendar day 20:00 ET through 00:00 ET on ``trade_day``
    (midnight open bar is recorded but post-midnight prices are not in the range).
    """
    prev = trade_day - timedelta(days=1)
    range_bars: list[Bar] = []
    midnight_open: float | None = None

    for b in bars:
        et = _to_et(b.ts)
        d, t = et.date(), et.time()
        # Evening slice on the previous calendar day (e.g. 20:00–23:55).
        if d == prev and t >= cfg.range_start:
            range_bars.append(b)
        # Optional morning slice when range_end is after midnight (rare).
        elif d == trade_day and cfg.range_end != time(0, 0) and t < cfg.range_end:
            range_bars.append(b)
        # Capture NY midnight open on trade_day.
        if d == trade_day and midnight_open is None:
            midnight_open = b.open

    if not range_bars:
        return None

    hi = max(b.high for b in range_bars)
    lo = min(b.low for b in range_bars)
    width = hi - lo
    eq = (hi + lo) / 2.0
    if width < cfg.min_width_points:
        return AsiaRange(
            trade_day=trade_day,
            high=hi,
            low=lo,
            eq=eq,
            midnight_open=midnight_open,
            bar_count=len(range_bars),
            skipped=True,
            skip_reason=f"asia range too narrow ({width:.2f} < {cfg.min_width_points})",
        )
    if width > cfg.max_width_points:
        return AsiaRange(
            trade_day=trade_day,
            high=hi,
            low=lo,
            eq=eq,
            midnight_open=midnight_open,
            bar_count=len(range_bars),
            skipped=True,
            skip_reason=f"asia range too wide ({width:.2f} > {cfg.max_width_points})",
        )
    return AsiaRange(
        trade_day=trade_day,
        high=hi,
        low=lo,
        eq=eq,
        midnight_open=midnight_open,
        bar_count=len(range_bars),
    )


def _bias_at_search_open(
    bars: list[Bar],
    trade_day: date,
    ar: AsiaRange,
    cfg: AsiaRangeConfig,
) -> Direction:
    """EQ bias: above EQ → expect bearish Judas (sweep high); below → bullish."""
    for b in bars:
        et = _to_et(b.ts)
        if et.date() != trade_day:
            continue
        if et.time() >= cfg.search_start:
            if b.close >= ar.eq:
                return Direction.SHORT
            return Direction.LONG
    return Direction.NEUTRAL


def detect_asia_judas(
    bars: list[Bar],
    ar: AsiaRange,
    cfg: AsiaRangeConfig,
    *,
    tick_size: float = 0.25,
    after_bar_index: int | None = None,
) -> AsiaJudasSetup | None:
    """Scan London/NY search window for Asia liquidity sweep + reclaim close."""
    if ar.skipped or ar.high <= ar.low:
        return None

    bias = _bias_at_search_open(bars, ar.trade_day, ar, cfg)
    if cfg.require_eq_bias and bias is Direction.NEUTRAL:
        return None

    buf = cfg.stop_buffer_ticks * tick_size
    post: list[tuple[int, Bar]] = []
    for i, b in enumerate(bars):
        if after_bar_index is not None and i <= after_bar_index:
            continue
        et = _to_et(b.ts)
        if et.date() != ar.trade_day:
            continue
        if cfg.search_start <= et.time() < cfg.search_end:
            post.append((i, b))
    if not post:
        return None

    allow_long = (not cfg.require_eq_bias) or bias is Direction.LONG
    allow_short = (not cfg.require_eq_bias) or bias is Direction.SHORT

    swept_long = False
    swept_short = False
    sweep_low = ar.low
    sweep_high = ar.high

    for i, b in post:
        if allow_long and b.low < ar.low:
            swept_long = True
            sweep_low = min(sweep_low, b.low)
        if allow_short and b.high > ar.high:
            swept_short = True
            sweep_high = max(sweep_high, b.high)

        # Reclaim / CHoCH proxy: close back through the swept Asia extreme
        if swept_long and b.close > ar.low:
            entry = float(b.close)
            stop = sweep_low - buf
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            if cfg.target_mode == "2R":
                target = entry + cfg.target_r * risk
                label = "2R"
            else:
                target = ar.high
                label = "asia_high"
            return AsiaJudasSetup(
                trade_day=ar.trade_day,
                direction=Direction.LONG,
                asia_high=ar.high,
                asia_low=ar.low,
                asia_eq=ar.eq,
                sweep_extreme=sweep_low,
                entry_bar_index=i,
                entry_price=entry,
                initial_stop=stop,
                target_price=target,
                target_label=label,
                state=AsiaSetupState.RECLAIMED,
                notes="asia Judas long: SSL sweep + reclaim",
            )
        if swept_short and b.close < ar.high:
            entry = float(b.close)
            stop = sweep_high + buf
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            if cfg.target_mode == "2R":
                target = entry - cfg.target_r * risk
                label = "2R"
            else:
                target = ar.low
                label = "asia_low"
            return AsiaJudasSetup(
                trade_day=ar.trade_day,
                direction=Direction.SHORT,
                asia_high=ar.high,
                asia_low=ar.low,
                asia_eq=ar.eq,
                sweep_extreme=sweep_high,
                entry_bar_index=i,
                entry_price=entry,
                initial_stop=stop,
                target_price=target,
                target_label=label,
                state=AsiaSetupState.RECLAIMED,
                notes="asia Judas short: BSL sweep + reclaim",
            )

    return None


def asia_config_from_raw(raw: dict | None) -> AsiaRangeConfig:
    raw = raw or {}

    def _t(key: str, default: str) -> time:
        hh, mm = str(raw.get(key, default)).split(":")[:2]
        return time(int(hh), int(mm))

    return AsiaRangeConfig(
        enabled=bool(raw.get("enabled", False)),
        range_start=_t("range_start", "20:00"),
        range_end=_t("range_end", "00:00"),
        search_start=_t("search_start", "02:00"),
        search_end=_t("search_end", "11:00"),
        force_flat=_t("force_flat", "11:55"),
        min_width_points=float(raw.get("min_width_points", 2.0)),
        max_width_points=float(raw.get("max_width_points", 40.0)),
        stop_buffer_ticks=int(raw.get("stop_buffer_ticks", 2)),
        target_mode=str(raw.get("target_mode", "opposite_extreme")),
        target_r=float(raw.get("target_r", 2.0)),
        scale_fraction=min(max(float(raw.get("scale_fraction", 0.5)), 0.0), 1.0),
        require_eq_bias=bool(raw.get("require_eq_bias", True)),
    )
