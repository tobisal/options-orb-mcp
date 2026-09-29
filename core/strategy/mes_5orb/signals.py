"""Break and retest / close-break signal detection for MES 5ORB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from core.models import Bar, Direction
from core.strategy.mes_5orb.exits import or_stop_price
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


def _weekday_allowed(session: MesSession, day: date) -> bool:
    allowed = session.entry.allowed_weekdays
    if allowed is None:
        return True
    return day.weekday() in allowed


def _direction_allowed(session: MesSession, direction: Direction) -> bool:
    mode = session.entry.allowed_directions
    if mode == "long":
        return direction is Direction.LONG
    if mode == "short":
        return direction is Direction.SHORT
    return True


def detect_orb_setup(
    bars: list[Bar],
    session: MesSession,
    orb: OpeningRange,
    *,
    tick_size: float = 0.25,
    after_bar_index: int | None = None,
    stop_buffer_ticks: int = 1,
    max_stop_points: float | None = None,
) -> BreakRetestSetup | None:
    """Dispatch to retest or close-break entry per session.entry.mode."""
    if not _weekday_allowed(session, orb.day):
        return BreakRetestSetup(
            session_name=session.name,
            day=orb.day,
            direction=Direction.NEUTRAL,
            break_level=0.0,
            break_bar_index=-1,
            state=SetupState.INVALID,
            notes=f"weekday {orb.day.weekday()} not allowed",
        )
    mode = (session.entry.mode or "retest").lower().strip()
    if mode == "close_break":
        return detect_close_break(
            bars,
            session,
            orb,
            tick_size=tick_size,
            after_bar_index=after_bar_index,
            stop_buffer_ticks=stop_buffer_ticks,
            max_stop_points=max_stop_points,
        )
    if mode == "fib_macd":
        from core.strategy.mes_5orb.fib_macd import detect_fib_macd

        return detect_fib_macd(
            bars,
            session,
            orb,
            tick_size=tick_size,
            after_bar_index=after_bar_index,
            stop_buffer_ticks=stop_buffer_ticks,
        )
    return detect_break_retest(
        bars,
        session,
        orb,
        tick_size=tick_size,
        after_bar_index=after_bar_index,
        stop_buffer_ticks=stop_buffer_ticks,
        max_stop_points=max_stop_points,
    )


def detect_close_break(
    bars: list[Bar],
    session: MesSession,
    orb: OpeningRange,
    *,
    tick_size: float = 0.25,
    after_bar_index: int | None = None,
    stop_buffer_ticks: int = 1,
    max_stop_points: float | None = None,
) -> BreakRetestSetup | None:
    """Edgeful-style: enter on first 5m close outside the OR."""
    if orb.skipped or orb.high <= orb.low:
        return None

    post = _bars_after_or(bars, session, orb.day)
    if after_bar_index is not None:
        post = [(i, b) for i, b in post if i > after_bar_index]
    if not post:
        return None

    allow = session.entry.allowed_directions
    skip_opp = session.entry.skip_if_opposite_first

    for global_i, bar in post:
        broke_long = bar.close > orb.high
        broke_short = bar.close < orb.low
        if not broke_long and not broke_short:
            continue

        if broke_long and broke_short:
            continue

        if broke_long:
            direction = Direction.LONG
            level = orb.high
        else:
            direction = Direction.SHORT
            level = orb.low

        if not _direction_allowed(session, direction):
            if skip_opp or allow in ("long", "short"):
                return BreakRetestSetup(
                    session_name=session.name,
                    day=orb.day,
                    direction=direction,
                    break_level=level,
                    break_bar_index=global_i,
                    state=SetupState.INVALID,
                    notes=f"opposite/first close {direction.value} blocked",
                )
            continue

        entry_px = bar.close
        stop = or_stop_price(
            direction,
            orb,
            tick_size=tick_size,
            buffer_ticks=stop_buffer_ticks,
            entry_price=entry_px,
            max_stop_points=max_stop_points,
        )
        return BreakRetestSetup(
            session_name=session.name,
            day=orb.day,
            direction=direction,
            break_level=level,
            break_bar_index=global_i,
            retest_bar_index=global_i,
            entry_bar_index=global_i,
            entry_price=entry_px,
            initial_stop=stop,
            state=SetupState.RETESTED,
            notes=f"close_break {direction.value} @ {entry_px:.2f}",
        )

    return None


def detect_break_retest(
    bars: list[Bar],
    session: MesSession,
    orb: OpeningRange,
    *,
    tick_size: float = 0.25,
    after_bar_index: int | None = None,
    stop_buffer_ticks: int = 1,
    max_stop_points: float | None = None,
) -> BreakRetestSetup | None:
    """Scan post-OR bars for break then valid retest confirmation.

    ``after_bar_index`` skips bars at/before that global index (same-session re-entry).
    Stop is the far side of the OR (break of the range), optionally capped.
    Returns a setup ready for entry (state=RETESTED) or None / INVALID.
    """
    if orb.skipped or orb.high <= orb.low:
        return None

    post = _bars_after_or(bars, session, orb.day)
    if after_bar_index is not None:
        post = [(i, b) for i, b in post if i > after_bar_index]
    if not post:
        return None

    tol = session.retest.tolerance_ticks * tick_size
    timeout = session.retest.timeout_bars
    require_rej = session.retest.require_rejection_candle
    skip_opp = session.entry.skip_if_opposite_first

    break_dir = Direction.NEUTRAL
    break_idx_local = -1
    break_level = 0.0
    break_global_i = -1

    for local_i, (global_i, bar) in enumerate(post):
        if bar.close > orb.high:
            cand = Direction.LONG
            level = orb.high
        elif bar.close < orb.low:
            cand = Direction.SHORT
            level = orb.low
        else:
            continue

        if not _direction_allowed(session, cand):
            if skip_opp:
                return BreakRetestSetup(
                    session_name=session.name,
                    day=orb.day,
                    direction=cand,
                    break_level=level,
                    break_bar_index=global_i,
                    state=SetupState.INVALID,
                    notes=f"opposite close first ({cand.value})",
                )
            continue

        break_dir = cand
        break_level = level
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

    for offset, (global_i, bar) in enumerate(post[break_idx_local + 1 :], start=1):
        if offset > timeout:
            setup.state = SetupState.INVALID
            setup.notes = "retest timeout"
            return setup

        if break_dir is Direction.LONG and bar.close < orb.low:
            setup.state = SetupState.INVALID
            setup.notes = "close back through OR low"
            return setup
        if break_dir is Direction.SHORT and bar.close > orb.high:
            setup.state = SetupState.INVALID
            setup.notes = "close back through OR high"
            return setup

        if break_dir is Direction.LONG:
            touched = bar.low <= break_level + tol and bar.low >= break_level - tol
            held = bar.low >= break_level - tol
            closed_ok = bar.close >= break_level
            rej_ok = (not require_rej) or _is_rejection_long(bar)
            if touched and held and closed_ok and rej_ok:
                entry_px = bar.close
                setup.state = SetupState.RETESTED
                setup.retest_bar_index = global_i
                setup.entry_bar_index = global_i
                setup.entry_price = entry_px
                setup.initial_stop = or_stop_price(
                    Direction.LONG,
                    orb,
                    tick_size=tick_size,
                    buffer_ticks=stop_buffer_ticks,
                    entry_price=entry_px,
                    max_stop_points=max_stop_points,
                )
                setup.notes = "long retest confirmed"
                return setup
        else:
            touched = bar.high >= break_level - tol and bar.high <= break_level + tol
            held = bar.high <= break_level + tol
            closed_ok = bar.close <= break_level
            rej_ok = (not require_rej) or _is_rejection_short(bar)
            if touched and held and closed_ok and rej_ok:
                entry_px = bar.close
                setup.state = SetupState.RETESTED
                setup.retest_bar_index = global_i
                setup.entry_bar_index = global_i
                setup.entry_price = entry_px
                setup.initial_stop = or_stop_price(
                    Direction.SHORT,
                    orb,
                    tick_size=tick_size,
                    buffer_ticks=stop_buffer_ticks,
                    entry_price=entry_px,
                    max_stop_points=max_stop_points,
                )
                setup.notes = "short retest confirmed"
                return setup

    setup.state = SetupState.INVALID
    setup.notes = "no retest before search_end"
    return setup
