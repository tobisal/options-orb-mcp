from datetime import datetime, timedelta

from core.models import Bar, Direction, Regime, SessionWindow
from core.sessions import get_window_config
from core.strategy.orb import (
    average_true_range,
    compute_orb_signal,
    passes_entry_filters,
    session_vwap,
    volume_confirmed,
)


def _ny_bars(breakout_price: float, *, vol_or: float = 1000.0, vol_entry: float = 1000.0) -> list[Bar]:
    """Build a New York session (09:30 ET = 14:30 UTC in January/EST).

    30-min opening range around 100 (99.5-100.5), then a bar at ``breakout_price``.
    """
    base = datetime(2026, 1, 6, 14, 30)  # 09:30 ET
    bars: list[Bar] = []
    # Opening range: 7 bars spanning 30 minutes, range [99.5, 100.5].
    prices = [100.0, 100.3, 99.6, 100.4, 99.8, 100.2, 100.0]
    for i, p in enumerate(prices):
        ts = base + timedelta(minutes=5 * i)
        bars.append(
            Bar(ts=ts, open=p, high=p + 0.5, low=p - 0.5, close=p, volume=vol_or)
        )
    # Post-range breakout bar (35 min in).
    ts = base + timedelta(minutes=35)
    bars.append(
        Bar(
            ts=ts,
            open=100.5,
            high=breakout_price + 0.2,
            low=100.4,
            close=breakout_price,
            volume=vol_entry,
        )
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


def test_session_vwap_and_volume_helpers():
    bars = _ny_bars(102.0, vol_or=100.0, vol_entry=250.0)
    vwap = session_vwap(bars)
    assert vwap is not None and vwap > 0
    assert volume_confirmed(bars[-1], bars[:-1], 2.0) is True
    assert volume_confirmed(bars[-1], bars[:-1], 3.0) is False


def test_vwap_filter_blocks_long_below_vwap():
    entry = Bar(
        ts=datetime(2026, 1, 6, 15, 5),
        open=101.0,
        high=101.2,
        low=100.9,
        close=101.0,
        volume=500.0,
    )
    heavy = [
        Bar(ts=datetime(2026, 1, 6, 14, 30), open=110, high=111, low=109, close=110, volume=10_000),
        Bar(ts=datetime(2026, 1, 6, 14, 35), open=110, high=111, low=109, close=110, volume=10_000),
    ]
    ok, reason = passes_entry_filters(
        direction=Direction.LONG,
        price=101.0,
        vwap_bars=heavy + [entry],
        entry_bar=entry,
        orb_bars=heavy,
        regime=Regime.TREND,
        require_vwap_align=True,
        volume_confirm_mult=0.0,
    )
    assert ok is False
    assert "VWAP" in reason


def test_volume_filter_via_overlay():
    cfg = get_window_config(SessionWindow.NEW_YORK).overlay(
        {"volume_confirm_mult": 2.0, "min_strength": 0.1, "breakout_buffer_atr": 0.0}
    )
    bars = _ny_bars(102.0, vol_or=1000.0, vol_entry=1000.0)
    sig = compute_orb_signal("SPY", SessionWindow.NEW_YORK, bars, cfg)
    assert sig.breakout is False
    assert "volume" in sig.notes

    bars_ok = _ny_bars(102.0, vol_or=1000.0, vol_entry=2500.0)
    sig_ok = compute_orb_signal("SPY", SessionWindow.NEW_YORK, bars_ok, cfg)
    assert sig_ok.breakout is True
    assert sig_ok.direction is Direction.LONG


def test_range_regime_skipped_when_enabled():
    entry = Bar(
        ts=datetime(2026, 1, 6, 15, 5),
        open=102,
        high=102.2,
        low=101.8,
        close=102.0,
        volume=2000.0,
    )
    ok, reason = passes_entry_filters(
        direction=Direction.LONG,
        price=102.0,
        vwap_bars=[entry],
        entry_bar=entry,
        orb_bars=[entry],
        regime=Regime.RANGE,
        require_trend_regime=True,
    )
    assert ok is False
    assert "range" in reason
    ok2, _ = passes_entry_filters(
        direction=Direction.LONG,
        price=102.0,
        vwap_bars=[entry],
        entry_bar=entry,
        orb_bars=[entry],
        regime=Regime.UNCERTAIN,
        require_trend_regime=True,
    )
    assert ok2 is True
