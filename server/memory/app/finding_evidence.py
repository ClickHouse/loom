"""The live revisions a diagnostic report's samples point at.

A finding's sample names the row that matched by its `memory_id`, or by its full
identity (agent, user_id, session_id, memory_key) when it was read from a table that
carries no id. A sample with neither (a hot key from the ledger, a key's version
history) names no single row and is not cited. One read per report resolves every
sample of every finding at once.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from server.core.repo_base import _CHClient
from server.identity.domain.read_scope import ReadFilters
from server.memory.store.reads import RowKey, live_revisions
from server.settings import settings

_IDENTITY = ("agent", "user_id", "session_id", "memory_key")


def sample_ref(sample: dict[str, Any]) -> str | RowKey | None:
    """The row a sample names: its memory_id, else its full identity, else None."""
    mid = sample.get("memory_id")
    if isinstance(mid, str) and mid:
        return mid
    parts = [sample.get(c) for c in _IDENTITY]
    if all(isinstance(p, str) and p for p in parts):
        return cast(RowKey, tuple(parts))
    return None


async def revisions_for(
    org: str,
    namespace: str,
    samples: Iterable[dict[str, Any]],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> dict[str | RowKey, dict[str, Any]]:
    """Each sampled row's ref -> its live revision (identifiers only, no value). A ref
    whose row is no longer live has no entry."""
    refs = {r for r in (sample_ref(s) for s in samples) if r is not None}
    ids = sorted(r for r in refs if isinstance(r, str))
    identities = sorted(r for r in refs if isinstance(r, tuple))
    rows = await live_revisions(
        org,
        namespace,
        scope=scope,
        memory_ids=ids,
        identities=identities,
        hash_max_bytes=settings.namespace_report_hash_max_bytes,
        ch=ch,
    )
    out: dict[str | RowKey, dict[str, Any]] = {}
    for row in rows:
        out[row["memory_id"]] = row
        out[(row["agent"], row["user_id"], row["session_id"], row["memory_key"])] = row
    return out
