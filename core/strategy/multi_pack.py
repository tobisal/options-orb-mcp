"""Multi-signal day pack (edge discovery 2026-09-24).

Causal stack ranked best on Barchart walk-forward for *week-hit rate*
(still far below an 80% × 5% goal):

1. 5-minute NY ORB (first break, light filters)
2. Overnight gap fade (≥0.3%) into 11:00 ET
3. Power-hour 15:00 ET break (±0.8 pts) without gamma gate

Engine uses this when ``ENTRY_STRATEGY=multi_pack`` or weekly hunter
``use_multi_pack`` is true. One evaluation path returns the first active
signal in session order (gap → ORB → power-hour) so AutoTrader can place
sequentially across the day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal

import pytz

from core.models import Bar, Direction, ORBSignal, Regime, SessionWindow
from core.sessions import EASTERN, get_window_config, group_by_session, opening_range_of
from core.strategy.orb import average_true_range
from core.timeutils import utcnow

SignalKind = Literal["gap_fade", "orb_5m", "power_hour"]


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


@dataclass(frozen=True)
class MultiPackConfig:
    gap_min_pct: float = 0.003
    orb_minutes: int = 5
    orb_buffer_atr: float = 0.02
    power_break: float = 0.8
    target_r: float = 2.0
    stop_r: float = 1.0


def _rth_day_bars(bars: list[Bar], day) -> list[Bar]:
    out = []
    for b in bars:
        et = _to_et(b.ts)
        if et.date() == day and time(9, 30) <= et.time() < time(16, 0):
            out.append(b)
    return out


def _signal(
    symbol: str,
    window: SessionWindow,
    as_of: datetime,
    *,
    kind: SignalKind,
    direction: Direction,
    last: float,
    hi: float,
    lo: float,
    notes: str,
) -> ORBSignal:
    return ORBSignal(
        symbol=symbol,
        window=window,
        as_of=as_of,
        range_high=round(hi, 4),
        range_low=round(lo, 4),
        last_price=round(last, 4),
        direction=direction,
        breakout=direction is not Direction.NEUTRAL,
        strength=1.0 if direction is not Direction.NEUTRAL else 0.0,
        regime=Regime.TREND,
        notes=f"multi_pack:{kind} {notes}",
    )


def compute_multi_pack_signal(
    symbol: str,
    bars: list[Bar],
    *,
    as_of: datetime | None = None,
    cfg: MultiPackConfig | None = None,
    prefer: SignalKind | None = None,
) -> ORBSignal:
    """Return the pack signal that matches the current clock (causal)."""
    cfg = cfg or MultiPackConfig()
    as_of = as_of or (bars[-1].ts if bars else utcnow())
    et = _to_et(as_of)
    day = et.date()
    day_bars = _rth_day_bars(bars, day)
    empty = _signal(
        symbol,
        SessionWindow.NEW_YORK,
        as_of,
        kind="orb_5m",
        direction=Direction.NEUTRAL,
        last=bars[-1].close if bars else 0.0,
        hi=0.0,
        lo=0.0,
        notes="idle",
    )
    if len(day_bars) < 3:
        return empty.model_copy(update={"notes": "multi_pack: insufficient RTH bars"})

    # --- Gap fade: active 09:35–11:00 ET ---
    if prefer in (None, "gap_fade") and time(9, 35) <= et.time() < time(11, 0):
        # prior RTH day
        prev_day = day
        from datetime import timedelta

        for _ in range(1, 6):
            prev_day = prev_day - timedelta(days=1)
            prev = _rth_day_bars(bars, prev_day)
            if prev:
                break
        else:
            prev = []
        if prev:
            gap = (day_bars[0].open - prev[-1].close) / prev[-1].close
            if abs(gap) >= cfg.gap_min_pct:
                direction = Direction.SHORT if gap > 0 else Direction.LONG
                last = day_bars[-1].close
                return _signal(
                    symbol,
                    SessionWindow.NEW_YORK,
                    as_of,
                    kind="gap_fade",
                    direction=direction,
                    last=last,
                    hi=day_bars[0].open,
                    lo=prev[-1].close,
                    notes=f"gap={gap:.3%} fade",
                )

    # --- 5m ORB: after range complete until 12:00 ---
    if prefer in (None, "orb_5m") and time(9, 35) <= et.time() < time(12, 0):
        win_cfg = get_window_config(SessionWindow.NEW_YORK)
        # use today's in-window bars only
        sessions = group_by_session(day_bars, win_cfg)
        if sessions:
            _, sbars = sessions[-1]
            orb, post = opening_range_of(sbars, cfg.orb_minutes)
            if orb and post:
                rh, rl = max(b.high for b in orb), min(b.low for b in orb)
                atr = average_true_range(day_bars) or (rh - rl)
                buf = cfg.orb_buffer_atr * atr
                last = day_bars[-1].close
                direction = Direction.NEUTRAL
                if last > rh + buf:
                    direction = Direction.LONG
                elif last < rl - buf:
                    direction = Direction.SHORT
                if direction is not Direction.NEUTRAL:
                    return _signal(
                        symbol,
                        SessionWindow.NEW_YORK,
                        as_of,
                        kind="orb_5m",
                        direction=direction,
                        last=last,
                        hi=rh + buf,
                        lo=rl - buf,
                        notes=f"OR{cfg.orb_minutes}m break",
                    )

    # --- Power hour after 15:00 ---
    if prefer in (None, "power_hour") and time(15, 0) <= et.time() < time(16, 0):
        post = [b for b in day_bars if _to_et(b.ts).time() >= time(15, 0)]
        if post:
            anchor = post[0].open
            last = day_bars[-1].close
            direction = Direction.NEUTRAL
            if last >= anchor + cfg.power_break:
                direction = Direction.LONG
            elif last <= anchor - cfg.power_break:
                direction = Direction.SHORT
            if direction is not Direction.NEUTRAL:
                return _signal(
                    symbol,
                    SessionWindow.NEW_YORK,
                    as_of,
                    kind="power_hour",
                    direction=direction,
                    last=last,
                    hi=anchor + cfg.power_break,
                    lo=anchor - cfg.power_break,
                    notes=f"anchor={anchor:.2f}",
                )

    return empty.model_copy(update={"notes": "multi_pack: no active leg right now"})
