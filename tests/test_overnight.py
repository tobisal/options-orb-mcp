"""Tests for the overnight long (non-ORB) entry model."""

from datetime import datetime, timedelta

from core.models import Bar, Direction
from core.strategy.overnight import (
    OvernightConfig,
    compute_overnight_signal,
    overnight_entry_window_active,
)


def _rth_day(*, close_px: float = 100.0, open_px: float = 100.0) -> list[Bar]:
    """One NYSE RTH day in naive UTC (EST January: ET = UTC-5).

    09:30 ET = 14:30 UTC. Last bar ~15:55 ET = 20:55 UTC.
    """
    base = datetime(2026, 1, 6, 14, 30)  # 09:30 ET
    bars: list[Bar] = []
    # Morning drift toward close
    for i in range(77):  # 09:30 -> 15:55
        ts = base + timedelta(minutes=5 * i)
        # linear from open to close
        px = open_px + (close_px - open_px) * (i / 76)
        bars.append(Bar(ts=ts, open=px, high=px + 0.1, low=px - 0.1, close=px, volume=1000))
    return bars


def test_long_signal_in_entry_window():
    bars = _rth_day(close_px=101.0, open_px=100.0)
    cfg = OvernightConfig(enabled=True)
    as_of = bars[-1].ts  # ~15:55 ET
    assert overnight_entry_window_active(now=as_of, cfg=cfg)
    sig = compute_overnight_signal("SPY", bars, cfg=cfg, as_of=as_of)
    assert sig.breakout is True
    assert sig.direction == Direction.LONG
    assert "overnight" in sig.notes


def test_idle_before_entry_window():
    bars = _rth_day()
    cfg = OvernightConfig(enabled=True)
    early = bars[10].ts  # ~10:20 ET
    sig = compute_overnight_signal("SPY", bars[:11], cfg=cfg, as_of=early)
    assert sig.breakout is False
    assert "outside entry window" in sig.notes


def test_require_red_day_skips_green():
    bars = _rth_day(close_px=101.0, open_px=100.0)
    cfg = OvernightConfig(enabled=True, require_red_day=True)
    sig = compute_overnight_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is False
    assert "skip green day" in sig.notes


def test_require_red_day_allows_red():
    bars = _rth_day(close_px=99.0, open_px=100.0)
    cfg = OvernightConfig(enabled=True, require_red_day=True)
    sig = compute_overnight_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is True
    assert sig.direction == Direction.LONG
