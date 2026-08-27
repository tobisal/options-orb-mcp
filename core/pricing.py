"""Black-Scholes option pricing and Greeks.

Used by the optimiser to model vertical-spread P&L from underlying price paths
plus an implied-volatility proxy, and by the executor to sanity-check quotes.
Pure stdlib (math only) so it is fast in tight backtest loops and runs without
any broker connection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.models import OptionRight

# A conservative default risk-free rate; overridable per call.
DEFAULT_RISK_FREE = 0.04

_SQRT_2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (fast, stdlib-only)."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point (0.01)


def _d1_d2(s: float, k: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    if t <= 0 or sigma <= 0:
        # Degenerate: treat as immediate expiry (handled by callers).
        return math.inf, math.inf
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2


def black_scholes_price(
    s: float,
    k: float,
    t: float,
    sigma: float,
    right: OptionRight,
    r: float = DEFAULT_RISK_FREE,
) -> float:
    """Price a European option.

    Args:
        s: spot price of the underlying.
        k: strike.
        t: time to expiry in years.
        sigma: annualised implied volatility (e.g. 0.20 for 20%).
        right: CALL or PUT.
        r: risk-free rate.
    """
    if t <= 0 or sigma <= 0:
        intrinsic = (s - k) if right is OptionRight.CALL else (k - s)
        return max(intrinsic, 0.0)
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    if right is OptionRight.CALL:
        return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)


def greeks(
    s: float,
    k: float,
    t: float,
    sigma: float,
    right: OptionRight,
    r: float = DEFAULT_RISK_FREE,
) -> Greeks:
    """Full set of first/second order Greeks for a European option."""
    price = black_scholes_price(s, k, t, sigma, right, r)
    if t <= 0 or sigma <= 0:
        delta = 0.0
        if right is OptionRight.CALL and s > k:
            delta = 1.0
        elif right is OptionRight.PUT and s < k:
            delta = -1.0
        return Greeks(price=price, delta=delta, gamma=0.0, theta=0.0, vega=0.0)

    d1, d2 = _d1_d2(s, k, t, r, sigma)
    pdf = _norm_pdf(d1)
    sqrt_t = math.sqrt(t)

    if right is OptionRight.CALL:
        delta = _norm_cdf(d1)
        theta_annual = -(s * pdf * sigma) / (2 * sqrt_t) - r * k * math.exp(-r * t) * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = -(s * pdf * sigma) / (2 * sqrt_t) + r * k * math.exp(-r * t) * _norm_cdf(-d2)

    gamma = pdf / (s * sigma * sqrt_t)
    vega = s * pdf * sqrt_t  # per 1.00 change in sigma

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        theta=theta_annual / 365.0,
        vega=vega / 100.0,  # per 1 vol point
    )


def implied_vol(
    market_price: float,
    s: float,
    k: float,
    t: float,
    right: OptionRight,
    r: float = DEFAULT_RISK_FREE,
    tol: float = 1e-5,
    max_iter: int = 100,
) -> float | None:
    """Recover implied volatility from a market price via bisection.

    Returns None if the price is outside no-arbitrage bounds.
    """
    if t <= 0 or market_price <= 0:
        return None
    intrinsic = max((s - k) if right is OptionRight.CALL else (k - s), 0.0)
    if market_price < intrinsic - tol:
        return None

    lo, hi = 1e-4, 5.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        price = black_scholes_price(s, k, t, mid, right, r)
        if abs(price - market_price) < tol:
            return mid
        if price > market_price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def vertical_spread_value(
    s: float,
    long_strike: float,
    short_strike: float,
    t: float,
    sigma: float,
    right: OptionRight,
    r: float = DEFAULT_RISK_FREE,
) -> float:
    """Net theoretical value of a one-lot vertical (long_strike - short_strike),
    per share (multiply by 100 for the contract value)."""
    long_v = black_scholes_price(s, long_strike, t, sigma, right, r)
    short_v = black_scholes_price(s, short_strike, t, sigma, right, r)
    return long_v - short_v
