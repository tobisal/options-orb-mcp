from datetime import datetime
from types import SimpleNamespace

from core.ibkr_client import (
    _nearest_strikes,
    atm_strike_grid,
    chain_is_usable,
    select_option_chain,
)
from core.models import Direction, ORBSignal, Regime, SessionWindow
from core.strategy.spreads import build_spread


def _chain(**kw) -> SimpleNamespace:
    return SimpleNamespace(
        exchange=kw.get("exchange", "SMART"),
        tradingClass=kw.get("tradingClass", "SPY"),
        multiplier=kw.get("multiplier", "100"),
        expirations=kw.get("expirations", ["20260918"]),
        strikes=kw.get("strikes", list(range(750, 801))),
    )


def test_select_prefers_standard_class_over_2spy():
    mini = _chain(tradingClass="2SPY", strikes=[668.0, 672.0, 682.0])
    standard = _chain(tradingClass="SPY", strikes=list(range(750, 801)))
    chosen = select_option_chain([mini, standard], "SPY", 775.12)
    assert chosen is not None
    assert chosen.tradingClass == "SPY"


def test_far_2spy_strikes_are_unusable():
    assert chain_is_usable(775.12, [668.0, 672.0, 682.0]) is False
    assert chain_is_usable(775.12, list(range(770, 781))) is True


def test_nearest_strikes_empty_when_spot_missing():
    assert _nearest_strikes([668.0, 672.0, 682.0], 0.0, 8) == []


def test_atm_grid_builds_a_vertical_when_ibkr_chain_does_not():
    signal = ORBSignal(
        symbol="SPY",
        window=SessionWindow.NEW_YORK,
        as_of=datetime(2026, 8, 17, 15, 0),
        range_high=776.78,
        range_low=775.02,
        last_price=774.5,
        direction=Direction.SHORT,
        breakout=True,
        strength=0.4,
        regime=Regime.UNCERTAIN,
    )
    bad = build_spread(
        signal,
        spot=775.12,
        expiry="20260904",
        days_to_expiry=18,
        iv=0.25,
        strikes=[668.0, 672.0, 682.0],
    )
    assert bad is None
    good = build_spread(
        signal,
        spot=775.12,
        expiry="20260904",
        days_to_expiry=18,
        iv=0.25,
        strikes=atm_strike_grid(775.12),
    )
    assert good is not None
    assert good.long_leg.strike != good.short_leg.strike
