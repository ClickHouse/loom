"""Memory webhook events: the fixed event types and the body a receiver gets.

A body names what changed — the event, the memory's key, id and version, its
identity tuple, who changed it, when and in which request — and never what it
says: no value, tags text or evidence. The receiver fetches the row with its
own token, so what it may read is decided at read time, as for any other read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, get_args

EventType = Literal[
    "memory.written",
    "memory.revised",
    "memory.tombstoned",
    "memory.restored",
    "memory.redacted",
    "memory.erased",
]
EVENT_TYPES: tuple[EventType, ...] = get_args(EventType)

# Every key a body carries, in this order; a test holds the body to it.
BODY_FIELDS: tuple[str, ...] = (
    "type",
    "event_id",
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_id",
    "version",
    "actor",
    "request_id",
    "erasure_id",
    "occurred_at",
)

# The wait before the next try after the n-th failed attempt: 5 s, 30 s, 2 min,
# 10 min, 30 min, then an hour for every failure after that.
BACKOFF_SECONDS: tuple[int, ...] = (5, 30, 120, 600, 1800, 3600)


def backoff_seconds(attempts: int) -> int:
    """Seconds until the next try after `attempts` failed attempts (at least 1)."""
    return BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]


def _iso(ts: Any) -> str:
    """`ts` as ISO 8601 UTC with milliseconds and a Z."""
    if not isinstance(ts, datetime):
        return str(ts)
    when: datetime = ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo is not None else ts
    return when.isoformat(timespec="milliseconds") + "Z"


def event_body(row: Mapping[str, Any]) -> bytes:
    """The JSON body for one outbox row (its columns, by name)."""
    body = {
        "type": str(row["event_type"]),
        "event_id": str(row["event_id"]),
        "org": str(row["org"]),
        "namespace": str(row["namespace"]),
        "agent": str(row.get("agent") or "-"),
        "user_id": str(row.get("user_id") or "-"),
        "session_id": str(row.get("session_id") or "-"),
        "memory_key": str(row.get("memory_key") or ""),
        "memory_id": str(row.get("memory_id") or ""),
        "version": int(row.get("memory_version") or 0),
        "actor": str(row.get("actor") or ""),
        "request_id": str(row.get("request_id") or ""),
        "erasure_id": str(row.get("erasure_id") or ""),
        "occurred_at": _iso(row["occurred_at"]),
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")
