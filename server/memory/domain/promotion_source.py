"""The `loom.shared.promoted_from` entry that names a promotion's source row. Pure.

Format: `org/namespace/agent/user_id/session_id/memory_key`. A key may hold `/` (a
`definition/` key does), so the parser keeps everything after the fifth `/` as the key,
and an agent, user or session holding `/` is read into it. The read-time definition check
compares whole entries, so it is not affected.
"""

from __future__ import annotations

_FIELDS = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


def promotion_source(
    org: str, namespace: str, agent: str, user_id: str, session_id: str, memory_key: str
) -> str:
    """The entry naming a source row."""
    return f"{org}/{namespace}/{agent}/{user_id}/{session_id}/{memory_key}"


def parse_promotion_source(entry: str) -> dict[str, str] | None:
    """An entry's six fields, or None when it is not one (so one malformed row does not
    fail a read)."""
    parts = entry.split("/", len(_FIELDS) - 1)
    if len(parts) != len(_FIELDS) or not parts[-1]:
        return None
    return dict(zip(_FIELDS, parts, strict=True))
