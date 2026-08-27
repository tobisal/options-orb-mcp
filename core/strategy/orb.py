"""Opening Range Breakout (ORB) signal generation.

The opening range is the high/low established in the first ``opening_range_minutes``
of a session window. A breakout beyond that range (plus an ATR-scaled buffer to
filter noise) produces a directional signal whose *strength* is the distance
beyond the range measured in range-widths. A regime classifier labels the
post-range action as trending or range-bound, which the spread builder uses to
pick a debit (directional) vs credit (income) structure.
"""

from __future__ import annotations

from datetime import datetime

from core.models import Bar, Direction, ORBSignal, Regime, SessionWindow
from core.sessions import (
    WindowConfig,
    get_window_config,
    opening_range_bars,
    session_bars_after_range,
)
from core.timeutils import utcnow


def _session_last_price(bars: list[Bar], cfg: WindowConfig, orb_bars: list[Bar]) -> tuple[float, datetime]:
    """Latest in-window price for the current session (falls back to opening range)."""
    post = session_bars_after_range(bars, cfg)
    ref = post[-1] if post else (orb_bars[-1] if orb_bars else bars[-1])
    return ref.close, ref.ts


def average_true_range(bars: list[Bar], period: int = 14) -> float:
    """Average True Range over the trailing ``period`` bars."""
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def classify_regime(bars: list[Bar], cfg: WindowConfig) -> Regime:
    """Trend vs range for the post-opening-range action of the latest session.

    Heuristic: compare the net directional move to the total path travelled
    (an efficiency ratio). High efficiency => trend; low => range-bound chop.
    """
    post = session_bars_after_range(bars, cfg)
    if len(post) < 3:
        return Regime.UNCERTAIN
    net_move = abs(post[-1].close - post[0].open)
    path = sum(abs(b.close - b.open) for b in post) + sum(
        abs(c.open - p.close) for p, c in zip(post[:-1], post[1:])
    )
    if path <= 0:
        return Regime.UNCERTAIN
    efficiency = net_move / path
    if efficiency >= 0.4:
        return Regime.TREND
    if efficiency <= 0.2:
        return Regime.RANGE
    return Regime.UNCERTAIN


def compute_orb_signal(
    symbol: str,
    window: SessionWindow,
    bars: list[Bar],
    cfg: WindowConfig | None = None,
) -> ORBSignal:
    """Compute the ORB signal for the most recent session in ``bars``."""
    cfg = cfg or get_window_config(window)
    orb_bars = opening_range_bars(bars, cfg)

    if not orb_bars or not bars:
        as_of = bars[-1].ts if bars else utcnow()
        return ORBSignal(
            symbol=symbol,
            window=window,
            as_of=as_of,
            range_high=0.0,
            range_low=0.0,
            last_price=bars[-1].close if bars else 0.0,
            direction=Direction.NEUTRAL,
            breakout=False,
            notes="No opening-range bars available for this window/session.",
        )

    range_high = max(b.high for b in orb_bars)
    range_low = min(b.low for b in orb_bars)
    range_width = max(range_high - range_low, 1e-9)
    last_price, as_of = _session_last_price(bars, cfg, orb_bars)
    atr = average_true_range(bars)
    buffer = cfg.breakout_buffer_atr * atr

    direction = Direction.NEUTRAL
    breakout = False
    strength = 0.0
    if last_price > range_high + buffer:
        direction = Direction.LONG
        breakout = True
        strength = (last_price - range_high) / range_width
    elif last_price < range_low - buffer:
        direction = Direction.SHORT
        breakout = True
        strength = (range_low - last_price) / range_width

    # Filter weak breakouts below the configured minimum strength.
    if breakout and strength < cfg.min_strength:
        breakout = False
        direction = Direction.NEUTRAL

    regime = classify_regime(bars, cfg)

    notes = (
        f"OR[{range_low:.2f}, {range_high:.2f}] width={range_width:.2f} "
        f"atr={atr:.2f} buffer={buffer:.2f} last={last_price:.2f}"
    )
    return ORBSignal(
        symbol=symbol,
        window=window,
        as_of=as_of,
        range_high=round(range_high, 4),
        range_low=round(range_low, 4),
        last_price=round(last_price, 4),
        direction=direction,
        breakout=breakout,
        strength=round(strength, 4),
        regime=regime,
        atr=round(atr, 4),
        notes=notes,
    )
