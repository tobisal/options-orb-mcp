"""Unit tests for ICT Asian Range (Judas sweep + reclaim)."""

from __future__ import annotations

from datetime import date, datetime, time

import pytz

from core.backtest_mes import evaluate_mes_signal_live, run_mes_5orb_backtest
from core.models import Bar, Direction
from core.strategy.mes_5orb.asia_range import (
    AsiaRangeConfig,
    AsiaSetupState,
    compute_asia_range,
    detect_asia_judas,
)
from core.strategy.mes_5orb.sessions import (
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)
from dataclasses import replace


def _et_to_naive_utc(year, month, day, hour, minute) -> datetime:
    et = pytz.timezone("America/New_York")
    local = et.localize(datetime(year, month, day, hour, minute))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=100)


def _asia_evening_bars() -> list[Bar]:
    """20:00–23:55 ET on Jan 14 → range for trade_day Jan 15. ARH=100.5 ARL=98.0."""
    bars: list[Bar] = []
    # 20:00 open of Asia
    bars.append(_bar(_et_to_naive_utc(2025, 1, 14, 20, 0), 99.0, 99.5, 98.8, 99.2))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 14, 21, 0), 99.2, 100.5, 99.0, 100.0))  # high
    bars.append(_bar(_et_to_naive_utc(2025, 1, 14, 22, 0), 100.0, 100.2, 98.0, 98.5))  # low
    bars.append(_bar(_et_to_naive_utc(2025, 1, 14, 23, 0), 98.5, 99.0, 98.2, 98.8))
    return bars


def test_load_asia_range_config():
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config("MES")
    assert cfg.asia_range.enabled is True
    # Live Asia opt: all weekdays (None), no EQ bias, NY+PD liq only.
    assert cfg.asia_range.allowed_weekdays is None
    assert cfg.asia_range.require_eq_bias is False
    assert cfg.asia_range.range_start == time(20, 0)
    assert cfg.asia_range.search_start == time(2, 0)
    assert cfg.asia_range.target_mode == "opposite_extreme"
    assert cfg.asia_range.use_ny_liquidity is True
    assert cfg.asia_range.ny_session_start == time(9, 30)
    assert cfg.asia_range.use_pd_liquidity is True
    assert cfg.asia_range.use_prev_session_liquidity is False
    assert cfg.asia_range.prev_session_start == time(2, 0)


def _london_prev_session_bars() -> list[Bar]:
    """Prior London on Jan 14: PSH=104.0 PSL=95.0."""
    return [
        _bar(_et_to_naive_utc(2025, 1, 14, 2, 0), 100.0, 101.0, 99.0, 100.5),
        _bar(_et_to_naive_utc(2025, 1, 14, 4, 0), 100.5, 104.0, 100.0, 103.0),  # high
        _bar(_et_to_naive_utc(2025, 1, 14, 6, 0), 103.0, 103.2, 95.0, 96.0),  # low
        _bar(_et_to_naive_utc(2025, 1, 14, 7, 30), 96.0, 97.0, 95.5, 96.5),
    ]


def test_compute_asia_range_attaches_pd_and_prev_session():
    bars = (
        _london_prev_session_bars()
        + _ny_rth_bars()
        + _asia_evening_bars()
    )
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.1, 98.9, 99.0))
    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        use_ny_liquidity=True,
        use_pd_liquidity=True,
        use_prev_session_liquidity=True,
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None and not ar.skipped
    assert ar.high == 100.5
    assert ar.low == 98.0
    assert ar.ny_high == 102.0
    assert ar.ny_low == 97.0
    # PD covers London+NY+Asia on Jan 14
    assert ar.pd_high == 104.0
    assert ar.pd_low == 95.0
    assert ar.prev_session_high == 104.0
    assert ar.prev_session_low == 95.0


