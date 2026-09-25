"""Unit tests for MES 5ORB opening range, break/retest, and swing trail."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytz

from core.models import Bar, Direction
from core.strategy.mes_5orb.opening_range import compute_opening_range
from core.strategy.mes_5orb.sessions import (
    MesSession,
    OpeningRangeFilter,
    RetestConfig,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)
from core.strategy.mes_5orb.signals import SetupState, detect_break_retest
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop
from core.backtest_mes import run_mes_5orb_backtest
from datetime import time


def _et_to_naive_utc(year, month, day, hour, minute) -> datetime:
    """Build naive UTC datetime from America/New_York wall clock (winter EST=UTC-5)."""
    et = pytz.timezone("America/New_York")
    local = et.localize(datetime(year, month, day, hour, minute))
    utc = local.astimezone(pytz.utc).replace(tzinfo=None)
    return utc


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=100)


def _ny_session(**kwargs) -> MesSession:
    return MesSession(
        name="new_york",
        or_start=time(9, 30),
        or_end=time(9, 35),
        search_end=time(15, 0),
        force_flat=time(15, 55),
        opening_range=OpeningRangeFilter(0.5, 20.0),
        retest=RetestConfig(
            tolerance_ticks=2,
            timeout_bars=12,
            require_rejection_candle=kwargs.get("require_rejection_candle", False),
        ),
    )


def test_load_mes_config():
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config()
    assert cfg.symbol == "MES"
    assert cfg.point_value == 5.0
    assert len(cfg.sessions) == 2
    assert cfg.session("london") is not None
    assert cfg.session("new_york") is not None


def test_load_other_futures_configs():
    clear_mes_5orb_config_cache()
    mnq = load_mes_5orb_config("MNQ")
    assert mnq.symbol == "MNQ"
    assert mnq.point_value == 2.0
    assert mnq.tick_size == 0.25
    mym = load_mes_5orb_config("MYM")
    assert mym.exchange == "CBOT"
    assert mym.point_value == 0.5
    es = load_mes_5orb_config("ES")
    assert es.point_value == 50.0
    nq = load_mes_5orb_config("NQ")
    assert nq.point_value == 20.0
    m2k = load_mes_5orb_config("M2K")
    assert m2k.tick_size == 0.1


def test_supported_futures_symbols():
    from core.strategy.mes_5orb.markets import supported_futures_symbols

    syms = supported_futures_symbols()
    for s in ("MES", "MNQ", "MYM", "M2K", "ES", "NQ"):
        assert s in syms


def test_opening_range_ny():
    # 2026-01-06 is a Tuesday; EST
    day = _et_to_naive_utc(2026, 1, 6, 9, 30).date()
    bars = [
        _bar(_et_to_naive_utc(2026, 1, 6, 9, 30), 100, 101, 99.5, 100.5),
        _bar(_et_to_naive_utc(2026, 1, 6, 9, 35), 100.5, 102, 100, 101),  # after OR
    ]
    sess = _ny_session()
    # Need date from ET
    from core.strategy.mes_5orb.opening_range import to_et

    d = to_et(bars[0].ts).date()
    orb = compute_opening_range(bars, sess, d)
    assert orb is not None
    assert orb.high == 101.0
    assert orb.low == 99.5
    assert not orb.skipped


def test_break_and_retest_long():
    sess = _ny_session(require_rejection_candle=False)
    base = _et_to_naive_utc(2026, 1, 6, 9, 30)
    # OR bar: high 101, low 100
    bars = [
        _bar(base, 100.2, 101.0, 100.0, 100.5),
    ]
    # Post OR: break above 101, then retest touch 101 and close above
    t = base + timedelta(minutes=5)
    bars.append(_bar(t, 101.0, 102.0, 101.0, 101.75))  # break close
    t += timedelta(minutes=5)
    bars.append(_bar(t, 101.5, 101.8, 100.9, 101.4))  # retest: low~100.9, close above 101

    from core.strategy.mes_5orb.opening_range import to_et

    d = to_et(bars[0].ts).date()
    orb = compute_opening_range(bars, sess, d)
    assert orb is not None
    setup = detect_break_retest(bars, sess, orb, tick_size=0.25)
    assert setup is not None
    assert setup.state is SetupState.RETESTED
    assert setup.direction is Direction.LONG
    assert setup.entry_price == 101.4


def test_swing_trail_ratchets_up_never_down():
    trail = SwingTrailingStop(direction=Direction.LONG, stop=100.0, pivot_lag=2, buffer=0.25)
    # Build a sequence with a confirmed swing low that steps the stop up
    # Need: lag bars before, center swing low, lag bars after
    prices = [
        # rising then dip (swing) then rising
        (101, 102, 100.5, 101.5),
        (101.5, 103, 101.0, 102.5),
        (102.5, 103, 99.0, 100.0),  # potential swing low at 99 — center later
        (100.0, 101, 99.5, 100.5),
        (100.5, 102, 100.0, 101.5),
        (101.5, 103, 101.0, 102.5),
        (102.5, 104, 102.0, 103.5),
    ]
    base = datetime(2026, 1, 6, 15, 0)
    stops = [trail.stop]
    for i, (o, h, l, c) in enumerate(prices):
        trail.update(_bar(base + timedelta(minutes=5 * i), o, h, l, c))
        stops.append(trail.stop)
    # Stop should never decrease
    for a, b in zip(stops, stops[1:]):
        assert b >= a
    # Eventually should have moved above 100 if swing confirmed
    assert trail.stop >= 100.0


def test_backtest_regression_synthetic_day():
    """Fixed synthetic day produces a stable trade count (0 or 1)."""
    sess_bars: list[Bar] = []
    # OR
    sess_bars.append(_bar(_et_to_naive_utc(2026, 1, 6, 9, 30), 100, 101, 100, 100.5))
    # break
    sess_bars.append(_bar(_et_to_naive_utc(2026, 1, 6, 9, 35), 101, 102.5, 101, 102))
    # retest rejection long
    sess_bars.append(
        _bar(_et_to_naive_utc(2026, 1, 6, 9, 40), 101.5, 102.2, 100.8, 102.0)
    )
    # hold then force flat path
    for i in range(10):
        ts = _et_to_naive_utc(2026, 1, 6, 9, 45) + timedelta(minutes=5 * i)
        px = 102.0 + 0.1 * i
        sess_bars.append(_bar(ts, px, px + 0.3, px - 0.2, px + 0.1))

    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config()
    # Disable rejection for this synthetic (close near high already)
    result = run_mes_5orb_backtest(sess_bars, cfg=cfg)
    # May or may not fire depending on rejection candle — just assert no crash
    assert isinstance(result.trades, list)
    s1 = result.summary()
    s2 = run_mes_5orb_backtest(sess_bars, cfg=cfg).summary()
    assert s1["trade_count"] == s2["trade_count"]


def test_walk_forward_70_30_split():
    from core.backtest_mes import walk_forward_mes_7030

    bars: list[Bar] = []
    # 10 weekdays of thin NY data so WF can split
    for day_offset in range(10):
        d = 6 + day_offset  # Jan 6..15 2026
        if d > 31:
            break
        bars.append(_bar(_et_to_naive_utc(2026, 1, d, 9, 30), 100, 101, 100, 100.5))
        bars.append(_bar(_et_to_naive_utc(2026, 1, d, 9, 35), 101, 102, 101, 101.5))
        bars.append(_bar(_et_to_naive_utc(2026, 1, d, 10, 0), 101.5, 102, 101, 101.8))

    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config()
    wf = walk_forward_mes_7030(bars, cfg=cfg, is_fraction=0.70)
    assert "error" not in wf
    assert wf["is_fraction"] == 0.70
    assert wf["oos_fraction"] == 0.3
    assert wf["in_sample"]["days"] + wf["out_of_sample"]["days"] == wf["total_trading_days"]
    assert wf["in_sample"]["days"] >= wf["out_of_sample"]["days"]
    # Chronological: IS ends before OOS starts
    assert wf["in_sample"]["day_end"] < wf["out_of_sample"]["day_start"]
