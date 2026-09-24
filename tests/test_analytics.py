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


def test_daily_revenue_groups_by_utc_day():
    from datetime import datetime

    from core.analytics import daily_revenue

    trades = [
        {"pnl": 10.0, "closed_at": datetime(2026, 9, 1, 10, 0)},
        {"pnl": -4.0, "closed_at": datetime(2026, 9, 1, 15, 0)},
        {"pnl": 7.0, "closed_at": datetime(2026, 9, 2, 12, 0)},
    ]
    out = daily_revenue(trades, today_unrealized=0.0, today=datetime(2026, 9, 2))
    assert len(out["days"]) == 2
    assert out["days"][0]["date"] == "2026-09-01"
    assert out["days"][0]["realised_pnl"] == 6.0
    assert out["days"][0]["trades"] == 2
    assert out["days"][1]["date"] == "2026-09-02"
    assert out["days"][1]["total_pnl"] == 7.0
    assert out["days"][1]["cumulative_pnl"] == 13.0
    assert out["summary"]["winning_days"] == 2
    assert out["summary"]["best_day"]["date"] == "2026-09-02"


def test_daily_revenue_includes_today_open_mark():
    from datetime import datetime

    from core.analytics import daily_revenue

    out = daily_revenue(
        [],
        today_unrealized=3.5,
        today=datetime(2026, 9, 10, 8, 0),
    )
    assert len(out["days"]) == 1
    assert out["days"][0]["date"] == "2026-09-10"
    assert out["days"][0]["open_mark"] == 3.5
    assert out["days"][0]["total_pnl"] == 3.5
    assert out["days"][0]["is_today"] is True