def test_asia_judas_long_pd_low_sweep():
    """Sweep prior-session / PD low above Asia low without taking ARL."""
    bars = [
        _bar(_et_to_naive_utc(2025, 1, 14, 3, 0), 99.0, 100.0, 97.5, 98.5),  # PSL=97.5
        _bar(_et_to_naive_utc(2025, 1, 14, 20, 0), 98.5, 100.5, 96.0, 97.0),  # ARL=96
        _bar(_et_to_naive_utc(2025, 1, 14, 22, 0), 97.0, 97.5, 96.5, 97.2),
        _bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 97.5, 97.6, 97.4, 97.5),
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 97.5, 97.6, 97.3, 97.4),
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 97.4, 97.8, 97.2, 97.6),  # sweep PSL
    ]
    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        use_ny_liquidity=False,
        use_pd_liquidity=True,
        use_prev_session_liquidity=True,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None and not ar.skipped
    assert ar.prev_session_low == 97.5
    assert ar.low == 96.0
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert "prev_session_low" in setup.sweep_levels or "pd_low" in setup.sweep_levels
    assert "asia_low" not in setup.sweep_levels
    assert setup.entry_price == 97.6


def test_asia_judas_short_prev_session_high_sweep():
    """Sweep prior London high without needing Asia high alone."""
    bars = [
        _bar(_et_to_naive_utc(2025, 1, 14, 3, 0), 100.0, 103.5, 99.5, 102.0),  # PSH=103.5
        _bar(_et_to_naive_utc(2025, 1, 14, 20, 0), 101.0, 101.5, 99.0, 100.0),  # ARH=101.5
        _bar(_et_to_naive_utc(2025, 1, 14, 22, 0), 100.0, 100.5, 99.0, 99.5),
        _bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 100.5, 100.6, 100.4, 100.5),
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 100.5, 100.8, 100.4, 100.6),  # above EQ
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 100.6, 104.0, 100.5, 103.8),
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 35), 103.5, 103.6, 100.0, 101.0),
    ]
    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        use_ny_liquidity=False,
        use_pd_liquidity=True,
        use_prev_session_liquidity=True,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None
    assert ar.prev_session_high == 103.5
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert (
        "prev_session_high" in setup.sweep_levels
        or "pd_high" in setup.sweep_levels
    )



def _ny_rth_bars() -> list[Bar]:
    """Prior NY RTH on Jan 14: NYH=102.0 NYL=97.0 (outside Asia range)."""
    return [
        _bar(_et_to_naive_utc(2025, 1, 14, 9, 30), 100.0, 101.0, 99.5, 100.5),
        _bar(_et_to_naive_utc(2025, 1, 14, 11, 0), 100.5, 102.0, 100.0, 101.5),  # high
        _bar(_et_to_naive_utc(2025, 1, 14, 14, 0), 101.0, 101.2, 97.0, 97.5),  # low
        _bar(_et_to_naive_utc(2025, 1, 14, 15, 55), 97.5, 98.0, 97.2, 97.8),
    ]


def test_compute_asia_range_attaches_ny_hl():
    bars = _ny_rth_bars() + _asia_evening_bars()
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.1, 98.9, 99.0))
    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        use_ny_liquidity=True,
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None and not ar.skipped
    assert ar.high == 100.5
    assert ar.low == 98.0
    assert ar.ny_high == 102.0
    assert ar.ny_low == 97.0


