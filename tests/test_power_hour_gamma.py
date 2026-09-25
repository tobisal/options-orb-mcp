"""Tests for the 3 PM power-hour gamma breakout strategy."""

from datetime import datetime, timedelta
from dataclasses import replace

from core.models import Bar, Direction
from core.strategy.power_hour_gamma import (
    PowerHourConfig,
    compute_power_hour_signal,
    estimate_negative_gamma,
)


def _rth_day(*, break_move: float = 0.0, pin: bool = True) -> list[Bar]:
    """Build one NYSE RTH day in naive UTC (EST January: ET = UTC-5).

    09:30 ET = 14:30 UTC. Anchor 15:00 ET = 20:00 UTC.
    """
    base = datetime(2026, 1, 6, 14, 30)  # 09:30 ET
    open_px = 100.0
    bars: list[Bar] = []
    # Morning: stay pinned near open if pin else trend away.
    px = open_px
    for i in range(66):  # 09:30 -> 15:00
        ts = base + timedelta(minutes=5 * i)
        px = open_px if pin else open_px + 0.05 * i
        bars.append(Bar(ts=ts, open=px, high=px + 0.1, low=px - 0.1, close=px, volume=1000))
    # 15:00 ET anchor bar — continues from morning close
    anchor_ts = datetime(2026, 1, 6, 20, 0)
    anchor_px = px
    bars.append(
        Bar(
            ts=anchor_ts,
            open=anchor_px,
            high=anchor_px + 0.1,
            low=anchor_px - 0.1,
            close=anchor_px,
            volume=2000,
        )
    )
    # Post-anchor breakout bar
    last = anchor_px + break_move
    bars.append(
        Bar(
            ts=anchor_ts + timedelta(minutes=5),
            open=anchor_px,
            high=max(anchor_px, last) + 0.1,
            low=min(anchor_px, last) - 0.1,
            close=last,
            volume=3000,
        )
    )
    return bars


def test_gamma_proxy_pin():
    bars = _rth_day(pin=True)
    cfg = PowerHourConfig(pin_pct=0.01)
    ok, reason = estimate_negative_gamma(spot=100.0, day_bars=bars[:-1], cfg=cfg)
    assert ok is True
    assert "pin" in reason


def test_long_breakout_when_gamma_ok():
    cfg = PowerHourConfig(
        break_points=1.0,
        stop_points=0.25,
        target_points=0.5,
        require_negative_gamma=True,
        pin_pct=0.01,
    )
    bars = _rth_day(break_move=1.2, pin=True)
    sig = compute_power_hour_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is True
    assert sig.direction is Direction.LONG
    assert "power_hour_gamma" in sig.notes


def test_no_trade_when_gamma_not_negative():
    cfg = PowerHourConfig(
        break_points=1.0,
        require_negative_gamma=True,
        pin_pct=0.001,  # very tight — morning drift fails pin
    )
    bars = _rth_day(break_move=1.5, pin=False)
    sig = compute_power_hour_signal("SPY", bars, cfg=cfg, as_of=bars[-1].ts)
    assert sig.breakout is False
    assert "gamma not negative" in sig.notes


def test_before_anchor_no_signal():
    cfg = PowerHourConfig(break_points=1.0, require_negative_gamma=False)
    bars = _rth_day(break_move=0.0)
    early = bars[10]
    sig = compute_power_hour_signal("SPY", bars[:11], cfg=cfg, as_of=early.ts)
    assert sig.breakout is False
    assert "before" in sig.notes


def test_external_gamma_override():
    cfg = replace(PowerHourConfig(break_points=1.0, require_negative_gamma=True), use_gamma_proxy=False)
    bars = _rth_day(break_move=1.5, pin=False)
    blocked = compute_power_hour_signal(
        "SPY", bars, cfg=PowerHourConfig(break_points=1.0, require_negative_gamma=True, pin_pct=0.001),
        as_of=bars[-1].ts,
    )
    assert blocked.breakout is False
    allowed = compute_power_hour_signal(
        "SPY", bars, cfg=PowerHourConfig(break_points=1.0, require_negative_gamma=True),
        as_of=bars[-1].ts, gamma_negative=True,
    )
    assert allowed.breakout is True
