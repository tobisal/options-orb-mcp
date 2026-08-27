import math

from core.models import OptionRight
from core.pricing import (
    black_scholes_price,
    greeks,
    implied_vol,
    vertical_spread_value,
)


def test_put_call_parity():
    s, k, t, sigma, r = 100.0, 100.0, 0.5, 0.2, 0.04
    c = black_scholes_price(s, k, t, sigma, OptionRight.CALL, r)
    p = black_scholes_price(s, k, t, sigma, OptionRight.PUT, r)
    # C - P = S - K*e^{-rT}
    assert math.isclose(c - p, s - k * math.exp(-r * t), abs_tol=1e-6)


def test_intrinsic_at_expiry():
    assert black_scholes_price(110, 100, 0, 0.2, OptionRight.CALL) == 10
    assert black_scholes_price(90, 100, 0, 0.2, OptionRight.CALL) == 0
    assert black_scholes_price(90, 100, 0, 0.2, OptionRight.PUT) == 10


def test_implied_vol_roundtrip():
    s, k, t, sigma = 100.0, 105.0, 0.25, 0.30
    price = black_scholes_price(s, k, t, sigma, OptionRight.CALL)
    recovered = implied_vol(price, s, k, t, OptionRight.CALL)
    assert recovered is not None
    assert abs(recovered - sigma) < 1e-3


def test_greeks_delta_bounds():
    g_call = greeks(100, 100, 0.5, 0.2, OptionRight.CALL)
    g_put = greeks(100, 100, 0.5, 0.2, OptionRight.PUT)
    assert 0.0 < g_call.delta < 1.0
    assert -1.0 < g_put.delta < 0.0
    assert g_call.gamma > 0
    assert g_call.vega > 0
    assert g_call.theta < 0  # long options lose to time


def test_vertical_spread_bounded_by_width():
    # A call debit spread value stays within [0, width].
    width = 5.0
    v = vertical_spread_value(100, 100, 105, 0.25, 0.2, OptionRight.CALL)
    assert 0.0 <= v <= width