def test_asia_judas_long_ny_low_sweep_without_asia_low():
    """Sweep prior NY low (above Asia low) counts as SSL even if ARL untouched."""
    # Asia: high 100.5 low 96.0 — NYL=97.5 sits inside Asia so we can sweep NYL
    # without taking ARL.
    bars = [
        _bar(_et_to_naive_utc(2025, 1, 14, 9, 30), 99.0, 100.0, 98.5, 99.5),
        _bar(_et_to_naive_utc(2025, 1, 14, 12, 0), 99.5, 100.0, 97.5, 98.0),  # NYL=97.5
        _bar(_et_to_naive_utc(2025, 1, 14, 15, 0), 98.0, 98.5, 97.8, 98.2),
        _bar(_et_to_naive_utc(2025, 1, 14, 20, 0), 98.5, 100.5, 96.0, 97.0),  # Asia H/L
        _bar(_et_to_naive_utc(2025, 1, 14, 22, 0), 97.0, 97.5, 96.5, 97.2),
        _bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 97.5, 97.6, 97.4, 97.5),
        # Below EQ → bullish; EQ = (100.5+96)/2 = 98.25
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 97.5, 97.6, 97.3, 97.4),
        # Sweep NYL 97.5 but stay above ARL 96.0, reclaim above NYL
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 97.4, 97.8, 97.2, 97.6),
    ]
    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        use_ny_liquidity=True,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None and not ar.skipped
    assert ar.ny_low == 97.5
    assert ar.low == 96.0
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert "ny_low" in setup.sweep_levels
    assert "asia_low" not in setup.sweep_levels
    assert setup.entry_price == 97.6


def test_asia_judas_short_ny_high_sweep():
    """Sweep prior NY high (above Asia high) counts as BSL."""
    bars = [
        _bar(_et_to_naive_utc(2025, 1, 14, 9, 30), 100.0, 103.0, 99.5, 102.0),  # NYH=103
        _bar(_et_to_naive_utc(2025, 1, 14, 15, 0), 102.0, 102.5, 101.0, 101.5),
        _bar(_et_to_naive_utc(2025, 1, 14, 20, 0), 101.0, 101.5, 99.0, 100.0),  # ARH=101.5
        _bar(_et_to_naive_utc(2025, 1, 14, 22, 0), 100.0, 100.5, 99.0, 99.5),
        _bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 100.5, 100.6, 100.4, 100.5),
        # Above EQ → bearish; EQ=(101.5+99)/2=100.25
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 100.5, 100.8, 100.4, 100.6),
        # Sweep NYH 103 without reclaim yet
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 100.6, 103.5, 100.5, 103.2),
        # Reclaim below NYH (and below ARH)
        _bar(_et_to_naive_utc(2025, 1, 15, 2, 35), 103.0, 103.1, 100.0, 100.8),
    ]
    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        use_ny_liquidity=True,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None
    assert ar.ny_high == 103.0
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert "ny_high" in setup.sweep_levels
    assert setup.sweep_extreme == 103.5



def test_compute_asia_range_20_to_midnight():
    bars = _asia_evening_bars()
    # Midnight open + morning (not in range)
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.2, 98.9, 99.1))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 1, 0), 99.1, 101.0, 99.0, 100.5))  # post-midnight high ignored
    cfg = AsiaRangeConfig(enabled=True, min_width_points=1.0, max_width_points=50.0)
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None
    assert not ar.skipped
    assert ar.high == 100.5
    assert ar.low == 98.0
    assert ar.eq == (100.5 + 98.0) / 2.0
    assert ar.midnight_open == 99.0
    assert ar.bar_count == 4


def test_asia_judas_long_ssl_sweep_reclaim():
    bars = _asia_evening_bars()
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.1, 98.9, 99.0))
    # Below EQ at search open → bullish bias (ARL=98, ARH=100.5, EQ=99.25)
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 99.0, 99.1, 98.5, 98.8))
    # Sweep below ARL with reclaim close on the same bar (Judas wick + close back inside)
    sweep = _bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 98.8, 98.9, 97.5, 98.5)
    bars.append(sweep)
    # Target hit at ARH
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 4, 0), 99.0, 100.6, 98.9, 100.5))

    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
        scale_fraction=1.0,
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None and not ar.skipped
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert setup.state is AsiaSetupState.RECLAIMED
    assert setup.entry_price == 98.5
    assert setup.sweep_extreme == 97.5
    assert setup.initial_stop == 97.5 - 0.5  # 2 ticks
    assert setup.target_price == 100.5


