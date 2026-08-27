"""research-mcp: the research agent.

Observes and learns from the trade journal: logs trades, answers queries,
produces performance reports, and turns realised history into an actionable
"should I take this setup?" recommendation broken down by window and regime.
"""

from __future__ import annotations

from typing import Any

from core.analytics import performance_metrics, summarize
from core.db import Database
from core.models import (
    Direction,
    Regime,
    SessionWindow,
    SpreadType,
    TradeRecord,
    TradeStatus,
)
from servers._compat import create_server

mcp = create_server(
    "research-mcp",
    instructions=(
        "Research agent for the Options ORB system. It learns from the trade "
        "journal. Use performance_report for overall stats, learn_from_history to "
        "check whether a given window/regime has positive expectancy before "
        "trading, and query_trades to inspect individual records."
    ),
)

_db = Database()

# Minimum sample size before we trust a window/regime edge.
_MIN_SAMPLE = 10


@mcp.tool()
async def log_trade(
    symbol: str,
    window: str,
    regime: str,
    spread_type: str,
    direction: str,
    contracts: int,
    entry_price: float,
    max_loss: float,
    max_profit: float,
    target_r: float = 1.5,
    signal_strength: float = 0.0,
    environment: str = "PAPER",
    notes: str = "",
) -> dict[str, Any]:
    """Manually record a trade in the journal (execution normally does this)."""
    record = TradeRecord(
        environment=environment,
        symbol=symbol,
        window=SessionWindow(window.lower()),
        regime=Regime(regime.lower()),
        spread_type=SpreadType(spread_type),
        direction=Direction(direction.lower()),
        contracts=contracts,
        entry_price=entry_price,
        max_loss=max_loss,
        max_profit=max_profit,
        target_r=target_r,
        status=TradeStatus.OPEN,
        signal_strength=signal_strength,
        notes=notes,
    )
    trade_id = _db.insert_trade(record)
    return {"ok": True, "trade_id": trade_id}


@mcp.tool()
async def query_trades(
    window: str | None = None,
    regime: str | None = None,
    status: str | None = None,
    environment: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Query journal trades with optional filters."""
    trades = _db.query_trades(
        window=SessionWindow(window.lower()) if window else None,
        regime=Regime(regime.lower()) if regime else None,
        status=TradeStatus(status.lower()) if status else None,
        environment=environment,
        limit=limit,
    )
    return {
        "count": len(trades),
        "trades": [t.model_dump(mode="json") for t in trades],
    }


@mcp.tool()
async def performance_report(environment: str | None = None) -> dict[str, Any]:
    """Aggregate performance across closed trades, with per-window/regime breakdowns."""
    closed = _db.query_trades(status=TradeStatus.CLOSED, environment=environment, limit=100000)
    pnls = [t.pnl for t in closed if t.pnl is not None]
    overall = summarize(pnls)

    by_window: dict[str, Any] = {}
    for w in SessionWindow:
        wp = [t.pnl for t in closed if t.window is w and t.pnl is not None]
        if wp:
            by_window[w.value] = performance_metrics(wp).as_dict()

    by_regime: dict[str, Any] = {}
    for r in Regime:
        rp = [t.pnl for t in closed if t.regime is r and t.pnl is not None]
        if rp:
            by_regime[r.value] = performance_metrics(rp).as_dict()

    return {
        "closed_trades": len(pnls),
        "overall": overall,
        "by_window": by_window,
        "by_regime": by_regime,
    }


@mcp.tool()
async def learn_from_history(
    window: str,
    regime: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Recommend whether to trade a given window/regime based on realised history.

    Returns the sample size, metrics, an edge verdict and a plain-English
    recommendation. Below the minimum sample size the verdict is 'insufficient'
    so the client knows the edge is unproven, not absent.
    """
    win = SessionWindow(window.lower())
    reg = Regime(regime.lower()) if regime else None
    closed = _db.query_trades(
        window=win, regime=reg, status=TradeStatus.CLOSED, environment=environment, limit=100000
    )
    pnls = [t.pnl for t in closed if t.pnl is not None]
    metrics = performance_metrics(pnls)

    if metrics.trades < _MIN_SAMPLE:
        verdict = "insufficient"
        recommend = (
            f"Only {metrics.trades} closed trades for {win.value}"
            + (f"/{reg.value}" if reg else "")
            + f"; need >= {_MIN_SAMPLE}. Keep paper trading to build a sample."
        )
    elif metrics.expectancy > 0 and metrics.profit_factor > 1.1:
        verdict = "positive_edge"
        recommend = (
            f"Positive edge: expectancy {metrics.expectancy:.2f}, "
            f"profit factor {metrics.profit_factor:.2f}, win rate {metrics.win_rate:.0%}. "
            "Setup is worth taking within risk limits."
        )
    else:
        verdict = "no_edge"
        recommend = (
            f"No demonstrated edge: expectancy {metrics.expectancy:.2f}, "
            f"profit factor {metrics.profit_factor:.2f}. Avoid or refine parameters."
        )

    return {
        "window": win.value,
        "regime": reg.value if reg else "all",
        "sample_size": metrics.trades,
        "metrics": metrics.as_dict(),
        "verdict": verdict,
        "recommendation": recommend,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
