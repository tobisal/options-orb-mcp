"""Tests for autotrade state persistence / resume helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.autotrade_state import (
    default_state,
    load_state,
    save_state,
    should_autostart,
    start_kwargs,
)


def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "autotrade.json"
    monkeypatch.setattr("core.autotrade_state.state_path", lambda db_path=None: path)
    save_state(
        {
            "enabled": True,
            "symbol": "spy",
            "window": "london",
            "demo": False,
            "interval": 45,
            "target_r": 1.25,
        },
        path=path,
    )
    loaded = load_state(path)
    assert loaded["enabled"] is True
    assert loaded["symbol"] == "SPY"
    assert loaded["window"] == "london"
    assert loaded["interval"] == 45.0
    assert loaded["target_r"] == 1.25


def test_load_missing_returns_defaults(tmp_path: Path):
    path = tmp_path / "missing.json"
    loaded = load_state(path)
    assert loaded["enabled"] is False
    assert "symbol" in loaded


def test_should_autostart_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "autotrade.json"
    monkeypatch.setattr("core.autotrade_state.get_settings", lambda: _Settings(False))
    save_state({**default_state(), "enabled": True}, path=path)
    assert should_autostart(load_state(path)) is True
    save_state({**default_state(), "enabled": False}, path=path)
    assert should_autostart(load_state(path)) is False


def test_should_autostart_env_forces_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.autotrade_state.get_settings", lambda: _Settings(True))
    assert should_autostart({"enabled": False}) is True


def test_start_kwargs_env_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "core.autotrade_state.get_settings",
        lambda: _Settings(True, symbol="QQQ", window="new_york", interval=120.0, demo=True),
    )
    kwargs = start_kwargs({"enabled": True, "symbol": "SPY", "window": "auto", "demo": False, "interval": 60})
    assert kwargs["symbol"] == "QQQ"
    assert kwargs["window"] == "new_york"
    assert kwargs["interval"] == 120.0
    assert kwargs["demo"] is True


class _Settings:
    def __init__(
        self,
        autostart: bool,
        *,
        symbol: str | None = None,
        window: str | None = None,
        interval: float | None = None,
        demo: bool | None = None,
    ) -> None:
        self.auto_trade_autostart = autostart
        self.auto_trade_symbol = symbol
        self.auto_trade_window = window
        self.auto_trade_interval = interval
        self.auto_trade_demo = demo
        self.default_symbol = "SPY"