def test_asia_judas_reclaim_when_level_inside_bar_hl():
    """No ticks: after SSL sweep, reclaim if ARL sits inside later bar H/L and close holds."""
    bars = _asia_evening_bars()
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.1, 98.9, 99.0))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 99.0, 99.1, 98.5, 98.8))
    # Sweep SSL, close still below ARL=98 (no reclaim yet)
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 98.5, 98.6, 97.2, 97.8))
    # Level 98 inside H/L; close back at reclaim (OHLC proxy, no tick path)
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 35), 97.9, 98.4, 97.7, 98.0))

    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
        scale_fraction=1.0,
        use_ny_liquidity=False,
        use_pd_liquidity=False,
        use_prev_session_liquidity=False,
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    assert ar is not None and not ar.skipped
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert setup.entry_price == 98.0
    assert "OHLC range" in setup.notes


def test_asia_judas_short_bsl_sweep_reclaim():
    bars = _asia_evening_bars()
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.5, 99.6, 99.4, 99.5))
    # Above EQ at search open → bearish bias
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 99.5, 99.8, 99.4, 99.6))
    # Sweep above ARH then reclaim close below
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 99.6, 101.2, 99.5, 100.8))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 35), 100.8, 100.9, 100.0, 100.2))  # close < ARH

    cfg = AsiaRangeConfig(
        enabled=True,
        min_width_points=1.0,
        max_width_points=50.0,
        require_eq_bias=True,
        stop_buffer_ticks=2,
        target_mode="opposite_extreme",
    )
    ar = compute_asia_range(bars, date(2025, 1, 15), cfg)
    setup = detect_asia_judas(bars, ar, cfg, tick_size=0.25)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.state is AsiaSetupState.RECLAIMED
    assert setup.target_price == 98.0
    assert setup.sweep_extreme == 101.2


def test_asia_backtest_records_trade():
    bars = _asia_evening_bars()
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.1, 98.9, 99.0))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 99.0, 99.1, 98.5, 98.8))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 98.8, 98.9, 97.5, 98.5))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 4, 0), 99.0, 100.6, 98.9, 100.5))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 11, 55), 100.5, 100.6, 100.4, 100.5))

    clear_mes_5orb_config_cache()
    base = load_mes_5orb_config("MES")
    # Isolate Asia: no OR sessions so only Judas trades appear.
    cfg = replace(
        base,
        sessions=(),
        asia_range=AsiaRangeConfig(
            enabled=True,
            min_width_points=1.0,
            max_width_points=50.0,
            require_eq_bias=True,
            stop_buffer_ticks=2,
            target_mode="opposite_extreme",
            scale_fraction=1.0,
            search_start=time(2, 0),
            search_end=time(11, 0),
            force_flat=time(11, 55),
        ),
    )
    result = run_mes_5orb_backtest(bars, cfg=cfg)
    asia = [t for t in result.trades if t.session_name == "asia"]
    assert len(asia) == 1
    assert asia[0].direction == "long"
    assert asia[0].pnl_usd > 0


def test_evaluate_asia_live_ok():
    bars = _asia_evening_bars()
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 0, 0), 99.0, 99.1, 98.9, 99.0))
    bars.append(_bar(_et_to_naive_utc(2025, 1, 15, 2, 0), 99.0, 99.1, 98.5, 98.8))
    last = _bar(_et_to_naive_utc(2025, 1, 15, 2, 30), 98.8, 98.9, 97.5, 98.5)
    bars.append(last)

    clear_mes_5orb_config_cache()
    base = load_mes_5orb_config("MES")
    cfg = replace(
        base,
        asia_range=AsiaRangeConfig(
            enabled=True,
            min_width_points=1.0,
            max_width_points=50.0,
            require_eq_bias=True,
            stop_buffer_ticks=2,
            target_mode="opposite_extreme",
            scale_fraction=0.5,
        ),
    )
    sig = evaluate_mes_signal_live(bars, session_name="asia", cfg=cfg, as_of=last.ts)
    assert sig["ok"] is True
    assert sig["session"] == "asia"
    assert sig["plan"]["direction"] == "long"
    assert sig["plan"]["stop_mode"] == "asia_sweep"
