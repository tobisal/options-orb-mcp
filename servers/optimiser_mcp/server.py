"""optimiser-mcp: the optimising agent.

Backtests the ORB spread strategy, runs a grid search + walk-forward validation,
and compares a candidate parameter set against an incumbent using
production-standard metrics that balance win probability against reward:risk.
Results are persisted so the research agent and operator can review them.
"""

from __future__ import annotations

from typing import Any

from core.backtest import (
    DEFAULT_ORB_GRID,
    BacktestParams,
    dedupe_by_outcome,
    grid_search,
    run_backtest,
    walk_forward,
)
from core.backtest import _score as score_metrics
from core.db import Database
from core.ibkr_client import IBKRUnavailable
from core.marketdata import fetch_bars_with_fallback
from core.models import SessionWindow
from servers._compat import create_server

mcp = create_server(
    "optimiser-mcp",
    instructions=(
        "Optimising agent for the Options ORB system. backtest evaluates a single "
        "parameter set; walk_forward searches a grid in-sample and validates "
        "out-of-sample (the honest estimate); compare ranks a candidate vs an "
        "incumbent on expectancy, reward:risk, profit factor, Sharpe and drawdown. "
        "Use use_synthetic=true / longer synthetic_days to run offline."
    ),
)

_db = Database()


def _resolve_window(window: str) -> SessionWindow:
    if window.lower() in ("auto", "active", "current"):
        return SessionWindow.NEW_YORK
    return SessionWindow(window.lower())


async def _load_bars(
    symbol: str, use_synthetic: bool, synthetic_days: int, synthetic_seed: int, duration: str
):
    return await fetch_bars_with_fallback(
        symbol,
        duration=duration,
        bar_size="5 mins",
        use_synthetic=use_synthetic,
        allow_synthetic_fallback=use_synthetic,
        synthetic_days=synthetic_days,
        synthetic_seed=synthetic_seed,
    )


@mcp.tool()
async def backtest(
    symbol: str = "SPY",
    window: str = "new_york",
    opening_range_minutes: int = 30,
    breakout_buffer_atr: float = 0.10,
    min_strength: float = 0.25,
    target_r: float = 1.5,
    stop_r: float = 1.0,
    iv: float = 0.25,
    dte: int = 7,
    cost_per_trade: float = 0.0,
    use_synthetic: bool = False,
    synthetic_days: int = 60,
    synthetic_seed: int = 3,
    persist: bool = False,
) -> dict[str, Any]:
    """Backtest one ORB spread parameter set over historical (or synthetic) data.

    Returns full performance metrics (win rate, expectancy, reward:risk, profit
    factor, Sharpe/Sortino, drawdown, Monte-Carlo) plus the individual trades.
    """
    win = _resolve_window(window)
    try:
        bars, source, warning = await _load_bars(
            symbol, use_synthetic, synthetic_days, synthetic_seed, "30 D"
        )
    except IBKRUnavailable as exc:
        return {"error": str(exc), "hint": "Pass use_synthetic=true for offline data."}

    params = BacktestParams(
        opening_range_minutes=opening_range_minutes,
        breakout_buffer_atr=breakout_buffer_atr,
        min_strength=min_strength,
        target_r=target_r,
        stop_r=stop_r,
        iv=iv,
        dte=dte,
        cost_per_trade=cost_per_trade,
    )
    result = run_backtest(bars, win, params).summary()
    result["data_source"] = source
    result["score"] = round(score_metrics(result), 4)
    if warning:
        result["warning"] = warning
    if persist:
        result["backtest_id"] = _db.insert_backtest(
            label=f"backtest {symbol} {win.value}",
            symbol=symbol,
            window=win,
            params=params.as_dict(),
            metrics=result["metrics"],
        )
    return result


@mcp.tool()
async def optimise(
    symbol: str = "SPY",
    window: str = "new_york",
    use_synthetic: bool = False,
    synthetic_days: int = 60,
    synthetic_seed: int = 3,
    top_n: int = 5,
) -> dict[str, Any]:
    """Grid-search a sensible ORB parameter space and return the top-N by score.

    The score balances expectancy, win rate, profit factor and drawdown, so it
    rewards strategies that win often *and* have healthy reward:risk.
    """
    win = _resolve_window(window)
    try:
        bars, source, warning = await _load_bars(
            symbol, use_synthetic, synthetic_days, synthetic_seed, "30 D"
        )
    except IBKRUnavailable as exc:
        return {"error": str(exc), "hint": "Pass use_synthetic=true for offline data."}

    ranked = grid_search(bars, win, DEFAULT_ORB_GRID)
    distinct = dedupe_by_outcome(ranked)
    top = [
        {"params": r["params"], "score": r["score"], "metrics": r["metrics"]}
        for r in distinct[:top_n]
    ]
    persisted_id = None
    best = distinct[0] if distinct else None
    if best is not None and best["score"] > -1e8:
        persisted_id = _db.insert_backtest(
            label=f"optimise {symbol} {win.value} (best of {len(ranked)})",
            symbol=symbol,
            window=win,
            params=best["params"],
            metrics=best["metrics"],
        )
    return {
        "window": win.value,
        "data_source": source,
        "warning": warning,
        "combinations_tested": len(ranked),
        "distinct_outcomes": len(distinct),
        "top": top,
        "persisted_id": persisted_id,
    }


