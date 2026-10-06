"""Timestamp helpers. All persisted timestamps are ISO-8601 UTC strings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return to_iso(utcnow())


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """An aware datetime from ISO-8601 text; a value without a zone is taken as UTC.

    Contract:
        Silences:
            - Empty, unparseable or non-string values give ``None`` (``TypeError`` and
              ``ValueError`` from ``fromisoformat`` are caught). Callers such as
              :func:`older_than` treat ``None`` as "very old".
    """
    if not value:
        return None
    if isinstance(value, str) and value.endswith("Z"):
        value = value[:-1] + "+00:00"  # Python 3.10's fromisoformat rejects "Z"; 3.11+ accepts it
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def older_than(value: Optional[str], delta: timedelta, now: Optional[datetime] = None) -> bool:
    """True when ``value`` is missing/unparseable or older than ``delta``."""
    parsed = parse_iso(value)
    if parsed is None:
        return True
    return (now or utcnow()) - parsed > delta


def seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 1)
