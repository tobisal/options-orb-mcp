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
    entry_price: float | None = None,
    max_stop_points: float | None = None,
) -> float:
    """Stop is a break of the opening range; optionally capped from entry."""
    buf = max(int(buffer_ticks), 0) * tick_size
    if direction is Direction.LONG:
        stop = orb.low - buf
    else:
        stop = orb.high + buf
    if entry_price is not None and max_stop_points is not None and max_stop_points > 0:
        cap = float(max_stop_points)
        if direction is Direction.LONG:
            capped = entry_price - cap
            stop = max(stop, capped)
        else:
            capped = entry_price + cap
            stop = min(stop, capped)
    return stop


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
    max_stop_points: float | None = None,
    target_mode: str = "r_multiple",
    target_or_fraction: float = 0.5,
) -> ExitLevels | None:
    stop = or_stop_price(
        direction,
        orb,
        tick_size=tick_size,
        buffer_ticks=buffer_ticks,
        entry_price=entry_price,
        max_stop_points=max_stop_points,
    )
    risk = abs(entry_price - stop)
    if risk <= 0:
        return None
    mode = (target_mode or "r_multiple").lower().strip()
    if mode == "or_fraction":
        width = max(orb.width, tick_size)
        frac = max(float(target_or_fraction), 0.05)
        move = frac * width
        if direction is Direction.LONG:
            target = entry_price + move
        else:
            target = entry_price - move
    else:
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
    target_label: str = "2R",
) -> tuple[float, str]:
    """Usually 2R / OR-fraction; use prior HOD/LOD only when far enough."""
    t2 = levels.target_2r
    label = target_label or "2R"
    if not use_hod_lod or levels.risk_points <= 0:
        return t2, label
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
    return t2, label


def target_progress(
    direction: Direction,
    entry: float,
    target: float,
    mark: float,
) -> float | None:
    """Fraction of entry→target path realized at ``mark`` (None if no span)."""
    span = abs(float(target) - float(entry))
    if span <= 1e-12:
        return None
    if direction is Direction.LONG:
        return (float(mark) - float(entry)) / span
    if direction is Direction.SHORT:
        return (float(entry) - float(mark)) / span
    return None


def profit_lock_progress_mark(direction: Direction, bar: Bar) -> float:
    """Favorable extreme for arming locks (high for long, low for short)."""
    if direction is Direction.LONG:
        return float(bar.high)
    if direction is Direction.SHORT:
        return float(bar.low)
    return float(bar.close)


def profit_lock_stop_price(
    direction: Direction,
    entry: float,
    target: float,
    *,
    lock_fraction: float = 0.35,
) -> float | None:
    """Stop that locks ``lock_fraction`` of the entry→target span (BE + that %)."""
    span = abs(float(target) - float(entry))
    if span <= 1e-12 or lock_fraction <= 0:
        return None
    frac = min(max(float(lock_fraction), 0.0), 1.0)
    if direction is Direction.LONG:
        return float(entry) + frac * span
    if direction is Direction.SHORT:
        return float(entry) - frac * span
    return None


def normalize_profit_lock_tiers(
    tiers: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None,
    *,
    arm_fraction: float | None = None,
    lock_fraction: float | None = None,
) -> list[tuple[float, float]]:
    """Dedupe/clamp/sort (arm, lock) tiers ascending by arm.

    If ``tiers`` is empty/None, falls back to a single ``(arm, lock)`` pair.
    """
    out: list[tuple[float, float]] = []
    if tiers:
        for arm, lock in tiers:
            a = min(max(float(arm), 0.0), 1.0)
            f = min(max(float(lock), 0.0), 1.0)
            if a > 0 and f > 0:
                out.append((a, f))
    elif arm_fraction is not None and lock_fraction is not None:
        a = min(max(float(arm_fraction), 0.0), 1.0)
        f = min(max(float(lock_fraction), 0.0), 1.0)
        if a > 0 and f > 0:
            out.append((a, f))
    # Keep highest lock for a given arm (last write wins after sort by arm).
    out.sort(key=lambda t: (t[0], t[1]))
    merged: list[tuple[float, float]] = []
    for arm, lock in out:
        if merged and abs(merged[-1][0] - arm) < 1e-12:
            merged[-1] = (arm, max(merged[-1][1], lock))
        else:
            merged.append((arm, lock))
    return merged


def active_profit_lock_fraction(
    progress: float | None,
    tiers: list[tuple[float, float]] | tuple[tuple[float, float], ...],
) -> float | None:
    """Highest lock fraction among tiers whose arm ≤ progress."""
    if progress is None:
        return None
    best: float | None = None
    for arm, lock in tiers:
        if progress + 1e-12 >= float(arm):
            best = float(lock) if best is None else max(best, float(lock))
    return best


def apply_profit_lock_stop(
    direction: Direction,
    entry: float,
    target: float,
    mark: float,
    current_stop: float,
    *,
    arm_fraction: float = 0.75,
    lock_fraction: float = 0.35,
    tiers: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None = None,
) -> float:
    """If mark has reached a tier arm, tighten stop to that tier's lock.

    Never loosens the stop. Returns ``current_stop`` unchanged when not armed.
    ``tiers`` is a list of ``(arm_fraction, lock_fraction)``; when omitted,
    uses the single ``(arm_fraction, lock_fraction)`` pair.
    """
    resolved = normalize_profit_lock_tiers(
        tiers, arm_fraction=arm_fraction, lock_fraction=lock_fraction
    )
    if not resolved:
        return float(current_stop)
    progress = target_progress(direction, entry, target, mark)
    lock = active_profit_lock_fraction(progress, resolved)
    if lock is None:
        return float(current_stop)
    locked = profit_lock_stop_price(
        direction, entry, target, lock_fraction=lock
    )
    if locked is None:
        return float(current_stop)
    cur = float(current_stop)
    if direction is Direction.LONG:
        return max(cur, locked)
    if direction is Direction.SHORT:
        return min(cur, locked)
    return cur


def tighten_stop_to_be(
    direction: Direction, entry: float, current_stop: float
) -> float:
    """Move stop to breakeven only when that improves protection."""
    entry_f = float(entry)
    cur = float(current_stop)
    if direction is Direction.LONG:
        return max(cur, entry_f)
    if direction is Direction.SHORT:
        return min(cur, entry_f)
    return cur
