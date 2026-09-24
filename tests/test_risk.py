from core.config import AccountMode, Settings
from core.db import Database
from core.models import Direction, Regime, SessionWindow, SpreadType, TradeRecord, TradeStatus
from core.risk import RiskManager
import pytest


def _settings(capital=1000.0, risk=0.05) -> Settings:
    s = Settings()
    s.starting_capital = capital
    s.max_risk_per_trade = risk
    s.max_daily_loss = 0.10
    s.max_open_positions = 9
    s.max_open_positions_per_window = 3
    s.account_mode = AccountMode.PAPER
    s.live_trading_confirm = ""
    return s


def _rm(tmp_path, **kw) -> RiskManager:
    db = Database(path=tmp_path / "risk.db")
    return RiskManager(settings=_settings(**kw), db=db, acct_ccy_per_usd=1.0)


def _open_trade(db: Database, window: SessionWindow, n: int = 1) -> None:
    for _ in range(n):
        db.insert_trade(
            TradeRecord(
                environment="PAPER",
                symbol="SPY",
                window=window,
                regime=Regime.TREND,
                spread_type=SpreadType.BULL_CALL,
                direction=Direction.LONG,
                contracts=1,
                entry_price=1.0,
                max_loss=40.0,
                max_profit=60.0,
                target_r=1.5,
                status=TradeStatus.OPEN,
            )
        )


def test_size_position_basic(tmp_path):
    rm = _rm(tmp_path)  # budget = 1000 * 0.05 = 50
    assert rm.size_position(25.0) == 2
    assert rm.size_position(50.0) == 1
    assert rm.size_position(60.0) == 0  # one contract exceeds budget


def test_pre_trade_approves_within_budget(tmp_path):
    rm = _rm(tmp_path)
    decision = rm.pre_trade_checks(40.0)
    assert decision.approved is True
    assert decision.contracts == 1


def test_pre_trade_rejects_when_too_expensive(tmp_path):
    rm = _rm(tmp_path)
    decision = rm.pre_trade_checks(200.0)
    assert decision.approved is False
    assert decision.contracts == 0
    assert any("exceeding the per-trade cap" in r for r in decision.reasons)


def test_live_gate_blocks_unconfirmed(tmp_path):
    db = Database(path=tmp_path / "risk.db")
    s = _settings()
    s.account_mode = AccountMode.LIVE
    s.live_trading_confirm = ""  # interlock missing
    rm = RiskManager(settings=s, db=db, acct_ccy_per_usd=1.0)
    assert s.live_requested_but_unconfirmed is True
    decision = rm.pre_trade_checks(40.0)
    assert decision.approved is False
    assert any("interlock" in r for r in decision.reasons)


def test_live_gate_allows_when_confirmed(tmp_path):
    s = _settings()
    s.account_mode = AccountMode.LIVE
    s.live_trading_confirm = "I_UNDERSTAND_THE_RISK"
    assert s.is_live is True


def test_per_window_cap_blocks_fourth_in_same_window(tmp_path):
    rm = _rm(tmp_path)
    _open_trade(rm.db, SessionWindow.NEW_YORK, 3)
    blocked = rm.pre_trade_checks(40.0, window=SessionWindow.NEW_YORK)
    assert blocked.approved is False
    assert any("new_york" in r for r in blocked.reasons)
    # Other windows can still take trades (3 per window, 9/day).
    london = rm.pre_trade_checks(40.0, window=SessionWindow.LONDON)
    assert london.approved is True


def test_daily_cap_blocks_tenth_trade(tmp_path):
    rm = _rm(tmp_path)
    _open_trade(rm.db, SessionWindow.ASIA, 3)
    _open_trade(rm.db, SessionWindow.LONDON, 3)
    _open_trade(rm.db, SessionWindow.NEW_YORK, 3)
    decision = rm.pre_trade_checks(40.0, window=SessionWindow.NEW_YORK)
    assert decision.approved is False
    assert any("Max open positions reached" in r or "Daily" in r for r in decision.reasons)


def test_open_risk_uses_current_stop(tmp_path):
    import json

    rm = _rm(tmp_path)
    tid = rm.db.insert_trade(
        TradeRecord(
            environment="PAPER",
            symbol="SPY",
            window=SessionWindow.NEW_YORK,
            regime=Regime.TREND,
            spread_type=SpreadType.BULL_CALL,
            direction=Direction.LONG,
            contracts=1,
            entry_price=1.5,
            max_loss=50.0,
            max_profit=50.0,
            target_r=1.5,
            status=TradeStatus.OPEN,
            plan_json=json.dumps(
                {"stop_loss_price": 1.0, "original_stop_loss_price": 1.0}
            ),
        )
    )
    trade = rm.db.get_trade(tid)
    assert rm.open_risk_acct(trade) == pytest.approx(50.0)  # 0.5 * 100
    rm.db.update_plan_json(
        tid, {"stop_loss_price": 1.5, "original_stop_loss_price": 1.0}
    )
    trade2 = rm.db.get_trade(tid)
    assert rm.open_risk_acct(trade2) == 0.0
