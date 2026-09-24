"""Construct defined-risk vertical spreads from an ORB signal.

Design choice that keeps the maths uniform: every vertical is modelled as being
**long the structure** ``V = value(long_leg) - value(short_leg)``, placed as a
combo BUY at a signed limit price (positive = debit paid, negative = credit
received). You profit when ``V`` rises, so take-profit is always *above* the
entry and stop-loss always *below* it, for both debit and credit spreads.

Strike/structure selection:

- TREND / UNCERTAIN breakout -> directional **debit** spread (bull call / bear put).
- RANGE -> **credit** spread that harvests time decay, leaning with the weak
  breakout bias (bull put when leaning up, bear call when leaning down).
"""

from __future__ import annotations

from collections.abc import Callable

from core.models import (
    Direction,
    OptionLeg,
    OptionRight,
    ORBSignal,
    Regime,
    SpreadPlan,
    SpreadType,
)
from core.pricing import black_scholes_price

QuoteGetter = Callable[[OptionRight, float], float | None]

CONTRACT_MULTIPLIER = 100


def _strike_increment(strikes: list[float]) -> float:
    diffs = sorted({round(b - a, 4) for a, b in zip(strikes[:-1], strikes[1:]) if b > a})
    return diffs[0] if diffs else 1.0


def _nearest(strikes: list[float], target: float, *, floor: bool | None = None) -> float:
    if floor is True:
        below = [s for s in strikes if s <= target]
        return max(below) if below else min(strikes)
    if floor is False:
        above = [s for s in strikes if s >= target]
        return min(above) if above else max(strikes)
    return min(strikes, key=lambda s: abs(s - target))


def _select_spread_type(signal: ORBSignal) -> SpreadType:
    if signal.regime is Regime.RANGE:
        # Income: sell premium leaning with the (weak) bias.
        return SpreadType.BULL_PUT if signal.direction is not Direction.SHORT else SpreadType.BEAR_CALL
    # Trend / uncertain breakout: directional debit spread.
    return SpreadType.BULL_CALL if signal.direction is Direction.LONG else SpreadType.BEAR_PUT


def build_spread(
    signal: ORBSignal,
    *,
    spot: float,
    expiry: str,
    days_to_expiry: float,
    iv: float,
    strikes: list[float],
    quote_getter: QuoteGetter | None = None,
    target_r: float = 1.5,
    stop_r: float = 1.0,
    width_strikes: int = 1,
    contracts: int = 1,
    use_trailing_stop: bool = False,
    trail_activate_r: float = 0.5,
    trail_distance_r: float = 0.3,
) -> SpreadPlan | None:
    """Build a :class:`SpreadPlan` for the given signal, or None if infeasible.

    ``quote_getter`` returns the per-share mid price for a (right, strike); when
    absent, Black-Scholes with ``iv`` is used as the price proxy.
    """
    if not strikes or spot <= 0:
        return None

    strikes = sorted(strikes)
    inc = _strike_increment(strikes)
    width = inc * max(width_strikes, 1)
    t = max(days_to_expiry, 0.0) / 365.0
    stop_r = min(max(stop_r, 0.0), 1.0)

    def price(right: OptionRight, strike: float) -> float:
        if quote_getter is not None:
            q = quote_getter(right, strike)
            if q is not None and q > 0:
                return q
        return black_scholes_price(spot, strike, t, iv, right)

    spread_type = _select_spread_type(signal)

    if spread_type is SpreadType.BULL_CALL:
        right = OptionRight.CALL
        long_strike = _nearest(strikes, spot, floor=True)
        short_strike = long_strike + width
        direction = Direction.LONG
    elif spread_type is SpreadType.BEAR_PUT:
        right = OptionRight.PUT
        long_strike = _nearest(strikes, spot, floor=False)
        short_strike = long_strike - width
        direction = Direction.SHORT
    elif spread_type is SpreadType.BULL_PUT:  # credit, bullish
        right = OptionRight.PUT
        short_strike = _nearest(strikes, spot, floor=True)
        long_strike = short_strike - width
        direction = Direction.LONG
    else:  # BEAR_CALL credit, bearish
        right = OptionRight.CALL
        short_strike = _nearest(strikes, spot, floor=False)
        long_strike = short_strike + width
        direction = Direction.SHORT

    if long_strike not in strikes or short_strike not in strikes:
        # Snap to the nearest available strikes if the computed ones are off-grid.
        long_strike = _nearest(strikes, long_strike)
        short_strike = _nearest(strikes, short_strike)
    if long_strike == short_strike:
        return None

    long_val = price(right, long_strike)
    short_val = price(right, short_strike)
    v0 = long_val - short_val  # structure value: >0 debit, <0 credit

    is_debit = spread_type in (SpreadType.BULL_CALL, SpreadType.BEAR_PUT)
    if is_debit:
        v_low, v_high = 0.0, width
    else:
        v_low, v_high = -width, 0.0

    # Clamp entry value into the theoretical band to avoid nonsense from stale quotes.
    v0 = min(max(v0, v_low), v_high)

    max_loss_share = v0 - v_low
    max_profit_share = v_high - v0
    if max_loss_share <= 0 or max_profit_share <= 0:
        return None

    risk_share = stop_r * max_loss_share
    reward_share = min(target_r * risk_share, max_profit_share)

    take_profit_price = round(v0 + reward_share, 4)
    stop_loss_price = round(v0 - risk_share, 4)

    from core.trail import normalize_trail_params

    use_trail, act_r, dist_r = normalize_trail_params(
        use_trailing_stop, trail_activate_r, trail_distance_r
    )

    contracts = max(contracts, 0)
    mult = CONTRACT_MULTIPLIER
    plan = SpreadPlan(
        symbol=signal.symbol,
        spread_type=spread_type,
        direction=direction,
        expiry=expiry,
        long_leg=OptionLeg(
            right=right,
            strike=long_strike,
            expiry=expiry,
            action="BUY",
            quantity=1,
        ),
        short_leg=OptionLeg(
            right=right,
            strike=short_strike,
            expiry=expiry,
            action="SELL",
            quantity=1,
        ),
        contracts=contracts,
        net_debit=round(v0, 4),
        max_loss=round(max_loss_share * mult * contracts, 2),
        max_profit=round(max_profit_share * mult * contracts, 2),
        contract_multiplier=mult,
        target_r=target_r,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        use_trailing_stop=use_trail,
        trail_activate_r=act_r,
        trail_distance_r=dist_r,
        original_stop_loss_price=stop_loss_price,
        rationale=(
            f"{spread_type.value} on {signal.symbol}: {'debit' if is_debit else 'credit'} "
            f"structure V0={v0:.2f}, width={width:.2f}, regime={signal.regime.value}, "
            f"signal={signal.direction.value} (strength {signal.strength:.2f})."
        ),
    )
    return plan


def per_contract_max_loss(plan: SpreadPlan) -> float:
    """USD max loss for a single contract of this spread (incl. multiplier)."""
    if plan.contracts > 0:
        return plan.max_loss / plan.contracts
    # contracts==0 preview: derive from band width via net_debit.
    return abs(plan.net_debit) * plan.contract_multiplier
