"""Break and retest signal detection for MES 5ORB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from core.models import Bar, Direction
from core.strategy.mes_5orb.opening_range import OpeningRange, to_et
from core.strategy.mes_5orb.sessions import MesSession


class SetupState(str, Enum):
    IDLE = "idle"
    BROKEN = "broken"
    RETESTED = "retested"
    INVALID = "invalid"


@dataclass
class BreakRetestSetup:
    session_name: str
    day: date
    direction: Direction
    break_level: float
    break_bar_index: int
    retest_bar_index: int | None = None
    entry_bar_index: int | None = None
    entry_price: float | None = None
    initial_stop: float | None = None
    state: SetupState = SetupState.IDLE
    notes: str = ""


def _bars_after_or(
    bars: list[Bar], session: MesSession, day: date
) -> list[tuple[int, Bar]]:
    """Indexed bars on ``day`` with ET time in [or_end, search_end)."""
    out: list[tuple[int, Bar]] = []
    for i, b in enumerate(bars):
        et = to_et(b.ts)
        if et.date() != day:
            continue
        t = et.time()
        if session.or_end <= t < session.search_end:
            out.append((i, b))
    return out


def _is_rejection_long(bar: Bar) -> bool:
    if bar.close <= bar.open:
        return False
    rng = bar.high - bar.low
    if rng <= 0:
        return True
    return (bar.close - bar.low) / rng >= 0.6


def _is_rejection_short(bar: Bar) -> bool:
    if bar.close >= bar.open:
        return False
    rng = bar.high - bar.low
    if rng <= 0:
        return True
    return (bar.high - bar.close) / rng >= 0.6


def detect_break_retest(
    bars: list[Bar],
    session: MesSession,
    orb: OpeningRange,
    *,
    tick_size: float = 0.25,
) -> BreakRetestSetup | None:
    """Scan post-OR bars for break then valid retest confirmation.

    Returns a setup ready for entry (state=RETESTED) or None / INVALID.
    """
    if orb.skipped or orb.high <= orb.low:
        return None

    post = _bars_after_or(bars, session, orb.day)
    if not post:
        return None

    tol = session.retest.tolerance_ticks * tick_size
    timeout = session.retest.timeout_bars
    require_rej = session.retest.require_rejection_candle

    break_dir = Direction.NEUTRAL
    break_idx_local = -1
    break_level = 0.0
    break_global_i = -1

    for local_i, (global_i, bar) in enumerate(post):
        if bar.close > orb.high:
            break_dir = Direction.LONG
            break_level = orb.high
            break_idx_local = local_i
            break_global_i = global_i
            break
        if bar.close < orb.low:
            break_dir = Direction.SHORT
            break_level = orb.low
            break_idx_local = local_i
            break_global_i = global_i
            break

    if break_dir is Direction.NEUTRAL:
        return None

    setup = BreakRetestSetup(
        session_name=session.name,
        day=orb.day,
        direction=break_dir,
        break_level=break_level,
        break_bar_index=break_global_i,
        state=SetupState.BROKEN,
        notes=f"break {break_dir.value} @ {break_level:.2f}",
    )

    # Scan for retest after break bar
    for offset, (global_i, bar) in enumerate(post[break_idx_local + 1 :], start=1):
        if offset > timeout:
            setup.state = SetupState.INVALID
            setup.notes = "retest timeout"
            return setup

        # Invalidate if close back inside OR
        if break_dir is Direction.LONG and bar.close < orb.low:
            setup.state = SetupState.INVALID
            setup.notes = "close back inside OR"
            return setup
        if break_dir is Direction.SHORT and bar.close > orb.high:
            setup.state = SetupState.INVALID
            setup.notes = "close back inside OR"
            return setup

        if break_dir is Direction.LONG:
            # Touch: low within tolerance of break_level from above
            touched = bar.low <= break_level + tol and bar.low >= break_level - tol
            held = bar.low >= break_level - tol
            closed_ok = bar.close >= break_level
            rej_ok = (not require_rej) or _is_rejection_long(bar)
            if touched and held and closed_ok and rej_ok:
                setup.state = SetupState.RETESTED
                setup.retest_bar_index = global_i
                setup.entry_bar_index = global_i
                setup.entry_price = bar.close
                setup.initial_stop = bar.low - tick_size  # buffer applied by caller too
                setup.notes = "long retest confirmed"
                return setup
        else:
            touched = bar.high >= break_level - tol and bar.high <= break_level + tol
            held = bar.high <= break_level + tol
            closed_ok = bar.close <= break_level
            rej_ok = (not require_rej) or _is_rejection_short(bar)
            if touched and held and closed_ok and rej_ok:
                setup.state = SetupState.RETESTED
                setup.retest_bar_index = global_i
                setup.entry_bar_index = global_i
                setup.entry_price = bar.close
                setup.initial_stop = bar.high + tick_size
                setup.notes = "short retest confirmed"
                return setup

    setup.state = SetupState.INVALID
    setup.notes = "no retest before search_end"
    return setup
