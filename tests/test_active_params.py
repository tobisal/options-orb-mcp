from core.active_params import resolve_trading_config, strategy_payload
from core.db import Database
from core.models import SessionWindow
from core.sessions import get_window_config


def test_overlay_does_not_mutate_defaults():
    base = get_window_config(SessionWindow.LONDON)
    original = base.opening_range_minutes
    over = base.overlay({"opening_range_minutes": 10, "min_strength": 0.4})
    assert over.opening_range_minutes == 10
    assert over.min_strength == 0.4
    assert base.opening_range_minutes == original
    assert get_window_config(SessionWindow.LONDON).opening_range_minutes == original


def test_overlay_ignores_walk_forward_grid():
    base = get_window_config(SessionWindow.NEW_YORK)
    over = base.overlay({"grid": {"min_strength": [0.1]}, "folds": 3})
    assert over.opening_range_minutes == base.opening_range_minutes
    assert over.min_strength == base.min_strength


def test_saved_optimiser_is_not_used_until_selected(tmp_path):
    db = Database(path=tmp_path / "t.db")
    win = SessionWindow.LONDON
    db.insert_backtest(
        label="optimise SPY london (best of 80)",
        symbol="SPY",
        window=win,
        params={"opening_range_minutes": 15, "breakout_buffer_atr": 0.05, "min_strength": 0.10},
        metrics={"trades": 12, "expectancy": 1.2},
    )
    cfg, found = resolve_trading_config("SPY", win, db)
    assert found is None
    assert cfg.opening_range_minutes == get_window_config(win).opening_range_minutes
    assert strategy_payload(cfg, found)["source"] == "defaults"


def test_selected_params_overlay_defaults(tmp_path):
    db = Database(path=tmp_path / "t.db")
    win = SessionWindow.NEW_YORK
    run_id = db.insert_backtest(
        label="optimise SPY new_york (best of 80)",
        symbol="SPY",
        window=win,
        params={"opening_range_minutes": 45, "breakout_buffer_atr": 0.02, "min_strength": 0.15, "target_r": 1.5},
        metrics={"trades": 20},
    )
    db.set_active_strategy(
        "SPY",
        win,
        params={"opening_range_minutes": 45, "breakout_buffer_atr": 0.02, "min_strength": 0.15, "target_r": 1.5},
        backtest_id=run_id,
        label="optimise SPY new_york (best of 80)",
    )
    cfg, found = resolve_trading_config("SPY", win, db)
    assert found is not None
    assert cfg.opening_range_minutes == 45
    assert cfg.breakout_buffer_atr == 0.02
    assert cfg.min_strength == 0.15
    assert strategy_payload(cfg, found)["source"] == "selected"


def test_clear_active_strategy_restores_defaults(tmp_path):
    db = Database(path=tmp_path / "t.db")
    win = SessionWindow.ASIA
    db.set_active_strategy(
        "SPY",
        win,
        params={"opening_range_minutes": 20, "min_strength": 0.40, "target_r": 2.0},
        label="manual",
    )
    assert resolve_trading_config("SPY", win, db)[0].opening_range_minutes == 20
    db.clear_active_strategy("SPY", win)
    cfg, found = resolve_trading_config("SPY", win, db)
    assert found is None
    assert cfg.opening_range_minutes == get_window_config(win).opening_range_minutes


def test_walk_forward_cannot_be_selected(tmp_path):
    db = Database(path=tmp_path / "t.db")
    win = SessionWindow.NEW_YORK
    run_id = db.insert_backtest(
        label="walk_forward SPY new_york",
        symbol="SPY",
        window=win,
        params={"grid": {"opening_range_minutes": [15, 30]}, "folds": 3},
        metrics={"trades": 9},
        is_out_of_sample=True,
    )
    try:
        db.set_active_strategy("SPY", win, params={"grid": {}, "folds": 3}, backtest_id=run_id)
        raise AssertionError("walk-forward grid should be rejected")
    except ValueError:
        pass
