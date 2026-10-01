"""Regression: full take-profit must never wipe to breakeven (2026-09-28 bug)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from core.db import Database
from core.models import (
    Bar,
    Direction,
    Regime,
    SessionWindow,
    SpreadType,
    TradeRecord,
    TradeStatus,
)


def _mes_short_trade(db: Database, *, plan_extra: dict | None = None) -> TradeRecord:
    """Reproduce today's London short: 100% scale, OR stop, LOD/2R target."""
    plan = {
        "symbol": "MES",
        "session_name": "london",
        "direction": "short",
        "entry_price": 7769.75,
        "stop_price": 7776.75,
        "contracts": 1,
        "break_level": 7770.25,
        "or_high": 7776.25,
        "or_low": 7770.25,
        "point_value": 5.0,
        "tick_size": 0.25,
        "target_price": 7755.75,  # true 2R
        "target_label": "2R",
        "target_r": 2.5,
        "scale_fraction": 1.0,
        "use_trailing_stop": True,
        "trail_pivot_lag": 2,
        "trail_buffer_ticks": 1,
        "stop_mode": "or_extreme",
        "entry_model": "mes_5orb",
        "instrument": "future",
        "trail_active": False,
        "scaled_out": False,
        "original_stop_loss_price": 7776.75,
        "stop_loss_price": 7776.75,
    }
    if plan_extra:
        plan.update(plan_extra)
    rec = TradeRecord(
        environment="PAPER(sim)",
        symbol="MES",
        window=SessionWindow.LONDON,
        regime=Regime.TREND,
        spread_type=SpreadType.BEAR_PUT,
        direction=Direction.SHORT,
        contracts=1,
        entry_price=7769.75,
        max_loss=35.0,
        max_profit=70.0,
        target_r=2.5,
        status=TradeStatus.OPEN,
        order_ref="SIM-MES-REGRESSION",
        notes="short retest confirmed",
        plan_json=json.dumps(plan),
    )
    rec.id = db.insert_trade(rec)
    return rec


@pytest.mark.asyncio
async def test_full_target_closes_at_target_not_breakeven(tmp_path):
    from core.engine import settle_session_exits

    db = Database(path=tmp_path / "mes_settle.db")
    rec = _mes_short_trade(db)
    # 04:30 America/New_York → 08:30 UTC (inside London OR window)
    bars = [
        Bar(
            ts=datetime(2026, 9, 28, 8, 30),
            open=7765.0,
            high=7766.0,
            low=7755.0,
            close=7756.0,
        )
    ]
    closed = await settle_session_exits(
        db, "MES", bars, now_window=SessionWindow.LONDON, ib=None
    )
    assert len(closed) == 1
    assert closed[0]["reason"] == "target_2R"
    assert closed[0]["exit_price"] == 7755.75
    # Short 14 pts × $5 = $70
    assert abs(float(closed[0]["pnl"]) - 70.0) < 1e-6
    assert db.get_trade(rec.id).status is TradeStatus.CLOSED
    assert "breakeven" not in str(closed[0]["reason"]).lower()


@pytest.mark.asyncio
async def test_buggy_be_state_recovers_to_target_fill(tmp_path):
    """Exact 2026-09-28 failure mode: scaled_out + BE stop still OPEN → close at target."""
    from core.engine import settle_session_exits

    db = Database(path=tmp_path / "mes_recover.db")
    rec = _mes_short_trade(
        db,
        plan_extra={
            "target_price": 7768.0,
            "target_label": "LOD",
            "scaled_out": True,
            "scale_fill_price": 7768.0,
            "pending_full_target": True,
            # Bug left the stop at entry (breakeven)
            "stop_loss_price": 7769.75,
            "trail_active": True,
        },
    )
    bars = [
        Bar(
            ts=datetime(2026, 9, 28, 8, 35),
            open=7769.75,
            high=7770.0,
            low=7769.0,
            close=7769.75,
        )
    ]
    closed = await settle_session_exits(
        db, "MES", bars, now_window=SessionWindow.LONDON, ib=None
    )
    assert len(closed) == 1
    assert closed[0]["reason"] == "target_LOD"
    assert closed[0]["exit_price"] == 7768.0
    assert abs(float(closed[0]["pnl"]) - 8.75) < 1e-6  # 1.75 pts × $5
    assert db.get_trade(rec.id).status is TradeStatus.CLOSED


