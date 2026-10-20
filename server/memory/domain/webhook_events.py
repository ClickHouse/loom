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


def _iso(ts: datetime) -> str:
    """`ts` (naive UTC, or aware) as ISO 8601 UTC with milliseconds and a Z."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts.isoformat(timespec="milliseconds") + "Z"


def event_body(row: Mapping[str, Any]) -> bytes:
    """The JSON body for one outbox row (its columns, by name)."""
    body = {
        "type": row["event_type"],
        "event_id": row["event_id"],
        "org": row["org"],
        "namespace": row["namespace"],
        "agent": row["agent"],
        "user_id": row["user_id"],
        "session_id": row["session_id"],
        "memory_key": row["memory_key"],
        "memory_id": row["memory_id"],
        "version": int(row["memory_version"]),
        "actor": row["actor"],
        "request_id": row["request_id"],
        "erasure_id": row["erasure_id"],
        "occurred_at": _iso(row["occurred_at"]),
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")
