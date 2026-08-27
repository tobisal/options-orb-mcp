from core.config import Settings
from core.discord_format import (
    clip,
    format_log_line,
    format_nightly,
    format_preview,
    format_signals,
    format_status,
    should_relay_log,
)


def test_allowlist_parses_ids():
    s = Settings(discord_allowed_user_ids="123, 456, not-an-id")
    assert s.discord_allowlist() == {123, 456}


def test_empty_allowlist():
    s = Settings(discord_allowed_user_ids="")
    assert s.discord_allowlist() == set()


def test_clip_truncates():
    assert "truncated" in clip("x" * 50, limit=30)


def test_format_status_account_and_auto():
    text = format_status(
        {
            "environment": "PAPER",
            "paper_equity": 1009.89,
            "account_currency": "GBP",
            "daily_pnl": 9.89,
            "open_unrealized_pnl": 0.0,
            "open_positions": 0,
            "max_open_positions": 9,
            "daily_kill_switch_tripped": False,
            "ibkr_connected": True,
        },
        {"running": True, "symbol": "SPY", "window": "auto", "cycles": 3, "trades_placed": 1},
    )
    assert "PAPER" in text
    assert "1009.89" in text
    assert "RUNNING" in text


def test_format_status_auto_only():
    text = format_status({}, {"running": False, "symbol": "SPY", "window": "auto", "cycles": 0})
    assert "Paper account" not in text
    assert "stopped" in text


def test_format_signals_and_preview():
    sig = format_signals(
        {
            "symbol": "SPY",
            "data_source": "ibkr",
            "signals": [
                {
                    "window": "new_york",
                    "breakout": True,
                    "direction": "short",
                    "regime": "trend",
                    "last_price": 772.6,
                    "range_low": 775.0,
                    "range_high": 776.8,
                    "strength": 1.3,
                }
            ],
        }
    )
    assert "BREAKOUT SHORT" in sig
    preview = format_preview(
        {
            "ok": True,
            "tradeable": True,
            "signal": {"window": "new_york", "direction": "short", "symbol": "SPY"},
            "plan": {
                "symbol": "SPY",
                "spread_type": "bear_call_credit",
                "contracts": 1,
                "net_debit": -0.4,
                "max_loss": 35.0,
                "max_profit": 40.0,
                "expiry": "20260821",
                "long_leg": {"strike": 775, "right": "C"},
                "short_leg": {"strike": 774, "right": "C"},
            },
            "risk": {"reasons": []},
        }
    )
    assert "bear_call_credit" in preview
    assert "tradeable" in preview


def test_format_nightly_and_log_filter():
    text = format_nightly(
        {
            "ok": True,
            "symbol": "SPY",
            "applied_windows": 1,
            "bars": 100,
            "windows": [
                {
                    "window": "new_york",
                    "applied": True,
                    "reason": "ok",
                    "best": {"params": {"opening_range_minutes": 45}},
                }
            ],
        }
    )
    assert "APPLIED" in text
    assert should_relay_log({"level": "trade", "msg": "PLACED"}, verbose=False)
    assert should_relay_log({"level": "error", "msg": "fail"}, verbose=False)
    assert not should_relay_log({"level": "muted", "msg": "No entry"}, verbose=False)
    assert should_relay_log({"level": "muted", "msg": "No entry"}, verbose=True)
    assert "PLACED" in format_log_line({"t": "2026-08-17T19:00:00", "level": "trade", "msg": "PLACED"})