@mcp.tool()
async def walk_forward_test(
    symbol: str = "SPY",
    window: str = "new_york",
    folds: int = 3,
    use_synthetic: bool = False,
    synthetic_days: int = 90,
    synthetic_seed: int = 3,
    persist: bool = False,
) -> dict[str, Any]:
    """Walk-forward optimisation: fit params in-sample, validate out-of-sample.

    The aggregate out-of-sample metrics are the honest, overfitting-resistant
    estimate of live-like performance.
    """
    win = _resolve_window(window)
    try:
        bars, source, warning = await _load_bars(
            symbol, use_synthetic, synthetic_days, synthetic_seed, "90 D"
        )
    except IBKRUnavailable as exc:
        return {"error": str(exc), "hint": "Pass use_synthetic=true for offline data."}

    report = walk_forward(bars, win, DEFAULT_ORB_GRID, folds=folds)
    report["data_source"] = source
    if warning:
        report["warning"] = warning
    if persist and "error" not in report:
        _db.insert_backtest(
            label=f"walk_forward {symbol} {win.value}",
            symbol=symbol,
            window=win,
            params={"grid": DEFAULT_ORB_GRID, "folds": folds},
            metrics=report.get("aggregate_out_of_sample", {}),
            is_out_of_sample=True,
        )
    return report


@mcp.tool()
async def compare(
    symbol: str = "SPY",
    window: str = "new_york",
    candidate: dict[str, Any] | None = None,
    incumbent: dict[str, Any] | None = None,
    use_synthetic: bool = False,
    synthetic_days: int = 90,
    synthetic_seed: int = 3,
) -> dict[str, Any]:
    """Compare a candidate parameter set against an incumbent, production-style.

    Both are backtested on the same data. The candidate is promoted only if it
    beats the incumbent on the balanced score AND does not materially worsen the
    worst-case (Monte-Carlo p05 / max drawdown) - a promotion gate that resists
    cherry-picking a lucky average.
    """
    win = _resolve_window(window)
    try:
        bars, source, warning = await _load_bars(
            symbol, use_synthetic, synthetic_days, synthetic_seed, "90 D"
        )
    except IBKRUnavailable as exc:
        return {"error": str(exc), "hint": "Pass use_synthetic=true for offline data."}

    cand_params = BacktestParams(**{**BacktestParams().as_dict(), **(candidate or {})})
    inc_params = BacktestParams(**{**BacktestParams().as_dict(), **(incumbent or {})})

    cand = run_backtest(bars, win, cand_params).summary()
    inc = run_backtest(bars, win, inc_params).summary()
    cand_score = score_metrics(cand)
    inc_score = score_metrics(inc)

    cand_m = cand["metrics"]
    inc_m = inc["metrics"]
    cand_dd = cand_m.get("max_drawdown", 0.0)
    inc_dd = inc_m.get("max_drawdown", 0.0)
    cand_p05 = cand_m.get("monte_carlo", {}).get("final_pnl_p05", 0.0)
    inc_p05 = inc_m.get("monte_carlo", {}).get("final_pnl_p05", 0.0)

    score_better = cand_score > inc_score
    not_worse_tail = cand_p05 >= inc_p05 - abs(inc_p05) * 0.25
    not_worse_dd = cand_dd <= inc_dd * 1.25 if inc_dd > 0 else True
    enough_trades = cand_m.get("trades", 0) >= 5

    promote = score_better and not_worse_tail and not_worse_dd and enough_trades
    reasons = []
    reasons.append(f"score {cand_score:.3f} vs {inc_score:.3f} -> {'better' if score_better else 'not better'}")
    reasons.append(f"tail p05 {cand_p05:.2f} vs {inc_p05:.2f} -> {'ok' if not_worse_tail else 'worse'}")
    reasons.append(f"maxDD {cand_dd:.2f} vs {inc_dd:.2f} -> {'ok' if not_worse_dd else 'worse'}")
    if not enough_trades:
        reasons.append("candidate has too few trades to trust")

    return {
        "window": win.value,
        "data_source": source,
        "warning": warning,
        "candidate": {"params": cand_params.as_dict(), "score": round(cand_score, 4), "metrics": cand_m},
        "incumbent": {"params": inc_params.as_dict(), "score": round(inc_score, 4), "metrics": inc_m},
        "promote_candidate": promote,
        "rationale": reasons,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
