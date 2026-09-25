"""Weekly return hunter — aspirational sizing + day filters.

Target: ~5% of capital per calendar week. That is an aggressive goal
(~12x a strong annual return if sustained). This module does not invent
edge; it stacks the best surviving signals and sizes harder when the week
is behind target, with hard daily/weekly kill switches.

Empirical basis in this repo (non-ORB discovery on Barchart SPY 5m):
- Overnight long (buy near cash close → flatten next morning) was the
  strongest walk-forward OOS non-ORB candidate for P(week≥5%).
- Filtered NY ORB remains available but is disabled in the default playbook
  when ``use_overnight`` is true.
- Tokyo range stays off (no SPY Tokyo-hour history).
- Trade days default Mon/Wed/Fri.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import REPO_ROOT, get_settings
from core.db import Database
from core.timeutils import utcnow

_CONFIG_PATH = REPO_ROOT / "configs" / "weekly_hunter.json"


@dataclass(frozen=True)
class WeeklyHunterConfig:
    enabled: bool = False
    weekly_return_target: float = 0.05
    # ISO weekdays: Mon=0 … Sun=6. Default MWF.
    trade_weekdays: tuple[int, ...] = (0, 2, 4)
    symbols: tuple[str, ...] = ("SPY", "QQQ")
    # Hard contract floor at starting capital (scales up with equity in RiskManager).
    contracts_scale: int = 2
    # Base risk as fraction of **current equity**; may be boosted when behind.
    base_risk_per_trade: float = 0.10
    max_risk_per_trade: float = 0.10
    max_daily_loss: float = 0.12
    # When week P&L already >= target, cut size.
    after_target_risk_scale: float = 0.5
    # Max boost vs base when far behind (1.0 = no boost; use with contracts_scale).
    max_behind_boost: float = 1.0
    enable_power_hour: bool = False
    enable_tokyo_range: bool = False
    enable_orb_filters: bool = False
    use_multi_pack: bool = False
    # Non-ORB overnight long (configs/overnight.json) — discovery winner.
    use_overnight: bool = True
    # NY filtered winner from Barchart filter WF (unused when use_overnight).
    new_york_params: dict[str, Any] | None = None
    london_params: dict[str, Any] | None = None


def _parse_weekdays(raw: list | tuple | None) -> tuple[int, ...]:
    if not raw:
        return (0, 2, 4)
    out: list[int] = []
    for x in raw:
        if isinstance(x, int):
            out.append(x)
        else:
            name = str(x).strip().lower()[:3]
            out.append({"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}[name])
    return tuple(out)


@lru_cache
def load_weekly_hunter_config() -> WeeklyHunterConfig:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    return WeeklyHunterConfig(
        enabled=bool(raw.get("enabled", False)),
        weekly_return_target=float(raw.get("weekly_return_target", 0.05)),
        trade_weekdays=_parse_weekdays(raw.get("trade_weekdays")),
        symbols=tuple(str(s).upper() for s in (raw.get("symbols") or ["SPY", "QQQ"])),
        contracts_scale=max(int(raw.get("contracts_scale", 2)), 1),
        base_risk_per_trade=float(raw.get("base_risk_per_trade", 0.10)),
        max_risk_per_trade=float(raw.get("max_risk_per_trade", 0.10)),
        max_daily_loss=float(raw.get("max_daily_loss", 0.12)),
        after_target_risk_scale=float(raw.get("after_target_risk_scale", 0.5)),
        max_behind_boost=float(raw.get("max_behind_boost", 1.0)),
        enable_power_hour=bool(raw.get("enable_power_hour", False)),
        enable_tokyo_range=bool(raw.get("enable_tokyo_range", False)),
        enable_orb_filters=bool(raw.get("enable_orb_filters", False)),
        use_multi_pack=bool(raw.get("use_multi_pack", False)),
        use_overnight=bool(raw.get("use_overnight", True)),
        new_york_params=dict(raw.get("new_york_params") or {}),
        london_params=dict(raw.get("london_params") or {}) or None,
    )


def clear_weekly_hunter_cache() -> None:
    load_weekly_hunter_config.cache_clear()


def week_start_utc(now: datetime | None = None) -> datetime:
    """Monday 00:00 UTC of the current ISO week."""
    now = now or utcnow()
    d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def is_trade_weekday(now: datetime | None = None, cfg: WeeklyHunterConfig | None = None) -> bool:
    cfg = cfg or load_weekly_hunter_config()
    now = now or utcnow()
    return now.weekday() in cfg.trade_weekdays


def mwf_days_left_including_today(now: datetime | None = None, cfg: WeeklyHunterConfig | None = None) -> int:
    """Count remaining configured weekdays from today through Friday."""
    cfg = cfg or load_weekly_hunter_config()
    now = now or utcnow()
    # Look through Sunday of this week.
    end = week_start_utc(now) + timedelta(days=6)
    cur = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    while cur <= end:
        if cur.weekday() in cfg.trade_weekdays and cur.weekday() >= now.weekday():
            if cur.date() >= now.date():
                n += 1
        cur += timedelta(days=1)
    return max(n, 1)


@dataclass
class WeekStatus:
    week_pnl: float
    target_pnl: float
    remaining: float
    hit_target: bool
    risk_fraction: float
    days_left: int
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "week_pnl": round(self.week_pnl, 2),
            "target_pnl": round(self.target_pnl, 2),
            "remaining": round(self.remaining, 2),
            "hit_target": self.hit_target,
            "risk_fraction": round(self.risk_fraction, 4),
            "days_left": self.days_left,
            "notes": self.notes,
        }


def week_status(db: Database | None = None, cfg: WeeklyHunterConfig | None = None) -> WeekStatus:
    settings = get_settings()
    cfg = cfg or load_weekly_hunter_config()
    db = db or Database()
    # Size the weekly target off current equity so goals scale with the book.
    realised = db.realised_pnl(environment=settings.trading_environment())
    capital = max(settings.starting_capital + realised, settings.starting_capital * 0.05, 1.0)
    target = capital * cfg.weekly_return_target
    since = week_start_utc()
    week_pnl = db.realised_pnl_since(since, environment=settings.trading_environment())
    remaining = target - week_pnl
    days_left = mwf_days_left_including_today(cfg=cfg)
    hit = week_pnl >= target

    if not cfg.enabled:
        frac = settings.max_risk_per_trade
        note = "weekly_hunter off — using default risk"
    elif hit:
        frac = min(cfg.base_risk_per_trade * cfg.after_target_risk_scale, cfg.max_risk_per_trade)
        note = f"week target hit — size cut (cap scales with equity)"
    else:
        # No behind-target boost; contracts_scale grows with equity in RiskManager.
        boost = 1.0 if cfg.max_behind_boost <= 1.0 else (
            1.0 + max(remaining / target, 0.0) * (cfg.max_behind_boost - 1.0)
        )
        frac = min(cfg.base_risk_per_trade * boost, cfg.max_risk_per_trade)
        note = f"10% of equity £{capital:.0f}; scale floor {cfg.contracts_scale}× at start"

    return WeekStatus(
        week_pnl=week_pnl,
        target_pnl=target,
        remaining=remaining,
        hit_target=hit,
        risk_fraction=frac,
        days_left=days_left,
        notes=note,
    )


def apply_hunter_params_to_db(db: Database | None = None) -> dict[str, Any]:
    """Write NY (and optional London) active_strategy rows from the playbook.

    When ``use_overnight`` is on, NY/London/Asia ORB params are cleared so the
    engine does not fall through to opening-range rules.
    """
    from core.models import SessionWindow

    cfg = load_weekly_hunter_config()
    db = db or Database()
    applied: dict[str, Any] = {"entry": "overnight" if cfg.use_overnight else "orb"}
    for symbol in cfg.symbols:
        if cfg.use_overnight:
            db.clear_active_strategy(symbol, SessionWindow.NEW_YORK)
            db.clear_active_strategy(symbol, SessionWindow.LONDON)
            db.clear_active_strategy(symbol, SessionWindow.ASIA)
            applied[f"{symbol}:new_york"] = None
            applied[f"{symbol}:london"] = None
            applied[f"{symbol}:asia"] = None
            continue
        if cfg.new_york_params:
            params = dict(cfg.new_york_params)
            if cfg.enable_orb_filters:
                params.setdefault("require_vwap_align", True)
                params.setdefault("require_trend_regime", True)
                params.setdefault("volume_confirm_mult", 1.0)
            db.set_active_strategy(
                symbol,
                SessionWindow.NEW_YORK,
                params=params,
                label=f"weekly_hunter {symbol} new_york",
            )
            applied[f"{symbol}:new_york"] = params
        if cfg.london_params:
            db.set_active_strategy(
                symbol,
                SessionWindow.LONDON,
                params=dict(cfg.london_params),
                label=f"weekly_hunter {symbol} london",
            )
            applied[f"{symbol}:london"] = cfg.london_params
        else:
            db.clear_active_strategy(symbol, SessionWindow.LONDON)
            applied[f"{symbol}:london"] = None
        db.clear_active_strategy(symbol, SessionWindow.ASIA)
        applied[f"{symbol}:asia"] = None
    return applied
