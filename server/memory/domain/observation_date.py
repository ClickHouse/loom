"""The observation date a write is anchored to when the caller sends none.

`valid_at` — the bitemporal "true from" of a memory — is derived from the
request's `observation_date` (the resolver wins, else the session date).
Without one, every fact in the call would be left with `valid_at = NULL`: as-of
reads, the date-proximity boost and the chronology sort cannot see the row, and
relative phrases in the text ("yesterday") have nothing to resolve against.

Live agents rarely send `observation_date`. For a live agent the request time
*is* the conversation time, so that is the default. Benches and imports that
know a better date keep sending it and win.
"""

from __future__ import annotations

from datetime import UTC, datetime

REQUEST_TIME = "request_time"


def resolve_observation_date(
    raw: str | None,
    *,
    default: str = REQUEST_TIME,
    now: datetime | None = None,
) -> tuple[str | None, str]:
    """`(observation_date, source)` for a write.

    The caller's value wins whenever it is non-blank (`source="caller"`).
    Otherwise `default="request_time"` yields today's UTC date (`source=
    "request_time"`); `default="none"` yields None (`source=""`), leaving
    the row undated. Date-only, because the extraction prompt and the
    digest header print it as a session date and the resolver parses either.
    """
    text = (raw or "").strip()
    if text:
        return text, "caller"
    if (default or "").strip().lower() == REQUEST_TIME:
        stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
        return stamp, REQUEST_TIME
    return None, ""
