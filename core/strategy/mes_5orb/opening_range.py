"""Opening-range extraction for MES 5ORB sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pytz

from core.models import Bar
from core.strategy.mes_5orb.sessions import MesSession

EASTERN = pytz.timezone("America/New_York")


def to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


@dataclass(frozen=True)
class OpeningRange:
    session_name: str
    day: date
    high: float
    low: float
    mid: float
    bar_count: int
    skipped: bool = False
    skip_reason: str = ""

    @property
    def width(self) -> float:
        return self.high - self.low


def opening_range_bars(bars: list[Bar], session: MesSession, day: date) -> list[Bar]:
    """Bars whose ET time falls in [or_start, or_end) on ``day``."""
    out: list[Bar] = []
    for b in bars:
        et = to_et(b.ts)
        if et.date() != day:
            continue
        t = et.time()
        if session.or_start <= t < session.or_end:
            out.append(b)
    return sorted(out, key=lambda x: x.ts)


def compute_opening_range(
    bars: list[Bar],
    session: MesSession,
    day: date,
) -> OpeningRange | None:
    """OR high/low for one session on one day, or None if no OR bars."""
    or_bars = opening_range_bars(bars, session, day)
    if not or_bars:
        return None
    high = max(b.high for b in or_bars)
    low = min(b.low for b in or_bars)
    width = high - low
    filt = session.opening_range
    skipped = False
    reason = ""
    if width < filt.min_range_points:
        skipped = True
        reason = f"OR width {width:.2f} < min {filt.min_range_points}"
    elif width > filt.max_range_points:
        skipped = True
        reason = f"OR width {width:.2f} > max {filt.max_range_points}"
    return OpeningRange(
        session_name=session.name,
        day=day,
        high=high,
        low=low,
        mid=(high + low) / 2.0,
        bar_count=len(or_bars),
        skipped=skipped,
        skip_reason=reason,
    )
