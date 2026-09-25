"""Market-data access with an offline synthetic fallback.

Real bars come from IBKR via :class:`core.ibkr_client.IBKRClient`. For demos,
CI and unit tests (where no Gateway is running) a deterministic synthetic
intraday series is available so the whole pipeline can be exercised offline.
"""

from __future__ import annotations

import asyncio
import csv
import math
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from core.config import REPO_ROOT
from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.models import Bar
from core.timeutils import as_naive_utc, utcnow

_BAR_MINUTES = 5
_HISTORY_DIR = REPO_ROOT / "data" / "history"
# IBKR typically caps 5-min history at ~1 month per request; chunk below that.
_CHUNK_DAYS = 20
_PACE_SECONDS = 1.2


def generate_synthetic_bars(
    *,
    days: int = 10,
    start_price: float = 100.0,
    annual_vol: float = 0.25,
    drift: float = 0.0,
    seed: int = 42,
    bar_minutes: int = _BAR_MINUTES,
) -> list[Bar]:
    """Deterministic 24h intraday GBM series (naive UTC timestamps).

    Produces ``days`` consecutive calendar days of ``bar_minutes`` bars covering
    the full 24h clock, so every session window (Asia/London/New York) has data.
    """
    rng = random.Random(seed)
    bars_per_day = (24 * 60) // bar_minutes
    dt_years = bar_minutes / (60 * 24 * 252)
    sigma_step = annual_vol * math.sqrt(dt_years)
    mu_step = (drift) * dt_years

    price = start_price
    # Anchor to a recent, fixed UTC midnight for reproducibility.
    start = datetime(2026, 1, 5, 0, 0, 0)  # a Monday
    bars: list[Bar] = []
    for d in range(days):
        for i in range(bars_per_day):
            ts = start + timedelta(days=d, minutes=i * bar_minutes)
            shock = rng.gauss(mu_step, sigma_step)
            open_p = price
            close_p = max(open_p * math.exp(shock), 0.01)
            intrabar = abs(rng.gauss(0, sigma_step)) * open_p
            high = max(open_p, close_p) + intrabar * 0.5
            low = min(open_p, close_p) - intrabar * 0.5
            bars.append(
                Bar(
                    ts=ts,
                    open=round(open_p, 2),
                    high=round(high, 2),
                    low=round(max(low, 0.01), 2),
                    close=round(close_p, 2),
                    volume=round(rng.uniform(1e5, 5e5)),
                )
            )
            price = close_p
    return bars


def synthetic_chain(
    spot: float,
    *,
    increment: float = 1.0,
    count: int = 8,
    dte: int = 7,
    iv: float = 0.25,
) -> dict:
    """Build a plausible offline option chain centred on ``spot`` for demos/tests."""
    atm = round(spot / increment) * increment
    strikes = sorted(atm + increment * k for k in range(-count, count + 1) if atm + increment * k > 0)
    expiry_dt = utcnow() + timedelta(days=dte)
    return {
        "spot": round(spot, 2),
        "strikes": strikes,
        "expiry": expiry_dt.strftime("%Y%m%d"),
        "days_to_expiry": float(dte),
        "iv": iv,
        "multiplier": 100,
    }


async def fetch_bars(
    symbol: str,
    *,
    duration: str = "3 D",
    bar_size: str = "5 mins",
) -> list[Bar]:
    """Fetch real OHLCV bars from IBKR (raises :class:`IBKRUnavailable` on failure).

    Spans longer than ~20 days of 5-minute bars are requested in chunks and
    merged with the on-disk cache under ``data/history/``.
    """
    days = _duration_days(duration)
    if days > _CHUNK_DAYS:
        return await fetch_bars_range(symbol, days=days, bar_size=bar_size)
    async with IBKRClient() as ib:
        if symbol.upper() == "MES":
            return await ib.historical_bars_mes(duration=duration, bar_size=bar_size)
        return await ib.historical_bars(symbol, duration=duration, bar_size=bar_size)


def _duration_days(duration: str) -> int:
    parts = duration.strip().upper().split()
    n = int(parts[0])
    unit = parts[1] if len(parts) > 1 else "D"
    if unit.startswith("Y"):
        return n * 365
    if unit.startswith("M"):
        return n * 30
    if unit.startswith("W"):
        return n * 7
    return n


