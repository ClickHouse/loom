"""A bisect report's time window: absolute dates, relative phrases ("3 weeks ago")
and named spans, parsed into the since / until bounds the scan reads."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from server.core.datetimes import naive_utc_now_seconds

_RELATIVE_RE = re.compile(
    r"^(\d+)\s*(minute|min|hour|hr|day|week|month|year)s?\s+ago$", re.IGNORECASE
)
_UNIT_S = {
    "minute": 60,
    "min": 60,
    "hour": 3600,
    "hr": 3600,
    "day": 86400,
    "week": 604800,
    "month": 30 * 86400,
    "year": 365 * 86400,
}
_NAMED_S = {"now": 0, "today": 0, "yesterday": 86400, "last week": 604800, "last month": 30 * 86400}


def _relative_when(raw: str, now: datetime) -> datetime | None:
    """Relative times such as "7 days ago", "3 weeks ago", "yesterday", "last week", resolved
    against the request time (agents often try these before ISO)."""
    text = " ".join(raw.lower().split())
    if text in _NAMED_S:
        return now - timedelta(seconds=_NAMED_S[text])
    m = _RELATIVE_RE.match(text)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _UNIT_S[m.group(2).lower()])
    return None


def _parse_when(text: str, *, now: datetime | None = None) -> datetime | None:
    """ISO-8601 (with `Z` or an offset), `YYYY-MM-DD[ HH:MM[:SS]]`, or a
    relative time ("7 days ago", "yesterday"), as UTC-naive."""
    raw = (text or "").strip()
    if not raw:
        return None
    rel = _relative_when(raw, now or naive_utc_now_seconds())
    if rel is not None:
        return rel
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        from server.core.errors import ValidationError

        raise ValidationError(
            f"{raw!r} is not a time loom reads: ISO-8601 (2026-09-15T14:07:00Z), a date, "
            "or a relative time such as '7 days ago' or 'yesterday'",
            code="BAD_TIME",
        ) from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _iso(dt: datetime | None) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else ""
