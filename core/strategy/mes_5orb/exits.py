"""5m ORB exits: SL at OR extreme, 2R / HOD targets with optional runners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.models import Bar, Direction
from core.strategy.mes_5orb.opening_range import OpeningRange, to_et


@dataclass(frozen=True)
class ExitLevels:
    """Price levels for a break/retest entry."""

    stop: float
    risk_points: float
    target_2r: float
    # Day extremes known at entry (prior HOD/LOD); runner tracks developing extremes.
    hod_at_entry: float | None
    lod_at_entry: float | None


def or_stop_price(
    direction: Direction,
    orb: OpeningRange,
    *,
    tick_size: float,
    buffer_ticks: int = 1,
) -> float:
    """Stop is a break of the 5m opening range (classic ORB SL)."""
    buf = max(int(buffer_ticks), 0) * tick_size
    if direction is Direction.LONG:
        return orb.low - buf
    return orb.high + buf


def day_extremes(
    bars: list[Bar],
    day: date,
    *,
    through_index: int | None = None,
) -> tuple[float | None, float | None]:
    """High/low of ``day`` (ET), optionally only through ``through_index`` inclusive."""
    hi: float | None = None
    lo: float | None = None
    for i, b in enumerate(bars):
        if through_index is not None and i > through_index:
            break
        if to_et(b.ts).date() != day:
            continue
        hi = b.high if hi is None else max(hi, b.high)
        lo = b.low if lo is None else min(lo, b.low)
    return hi, lo


def build_exit_levels(
    direction: Direction,
    entry_price: float,
    orb: OpeningRange,
    bars: list[Bar],
    *,
    tick_size: float,
    buffer_ticks: int = 1,
    target_r: float = 2.0,
    entry_index: int | None = None,
) -> ExitLevels | None:
    stop = or_stop_price(direction, orb, tick_size=tick_size, buffer_ticks=buffer_ticks)
    risk = abs(entry_price - stop)
    if risk <= 0:
        return None
    r = max(float(target_r), 0.1)
    if direction is Direction.LONG:
        target = entry_price + r * risk
    else:
        target = entry_price - r * risk
    hod, lod = day_extremes(bars, orb.day, through_index=entry_index)
    return ExitLevels(
        stop=stop,
        risk_points=risk,
        target_2r=target,
        hod_at_entry=hod,
        lod_at_entry=lod,
    )


def hit_stop(direction: Direction, bar: Bar, stop: float) -> bool:
    if direction is Direction.LONG:
        return bar.low <= stop or bar.close < stop
    return bar.high >= stop or bar.close > stop


def hit_target(direction: Direction, bar: Bar, target: float) -> bool:
    if direction is Direction.LONG:
        return bar.high >= target
    return bar.low <= target


def primary_target(
    levels: ExitLevels,
    direction: Direction,
    *,
    use_hod_lod: bool = True,
    min_r_for_hod_lod: float = 1.0,
) -> tuple[float, str]:
    """Usually 2R; use prior HOD/LOD only when it is at least ``min_r_for_hod_lod`` R."""
    t2 = levels.target_2r
    if not use_hod_lod or levels.risk_points <= 0:
        return t2, "2R"
    min_move = max(float(min_r_for_hod_lod), 0.0) * levels.risk_points
    if direction is Direction.LONG:
        entry = levels.stop + levels.risk_points
        hod = levels.hod_at_entry
        if hod is not None and entry < hod <= t2 and (hod - entry) >= min_move:
            return hod, "HOD"
    else:
        entry = levels.stop - levels.risk_points
        lod = levels.lod_at_entry
        if lod is not None and t2 <= lod < entry and (entry - lod) >= min_move:
            return lod, "LOD"
    return t2, "2R"
