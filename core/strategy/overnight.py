"""Overnight long edge (non-ORB).

Buy near the cash close, hold the overnight gap, flatten after the next open.

On Barchart SPY 5m (vertical approx, 2× contracts) this was the strongest
*non-ORB* walk-forward candidate found in discovery: positive OOS weekly mean
with higher P(week≥5%) than filtered ORB. Still nowhere near 80%×5% weeks —
that remains blocked by SPY's own weekly distribution — but it is a real,
documented equity overnight drift adapted to the debit-vertical pipeline.

Live behaviour:
- Signal active in New York from ``entry_after_et`` (default 15:45) until close.
- Always long (TREND debit call vertical).
- Flatten after next session open once ``flatten_after_et`` (default 10:00) on
  the following RTH day (handled by short forward window in backtests; live
  via session settle / next-day signal expiry notes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import lru_cache
from typing import Any

import pytz

from core.config import REPO_ROOT
from core.models import Bar, Direction, ORBSignal, Regime, SessionWindow
from core.sessions import EASTERN
from core.timeutils import utcnow

_CONFIG_PATH = REPO_ROOT / "configs" / "overnight.json"


@dataclass(frozen=True)
class OvernightConfig:
    enabled: bool = True
    entry_after_et: time = time(15, 45)
    session_end_et: time = time(16, 0)
    flatten_after_et: time = time(10, 0)
    # Optional filter: only after a red cash day (close < open).
    require_red_day: bool = False
    target_r: float = 1.0
    stop_r: float = 1.0
    use_trailing_stop: bool = True
    trail_activate_r: float = 0.5
    trail_distance_r: float = 0.3


def _parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")[:2]
    return time(int(hh), int(mm))


@lru_cache
def load_overnight_config() -> OvernightConfig:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    return OvernightConfig(
        enabled=bool(raw.get("enabled", True)),
        entry_after_et=_parse_hhmm(str(raw.get("entry_after_et", "15:45"))),
        session_end_et=_parse_hhmm(str(raw.get("session_end_et", "16:00"))),
        flatten_after_et=_parse_hhmm(str(raw.get("flatten_after_et", "10:00"))),
        require_red_day=bool(raw.get("require_red_day", False)),
        target_r=float(raw.get("target_r", 1.0)),
        stop_r=float(raw.get("stop_r", 1.0)),
        use_trailing_stop=bool(raw.get("use_trailing_stop", True)),
        trail_activate_r=float(raw.get("trail_activate_r", 0.5)),
        trail_distance_r=float(raw.get("trail_distance_r", 0.3)),
    )


def clear_overnight_config_cache() -> None:
    load_overnight_config.cache_clear()


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


def _rth_bars(bars: list[Bar], day) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        et = _to_et(b.ts)
        if et.date() == day and time(9, 30) <= et.time() < time(16, 0):
            out.append(b)
    return out


def overnight_entry_window_active(
    now: datetime | None = None, cfg: OvernightConfig | None = None
) -> bool:
    cfg = cfg or load_overnight_config()
    et = _to_et(now or utcnow())
    return cfg.enabled and cfg.entry_after_et <= et.time() < cfg.session_end_et


def compute_overnight_signal(
    symbol: str,
    bars: list[Bar],
    *,
    cfg: OvernightConfig | None = None,
    as_of: datetime | None = None,
) -> ORBSignal:
    cfg = cfg or load_overnight_config()
    as_of = as_of or (bars[-1].ts if bars else utcnow())
    et = _to_et(as_of)
    last = bars[-1].close if bars else 0.0
    empty = ORBSignal(
        symbol=symbol,
        window=SessionWindow.NEW_YORK,
        as_of=as_of,
        range_high=0.0,
        range_low=0.0,
        last_price=last,
        direction=Direction.NEUTRAL,
        breakout=False,
        regime=Regime.TREND,
        notes="overnight: idle",
    )
    if not cfg.enabled:
        return empty.model_copy(update={"notes": "overnight: disabled"})
    if not (cfg.entry_after_et <= et.time() < cfg.session_end_et):
        return empty.model_copy(
            update={
                "notes": (
                    f"overnight: outside entry window "
                    f"{cfg.entry_after_et.strftime('%H:%M')}-"
                    f"{cfg.session_end_et.strftime('%H:%M')} ET"
                )
            }
        )

    day_bars = _rth_bars(bars, et.date())
    if len(day_bars) < 5:
        return empty.model_copy(update={"notes": "overnight: thin RTH session"})

    if cfg.require_red_day and day_bars[-1].close >= day_bars[0].open:
        return empty.model_copy(update={"notes": "overnight: skip green day"})

    spot = day_bars[-1].close
    return ORBSignal(
        symbol=symbol,
        window=SessionWindow.NEW_YORK,
        as_of=as_of,
        range_high=round(spot * 1.01, 4),
        range_low=round(spot * 0.99, 4),
        last_price=round(spot, 4),
        direction=Direction.LONG,
        breakout=True,
        strength=1.0,
        regime=Regime.TREND,
        notes=(
            f"overnight long into close; flatten after "
            f"{cfg.flatten_after_et.strftime('%H:%M')} ET next day"
        ),
    )
