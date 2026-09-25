"""MES 5ORB session windows and config loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time
from functools import lru_cache
from typing import Any

from core.config import REPO_ROOT

_CONFIG_PATH = REPO_ROOT / "configs" / "mes_5orb.json"


def _parse_hhmm(s: str) -> time:
    hh, mm = str(s).strip().split(":")[:2]
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class OpeningRangeFilter:
    min_range_points: float = 0.75
    max_range_points: float = 6.0


@dataclass(frozen=True)
class RetestConfig:
    tolerance_ticks: int = 2
    timeout_bars: int = 12
    require_rejection_candle: bool = True


@dataclass(frozen=True)
class MesSession:
    name: str
    or_start: time
    or_end: time
    search_end: time
    force_flat: time
    opening_range: OpeningRangeFilter = field(default_factory=OpeningRangeFilter)
    retest: RetestConfig = field(default_factory=RetestConfig)


@dataclass(frozen=True)
class TrailingStopConfig:
    method: str = "swing_low"
    pivot_lag_bars: int = 2
    buffer_ticks: int = 1


@dataclass(frozen=True)
class MesRiskConfig:
    contracts: int = 1
    max_concurrent: int = 1


@dataclass(frozen=True)
class Mes5OrbConfig:
    symbol: str = "MES"
    point_value: float = 5.0
    tick_size: float = 0.25
    timezone: str = "America/New_York"
    sessions: tuple[MesSession, ...] = ()
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    risk: MesRiskConfig = field(default_factory=MesRiskConfig)

    def session(self, name: str) -> MesSession | None:
        key = name.lower().replace(" ", "_")
        for s in self.sessions:
            if s.name == key or s.name.replace("_", "") == key.replace("_", ""):
                return s
        # aliases
        if key in ("ny", "newyork", "new_york"):
            return next((s for s in self.sessions if s.name == "new_york"), None)
        if key == "london":
            return next((s for s in self.sessions if s.name == "london"), None)
        return None


def _session_from_raw(name: str, raw: dict[str, Any]) -> MesSession:
    or_raw = raw.get("opening_range") or {}
    rt_raw = raw.get("retest") or {}
    return MesSession(
        name=name,
        or_start=_parse_hhmm(str(raw.get("or_start", "09:30"))),
        or_end=_parse_hhmm(str(raw.get("or_end", "09:35"))),
        search_end=_parse_hhmm(str(raw.get("search_end", "15:00"))),
        force_flat=_parse_hhmm(str(raw.get("force_flat", "15:55"))),
        opening_range=OpeningRangeFilter(
            min_range_points=float(or_raw.get("min_range_points", 0.75)),
            max_range_points=float(or_raw.get("max_range_points", 6.0)),
        ),
        retest=RetestConfig(
            tolerance_ticks=int(rt_raw.get("tolerance_ticks", 2)),
            timeout_bars=int(rt_raw.get("timeout_bars", 12)),
            require_rejection_candle=bool(rt_raw.get("require_rejection_candle", True)),
        ),
    )


@lru_cache
def load_mes_5orb_config() -> Mes5OrbConfig:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    sess_raw = raw.get("sessions") or {}
    sessions: list[MesSession] = []
    for name in ("london", "new_york"):
        if name in sess_raw:
            sessions.append(_session_from_raw(name, sess_raw[name]))
        elif name == "new_york" and "newyork" in sess_raw:
            sessions.append(_session_from_raw("new_york", sess_raw["newyork"]))
    if not sessions:
        sessions = [
            _session_from_raw(
                "london",
                {
                    "or_start": "03:00",
                    "or_end": "03:05",
                    "search_end": "08:00",
                    "force_flat": "08:00",
                },
            ),
            _session_from_raw(
                "new_york",
                {
                    "or_start": "09:30",
                    "or_end": "09:35",
                    "search_end": "15:00",
                    "force_flat": "15:55",
                },
            ),
        ]
    trail = raw.get("trailing_stop") or {}
    risk = raw.get("risk") or {}
    return Mes5OrbConfig(
        symbol=str(raw.get("symbol", "MES")).upper(),
        point_value=float(raw.get("point_value", 5.0)),
        tick_size=float(raw.get("tick_size", 0.25)),
        timezone=str(raw.get("timezone", "America/New_York")),
        sessions=tuple(sessions),
        trailing_stop=TrailingStopConfig(
            method=str(trail.get("method", "swing_low")),
            pivot_lag_bars=int(trail.get("pivot_lag_bars", 2)),
            buffer_ticks=int(trail.get("buffer_ticks", 1)),
        ),
        risk=MesRiskConfig(
            contracts=max(int(risk.get("contracts", 1)), 1),
            max_concurrent=max(int(risk.get("max_concurrent", 1)), 1),
        ),
    )


def clear_mes_5orb_config_cache() -> None:
    load_mes_5orb_config.cache_clear()
