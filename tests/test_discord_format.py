from core.config import Settings
from core.discord_format import (
    clip,
    format_log_line,
    format_nightly,
    format_preview,
    format_signal_alert,
    format_signals,
    format_status,
    format_trade_alert,
    session_label,
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


def test_session_label_asia_london_ny():
    assert session_label("asia") == "ASIA"
    assert session_label("asia_judas") == "ASIA"
    assert session_label("london") == "LONDON"
    assert session_label("new_york") == "NEW YORK"
    assert session_label("ny") == "NEW YORK"


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
            "symbol": "MES",
            "data_source": "ibkr",
            "signals": [
                {
                    "window": "asia",
                    "breakout": True,
                    "direction": "long",
                    "regime": "trend",
                    "last_price": 7740.0,
                    "range_low": 7710.0,
                    "range_high": 7750.0,
                    "strength": 1.0,
                },
                {
                    "window": "new_york",
                    "breakout": True,
                    "direction": "short",
                    "regime": "trend",
                    "last_price": 772.6,
                    "range_low": 775.0,
                    "range_high": 776.8,
                    "strength": 1.3,
                },
            ],
        }
    )
    assert "[ASIA]" in sig
    assert "SIGNAL LONG" in sig
    assert "[NEW YORK]" in sig
    assert "SIGNAL SHORT" in sig
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
    assert "[NEW YORK]" in preview


def test_format_signal_alert_labels_asia():
    text = format_signal_alert(
        {
            "window": "asia",
            "name": "Asia Judas",
            "direction": "long",
            "entry_price": 7740.25,
            "stop_price": 7711.75,
            "target_price": 7767.75,
            "target_label": "pd_high",
            "contracts": 1,
            "range_low": 7711.75,
            "range_high": 7755.0,
            "strategy": {"entry_model": "asia_judas"},
            "blocked": True,
            "block_reason": "Stop risk exceeds budget",
        },
        symbol="MES",
    )
    assert "SIGNAL [ASIA]" in text
    assert "BUY" in text
    assert "Entry `7740.25`" in text
    # risk = 28.5 pts = 114 pips (0.25 tick)
    assert "114 pips" in text
    assert "2R" in text
    assert "3R" in text
    assert "228 pips" in text  # 2R
    assert "342 pips" in text  # 3R
    assert "one ping per market per day" in text
    assert "Paper auto-place blocked" in text


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
    assert not should_relay_log({"level": "trade", "msg": "PLACED trade #1"}, verbose=False)
    assert not should_relay_log({"level": "trade", "msg": "CLOSED journal #1"}, verbose=False)
    assert should_relay_log({"level": "trade", "msg": "scaled out"}, verbose=False)
    assert should_relay_log({"level": "error", "msg": "fail"}, verbose=False)
    assert should_relay_log(
        {"level": "signal", "msg": "SIGNAL [ASIA] MES LONG entry 1 SL 2 TP 3"},
        verbose=False,
    )
    assert not should_relay_log({"level": "muted", "msg": "No entry"}, verbose=False)
    assert should_relay_log({"level": "muted", "msg": "No entry"}, verbose=True)
    assert "TRADE PLACED" in format_log_line(
        {"t": "2026-08-17T19:00:00", "level": "trade", "msg": "PLACED trade #1"}
    )
    assert format_log_line(
        {
            "t": "2026-08-17T19:00:00",
            "level": "signal",
            "msg": "SIGNAL [ASIA] MES LONG entry 7740.25 SL 7711.75 pd_high 7767.75",
        }
    ).startswith("SIGNAL [ASIA]")


def test_format_trade_alert_placed_and_closed():
    open_row = {
        "id": 65,
        "symbol": "MES",
        "window": "asia",
        "direction": "long",
        "contracts": 1,
        "entry_price": 7740.25,
        "status": "open",
        "environment": "PAPER",
        "notes": "asia Judas long: SSL sweep",
        "plan_json": {
            "session_name": "asia",
            "stop_price": 7711.75,
            "target_price": 7767.75,
            "target_label": "pd_high",
        },
    }
    placed = format_trade_alert(open_row, event="placed")
    assert "[ASIA]" in placed
    assert "FILLED / PLACED" in placed
    assert "7740.25" in placed
    assert "pd_high" in placed

    closed_row = {
        **open_row,
        "status": "closed",
        "exit_price": 7771.125,
        "pnl": 154.38,
        "notes": "asia Judas long | exit=scale_pd_high+force_flat",
    }
    closed = format_trade_alert(closed_row, event="closed")
    assert "[ASIA]" in closed
    assert "CLOSED" in closed
    assert "+154.38" in closed
    assert "exit=scale_pd_high+force_flat" in closed
