from datetime import datetime, timedelta

from core.models import Bar, Direction, SessionWindow
from core.sessions import get_window_config
from core.strategy.orb import average_true_range, compute_orb_signal


def _ny_bars(breakout_price: float) -> list[Bar]:
    """Build a New York session (09:30 ET = 14:30 UTC in January/EST).

    30-min opening range around 100 (99.5-100.5), then a bar at ``breakout_price``.
    """
    base = datetime(2026, 1, 6, 14, 30)  # 09:30 ET
    bars: list[Bar] = []
    # Opening range: 7 bars spanning 30 minutes, range [99.5, 100.5].
    prices = [100.0, 100.3, 99.6, 100.4, 99.8, 100.2, 100.0]
    for i, p in enumerate(prices):
        ts = base + timedelta(minutes=5 * i)
        bars.append(Bar(ts=ts, open=p, high=p + 0.5, low=p - 0.5, close=p))
    # Post-range breakout bar (35 min in).
    ts = base + timedelta(minutes=35)
    bars.append(
        Bar(ts=ts, open=100.5, high=breakout_price + 0.2, low=100.4, close=breakout_price)
    )
    return bars


def test_long_breakout_detected():
    bars = _ny_bars(breakout_price=102.0)
    sig = compute_orb_signal("SPY", SessionWindow.NEW_YORK, bars, get_window_config(SessionWindow.NEW_YORK))
    assert sig.breakout is True
    assert sig.direction is Direction.LONG
    assert sig.range_high <= 101.0
    assert sig.strength > 0.25


def test_no_breakout_inside_range():
    bars = _ny_bars(breakout_price=100.1)  # stays inside the range
    sig = compute_orb_signal("SPY", SessionWindow.NEW_YORK, bars, get_window_config(SessionWindow.NEW_YORK))
    assert sig.breakout is False
    assert sig.direction is Direction.NEUTRAL


def test_atr_positive():
    bars = _ny_bars(102.0)
    assert average_true_range(bars) > 0
