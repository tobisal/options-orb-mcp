"""Paper/test market-data lag (IBKR delayed ~10m)."""

from datetime import timedelta

from core.config import AccountMode, get_settings
from core.timeutils import market_data_lag, market_now, utcnow


def test_paper_default_lag_is_ten_minutes(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ACCOUNT_MODE", "paper")
    monkeypatch.setenv("MARKET_DATA_LAG_MINUTES", "10")
    get_settings.cache_clear()
    assert market_data_lag() == timedelta(minutes=10)
    skew = utcnow() - market_now()
    assert timedelta(minutes=9, seconds=50) <= skew <= timedelta(minutes=10, seconds=5)
    get_settings.cache_clear()


def test_live_mode_ignores_lag(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ACCOUNT_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "I_UNDERSTAND_THE_RISK")
    monkeypatch.setenv("MARKET_DATA_LAG_MINUTES", "10")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.account_mode is AccountMode.LIVE
    assert settings.is_live is True
    assert market_data_lag() == timedelta(0)
    assert abs((market_now() - utcnow()).total_seconds()) < 1
    get_settings.cache_clear()


def test_lag_can_be_disabled_in_paper(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ACCOUNT_MODE", "paper")
    monkeypatch.setenv("MARKET_DATA_LAG_MINUTES", "0")
    get_settings.cache_clear()
    assert market_data_lag() == timedelta(0)
    get_settings.cache_clear()
