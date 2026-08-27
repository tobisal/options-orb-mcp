"""execution-mcp: the executing agent.

Previews and places defined-risk vertical spreads with bracket-style
take-profit / stop-loss. Every placement passes the risk manager and the
paper/live safety gate. In synthetic mode it simulates fills so the full loop
can be demonstrated offline; otherwise it routes real orders to IBKR (paper by
default).
"""

from __future__ import annotations

from typing import Any

from core.config import get_settings
from core.db import Database
from core.engine import build_trade_plan, place_trade_plan
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.journal import paper_account_snapshot
from core.marketdata import fetch_bars_with_fallback
from core.models import TradeStatus
from core.risk import RiskManager
from servers._compat import create_server

mcp = create_server(
    "execution-mcp",
    instructions=(
        "Executing agent for the Options ORB system. preview_spread builds a "
        "risk-sized, defined-risk vertical from the current ORB signal WITHOUT "
        "trading. place_spread submits it (paper by default; live requires two "
        "safety switches). Use use_synthetic=true to simulate the full loop offline."
    ),
)

_db = Database()


@mcp.tool()
async def preview_spread(
    symbol: str = "SPY",
    window: str = "auto",
    target_r: float | None = None,
    use_synthetic: bool = False,
    synthetic_seed: int = 42,
) -> dict[str, Any]:
    """Preview a risk-sized, defined-risk vertical for the current ORB signal.

    Does NOT place any order. Returns the signal, the proposed spread (strikes,
    max loss/profit, TP/SL prices) and the risk decision.
    """
    return await build_trade_plan(
        symbol, window, use_synthetic=use_synthetic, target_r=target_r,
        synthetic_seed=synthetic_seed, db=_db,
    )


@mcp.tool()
async def place_spread(
    symbol: str = "SPY",
    window: str = "auto",
    target_r: float | None = None,
    use_synthetic: bool = False,
    confirm: bool = False,
    synthetic_seed: int = 42,
) -> dict[str, Any]:
    """Place the previewed vertical spread with bracket TP/SL.

    Requires confirm=true. Paper by default; live needs ACCOUNT_MODE=live plus
    the LIVE_TRADING_CONFIRM interlock. In synthetic mode the fill is simulated
    and recorded to the journal so the loop can be demonstrated offline.
    """
    if not confirm:
        return {"ok": False, "error": "Refusing to place without confirm=true."}

    preview = await build_trade_plan(
        symbol, window, use_synthetic=use_synthetic, target_r=target_r,
        synthetic_seed=synthetic_seed, db=_db,
    )
    return await place_trade_plan(
        preview, symbol=symbol, window=window, use_synthetic=use_synthetic, db=_db,
    )


@mcp.tool()
async def close_position(trade_id: int, exit_price: float) -> dict[str, Any]:
    """Close an open journal trade at ``exit_price`` (spread structure value).

    P&L = (exit_price - entry_price) * 100 * contracts, since the position is
    modelled as long the spread structure.
    """
    trade = _db.get_trade(trade_id)
    if trade is None:
        return {"ok": False, "error": f"No trade with id {trade_id}."}
    if trade.status is not TradeStatus.OPEN:
        return {"ok": False, "error": f"Trade {trade_id} is not open (status={trade.status.value})."}

    pnl = round((exit_price - trade.entry_price) * 100 * trade.contracts, 2)
    _db.close_trade(trade_id, exit_price=exit_price, pnl=pnl)
    return {"ok": True, "trade_id": trade_id, "exit_price": exit_price, "pnl": pnl}


@mcp.tool()
async def positions() -> dict[str, Any]:
    """List open journal trades and (if connected) live IBKR positions."""
    open_trades = [t.model_dump(mode="json") for t in _db.query_trades(status=TradeStatus.OPEN)]
    result: dict[str, Any] = {"open_trades": open_trades, "count": len(open_trades)}
    try:
        async with IBKRClient() as ib:
            result["ibkr_positions"] = await ib.positions()
    except IBKRUnavailable as exc:
        result["ibkr_positions"] = None
        result["ibkr_note"] = str(exc)
    return result


@mcp.tool()
async def account() -> dict[str, Any]:
    """Report environment, risk budgets, and (if connected) IBKR account values."""
    settings = get_settings()
    rm = RiskManager(db=_db)
    tripped, realised = rm.daily_loss_tripped()
    opens = _db.query_trades(status=TradeStatus.OPEN, environment=rm.environment(), limit=1000)
    spots: dict[str, float] = {}
    for symbol in {t.symbol for t in opens}:
        try:
            bars, _, _ = await fetch_bars_with_fallback(
                symbol, duration="2 D", bar_size="5 mins"
            )
        except IBKRUnavailable:
            continue
        if bars:
            spots[symbol.upper()] = bars[-1].close
    paper = paper_account_snapshot(
        _db,
        spots,
        starting_capital=settings.starting_capital,
        environment=rm.environment(),
    )
    out: dict[str, Any] = {
        "environment": rm.environment(),
        "account_currency": settings.account_currency,
        "starting_capital": settings.starting_capital,
        "paper_equity": paper["paper_equity"],
        "lifetime_realised_pnl": paper["lifetime_realised_pnl"],
        "open_unrealized_pnl": paper["open_unrealized_pnl"],
        "daily_pnl": paper["daily_pnl"],
        "risk_budget_per_trade": round(rm.risk_budget_per_trade(), 2),
        "daily_loss_limit": round(rm.daily_loss_limit(), 2),
        "daily_realised_pnl": round(realised, 2),
        "daily_kill_switch_tripped": tripped,
        "open_positions": _db.open_position_count(environment=rm.environment()),
        "max_open_positions": settings.max_open_positions,
        "max_open_positions_per_window": settings.max_open_positions_per_window,
        "live_gate_ok": settings.is_live or settings.account_mode.value == "paper",
    }
    try:
        async with IBKRClient() as ib:
            out["ibkr_account"] = await ib.account_summary()
    except IBKRUnavailable as exc:
        out["ibkr_account"] = None
        out["ibkr_note"] = str(exc)
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
