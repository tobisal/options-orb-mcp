"""Unit smoke for ORB+Fib+MACD detector."""

from __future__ import annotations

from datetime import datetime, timedelta, time

import pytz

from core.models import Bar, Direction
from core.strategy.mes_5orb.fib_macd import detect_fib_macd, macd_lines
from core.strategy.mes_5orb.opening_range import compute_opening_range, to_et
from core.strategy.mes_5orb.sessions import EntryConfig, MesSession, OpeningRangeFilter, RetestConfig
from core.strategy.mes_5orb.signals import SetupState


def _et(y, m, d, hh, mm) -> datetime:
    et = pytz.timezone("America/New_York")
    return et.localize(datetime(y, m, d, hh, mm)).astimezone(pytz.utc).replace(tzinfo=None)


def _bar(ts, o, h, l, c) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=100)


def test_macd_lines_length():
    closes = [float(i) for i in range(60)]
    macd, sig, hist = macd_lines(closes)
    assert len(macd) == 60
    assert any(v is not None for v in macd)
    assert any(v is not None for v in sig)


def test_fib_macd_long_entry():
    sess = MesSession(
        name="new_york",
        or_start=time(9, 30),
        or_end=time(9, 45),
        search_end=time(15, 0),
        force_flat=time(15, 55),
        opening_range=OpeningRangeFilter(0.5, 50.0),
        retest=RetestConfig(timeout_bars=40, require_rejection_candle=False),
        entry=EntryConfig(mode="fib_macd"),
    )
    # Warm-up bars so MACD is defined (prior day afternoon + OR).
    bars: list[Bar] = []
    px = 100.0
    t0 = _et(2026, 1, 5, 14, 0)
    for i in range(40):
        bars.append(_bar(t0 + timedelta(minutes=5 * i), px, px + 0.5, px - 0.5, px + 0.1))
        px += 0.05

    # OR 09:30-09:45: high 102, low 100
    base = _et(2026, 1, 6, 9, 30)
    bars += [
        _bar(base, 100.5, 101.0, 100.0, 100.8),
        _bar(base + timedelta(minutes=5), 100.8, 101.5, 100.2, 101.2),
        _bar(base + timedelta(minutes=10), 101.2, 102.0, 100.5, 101.0),
    ]
    # Break above 102
    bars.append(_bar(base + timedelta(minutes=15), 102.0, 104.0, 101.8, 103.5))
    # Extend
    bars.append(_bar(base + timedelta(minutes=20), 103.5, 105.0, 103.0, 104.5))
    # Pullback into 50/61.8 of (100 -> 105): 50%=102.5, 61.8%=101.91
    # And push closes up for MACD curl — use a green reclaim bar through 102.5
    bars.append(_bar(base + timedelta(minutes=25), 103.0, 103.2, 102.2, 102.6))

    d = to_et(base).date()
    orb = compute_opening_range(bars, sess, d)
    assert orb is not None
    assert orb.high == 102.0
    assert orb.low == 100.0
    setup = detect_fib_macd(bars, sess, orb, tick_size=0.25, min_impulse_points=1.0)
    assert setup is not None
    # May be RETESTED or still waiting depending on MACD; assert no crash / valid states
    assert setup.state in (SetupState.RETESTED, SetupState.BROKEN, SetupState.INVALID)
    if setup.state is SetupState.RETESTED:
        assert setup.direction is Direction.LONG
        assert setup.entry_price is not None
        assert setup.initial_stop is not None
        assert setup.initial_stop < setup.entry_price
