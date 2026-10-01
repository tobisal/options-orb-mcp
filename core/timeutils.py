"""Time helpers.

``datetime.utcnow()`` is deprecated. The codebase uses *naive* UTC datetimes
throughout (compared against naive bar timestamps and stored as naive ISO
strings), so this helper returns a naive UTC ``datetime`` - a drop-in
replacement that keeps the existing semantics without the deprecation warning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Current UTC time as a naive ``datetime`` (no tzinfo)."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_naive_utc(ts: datetime) -> datetime:
    """Return ``ts`` as naive UTC so it compares with :func:`utcnow`.

    IBKR (``formatDate=2``) often yields timezone-aware UTC datetimes; the rest
    of this codebase stores and compares naive UTC.
    """
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def market_data_lag() -> timedelta:
    """IBKR delayed-feed lag used in paper/test (0 when live).

    Free delayed CME data is typically ~10 minutes behind. Session clocks and
    force-flat must follow the data clock, not wall clock, or paper decisions
    fire a session early.
    """
    from core.config import get_settings

    settings = get_settings()
    if settings.is_live:
        return timedelta(0)
    minutes = float(getattr(settings, "market_data_lag_minutes", 0) or 0)
    if minutes <= 0:
        return timedelta(0)
    return timedelta(minutes=minutes)


def market_now() -> datetime:
    """Wall clock minus paper/test market-data lag (naive UTC)."""
    return utcnow() - market_data_lag()

