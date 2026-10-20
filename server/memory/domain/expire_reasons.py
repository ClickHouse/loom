"""The `expire_reason` prefix a blacklist sweep stamps on its tombstones.

The write fence (server/memory/app/write_fence.py) tells a sweep's tombstone from a
delete's by it, so it lives only on the sweep's tombstone: a caller may not send it, and
every other version drops it when it carries a row's reason forward (the row builder
`server.memory.store.rows.build_memory_version_row`, memory.set's and memory.bulk_set's
own rows). The sweep and namespace.rollback, which writes a swept version back as it
was, pass it to the builder as an override.
"""

from __future__ import annotations

SWEEP_REASON_PREFIX = "blacklist.sweep:"


def caller_reason(reason: str, field: str = "expire_reason") -> str:
    """`reason` when a caller may store it as an `expire_reason`; raises otherwise."""
    if reason.startswith(SWEEP_REASON_PREFIX):
        raise ValueError(f"`{field}` may not start with {SWEEP_REASON_PREFIX!r}")
    return reason


def carried_reason(reason: str | None) -> str:
    """A row's `expire_reason` as a new version of the row carries it: a sweep's marker
    stays on the sweep's tombstone."""
    reason = reason or ""
    return "" if reason.startswith(SWEEP_REASON_PREFIX) else reason
