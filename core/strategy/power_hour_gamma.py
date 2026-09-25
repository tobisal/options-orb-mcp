"""3 PM / power-hour gamma breakout (etalon.capital-style).

Rules distilled from https://www.instagram.com/reel/DdpjuNKALLS/ :

1. At 15:00 US/Eastern, mark the spot (``anchor``).
2. Prefer days when **0DTE dealer gamma is negative** (amplifies the last hour;
   their study: ~52 Nasdaq pts typical vs ~40 in positive gamma — size, not side).
3. When price breaks ``break_points`` beyond the anchor, go **with** the move.
4. Stop ``stop_points`` back from entry; target ``target_points`` (video: 40 / 10 / 20
   on Nasdaq futures). ETF defaults are NQ-scaled in ``configs/power_hour_gamma.json``.

Gamma without a full GEX feed uses a transparent proxy (pin near open/VWAP +
short-dated ATM gamma). Set ``require_negative_gamma: false`` to trade every
break, or supply ``gamma_negative`` from an external indicator later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from functools import lru_cache
from typing import Any

import pytz

from core.config import REPO_ROOT
from core.models import Bar, Direction, OptionRight, ORBSignal, Regime, SessionWindow
from core.pricing import greeks
from core.sessions import EASTERN
from core.strategy.orb import average_true_range, session_vwap
from core.timeutils import utcnow

_CONFIG_PATH = REPO_ROOT / "configs" / "power_hour_gamma.json"


@dataclass(frozen=True)
class PowerHourConfig:
    enabled: bool = True
    anchor_time_et: time = time(15, 0)
    session_end_et: time = time(16, 0)
    break_points: float = 40.0
    stop_points: float = 10.0
    target_points: float = 20.0
    require_negative_gamma: bool = True
    # When True, treat "pinned" near open/VWAP as negative-gamma proxy.
    use_gamma_proxy: bool = True
    pin_pct: float = 0.004  # 0.4% of spot
    notes: str = ""

    @property
    def target_r(self) -> float:
        return self.target_points / self.stop_points if self.stop_points > 0 else 2.0

    @property
    def stop_r(self) -> float:
        return 1.0


def _parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")[:2]
    return time(int(hh), int(mm))


@lru_cache
def load_power_hour_config(symbol: str | None = None) -> PowerHourConfig:
    raw: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    base = {k: v for k, v in raw.items() if not k.startswith("_") and k != "symbols"}
    sym = (symbol or "").upper()
    overrides = (raw.get("symbols") or {}).get(sym) or {}
    merged = {**base, **overrides}

    return PowerHourConfig(
        enabled=bool(merged.get("enabled", True)),
        anchor_time_et=_parse_hhmm(str(merged.get("anchor_time_et", "15:00"))),
        session_end_et=_parse_hhmm(str(merged.get("session_end_et", "16:00"))),
        break_points=float(merged.get("break_points", 40.0)),
        stop_points=float(merged.get("stop_points", 10.0)),
        target_points=float(merged.get("target_points", 20.0)),
        require_negative_gamma=bool(merged.get("require_negative_gamma", True)),
        use_gamma_proxy=bool(merged.get("use_gamma_proxy", True)),
        pin_pct=float(merged.get("pin_pct", 0.004)),
        notes=str(merged.get("notes") or ""),
    )


def clear_power_hour_config_cache() -> None:
    load_power_hour_config.cache_clear()


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


def _rth_bars_for_day(bars: list[Bar], day_et) -> list[Bar]:
    """Regular NYSE cash session bars (09:30–16:00 ET) for an Eastern date."""
    out: list[Bar] = []
    for b in bars:
        et = _to_et(b.ts)
        if et.date() != day_et:
            continue
        t = et.time()
        if time(9, 30) <= t < time(16, 0):
            out.append(b)
    return out


def _bar_at_or_after(bars: list[Bar], target: time) -> Bar | None:
    for b in bars:
        if _to_et(b.ts).time() >= target:
            return b
    return None


def estimate_negative_gamma(
    *,
    spot: float,
    day_bars: list[Bar],
    cfg: PowerHourConfig,
    iv: float | None = None,
    gamma_negative: bool | None = None,
) -> tuple[bool, str]:
    """Return (is_negative, reason).

    Priority:
    1. Explicit ``gamma_negative`` from caller / external GEX feed.
    2. Proxy: price pinned near day-open or session VWAP (dealers defending a pin)
       plus elevated short-dated ATM gamma when ``iv`` is provided.
    """
    if gamma_negative is not None:
        return bool(gamma_negative), "external_gex"
    if not cfg.use_gamma_proxy:
        return True, "proxy_disabled_pass"

    if not day_bars:
        return False, "no_day_bars"

    day_open = day_bars[0].open
    vwap = session_vwap(day_bars)
    pin_ref = vwap if vwap is not None else day_open
    pin_tol = max(cfg.pin_pct * spot, 1e-6)
    pinned = abs(spot - day_open) <= pin_tol or abs(spot - pin_ref) <= pin_tol

    gamma_elevated = True
    if iv is not None and iv > 0 and spot > 0:
        # 0DTE-ish: ~1 trading hour left → t ≈ 1/252/6.5
        t = max(1.0 / (252.0 * 6.5), 1e-6)
        g = greeks(spot, spot, t, iv, OptionRight.CALL).gamma
        # High gamma near expiry is the regime the reel cares about.
        gamma_elevated = g * spot >= 0.02

    if pinned and gamma_elevated:
        return True, "proxy_pin+atm_gamma"
    if pinned:
        return True, "proxy_pin"
    return False, "proxy_not_pinned"


def compute_power_hour_signal(
    symbol: str,
    bars: list[Bar],
    *,
    cfg: PowerHourConfig | None = None,
    as_of: datetime | None = None,
    iv: float | None = None,
    gamma_negative: bool | None = None,
    window: SessionWindow = SessionWindow.NEW_YORK,
) -> ORBSignal:
    """Compute the power-hour breakout signal (reuses :class:`ORBSignal` shape)."""
    cfg = cfg or load_power_hour_config(symbol)
    as_of = as_of or (bars[-1].ts if bars else utcnow())
    et = _to_et(as_of)
    day_bars = _rth_bars_for_day(bars, et.date())

    empty = ORBSignal(
        symbol=symbol,
        window=window,
        as_of=as_of,
        range_high=0.0,
        range_low=0.0,
        last_price=bars[-1].close if bars else 0.0,
        direction=Direction.NEUTRAL,
        breakout=False,
        regime=Regime.TREND,
        notes="power_hour_gamma: no RTH bars",
    )
    if not cfg.enabled:
        return empty.model_copy(update={"notes": "power_hour_gamma: disabled"})
    if not day_bars:
        return empty

    # Only active from anchor time until cash close.
    if et.time() < cfg.anchor_time_et:
        return empty.model_copy(
            update={
                "last_price": day_bars[-1].close,
                "notes": f"power_hour_gamma: before {cfg.anchor_time_et.strftime('%H:%M')} ET",
            }
        )
    if et.time() >= cfg.session_end_et:
        return empty.model_copy(
            update={
                "last_price": day_bars[-1].close,
                "notes": "power_hour_gamma: session ended",
            }
        )

    anchor_bar = _bar_at_or_after(day_bars, cfg.anchor_time_et)
    if anchor_bar is None:
        return empty.model_copy(update={"notes": "power_hour_gamma: no 15:00 bar yet"})

    anchor = anchor_bar.open if _to_et(anchor_bar.ts).time() == cfg.anchor_time_et else anchor_bar.close
    # Prefer the first bar that opens at/after 15:00 — use its open as 3 PM mark.
    for b in day_bars:
        if _to_et(b.ts).time() >= cfg.anchor_time_et:
            anchor = b.open
            anchor_bar = b
            break

    last = day_bars[-1]
    spot = last.close
    up_level = anchor + cfg.break_points
    down_level = anchor - cfg.break_points
    atr = average_true_range(day_bars)

    # Gamma regime is judged at the 3 PM mark (before the break), not after.
    bars_to_anchor = [b for b in day_bars if b.ts <= anchor_bar.ts]
    neg, gamma_reason = estimate_negative_gamma(
        spot=anchor,
        day_bars=bars_to_anchor or day_bars,
        cfg=cfg,
        iv=iv,
        gamma_negative=gamma_negative,
    )
    if cfg.require_negative_gamma and not neg:
        return ORBSignal(
            symbol=symbol,
            window=window,
            as_of=as_of,
            range_high=round(up_level, 4),
            range_low=round(down_level, 4),
            last_price=round(spot, 4),
            direction=Direction.NEUTRAL,
            breakout=False,
            strength=0.0,
            regime=Regime.TREND,
            atr=round(atr, 4),
            notes=(
                f"power_hour_gamma: gamma not negative ({gamma_reason}); "
                f"anchor={anchor:.2f} wait ±{cfg.break_points}"
            ),
        )

    direction = Direction.NEUTRAL
    breakout = False
    strength = 0.0
    if spot >= up_level:
        direction = Direction.LONG
        breakout = True
        strength = (spot - anchor) / cfg.break_points
    elif spot <= down_level:
        direction = Direction.SHORT
        breakout = True
        strength = (anchor - spot) / cfg.break_points

    notes = (
        f"power_hour_gamma anchor={anchor:.2f} break=±{cfg.break_points} "
        f"stop={cfg.stop_points} target={cfg.target_points} "
        f"gamma={gamma_reason} last={spot:.2f}"
    )
    return ORBSignal(
        symbol=symbol,
        window=window,
        as_of=as_of,
        range_high=round(up_level, 4),
        range_low=round(down_level, 4),
        last_price=round(spot, 4),
        direction=direction,
        breakout=breakout,
        strength=round(strength, 4),
        regime=Regime.TREND,  # always directional debit verticals
        atr=round(atr, 4),
        notes=notes,
    )


def power_hour_active_now(now: datetime | None = None, cfg: PowerHourConfig | None = None) -> bool:
    """True during the post-3pm ET watch window."""
    cfg = cfg or load_power_hour_config()
    et = _to_et(now or utcnow())
    return cfg.enabled and cfg.anchor_time_et <= et.time() < cfg.session_end_et
