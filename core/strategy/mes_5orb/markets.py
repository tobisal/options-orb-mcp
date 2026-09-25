"""Supported CME/CBOT futures for the 5ORB break/retest stack."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class FuturesMarket:
    symbol: str
    exchange: str
    point_value: float
    tick_size: float
    # Default OR filters (points). Per-symbol JSON can override.
    london_min_range: float
    london_max_range: float
    ny_min_range: float
    ny_max_range: float
    label: str = ""


# Equity-index futures commonly traded with a 5-min ORB / break-retest.
FUTURES_MARKETS: dict[str, FuturesMarket] = {
    "MES": FuturesMarket(
        symbol="MES",
        exchange="CME",
        point_value=5.0,
        tick_size=0.25,
        london_min_range=0.75,
        london_max_range=6.0,
        ny_min_range=1.0,
        ny_max_range=8.0,
        label="Micro E-mini S&P 500",
    ),
    "MNQ": FuturesMarket(
        symbol="MNQ",
        exchange="CME",
        point_value=2.0,
        tick_size=0.25,
        london_min_range=4.0,
        london_max_range=40.0,
        ny_min_range=6.0,
        ny_max_range=60.0,
        label="Micro E-mini Nasdaq-100",
    ),
    "MYM": FuturesMarket(
        symbol="MYM",
        exchange="CBOT",
        point_value=0.50,
        tick_size=1.0,
        london_min_range=20.0,
        london_max_range=200.0,
        ny_min_range=30.0,
        ny_max_range=300.0,
        label="Micro E-mini Dow",
    ),
    "M2K": FuturesMarket(
        symbol="M2K",
        exchange="CME",
        point_value=5.0,
        tick_size=0.10,
        london_min_range=1.5,
        london_max_range=12.0,
        ny_min_range=2.0,
        ny_max_range=16.0,
        label="Micro E-mini Russell 2000",
    ),
    "ES": FuturesMarket(
        symbol="ES",
        exchange="CME",
        point_value=50.0,
        tick_size=0.25,
        london_min_range=0.75,
        london_max_range=6.0,
        ny_min_range=1.0,
        ny_max_range=8.0,
        label="E-mini S&P 500",
    ),
    "NQ": FuturesMarket(
        symbol="NQ",
        exchange="CME",
        point_value=20.0,
        tick_size=0.25,
        london_min_range=4.0,
        london_max_range=40.0,
        ny_min_range=6.0,
        ny_max_range=60.0,
        label="E-mini Nasdaq-100",
    ),
}

DEFAULT_FUTURES_SYMBOL = "MES"


def normalize_futures_symbol(symbol: str | None) -> str:
    sym = (symbol or DEFAULT_FUTURES_SYMBOL).strip().upper()
    return sym


def is_supported_futures(symbol: str | None) -> bool:
    return normalize_futures_symbol(symbol) in FUTURES_MARKETS


def get_futures_market(symbol: str | None) -> FuturesMarket:
    sym = normalize_futures_symbol(symbol)
    if sym not in FUTURES_MARKETS:
        supported = ", ".join(sorted(FUTURES_MARKETS))
        raise KeyError(f"Unsupported futures symbol {sym!r}. Supported: {supported}")
    return FUTURES_MARKETS[sym]


def supported_futures_symbols() -> list[str]:
    return sorted(FUTURES_MARKETS.keys())


def coerce_futures_symbol(symbol: str | None, *, default: str = DEFAULT_FUTURES_SYMBOL) -> str:
    """Return symbol if supported, else default."""
    sym = normalize_futures_symbol(symbol)
    if sym in FUTURES_MARKETS:
        return sym
    return normalize_futures_symbol(default)


def iter_futures_markets() -> Iterable[FuturesMarket]:
    return FUTURES_MARKETS.values()
