"""Session-window definitions and opening-range extraction.

Windows are expressed in US/Eastern (the reference clock for US options).
Parameters live in ``configs/windows.json`` so they can be tuned per window by
the optimiser without code changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from functools import lru_cache
from typing import Any

import pytz

from core.config import REPO_ROOT
from core.models import Bar, SessionWindow
from core.timeutils import utcnow

_STRATEGY_FIELDS = (
    "opening_range_minutes",
    "breakout_buffer_atr",
    "min_strength",
    "target_r",
    "stop_r",
)

EASTERN = pytz.timezone("US/Eastern")

_CONFIG_PATH = REPO_ROOT / "configs" / "windows.json"


@dataclass
class WindowConfig:
    window: SessionWindow
    window_open: time
    window_close: time
    opening_range_minutes: int
    breakout_buffer_atr: float
    min_strength: float
    target_r: float
    stop_r: float

    @property
    def wraps_midnight(self) -> bool:
        return self.window_close <= self.window_open

    def strategy_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _STRATEGY_FIELDS}

    def overlay(self, params: dict[str, Any] | None) -> WindowConfig:
        """Return a copy with optimiser/backtest ORB fields applied.

        Session open/close times always come from ``windows.json``. Unknown or
        missing keys are ignored so walk-forward grid dumps cannot clobber this.
        """
        if not params:
            return self
        updates: dict[str, Any] = {}
        for name in _STRATEGY_FIELDS:
            if name not in params or params[name] is None:
                continue
            current = getattr(self, name)
            updates[name] = type(current)(params[name])
        return replace(self, **updates) if updates else self


def _parse_time(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


@lru_cache
def load_window_configs() -> dict[SessionWindow, WindowConfig]:
    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    configs: dict[SessionWindow, WindowConfig] = {}
    for key, cfg in raw.items():
        if key.startswith("_"):
            continue
        window = SessionWindow(key)
        configs[window] = WindowConfig(
            window=window,
            window_open=_parse_time(cfg["window_open"]),
            window_close=_parse_time(cfg["window_close"]),
            opening_range_minutes=int(cfg["opening_range_minutes"]),
            breakout_buffer_atr=float(cfg["breakout_buffer_atr"]),
            min_strength=float(cfg["min_strength"]),
            target_r=float(cfg["target_r"]),
            stop_r=float(cfg["stop_r"]),
        )
    return configs


def get_window_config(window: SessionWindow) -> WindowConfig:
    return load_window_configs()[window]


def _to_eastern(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        # Assume naive timestamps are already UTC (IBKR formatDate=2 => UTC epoch).
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


def _in_window(t: time, cfg: WindowConfig) -> bool:
    if cfg.wraps_midnight:
        return t >= cfg.window_open or t < cfg.window_close
    return cfg.window_open <= t < cfg.window_close


def active_window(now: datetime | None = None) -> SessionWindow | None:
    """Return the session window active at ``now`` (defaults to current UTC)."""
    now = now or utcnow()
    et = _to_eastern(now)
    for cfg in load_window_configs().values():
        if _in_window(et.time(), cfg):
            return cfg.window
    return None


def window_span_gmt(cfg: WindowConfig, now: datetime | None = None) -> tuple[str, str]:
    """Window open/close clocks in GMT (UTC), DST-aware from US/Eastern."""
    now = now or utcnow()
    et_date = _to_eastern(now).date()
    open_dt = EASTERN.localize(datetime.combine(et_date, cfg.window_open))
    close_date = et_date + timedelta(days=1) if cfg.wraps_midnight else et_date
    close_dt = EASTERN.localize(datetime.combine(close_date, cfg.window_close))
    return (
        open_dt.astimezone(pytz.UTC).strftime("%H:%M"),
        close_dt.astimezone(pytz.UTC).strftime("%H:%M"),
    )


def describe_windows_gmt(now: datetime | None = None) -> str:
    """Human-readable session hours in GMT, e.g. 'New York 13:30-20:00 GMT'."""
    parts: list[str] = []
    for cfg in load_window_configs().values():
        open_g, close_g = window_span_gmt(cfg, now)
        label = cfg.window.value.replace("_", " ").title()
        parts.append(f"{label} {open_g}-{close_g} GMT")
    return ", ".join(parts)


def bars_in_window(bars: list[Bar], cfg: WindowConfig) -> list[Bar]:
    """Filter bars to those whose (Eastern) time falls inside the window."""
    return [b for b in bars if _in_window(_to_eastern(b.ts).time(), cfg)]


def opening_range_bars(bars: list[Bar], cfg: WindowConfig) -> list[Bar]:
    """Bars that make up the opening range for the most recent session in the data.

    Groups by Eastern calendar date of the window's *open* and takes bars within
    ``opening_range_minutes`` of the first in-window bar of the latest session.
    """
    in_window = bars_in_window(bars, cfg)
    if not in_window:
        return []
    # Identify the latest session by anchoring on the most recent in-window bar.
    last_et = _to_eastern(in_window[-1].ts)
    # For midnight-wrapping windows, the session "date" is anchored to the open.
    session_bars = _same_session(in_window, cfg, last_et)
    if not session_bars:
        return []
    start = _to_eastern(session_bars[0].ts)
    cutoff = start.timestamp() + cfg.opening_range_minutes * 60
    return [b for b in session_bars if _to_eastern(b.ts).timestamp() <= cutoff]


def session_bars_after_range(bars: list[Bar], cfg: WindowConfig) -> list[Bar]:
    """In-window bars of the latest session that occur *after* the opening range."""
    orb = opening_range_bars(bars, cfg)
    if not orb:
        return []
    last_range_ts = orb[-1].ts
    in_window = bars_in_window(bars, cfg)
    session_bars = _same_session(in_window, cfg, _to_eastern(in_window[-1].ts))
    return [b for b in session_bars if b.ts > last_range_ts]


def group_by_session(bars: list[Bar], cfg: WindowConfig) -> list[tuple[str, list[Bar]]]:
    """Group in-window bars into ordered ``(session_key, bars)`` sessions."""
    in_window = bars_in_window(bars, cfg)
    groups: dict[str, list[Bar]] = {}
    order: list[str] = []
    for b in in_window:
        key = _session_key(b.ts, cfg)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)
    return [(k, groups[k]) for k in sorted(order)]


def _session_key(ts: datetime, cfg: WindowConfig) -> str:
    et = _to_eastern(ts)
    if cfg.wraps_midnight and et.time() < cfg.window_close:
        prior = et.date().toordinal() - 1
        return datetime.fromordinal(prior).strftime("%Y-%m-%d")
    return et.strftime("%Y-%m-%d")


def opening_range_of(session_bars: list[Bar], minutes: int) -> tuple[list[Bar], list[Bar]]:
    """Split a single session's bars into (opening-range bars, post-range bars)."""
    if not session_bars:
        return [], []
    start = _to_eastern(session_bars[0].ts).timestamp()
    cutoff = start + minutes * 60
    orb = [b for b in session_bars if _to_eastern(b.ts).timestamp() <= cutoff]
    post = [b for b in session_bars if _to_eastern(b.ts).timestamp() > cutoff]
    return orb, post


def _same_session(in_window: list[Bar], cfg: WindowConfig, anchor_et: datetime) -> list[Bar]:
    """Bars belonging to the same session as ``anchor_et``.

    For non-wrapping windows this is simply the same Eastern calendar date. For
    wrapping windows (e.g. Asia 20:00 -> 02:00) bars after midnight belong to
    the previous day's session.
    """

    key = _session_key(anchor_et, cfg)
    return [b for b in in_window if _session_key(b.ts, cfg) == key]
