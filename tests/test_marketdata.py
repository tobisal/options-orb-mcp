from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.marketdata import load_cached_bars, save_cached_bars
from core.models import Bar
from core.timeutils import as_naive_utc


def test_as_naive_utc_strips_aware_utc():
    aware = datetime(2026, 8, 1, 13, 30, tzinfo=UTC)
    naive = as_naive_utc(aware)
    assert naive.tzinfo is None
    assert naive == datetime(2026, 8, 1, 13, 30)


def test_history_cache_roundtrip_normalizes_tz(tmp_path, monkeypatch):
    from core import marketdata

    monkeypatch.setattr(marketdata, "_HISTORY_DIR", Path(tmp_path))
    aware = datetime(2026, 8, 1, 13, 30, tzinfo=UTC)
    bars = [
        Bar(ts=aware, open=100, high=101, low=99, close=100.5, volume=1),
        Bar(ts=aware + timedelta(minutes=5), open=100.5, high=101, low=100, close=100.8, volume=2),
    ]
    save_cached_bars("SPY", bars)
    loaded = load_cached_bars("SPY")
    assert len(loaded) == 2
    assert all(b.ts.tzinfo is None for b in loaded)
    assert loaded[0].ts == datetime(2026, 8, 1, 13, 30)
    assert loaded[0].close == 100.5


def test_stale_cache_used_when_lookback_is_newer(tmp_path, monkeypatch):
    from core import marketdata

    monkeypatch.setattr(marketdata, "_HISTORY_DIR", Path(tmp_path))
    old = datetime(2026, 8, 1, 13, 30)
    bars = [
        Bar(ts=old, open=100, high=101, low=99, close=100.5, volume=1),
        Bar(ts=old + timedelta(minutes=5), open=100.5, high=101, low=100, close=100.8, volume=2),
    ]
    save_cached_bars("SPY", bars)
    got = marketdata._cached_bars_or_tail("SPY", "2 D", "5 mins")
    assert got is not None
    assert len(got) == 2
    assert got[-1].close == 100.8
