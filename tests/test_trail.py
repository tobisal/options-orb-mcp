"""Tests for trailing-stop math and ops log."""

from __future__ import annotations

import json

import pytest

from core.ops_log import clear, emit, recent
from core.trail import (
    normalize_trail_params,
    open_risk_per_contract_usd,
    plan_uses_trailing,
    update_trailing_stop,
)


@pytest.fixture(autouse=True)
def _clear_ops():
    clear()
    yield
    clear()


def test_normalize_invalid_forces_off():
    on, a, d = normalize_trail_params(True, 0.5, 0.8)
    assert on is False
    on2, _, _ = normalize_trail_params(True, 0.5, 0.3)
    assert on2 is True


def test_update_trailing_noop_when_off(monkeypatch):
    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: True)
    plan = {
        "use_trailing_stop": False,
        "stop_loss_price": 1.0,
        "take_profit_price": 2.0,
        "original_stop_loss_price": 1.0,
    }
    out, changed, activated = update_trailing_stop(plan, 1.8, entry=1.5)
    assert changed is False
    assert activated is False
    assert out["take_profit_price"] == 2.0


def test_update_trailing_activates_and_drops_tp(monkeypatch):
    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: True)
    # entry 1.5, orig sl 1.0 → risk_share 0.5; activate at 1.5+0.25=1.75
    plan = {
        "use_trailing_stop": True,
        "trail_activate_r": 0.5,
        "trail_distance_r": 0.3,
        "stop_loss_price": 1.0,
        "take_profit_price": 2.5,
        "original_stop_loss_price": 1.0,
    }
    out, changed, activated = update_trailing_stop(plan, 1.8, entry=1.5)
    assert activated is True
    assert changed is True
    assert out["trail_active"] is True
    assert out["take_profit_price"] is None
    # peak 1.8, trail = 1.8 - 0.15 = 1.65, floor entry 1.5 → 1.65
    assert out["stop_loss_price"] == 1.65


def test_update_trailing_never_below_entry(monkeypatch):
    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: True)
    plan = {
        "use_trailing_stop": True,
        "trail_activate_r": 0.5,
        "trail_distance_r": 0.3,
        "stop_loss_price": 1.0,
        "original_stop_loss_price": 1.0,
        "trail_active": True,
        "peak_mark": 1.55,
    }
    out, _, _ = update_trailing_stop(plan, 1.55, entry=1.5)
    assert out["stop_loss_price"] >= 1.5


def test_update_trailing_only_raises_sl(monkeypatch):
    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: True)
    plan = {
        "use_trailing_stop": True,
        "trail_activate_r": 0.5,
        "trail_distance_r": 0.3,
        "stop_loss_price": 1.7,
        "original_stop_loss_price": 1.0,
        "trail_active": True,
        "peak_mark": 2.0,
    }
    out, changed, _ = update_trailing_stop(plan, 1.6, entry=1.5)
    # peak stays 2.0, trailed 2.0-0.15=1.85 > 1.7 → raise
    assert out["stop_loss_price"] == 1.85
    assert changed is True
    out2, changed2, _ = update_trailing_stop(out, 1.5, entry=1.5)
    assert out2["stop_loss_price"] == 1.85
    assert changed2 is False or out2["peak_mark"] == 2.0


def test_open_risk_at_breakeven(monkeypatch):
    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: True)
    plan = {"stop_loss_price": 1.5, "original_stop_loss_price": 1.0}
    assert open_risk_per_contract_usd(plan, 1.5) == 0.0


def test_plan_uses_trailing_respects_kill_switch(monkeypatch):
    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: False)
    assert plan_uses_trailing({"use_trailing_stop": True}) is False


def test_ops_log_ring():
    emit("hello", level="trade", source="trail")
    rows = recent(limit=10)
    assert rows[-1]["msg"] == "hello"
    assert rows[-1]["level"] == "trade"


def test_evaluate_exit_trailing(monkeypatch, tmp_path):
    from core.db import Database
    from core.journal import evaluate_exit
    from core.models import (
        Bar,
        Direction,
        Regime,
        SessionWindow,
        SpreadType,
        TradeRecord,
        TradeStatus,
    )
    from core.timeutils import utcnow

    monkeypatch.setattr("core.trail.trailing_globally_enabled", lambda: True)
    plan = {
        "use_trailing_stop": True,
        "trail_active": True,
        "take_profit_price": None,
        "stop_loss_price": 1.6,
        "original_stop_loss_price": 1.0,
        "long_leg": {"right": "C", "strike": 100, "expiry": "20990101"},
        "short_leg": {"right": "C", "strike": 101, "expiry": "20990101"},
        "iv": 0.2,
    }
    trade = TradeRecord(
        environment="PAPER",
        symbol="SPY",
        window=SessionWindow.NEW_YORK,
        regime=Regime.TREND,
        spread_type=SpreadType.BULL_CALL,
        direction=Direction.LONG,
        contracts=1,
        entry_price=1.5,
        max_loss=50,
        max_profit=50,
        target_r=1.5,
        status=TradeStatus.OPEN,
        signal_strength=0.5,
        plan_json=json.dumps(plan),
    )
    # Force structure value via monkeypatch
    monkeypatch.setattr(
        "core.journal._structure_value", lambda trade, plan, spot: spot
    )
    bars = [Bar(ts=utcnow(), open=1.7, high=1.7, low=1.7, close=1.7, volume=1)]
    assert evaluate_exit(trade, bars, window_live=True, plan=plan) is None
    bars3 = [Bar(ts=utcnow(), open=1.5, high=1.5, low=1.5, close=1.5, volume=1)]
    decision = evaluate_exit(trade, bars3, window_live=True, plan=plan)
    assert decision is not None
    assert decision[1] == "trailing_stop"