def history_cache_path(symbol: str, bar_size: str = "5 mins") -> Path:
    safe = bar_size.replace(" ", "")
    return _HISTORY_DIR / f"{symbol.upper()}_{safe}.csv"


def load_cached_bars(symbol: str, bar_size: str = "5 mins") -> list[Bar]:
    path = history_cache_path(symbol, bar_size)
    if not path.exists():
        return []
    bars: list[Bar] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append(
                Bar(
                    ts=as_naive_utc(datetime.fromisoformat(row["ts"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def save_cached_bars(symbol: str, bars: list[Bar], bar_size: str = "5 mins") -> Path:
    path = history_cache_path(symbol, bar_size)
    path.parent.mkdir(parents=True, exist_ok=True)
    uniq: dict[datetime, Bar] = {}
    for b in bars:
        uniq[b.ts] = b
    ordered = [uniq[k] for k in sorted(uniq)]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for b in ordered:
            writer.writerow(
                {
                    "ts": b.ts.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
            )
    return path


def _cached_bars_or_tail(symbol: str, duration: str, bar_size: str) -> list[Bar] | None:
    """Use on-disk history even when it does not fully cover ``duration``.

    A copied ``data/history/*.csv`` is often a few days stale relative to
    ``utcnow()``. Prefer showing that series over an empty ticker.
    """
    bars = load_cached_bars(symbol, bar_size)
    if not bars:
        return None
    days = max(_duration_days(duration), 1)
    cutoff = utcnow() - timedelta(days=days)
    subset = [b for b in bars if as_naive_utc(b.ts) >= cutoff]
    return subset or bars[-500:]


def cached_bars_for_lookback(
    symbol: str, days: int, bar_size: str = "5 mins"
) -> list[Bar] | None:
    """Return cached bars covering ``days`` if the file has enough history."""
    bars = load_cached_bars(symbol, bar_size)
    if not bars:
        return None
    cutoff = utcnow() - timedelta(days=days)
    subset = [b for b in bars if as_naive_utc(b.ts) >= cutoff]
    if not subset:
        return None
    # Cache must start close to the requested start (allow 7 calendar days slack
    # for weekends / holidays).
    if as_naive_utc(subset[0].ts) > cutoff + timedelta(days=7):
        return None
    return subset


async def fetch_bars_range(
    symbol: str,
    *,
    days: int = 365,
    bar_size: str = "5 mins",
    progress: Callable[[str], None] | None = None,
) -> list[Bar]:
    """Fetch ``days`` of bars in IBKR-safe chunks, merging into the local cache."""
    target_start = utcnow() - timedelta(days=days)
    have = cached_bars_for_lookback(symbol, days, bar_size)
    if have is not None:
        if progress:
            progress(
                f"Using cache ({len(have)} bars, {days}d) at {history_cache_path(symbol, bar_size)}"
            )
        return have

    collected: dict[datetime, Bar] = {
        as_naive_utc(b.ts): b.model_copy(update={"ts": as_naive_utc(b.ts)})
        for b in load_cached_bars(symbol, bar_size)
    }
    end = utcnow()
    if collected:
        oldest = min(collected)
        if oldest > target_start + timedelta(days=7):
            end = oldest
    seen_ends: set[str] = set()
    max_chunks = days // _CHUNK_DAYS + 6
    async with IBKRClient() as ib:
        for chunk_i in range(1, max_chunks + 1):
            if collected and min(collected) <= target_start:
                break
            end = as_naive_utc(end)
            end_str = end.strftime("%Y%m%d %H:%M:%S")
            if end_str in seen_ends:
                end = end - timedelta(days=_CHUNK_DAYS)
                continue
            seen_ends.add(end_str)
            if progress:
                progress(f"IBKR chunk {chunk_i}/{max_chunks}: {_CHUNK_DAYS} D ending {end_str} GMT")
            try:
                if symbol.upper() == "MES":
                    chunk = await ib.historical_bars_mes(
                        duration=f"{_CHUNK_DAYS} D",
                        bar_size=bar_size,
                        end_datetime=end_str,
                    )
                else:
                    chunk = await ib.historical_bars(
                        symbol,
                        duration=f"{_CHUNK_DAYS} D",
                        bar_size=bar_size,
                        end_datetime=end_str,
                    )
            except Exception as exc:
                msg = str(exc).lower()
                if "162" in str(exc) or "no data" in msg or "hmids" in msg or "hmds" in msg:
                    if progress:
                        progress(f"  no data at {end_str} GMT, stepping back")
                    end = end - timedelta(days=_CHUNK_DAYS)
                    await asyncio.sleep(_PACE_SECONDS)
                    continue
                raise IBKRUnavailable(f"Historical request failed at {end_str}: {exc!r}") from exc
            if chunk:
                for b in chunk:
                    ts = as_naive_utc(b.ts)
                    collected[ts] = b.model_copy(update={"ts": ts})
                oldest_chunk = min(as_naive_utc(b.ts) for b in chunk)
                end = oldest_chunk - timedelta(minutes=5)
                save_cached_bars(symbol, list(collected.values()), bar_size)
            else:
                end = end - timedelta(days=_CHUNK_DAYS)
            await asyncio.sleep(_PACE_SECONDS)

    ordered = [collected[k] for k in sorted(collected)]
    path = save_cached_bars(symbol, ordered, bar_size)
    if progress:
        progress(f"Saved {len(ordered)} bars -> {path}")
    return [b for b in ordered if as_naive_utc(b.ts) >= target_start]


async def refresh_recent_bars(
    symbol: str,
    *,
    days: int = 5,
    bar_size: str = "5 mins",
    progress: Callable[[str], None] | None = None,
) -> list[Bar]:
    """Fetch the latest ``days`` from IBKR and merge them into the on-disk cache.

    ``fetch_bars_range`` short-circuits when the cache already covers the
    lookback, so a nightly job would otherwise keep ranking yesterday's data.
    """
    days = max(int(days), 1)
    if progress:
        progress(f"Refreshing last {days}d of {symbol} from IBKR")
    async with IBKRClient() as ib:
        chunk = await ib.historical_bars(
            symbol, duration=f"{days} D", bar_size=bar_size
        )
    collected: dict[datetime, Bar] = {
        as_naive_utc(b.ts): b.model_copy(update={"ts": as_naive_utc(b.ts)})
        for b in load_cached_bars(symbol, bar_size)
    }
    for b in chunk:
        ts = as_naive_utc(b.ts)
        collected[ts] = b.model_copy(update={"ts": ts})
    ordered = [collected[k] for k in sorted(collected)]
    path = save_cached_bars(symbol, ordered, bar_size)
    if progress:
        progress(f"Cache now {len(ordered)} bars -> {path}")
    return ordered


async def fetch_bars_with_fallback(
    symbol: str,
    *,
    duration: str = "3 D",
    bar_size: str = "5 mins",
    use_synthetic: bool = False,
    allow_synthetic_fallback: bool = False,
    synthetic_days: int = 10,
    synthetic_seed: int = 42,
) -> tuple[list[Bar], str, str | None]:
    """Return ``(bars, source, warning)``.

    - ``use_synthetic=True``: skip IBKR entirely and return deterministic data.
    - otherwise fetch from IBKR; on failure fall back to synthetic only if
      ``allow_synthetic_fallback`` (or ``use_synthetic``) is set, else raise.
    """
    if use_synthetic:
        return generate_synthetic_bars(days=synthetic_days, seed=synthetic_seed), "synthetic", None
    try:
        bars = await fetch_bars(symbol, duration=duration, bar_size=bar_size)
        return bars, "ibkr", None
    except IBKRUnavailable as exc:
        cached = _cached_bars_or_tail(symbol, duration, bar_size)
        if cached:
            return cached, "cache", f"IBKR unavailable, using local history cache: {exc}"
        if not allow_synthetic_fallback:
            raise
        bars = generate_synthetic_bars(days=synthetic_days, seed=synthetic_seed)
        return bars, "synthetic", f"IBKR unavailable, using synthetic data: {exc}"
