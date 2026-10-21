"""Memory webhook events: their types, the change `announce` takes and the body sent.

The event types are fixed. A body names what changed — the event, the memory's
key, id and version, its identity tuple, who changed it, when and in which
request — and never what it says: no value, tags text or evidence. The receiver
fetches the row with its own token, so what it may read is decided at read
time, as for any other read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, get_args

EventType = Literal[
    "memory.written",
    "memory.revised",
    "memory.tombstoned",
    "memory.restored",
    "memory.redacted",
    "memory.erased",
]
EVENT_TYPES: tuple[EventType, ...] = get_args(EventType)

# The wait before the next try after the n-th failed attempt: 5 s, 30 s, 2 min,
# 10 min, 30 min, then the ceiling (memory_webhook_max_backoff_seconds) for every
# failure after that.
# not a setting: the retry curve's shape; the ceiling it levels off at is a setting
BACKOFF_SECONDS: tuple[int, ...] = (5, 30, 120, 600, 1800)


def backoff_seconds(attempts: int, ceiling: float) -> float:
    """Seconds until the next try after `attempts` failed attempts (at least 1):
    the schedule's step, never more than `ceiling`, and `ceiling` once past it."""
    n = max(attempts, 1)
    step = BACKOFF_SECONDS[n - 1] if n <= len(BACKOFF_SECONDS) else ceiling
    return min(step, ceiling)


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


class MemoryRef(Protocol):
    """What names a memory version for an event: a memory row, a history target."""

    @property
    def org(self) -> str: ...

    @property
    def namespace(self) -> str: ...

    @property
    def agent(self) -> str: ...

    @property
    def user_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def memory_key(self) -> str: ...

    @property
    def memory_id(self) -> str: ...


def memory_change(memory: MemoryRef, version: int) -> dict[str, Any]:
    """The change `announce` takes, for a memory row object at `version`."""
    return {
        "org": memory.org,
        "namespace": memory.namespace,
        "agent": memory.agent,
        "user_id": memory.user_id,
        "session_id": memory.session_id,
        "memory_key": memory.memory_key,
        "memory_id": memory.memory_id,
        "version": version,
    }
