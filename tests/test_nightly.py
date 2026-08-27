from core.active_params import resolve_trading_config
from core.db import Database
from core.marketdata import generate_synthetic_bars
from core.models import SessionWindow
from core.nightly import run_nightly_optimise, select_best_for_window

_TINY_GRID = {
    "opening_range_minutes": [15, 30],
    "breakout_buffer_atr": [0.05],
    "min_strength": [0.15],
}


def test_select_best_applies_tradable_set(tmp_path):
    db = Database(path=tmp_path / "n.db")
    bars = generate_synthetic_bars(days=40, seed=7, annual_vol=0.4)
    row = select_best_for_window(
        bars,
        SessionWindow.NEW_YORK,
        db=db,
        symbol="SPY",
        apply=True,
        min_trades=1,
        grid=_TINY_GRID,
    )
    assert row["best"] is not None
    assert row["applied"] is True
    cfg, found = resolve_trading_config("SPY", SessionWindow.NEW_YORK, db)
    assert found is not None
    assert cfg.opening_range_minutes == row["best"]["params"]["opening_range_minutes"]
    assert "nightly" in (found.get("label") or "")


def test_dry_run_does_not_change_active(tmp_path):
    db = Database(path=tmp_path / "n.db")
    bars = generate_synthetic_bars(days=40, seed=7, annual_vol=0.4)
    select_best_for_window(
        bars,
        SessionWindow.LONDON,
        db=db,
        symbol="SPY",
        apply=False,
        min_trades=1,
        grid=_TINY_GRID,
    )
    _, found = resolve_trading_config("SPY", SessionWindow.LONDON, db)
    assert found is None


def test_skips_when_too_few_trades(tmp_path):
    db = Database(path=tmp_path / "n.db")
    bars = generate_synthetic_bars(days=40, seed=7, annual_vol=0.4)
    row = select_best_for_window(
        bars,
        SessionWindow.ASIA,
        db=db,
        symbol="SPY",
        apply=True,
        min_trades=10_000,
        grid=_TINY_GRID,
    )
    assert row["applied"] is False
    _, found = resolve_trading_config("SPY", SessionWindow.ASIA, db)
    assert found is None


async def test_run_nightly_with_injected_bars(tmp_path):
    db = Database(path=tmp_path / "n.db")
    bars = generate_synthetic_bars(days=40, seed=3, annual_vol=0.35)
    report = await run_nightly_optimise(
        symbol="SPY",
        lookback_days=40,
        apply=True,
        min_trades=1,
        db=db,
        bars=bars,
        grid=_TINY_GRID,
    )
    assert report["ok"] is True
    assert len(report["windows"]) == 3
    assert {w["window"] for w in report["windows"]} == {"asia", "london", "new_york"}
