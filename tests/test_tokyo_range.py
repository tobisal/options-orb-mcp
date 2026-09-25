"""Tests for Tokyo Range Breakout (hooper.algo.fxx reel)."""

from datetime import datetime, timedelta

from core.models import Bar, Direction
from core.strategy.tokyo_range import TokyoRangeConfig, compute_tokyo_range_signal


def _tokyo_day(*, break_after: float = 0.0) -> list[Bar]:
    """One GMT day: range 00:00-06:00, then post-range bars.

    Naive timestamps are treated as UTC by the strategy helper.
    """
    day = datetime(2026, 1, 6, 0, 0, 0)
    bars: list[Bar] = []
    # Range: oscillate 100-101
    for i in range(72):  # 6h * 12 five-min bars
        ts = day + timedelta(minutes=5 * i)
        # alternate high/low pressure
        px = 100.0 + (0.5 if i % 2 == 0 else 0.0)
        hi = 101.0 if i % 5 == 0 else px + 0.2
        lo = 100.0 if i % 7 == 0 else px - 0.2
        bars.append(Bar(ts=ts, open=px, high=hi, low=lo, close=px, volume=1000))
    # Post-range at 06:05
    last = 101.0 + break_after
    bars.append(
        Bar(
            ts=day + timedelta(hours=6, minutes=5),
            open=101.0,
            high=max(101.0, last) + 0.1,
            low=min(101.0, last) - 0.1,
            close=last,
            volume=2000,
        )
    )
    return bars


def test_long_breakout_after_tokyo_range():
    cfg = TokyoRangeConfig(buffer=0.05, target_range_mult=1.5)
    bars = _tokyo_day(break_after=0.2)  # above 101+0.05
    sig = compute_tokyo_range_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is True
    assert sig.direction is Direction.LONG
    assert "tokyo_range" in sig.notes


def test_no_entry_before_range_end():
    cfg = TokyoRangeConfig(buffer=0.05)
    bars = _tokyo_day(break_after=0.0)[:40]
    sig = compute_tokyo_range_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is False
    assert "building" in sig.notes


def test_entry_cutoff():
    cfg = TokyoRangeConfig(buffer=0.05)
    bars = _tokyo_day(break_after=0.5)
    late = datetime(2026, 1, 6, 9, 30, 0)
    bars.append(
        Bar(ts=late, open=102, high=102.2, low=101.8, close=102.0, volume=1000)
    )
    sig = compute_tokyo_range_signal("SPY", bars, cfg=cfg, as_of=late)
    assert sig.breakout is False
    assert "cut-off" in sig.notes


def test_allowlist_blocks():
    cfg = TokyoRangeConfig(buffer=0.05, allowed_symbols=("USDJPY",))
    bars = _tokyo_day(break_after=0.5)
    sig = compute_tokyo_range_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is False
    assert "allowlist" in sig.notes
