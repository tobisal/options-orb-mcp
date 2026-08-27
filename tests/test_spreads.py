from datetime import datetime

from core.models import Direction, ORBSignal, Regime, SessionWindow, SpreadType
from core.strategy.spreads import build_spread


def _signal(direction: Direction, regime: Regime) -> ORBSignal:
    return ORBSignal(
        symbol="SPY",
        window=SessionWindow.NEW_YORK,
        as_of=datetime(2026, 1, 6, 15, 0),
        range_high=100.5,
        range_low=99.5,
        last_price=102.0,
        direction=direction,
        breakout=True,
        strength=1.5,
        regime=regime,
    )


_STRIKES = [95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0]


def test_trend_long_builds_bull_call_debit():
    plan = build_spread(
        _signal(Direction.LONG, Regime.TREND),
        spot=100.0,
        expiry="20260116",
        days_to_expiry=7,
        iv=0.25,
        strikes=_STRIKES,
        contracts=1,
    )
    assert plan is not None
    assert plan.spread_type is SpreadType.BULL_CALL
    assert plan.net_debit > 0  # debit paid
    # Defined risk: max loss + max profit == spread width * 100 * contracts.
    width = abs(plan.short_leg.strike - plan.long_leg.strike)
    assert abs((plan.max_loss + plan.max_profit) - width * 100) < 1e-6


def test_range_long_builds_credit_spread():
    plan = build_spread(
        _signal(Direction.LONG, Regime.RANGE),
        spot=100.0,
        expiry="20260116",
        days_to_expiry=7,
        iv=0.25,
        strikes=_STRIKES,
        contracts=1,
    )
    assert plan is not None
    assert plan.spread_type is SpreadType.BULL_PUT
    assert plan.net_debit < 0  # credit received


def test_tp_above_sl():
    plan = build_spread(
        _signal(Direction.LONG, Regime.TREND),
        spot=100.0,
        expiry="20260116",
        days_to_expiry=7,
        iv=0.25,
        strikes=_STRIKES,
        contracts=1,
    )
    assert plan.take_profit_price > plan.stop_loss_price
