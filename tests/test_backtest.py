from core.backtest import BacktestParams, grid_search, run_backtest
from core.marketdata import generate_synthetic_bars
from core.models import SessionWindow


def test_backtest_runs_and_is_consistent():
    bars = generate_synthetic_bars(days=30, seed=3, annual_vol=0.35)
    result = run_backtest(bars, SessionWindow.NEW_YORK, BacktestParams())
    # One P&L per recorded trade.
    assert len(result.pnls) == len(result.trades)
    summary = result.summary()
    assert summary["num_trades"] == len(result.pnls)
    assert summary["metrics"]["trades"] == len(result.pnls)
    # Every trade has a defined exit reason.
    for t in result.trades:
        assert t["exit_reason"] in {"take_profit", "stop_loss", "session_end"}


def test_grid_search_sorted_by_score():
    bars = generate_synthetic_bars(days=30, seed=5, annual_vol=0.4)
    grid = {"opening_range_minutes": [15, 30], "target_r": [1.0, 2.0]}
    ranked = grid_search(bars, SessionWindow.NEW_YORK, grid)
    assert len(ranked) == 4
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)
