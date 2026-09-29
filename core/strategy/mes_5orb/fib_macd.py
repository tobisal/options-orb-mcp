"""ORB break → Fib 50/61.8 pullback + MACD confirmation entry."""

from __future__ import annotations

from core.models import Bar, Direction
from core.strategy.mes_5orb.opening_range import OpeningRange
from core.strategy.mes_5orb.sessions import MesSession
from core.strategy.mes_5orb.signals import (
    BreakRetestSetup,
    SetupState,
    _bars_after_or,
    _direction_allowed,
)


def _ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def macd_lines(
    closes: list[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return (macd, signal, hist) series aligned to ``closes``."""
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    macd: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            macd[i] = float(ef[i]) - float(es[i])
    # Signal EMA of MACD values (skip Nones at start).
    macd_nums: list[float] = []
    macd_idx: list[int] = []
    for i, v in enumerate(macd):
        if v is not None:
            macd_nums.append(v)
            macd_idx.append(i)
    sig_partial = _ema(macd_nums, signal)
    signal_line: list[float | None] = [None] * len(closes)
    hist: list[float | None] = [None] * len(closes)
    for j, i in enumerate(macd_idx):
        if sig_partial[j] is None:
            continue
        signal_line[i] = float(sig_partial[j])
        hist[i] = float(macd[i]) - float(sig_partial[j])  # type: ignore[arg-type]
    return macd, signal_line, hist


def _macd_long_ok(macd: list[float | None], hist: list[float | None], i: int) -> bool:
    """Bullish curl / hist rising, or mild bullish divergence vs prior bar."""
    if i < 2:
        return False
    m0, m1, m2 = macd[i], macd[i - 1], macd[i - 2]
    h0, h1 = hist[i], hist[i - 1]
    if m0 is None or m1 is None:
        return False
    curl = m0 > m1  # curling up
    from_mid = m1 <= 0 <= m0 or (m0 > m1 and abs(m0) < abs(m1) and m0 < 0)
    hist_up = h0 is not None and h1 is not None and h0 > h1
    # Divergence proxy: MACD rising while we assume pullback (caller gates price).
    div = m2 is not None and m0 > m2 and m1 <= m2
    return bool(curl or from_mid or hist_up or div)


def _macd_short_ok(macd: list[float | None], hist: list[float | None], i: int) -> bool:
    if i < 2:
        return False
    m0, m1, m2 = macd[i], macd[i - 1], macd[i - 2]
    h0, h1 = hist[i], hist[i - 1]
    if m0 is None or m1 is None:
        return False
    curl = m0 < m1
    from_mid = m1 >= 0 >= m0 or (m0 < m1 and abs(m0) < abs(m1) and m0 > 0)
    hist_dn = h0 is not None and h1 is not None and h0 < h1
    div = m2 is not None and m0 < m2 and m1 >= m2
    return bool(curl or from_mid or hist_dn or div)


def detect_fib_macd(
    bars: list[Bar],
    session: MesSession,
    orb: OpeningRange,
    *,
    tick_size: float = 0.25,
    after_bar_index: int | None = None,
    stop_buffer_ticks: int = 1,
    fib_entry_levels: tuple[float, ...] = (0.5, 0.618),
    fib_stop_level: float = 0.786,
    min_impulse_points: float = 1.5,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> BreakRetestSetup | None:
    """ORB direction → Fib pullback to 50/61.8 → MACD confirm.

    Fib for longs: session/OR swing low → post-break extreme high.
    Stop under the 78.6% retracement (buffer ticks). Target left to exits (HOD).
    """
    if orb.skipped or orb.high <= orb.low:
        return None

    post = _bars_after_or(bars, session, orb.day)
    if after_bar_index is not None:
        post = [(i, b) for i, b in post if i > after_bar_index]
    if not post:
        return None

    closes = [float(b.close) for b in bars]
    macd, _sig, hist = macd_lines(
        closes, fast=macd_fast, slow=macd_slow, signal=macd_signal
    )
    buf = max(int(stop_buffer_ticks), 0) * tick_size

    break_dir = Direction.NEUTRAL
    break_global_i = -1
    break_level = 0.0
    for global_i, bar in post:
        if bar.close > orb.high:
            cand = Direction.LONG
            level = orb.high
        elif bar.close < orb.low:
            cand = Direction.SHORT
            level = orb.low
        else:
            continue
        if not _direction_allowed(session, cand):
            if session.entry.skip_if_opposite_first:
                return BreakRetestSetup(
                    session_name=session.name,
                    day=orb.day,
                    direction=cand,
                    break_level=level,
                    break_bar_index=global_i,
                    state=SetupState.INVALID,
                    notes=f"fib_macd: opposite close first ({cand.value})",
                )
            continue
        break_dir = cand
        break_global_i = global_i
        break_level = level
        break

    if break_dir is Direction.NEUTRAL:
        return None

    # Bars from break onward (inclusive of break bar for extreme tracking).
    trail = [(i, b) for i, b in post if i >= break_global_i]
    extreme = bars[break_global_i].high if break_dir is Direction.LONG else bars[break_global_i].low
    # Anchor: OR extreme on far side (session low/high proxy for the OR window).
    anchor = orb.low if break_dir is Direction.LONG else orb.high
    saw_extension = False

    for global_i, bar in trail:
        if break_dir is Direction.LONG:
            extreme = max(extreme, bar.high)
            # Invalidate if close back through OR low before entry.
            if global_i > break_global_i and bar.close < orb.low:
                return BreakRetestSetup(
                    session_name=session.name,
                    day=orb.day,
                    direction=break_dir,
                    break_level=break_level,
                    break_bar_index=break_global_i,
                    state=SetupState.INVALID,
                    notes="fib_macd: closed back through OR low",
                )
            impulse = extreme - anchor
            if impulse < min_impulse_points:
                continue
            if extreme > orb.high + min_impulse_points * 0.5:
                saw_extension = True
            if not saw_extension:
                continue
            # Must be pulling back (not making new extreme this bar exclusively).
            levels = [extreme - f * impulse for f in fib_entry_levels]
            touched = any(bar.low <= lvl <= bar.high for lvl in levels)
            if not touched:
                continue
            stop_786 = extreme - fib_stop_level * impulse
            if bar.close < stop_786:
                continue
            if not _macd_long_ok(macd, hist, global_i):
                continue
            entry_px = float(bar.close)
            stop = min(stop_786, bar.low) - buf
            if entry_px <= stop:
                continue
            return BreakRetestSetup(
                session_name=session.name,
                day=orb.day,
                direction=Direction.LONG,
                break_level=break_level,
                break_bar_index=break_global_i,
                retest_bar_index=global_i,
                entry_bar_index=global_i,
                entry_price=entry_px,
                initial_stop=stop,
                state=SetupState.RETESTED,
                notes=f"fib_macd long @ {entry_px:.2f} stop {stop:.2f}",
            )
        else:
            extreme = min(extreme, bar.low)
            if global_i > break_global_i and bar.close > orb.high:
                return BreakRetestSetup(
                    session_name=session.name,
                    day=orb.day,
                    direction=break_dir,
                    break_level=break_level,
                    break_bar_index=break_global_i,
                    state=SetupState.INVALID,
                    notes="fib_macd: closed back through OR high",
                )
            impulse = anchor - extreme
            if impulse < min_impulse_points:
                continue
            if extreme < orb.low - min_impulse_points * 0.5:
                saw_extension = True
            if not saw_extension:
                continue
            levels = [extreme + f * impulse for f in fib_entry_levels]
            touched = any(bar.low <= lvl <= bar.high for lvl in levels)
            if not touched:
                continue
            stop_786 = extreme + fib_stop_level * impulse
            if bar.close > stop_786:
                continue
            if not _macd_short_ok(macd, hist, global_i):
                continue
            entry_px = float(bar.close)
            stop = max(stop_786, bar.high) + buf
            if entry_px >= stop:
                continue
            return BreakRetestSetup(
                session_name=session.name,
                day=orb.day,
                direction=Direction.SHORT,
                break_level=break_level,
                break_bar_index=break_global_i,
                retest_bar_index=global_i,
                entry_bar_index=global_i,
                entry_price=entry_px,
                initial_stop=stop,
                state=SetupState.RETESTED,
                notes=f"fib_macd short @ {entry_px:.2f} stop {stop:.2f}",
            )

    return BreakRetestSetup(
        session_name=session.name,
        day=orb.day,
        direction=break_dir,
        break_level=break_level,
        break_bar_index=break_global_i,
        state=SetupState.BROKEN,
        notes="fib_macd: waiting for Fib 50/61.8 + MACD",
    )
