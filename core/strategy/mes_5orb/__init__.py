"""Futures 5-minute ORB break-and-retest strategy package."""

from core.strategy.mes_5orb.markets import (
    DEFAULT_FUTURES_SYMBOL,
    FUTURES_MARKETS,
    coerce_futures_symbol,
    get_futures_market,
    is_supported_futures,
    supported_futures_symbols,
)
from core.strategy.mes_5orb.opening_range import OpeningRange, compute_opening_range
from core.strategy.mes_5orb.plan import MesTradePlan
from core.strategy.mes_5orb.sessions import Mes5OrbConfig, MesSession, load_mes_5orb_config
from core.strategy.mes_5orb.signals import BreakRetestSetup, SetupState, detect_break_retest
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop

__all__ = [
    "BreakRetestSetup",
    "DEFAULT_FUTURES_SYMBOL",
    "FUTURES_MARKETS",
    "Mes5OrbConfig",
    "MesSession",
    "MesTradePlan",
    "OpeningRange",
    "SetupState",
    "SwingTrailingStop",
    "coerce_futures_symbol",
    "compute_opening_range",
    "detect_break_retest",
    "get_futures_market",
    "is_supported_futures",
    "load_mes_5orb_config",
    "supported_futures_symbols",
]
