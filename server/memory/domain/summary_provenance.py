"""Extractive digests reconstructed from their authoritative sources."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from server.memory.domain.sensitivity import value_reads_high
from server.memory.domain.temporal_sql import SERVED_STATUSES


def source_text(
    row: Any, *, now: datetime, as_of: datetime | None = None, as_of_basis: str = "valid_time"
) -> str | None:
    """Only a complete, currently served source can contribute digest text."""
    if (
        row.tombstoned_at is not None
        or row.status not in SERVED_STATUSES
        or getattr(row, "requires_review", False)
        or row.sensitivity == "high"
        or row.value_truncated
        or value_reads_high(row.value)
    ):
        return None
    for expiry in (row.expires_at, row.valid_to):
        if expiry is not None and _utc(expiry) <= _utc(now):
            return None
    if as_of is not None:
        at = _utc(as_of)
        if as_of_basis == "knowledge_time":
            elapsed = at - datetime(1970, 1, 1)
            cutoff = (
                elapsed.days * 86400 + elapsed.seconds
            ) * 1_000_000_000 + elapsed.microseconds * 1000
            if int(row.version) > cutoff:
                return None
        else:
            lower = row.valid_at or row.temporal_anchor or row.created_at
            if lower is None or _utc(lower) > at:
                return None
        if row.valid_to is not None and _utc(row.valid_to) <= at:
            return None
    return (
        row.value
        if isinstance(row.value, str)
        else json.dumps(row.value, ensure_ascii=False, sort_keys=True, default=str)
    )


def render_summary(facts: list[str]) -> str:
    """Every factual byte comes from the source texts, in their recorded order."""
    return "\n".join(facts)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
