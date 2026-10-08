"""A sensitive row carries its retention window at write.

A row's sensitivity is graded at write (`''`, `medium`, `high`), and an
expiring row has a post-action (`tombstone`, `demote`, `anonymize`). This joins
the two: without it a `medium` row carries no `expires_at` and no `on_expire`,
so a contact detail admitted for a task stays readable for good.

`LOOM_MEMORY_SENSITIVE_RETENTION_DAYS` (default 0, off) stamps a window on
`medium` and `high` rows at write, on the set and bulk paths alike. The
caller's own expiry wins when it is shorter; a longer one is pulled in to
the window. `on_expire` stays the caller's when given, else
`LOOM_MEMORY_SENSITIVE_ON_EXPIRE` (default `anonymize`: the value becomes a
marker and the entities go, the row stays as a record that something was
there). The expiry sweep does the rest.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

WINDOWED_TIERS: frozenset[str] = frozenset({"medium", "high"})


def sensitive_retention(
    *,
    sensitivity: str,
    expires_at: datetime | None,
    on_expire: str,
    now: datetime,
    settings: Any,
) -> tuple[datetime | None, str, bool]:
    """(expires_at, on_expire, windowed) for one row about to be written.

    `windowed` is True when this call changed either value. `now` sets the
    timezone convention: the window is `now + days` in `now`'s convention,
    and an `expires_at` in the other convention is compared on the wall
    clock, not rejected."""
    days = int(settings.memory_sensitive_retention_days)
    if days <= 0 or str(sensitivity or "") not in WINDOWED_TIERS:
        return expires_at, on_expire, False
    window = now + timedelta(days=days)
    new_expires = expires_at
    if expires_at is None or _later(expires_at, window):
        new_expires = window
    default_on = str(settings.memory_sensitive_on_expire)
    new_on = on_expire or default_on
    changed = (new_expires is not expires_at) or (new_on != on_expire)
    return new_expires, new_on, changed


def _later(a: datetime, b: datetime) -> bool:
    """a > b on the wall clock, tolerant of one side being tz-naive (UTC)."""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=None) if a.tzinfo is not None else a
        b = b.replace(tzinfo=None) if b.tzinfo is not None else b
    return a > b
