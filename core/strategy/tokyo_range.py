"""Tokyo Range Breakout (hooper.algo.fxx / Jacob Hooper).

Rules from https://www.instagram.com/reel/DZPpCeevmMr/ ("6 Steps Away From Being
Profitbale"):

1. Mark the Tokyo range **00:00–06:00 GMT** (session high / low).
2. At 06:00 GMT place breakout stops **buffer** beyond the range (video: 3 pips
   on FX). Favourite pairs in the reel: USD/JPY, AUD/JPY, AUD/USD.
3. Stop loss on the **opposite side of the range**.
4. Take profit = **1.5 × range width** from entry.
5. Cancel unfilled entries by **09:00 GMT**; flatten all by **12:00 GMT**
   (no overnight).
6. Stick to the listed pairs / liquid Asia-sensitive underlyings (config allowlist).

Adapted here to the options vertical pipeline: directional debit spread on the
breakout, with ``target_r`` / ``stop_r`` derived from the range geometry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

import pytz

from core.config import REPO_ROOT
from core.models import Bar, Direction, ORBSignal, Regime, SessionWindow
from core.timeutils import utcnow

_CONFIG_PATH = REPO_ROOT / "configs" / "tokyo_range_breakout.json"
GMT = pytz.UTC


@dataclass(frozen=True)
class TokyoRangeConfig:
    enabled: bool = True
    range_start_gmt: time = time(0, 0)
    range_end_gmt: time = time(6, 0)
    entry_until_gmt: time = time(9, 0)
    flatten_gmt: time = time(12, 0)
    buffer: float = 0.05  # SPY default; FX overrides in symbols
    target_range_mult: float = 1.5
    # Empty allowlist = any symbol. Reel favourites listed in config.
    allowed_symbols: tuple[str, ...] = ()
    notes: str = ""

    def risk_distance(self, width: float) -> float:
        return max(width + self.buffer, 1e-9)

    def target_r(self, width: float) -> float:
        return (self.target_range_mult * width) / self.risk_distance(width)

    def stop_r(self, width: float) -> float:
        # Risk is the full opposite-side distance ≈ width+buffer → 1R.
        return 1.0


def _parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")[:2]
    return time(int(hh), int(mm))


@lru_cache
def load_tokyo_range_config(symbol: str | None = None) -> TokyoRangeConfig:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    base = {k: v for k, v in raw.items() if not str(k).startswith("_") and k != "symbols"}
    allow = tuple(str(s).upper() for s in (raw.get("allowed_symbols") or []))
    sym = (symbol or "").upper()
    overrides = (raw.get("symbols") or {}).get(sym) or {}
    merged = {**base, **overrides}
    return TokyoRangeConfig(
        enabled=bool(merged.get("enabled", True)),
        range_start_gmt=_parse_hhmm(str(merged.get("range_start_gmt", "00:00"))),
        range_end_gmt=_parse_hhmm(str(merged.get("range_end_gmt", "06:00"))),
        entry_until_gmt=_parse_hhmm(str(merged.get("entry_until_gmt", "09:00"))),
        flatten_gmt=_parse_hhmm(str(merged.get("flatten_gmt", "12:00"))),
        buffer=float(merged.get("buffer", 0.05)),
        target_range_mult=float(merged.get("target_range_mult", 1.5)),
        allowed_symbols=allow,
        notes=str(merged.get("notes") or ""),
    )


def clear_tokyo_range_config_cache() -> None:
    load_tokyo_range_config.cache_clear()


def _to_gmt(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(GMT)


def _gmt_date_for_range(as_of_gmt: datetime, cfg: TokyoRangeConfig) -> date:
    """Which GMT calendar day owns the Tokyo range relative to ``as_of``.

    During/after the range on day D, use D. Before ``range_start`` on day D,
    the active completed range is day D-1.
    """
    d = as_of_gmt.date()
    if as_of_gmt.time() < cfg.range_start_gmt:
        return d - timedelta(days=1)
    return d


def _bars_in_gmt_window(
    bars: list[Bar], day, start: time, end: time
) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        g = _to_gmt(b.ts)
        if g.date() != day:
            continue
        t = g.time()
        if start <= t < end:
            out.append(b)
    return out


def tokyo_entry_window_active(
    now: datetime | None = None, cfg: TokyoRangeConfig | None = None
) -> bool:
    """True during 06:00–09:00 GMT entry watch (after range, before cancel)."""
    cfg = cfg or load_tokyo_range_config()
    g = _to_gmt(now or utcnow())
    return cfg.enabled and cfg.range_end_gmt <= g.time() < cfg.entry_until_gmt


def tokyo_should_flatten(
    now: datetime | None = None, cfg: TokyoRangeConfig | None = None
) -> bool:
    cfg = cfg or load_tokyo_range_config()
    g = _to_gmt(now or utcnow())
    return g.time() >= cfg.flatten_gmt


def compute_tokyo_range_signal(
    symbol: str,
    bars: list[Bar],
    *,
    cfg: TokyoRangeConfig | None = None,
    as_of: datetime | None = None,
    window: SessionWindow = SessionWindow.ASIA,
) -> ORBSignal:
    """Breakout of the 00:00–06:00 GMT Tokyo range (reuses ORBSignal shape)."""
    cfg = cfg or load_tokyo_range_config(symbol)
    as_of = as_of or (bars[-1].ts if bars else utcnow())
    g = _to_gmt(as_of)
    last_px = bars[-1].close if bars else 0.0

    empty = ORBSignal(
        symbol=symbol,
        window=window,
        as_of=as_of,
        range_high=0.0,
        range_low=0.0,
        last_price=last_px,
        direction=Direction.NEUTRAL,
        breakout=False,
        regime=Regime.TREND,
        notes="tokyo_range: idle",
    )
    if not cfg.enabled:
        return empty.model_copy(update={"notes": "tokyo_range: disabled"})

    if cfg.allowed_symbols and symbol.upper() not in cfg.allowed_symbols:
        return empty.model_copy(
            update={"notes": f"tokyo_range: {symbol} not in allowlist {cfg.allowed_symbols}"}
        )

    day = _gmt_date_for_range(g, cfg)
    range_bars = _bars_in_gmt_window(bars, day, cfg.range_start_gmt, cfg.range_end_gmt)
    if len(range_bars) < 2:
        return empty.model_copy(update={"notes": "tokyo_range: incomplete 00:00-06:00 GMT range"})

    range_high = max(b.high for b in range_bars)
    range_low = min(b.low for b in range_bars)
    width = max(range_high - range_low, 1e-9)
    up = range_high + cfg.buffer
    down = range_low - cfg.buffer

    if g.time() < cfg.range_end_gmt:
        return ORBSignal(
            symbol=symbol,
            window=window,
            as_of=as_of,
            range_high=round(range_high, 4),
            range_low=round(range_low, 4),
            last_price=round(last_px, 4),
            direction=Direction.NEUTRAL,
            breakout=False,
            strength=0.0,
            regime=Regime.TREND,
            notes=f"tokyo_range: building {cfg.range_start_gmt.strftime('%H:%M')}-{cfg.range_end_gmt.strftime('%H:%M')} GMT",
        )

    if g.time() >= cfg.entry_until_gmt:
        return ORBSignal(
            symbol=symbol,
            window=window,
            as_of=as_of,
            range_high=round(up, 4),
            range_low=round(down, 4),
            last_price=round(last_px, 4),
            direction=Direction.NEUTRAL,
            breakout=False,
            strength=0.0,
            regime=Regime.TREND,
            notes=f"tokyo_range: past entry cut-off {cfg.entry_until_gmt.strftime('%H:%M')} GMT",
        )

    # Entry window: first close beyond buffered range.
    direction = Direction.NEUTRAL
    breakout = False
    strength = 0.0
    if last_px >= up:
        direction = Direction.LONG
        breakout = True
        strength = (last_px - range_high) / width
    elif last_px <= down:
        direction = Direction.SHORT
        breakout = True
        strength = (range_low - last_px) / width

    notes = (
        f"tokyo_range {day.isoformat()} OR[{range_low:.4f},{range_high:.4f}] "
        f"buf={cfg.buffer} TP={cfg.target_range_mult}xW "
        f"entry_until={cfg.entry_until_gmt.strftime('%H:%M')} "
        f"flat={cfg.flatten_gmt.strftime('%H:%M')} GMT last={last_px:.4f}"
    )
    return ORBSignal(
        symbol=symbol,
        window=window,
        as_of=as_of,
        range_high=round(up, 4),
        range_low=round(down, 4),
        last_price=round(last_px, 4),
        direction=direction,
        breakout=breakout,
        strength=round(strength, 4),
        regime=Regime.TREND,
        notes=notes,
    )
