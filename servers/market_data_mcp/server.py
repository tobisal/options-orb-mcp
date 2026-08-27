"""market-data-mcp: the market-analysis agent.

Exposes tools to compute the ORB signal, classify the regime, and pull option
chains / implied volatility. Works offline (synthetic data) for the signal
tools; option chain / IV require a live IBKR connection.
"""

from __future__ import annotations

from typing import Any

from core.active_params import resolve_trading_config, strategy_payload
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.marketdata import fetch_bars_with_fallback
from core.models import SessionWindow
from core.sessions import active_window
from core.strategy.orb import classify_regime as classify_regime_fn
from core.strategy.orb import compute_orb_signal
from servers._compat import create_server

mcp = create_server(
    "market-data-mcp",
    instructions=(
        "Market-analysis agent for the Options ORB system. Use get_session_orb to "
        "detect an opening-range breakout on the underlying, classify_regime to tell "
        "trend from range, and get_option_chain / get_iv for option pricing context. "
        "Pass use_synthetic=true to run offline without IBKR (for demos/tests)."
    ),
)


def _resolve_window(window: str) -> SessionWindow:
    if window.lower() in ("auto", "active", "current"):
        w = active_window()
        if w is None:
            # Default to New York when no window is active right now.
            return SessionWindow.NEW_YORK
        return w
    return SessionWindow(window.lower())


@mcp.tool()
async def get_session_orb(
    symbol: str = "SPY",
    window: str = "auto",
    use_synthetic: bool = False,
    synthetic_seed: int = 42,
) -> dict[str, Any]:
    """Compute the Opening Range Breakout signal for a symbol and session window.

    Args:
        symbol: Underlying ticker (e.g. SPY, QQQ).
        window: One of asia | london | new_york | auto.
        use_synthetic: If true, use deterministic offline data (no IBKR).
        synthetic_seed: Seed for the offline data (reproducible demos).

    Returns the breakout direction, strength, opening range, ATR and regime.
    """
    win = _resolve_window(window)
    try:
        bars, source, warning = await fetch_bars_with_fallback(
            symbol, duration="3 D", bar_size="5 mins",
            use_synthetic=use_synthetic, synthetic_seed=synthetic_seed,
        )
    except IBKRUnavailable as exc:
        return {"error": str(exc), "hint": "Pass use_synthetic=true for an offline demo."}

    cfg, found = resolve_trading_config(symbol, win)
    signal = compute_orb_signal(symbol, win, bars, cfg)
    out = signal.model_dump(mode="json")
    out["data_source"] = source
    out["strategy"] = strategy_payload(cfg, found)
    if warning:
        out["warning"] = warning
    return out


@mcp.tool()
async def classify_regime(
    symbol: str = "SPY",
    window: str = "auto",
    use_synthetic: bool = False,
    synthetic_seed: int = 42,
) -> dict[str, Any]:
    """Classify the post-opening-range action as trend, range, or uncertain."""
    win = _resolve_window(window)
    try:
        bars, source, warning = await fetch_bars_with_fallback(
            symbol, duration="3 D", bar_size="5 mins",
            use_synthetic=use_synthetic, synthetic_seed=synthetic_seed,
        )
    except IBKRUnavailable as exc:
        return {"error": str(exc), "hint": "Pass use_synthetic=true for an offline demo."}

    cfg, found = resolve_trading_config(symbol, win)
    regime = classify_regime_fn(bars, cfg)
    result = {
        "symbol": symbol,
        "window": win.value,
        "regime": regime.value,
        "data_source": source,
        "strategy": strategy_payload(cfg, found),
    }
    if warning:
        result["warning"] = warning
    return result


@mcp.tool()
async def get_option_chain(symbol: str = "SPY") -> dict[str, Any]:
    """Return near-the-money expiries and strikes for a symbol (requires IBKR)."""
    try:
        async with IBKRClient() as ib:
            return await ib.option_chain(symbol)
    except IBKRUnavailable as exc:
        return {"error": str(exc)}


@mcp.tool()
async def get_iv(symbol: str = "SPY", expiry: str = "") -> dict[str, Any]:
    """Approximate at-the-money implied volatility for a symbol/expiry (requires IBKR).

    If expiry is empty, the nearest available expiry is used.
    """
    try:
        async with IBKRClient() as ib:
            if not expiry:
                chain = await ib.option_chain(symbol)
                if not chain.get("expiries"):
                    return {"error": f"No expiries available for {symbol}"}
                expiry = chain["expiries"][0]
            iv = await ib.atm_iv(symbol, expiry)
            return {"symbol": symbol, "expiry": expiry, "atm_iv": iv}
    except IBKRUnavailable as exc:
        return {"error": str(exc)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
