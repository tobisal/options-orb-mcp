"""Time helpers.

``datetime.utcnow()`` is deprecated. The codebase uses *naive* UTC datetimes
throughout (compared against naive bar timestamps and stored as naive ISO
strings), so this helper returns a naive UTC ``datetime`` - a drop-in
replacement that keeps the existing semantics without the deprecation warning.
"""

from __future__ import annotations

from datetime import UTC, datetime


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