@pytest.mark.asyncio
async def test_profit_lock_syncs_ibkr_stop_without_trail(tmp_path):
    """Profit-lock must push broker stop even when trail_active is False."""
    from core.engine import settle_session_exits

    db = Database(path=tmp_path / "mes_lock_ibkr.db")
    entry, target = 7769.75, 7755.75
    span = entry - target  # 14
    # 95% arm → 85% lock
    lock_px = entry - 0.85 * span
    rec = _mes_short_trade(
        db,
        plan_extra={
            "use_trailing_stop": False,
            "trail_active": False,
            "scale_fraction": 0.5,  # stay in manage path (not full TP)
            "profit_lock_tiers": [
                {"arm": 0.75, "lock": 0.35},
                {"arm": 0.80, "lock": 0.50},
                {"arm": 0.90, "lock": 0.75},
                {"arm": 0.95, "lock": 0.85},
                {"arm": 0.975, "lock": 0.90},
            ],
        },
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE trades SET order_ref=? WHERE id=?",
            ("MES-20261001071000", rec.id),
        )

    calls: list[dict] = []

    class _IB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def close_future_position(self, *args, **kwargs):
            return {"ok": True}

        async def modify_future_stop(self, *args, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "stop_price": kwargs.get("stop_price")}

    # Wick to 95% arm; high stays below the 85% lock so we don't stop out.
    arm_95 = entry - 0.95 * span
    bars = [
        Bar(
            ts=datetime(2026, 9, 28, 8, 40),
            open=arm_95 + 0.5,
            high=lock_px - 0.25,  # below Lock SL 85%
            low=arm_95,
            close=lock_px - 0.5,
        )
    ]
    closed = await settle_session_exits(
        db, "MES", bars, now_window=SessionWindow.LONDON, ib=_IB()
    )
    assert closed == []
    still = db.get_trade(rec.id)
    assert still.status is TradeStatus.OPEN
    plan = json.loads(still.plan_json)
    assert plan.get("profit_lock_armed") is True
    assert abs(float(plan["stop_loss_price"]) - lock_px) < 1e-6
    assert abs(float(plan.get("broker_stop_price")) - lock_px) < 1e-6
    assert len(calls) == 1
    assert abs(float(calls[0]["stop_price"]) - lock_px) < 1e-6


@pytest.mark.asyncio
async def test_ibkr_close_fail_does_not_be_exit_same_cycle(tmp_path):
    from core.engine import settle_session_exits

    db = Database(path=tmp_path / "mes_ibkr.db")
    rec = _mes_short_trade(db)
    with db._conn() as conn:
        conn.execute(
            "UPDATE trades SET order_ref=? WHERE id=?",
            ("MES-20260928072539", rec.id),
        )

    class _FailIB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def close_future_position(self, *args, **kwargs):
            return {"ok": False, "error": "timeout"}

        async def modify_future_stop(self, *args, **kwargs):
            return {"ok": True}

    bars = [
        Bar(
            ts=datetime(2026, 9, 28, 8, 30),
            open=7765.0,
            high=7766.0,
            low=7755.0,
            close=7756.0,
        )
    ]
    closed = await settle_session_exits(
        db, "MES", bars, now_window=SessionWindow.LONDON, ib=_FailIB()
    )
    assert closed == []
    still = db.get_trade(rec.id)
    assert still.status is TradeStatus.OPEN
    plan = json.loads(still.plan_json)
    assert plan.get("pending_full_target") is True
    assert plan.get("scale_fill_price") == 7755.75
    # Must NOT have moved stop to breakeven
    assert float(plan["stop_loss_price"]) == 7776.75
