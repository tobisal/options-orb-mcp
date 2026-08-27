from core.analytics import monte_carlo, performance_metrics


def test_performance_metrics_known_values():
    pnls = [10.0, -5.0, 10.0, -5.0]
    m = performance_metrics(pnls)
    assert m.trades == 4
    assert m.wins == 2
    assert m.losses == 2
    assert m.win_rate == 0.5
    assert m.avg_win == 10.0
    assert m.avg_loss == 5.0
    assert m.reward_risk == 2.0
    assert m.expectancy == 2.5
    assert m.profit_factor == 2.0
    assert m.total_pnl == 10.0


def test_max_drawdown():
    # Equity path: 10, 5, 15, 5 -> peak 15 then down to 5 => drawdown 10.
    pnls = [10.0, -5.0, 10.0, -10.0]
    m = performance_metrics(pnls)
    assert m.max_drawdown == 10.0


def test_monte_carlo_bounds():
    pnls = [5.0, -3.0, 4.0, -2.0, 6.0]
    mc = monte_carlo(pnls, iterations=500, seed=1)
    assert mc["iterations"] == 500
    assert 0.0 <= mc["prob_profit"] <= 1.0
    assert mc["final_pnl_p05"] <= mc["final_pnl_p50"] <= mc["final_pnl_p95"]


def test_empty_metrics():
    m = performance_metrics([])
    assert m.trades == 0
    assert m.expectancy == 0
