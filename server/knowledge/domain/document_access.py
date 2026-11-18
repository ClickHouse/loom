"""Source ownership and freshness of a mirrored document's permission evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID


def public_tags(tags: Sequence[str]) -> list[str]:
    """Omit a checked mirror's internal source ownership tags."""
    return [tag for tag in tags if not tag.startswith("source-id:")]


def source_id(source_uri: str, tags: Sequence[str]) -> str:
    """One canonical registration id, present in both its ownership tag and address."""
    found = [tag.removeprefix("source-id:") for tag in tags if tag.startswith("source-id:")]
    if len(found) != 1:
        return ""
    value = found[0]
    try:
        if str(UUID(value)) != value:
            return ""
    except ValueError:
        return ""
    address, separator, suffix = source_uri.rpartition("#")
    return value if address and separator and suffix == value else ""


def fresh(read_at: datetime | None, now: datetime, stale_seconds: int) -> bool:
    """A known observation no older than the bound and not from a future clock."""
    if read_at is None or stale_seconds <= 0:
        return False
    if read_at.tzinfo is None:
        read_at = read_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return 0 <= (now - read_at).total_seconds() <= stale_seconds
