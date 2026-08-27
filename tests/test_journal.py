import json
from datetime import datetime

from core.db import Database
from core.journal import close_open_paper_trades, evaluate_exit, mark_open_trade
from core.models import (
    Bar,
    Direction,
    Regime,
    SessionWindow,
    SpreadType,
    TradeRecord,
    TradeStatus,
)


def _trade(db: Database, **kw) -> TradeRecord:
    plan = {
        "expiry": "20260821",
        "iv": 0.2,
        "take_profit_price": 2.0,
        "stop_loss_price": 0.4,
        "long_leg": {"strike": 775.0, "right": "C", "expiry": "20260821"},
        "short_leg": {"strike": 776.0, "right": "C", "expiry": "20260821"},
    }
    rec = TradeRecord(
        environment=kw.get("environment", "PAPER(sim)"),
        symbol="SPY",
        window=SessionWindow.NEW_YORK,
        regime=Regime.RANGE,
        spread_type=SpreadType.BULL_CALL,
        direction=Direction.LONG,
        contracts=1,
        entry_price=1.0,
        max_loss=40.0,
        max_profit=60.0,
        target_r=1.5,
        status=TradeStatus.OPEN,
        order_ref=kw.get("order_ref"),
        plan_json=json.dumps(plan),
    )
    rec.id = db.insert_trade(rec)
    return rec


def test_query_trades_treats_sim_as_paper(tmp_path):
    db = Database(path=tmp_path / "j.db")
    _trade(db, environment="PAPER(sim)")
    found = db.query_trades(environment="PAPER")
    assert len(found) == 1
    assert found[0].environment == "PAPER(sim)"


def test_session_end_closes_when_window_not_live(tmp_path):
    db = Database(path=tmp_path / "j.db")
    _trade(db)
    bars = [
        Bar(ts=datetime(2026, 8, 17, 14, 0), open=776, high=776, low=775, close=775.5)
    ]
    closed = close_open_paper_trades(db, "SPY", bars, now_window=SessionWindow.LONDON)
    assert len(closed) == 1
    assert closed[0]["reason"] == "session_end"
    row = db.query_trades(status=TradeStatus.CLOSED)
    assert len(row) == 1
    assert row[0].pnl is not None


def test_live_window_does_not_force_session_end():
    rec = TradeRecord(
        environment="PAPER",
        symbol="SPY",
        window=SessionWindow.NEW_YORK,
        regime=Regime.RANGE,
        spread_type=SpreadType.BULL_CALL,
        direction=Direction.LONG,
        contracts=1,
        entry_price=1.0,
        max_loss=40.0,
        max_profit=60.0,
        target_r=1.5,
        plan_json=json.dumps(
            {
                "expiry": "20260821",
                "iv": 0.2,
                "take_profit_price": 50.0,
                "stop_loss_price": -50.0,
                "long_leg": {"strike": 775.0, "right": "C"},
                "short_leg": {"strike": 776.0, "right": "C"},
            }
        ),
    )
    bars = [
        Bar(ts=datetime(2026, 8, 17, 14, 0), open=776, high=776, low=775, close=775.5)
    ]
    assert evaluate_exit(rec, bars, window_live=True) is None


def test_mark_open_trade_moves_with_spot():
    rec = TradeRecord(
        environment="PAPER",
        symbol="SPY",
        window=SessionWindow.NEW_YORK,
        regime=Regime.TREND,
        spread_type=SpreadType.BULL_CALL,
        direction=Direction.LONG,
        contracts=1,
        entry_price=0.40,
        max_loss=40.0,
        max_profit=60.0,
        target_r=1.5,
        plan_json=json.dumps(
            {
                "expiry": "20260821",
                "iv": 0.2,
                "take_profit_price": 0.80,
                "stop_loss_price": 0.10,
                "long_leg": {"strike": 775.0, "right": "C"},
                "short_leg": {"strike": 776.0, "right": "C"},
            }
        ),
    )
    low = mark_open_trade(rec, 770.0)
    high = mark_open_trade(rec, 780.0)
    assert high["unrealized_pnl"] > low["unrealized_pnl"]
    assert 0.0 <= (high["progress_to_tp"] or 0) <= 1.0
    assert high["mark"] != rec.entry_price


def test_paper_account_includes_open_mark(tmp_path):
    from core.journal import paper_account_snapshot

    db = Database(path=tmp_path / "j.db")
    rec = _trade(db)
    db.close_trade(rec.id or 0, exit_price=1.2, pnl=20.0)
    open_rec = _trade(db)
    snap = paper_account_snapshot(
        db, {"SPY": 780.0}, starting_capital=1000.0, environment="PAPER"
    )
    marked = mark_open_trade(open_rec, 780.0)
    assert snap["lifetime_realised_pnl"] == 20.0
    assert snap["open_unrealized_pnl"] == marked["unrealized_pnl"]
    assert snap["paper_equity"] == round(1000.0 + 20.0 + marked["unrealized_pnl"], 2)


def test_ibkr_backed_trade_not_closed_by_journal_helper(tmp_path):
    db = Database(path=tmp_path / "j.db")
    rec = _trade(db, environment="LIVE", order_ref="ORB-SPY-20260820120000")
    bars = [
        Bar(ts=datetime(2026, 8, 17, 14, 0), open=776, high=776, low=775, close=775.5)
    ]
    closed = close_open_paper_trades(db, "SPY", bars, now_window=SessionWindow.LONDON)
    assert closed == []
    assert db.get_trade(rec.id).status is TradeStatus.OPEN


async def test_settle_flattens_ibkr_then_journals(tmp_path):
    from core.engine import settle_session_exits

    db = Database(path=tmp_path / "j.db")
    rec = _trade(db, environment="LIVE", order_ref="ORB-SPY-20260820120000")
    bars = [
        Bar(ts=datetime(2026, 8, 17, 14, 0), open=776, high=776, low=775, close=775.5)
    ]

    class _FakeIB:
        def __init__(self) -> None:
            self.calls = 0

        async def close_spread_order(self, *args, **kwargs):
            self.calls += 1
            return {"ok": True, "status": "Submitted", "already_flat": False}

    fake = _FakeIB()
    closed = await settle_session_exits(
        db, "SPY", bars, now_window=SessionWindow.LONDON, ib=fake
    )
    assert fake.calls == 1
    assert len(closed) == 1
    assert closed[0]["reason"] == "session_end"
    assert closed[0]["ibkr"] is True
    assert db.get_trade(rec.id).status is TradeStatus.CLOSED


async def test_settle_leaves_ibkr_open_if_flatten_fails(tmp_path):
    from core.engine import settle_session_exits

    db = Database(path=tmp_path / "j.db")
    rec = _trade(db, environment="LIVE", order_ref="ORB-SPY-20260820120000")
    bars = [
        Bar(ts=datetime(2026, 8, 17, 14, 0), open=776, high=776, low=775, close=775.5)
    ]

    class _FakeIB:
        async def close_spread_order(self, *args, **kwargs):
            return {"ok": False, "error": "market closed"}

    closed = await settle_session_exits(
        db, "SPY", bars, now_window=SessionWindow.LONDON, ib=_FakeIB()
    )
    assert closed == []
    assert db.get_trade(rec.id).status is TradeStatus.OPEN
