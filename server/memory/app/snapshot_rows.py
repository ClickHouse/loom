"""The memory rows a namespace snapshot reads and a restore writes back.

A snapshot selects every persisted loom.memory column except the identity of the
namespace it came from (org/namespace are the payload's); a restore re-emits each
snapshot row through the version-row builder (the single loom.memory writer) and
commits them with the typed insert, which refuses a row that is not a full version
row. Both column lists derive from the persisted columns, so a new column lands in
both at once (the registry test compares them to the schema)."""

from __future__ import annotations

from collections.abc import Container, Mapping, Sequence
from typing import Any

import server.memory.store as store
from server.core.hashing import hash_text


def persisted_columns() -> tuple[str, ...]:
    """Every loom.memory column a version row carries, in schema order."""
    return store.MEMORY_PERSISTED_COLS


SNAPSHOT_COLUMNS: tuple[str, ...] = tuple(
    # journal_version is the writing build's stamp, set again on the restore's insert.
    c
    for c in persisted_columns()
    if c not in ("org", "namespace", "memory_id", "journal_version")
)
SNAPSHOT_SELECT = ",\n            ".join(
    (
        "toString(cross_promoted_from_memory_id) AS cross_promoted_from_memory_id"
        if c == "cross_promoted_from_memory_id"
        else "arrayMap(x -> toString(x), conflict_with) AS conflict_with"
        if c == "conflict_with"
        # The JSON column comes back as a dict from the client; the payload carries its text,
        # as the repository projection does (`toString(value_json)`).
        else "toString(value_json) AS value_json"
        if c == "value_json"
        else c
    )
    for c in SNAPSHOT_COLUMNS
)
# Columns a restore sets itself; the rest are the source row's.
RESTORE_IDENTITY_OVERRIDES: frozenset[str] = frozenset(
    {
        "org",
        "namespace",
        "agent",
        "actor",
        "source_class",
        "version",
        "tombstoned_at",
        "content_sha256",
    }
)

_INT_COLUMNS = frozenset(
    {"version", "evidence_count", "recall_count", "reinforcement_count", "char_limit"}
)
_LIST_COLUMNS = frozenset({"tags", "entities", "conflict_with", "files", "concepts"})


async def read_namespace(ch: Any, *, org: str, namespace: str) -> list[dict[str, Any]]:
    """The namespace's live loom.memory rows, oldest first, as `SNAPSHOT_SELECT` projects them."""
    return list(
        await ch.query_all(
            f"""
            SELECT
                toString(memory_id) AS memory_id,
                {SNAPSHOT_SELECT}
            FROM loom.memory FINAL
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL
            ORDER BY created_at
            """,
            params={"org": org, "ns": namespace},
        )
    )


def snapshot_fields(r: Mapping[str, Any], keep: Container[str]) -> dict[str, Any]:
    """One loom.memory row (as `read_namespace` returns it) as payload fields: the
    columns in `keep` (the payload model's fields), None dropped, each coerced to
    its payload type."""
    data: dict[str, Any] = {"memory_id": str(r["memory_id"])}
    for c in SNAPSHOT_COLUMNS:
        if c not in keep:
            continue
        v = r.get(c)
        if v is None:
            continue
        if c == "content_sha256":
            v = hash_text(v)
        elif c == "cross_promoted_from_memory_id":
            v = store.uuid_or_none(v) or ""
        elif c == "pinned":
            v = bool(v)
        elif c in _LIST_COLUMNS:
            v = [str(x) for x in (v or [])]
        elif c in ("confidence", "temporal_confidence"):
            v = float(v)
        elif c in _INT_COLUMNS:
            v = int(v)
        elif isinstance(v, bytes):
            v = v.decode("utf-8", errors="replace")
        data[c] = v
    return data


def restore_rows(
    memories: Sequence[Any],
    *,
    org: str,
    target_namespace: str,
    rebind_agent: str | None,
    version_base: int,
    actor: str,
) -> list[dict[str, Any]]:
    """The loom.memory version rows a restore writes: each snapshot row on every column
    (created_at included — a payload without one lands the restore time), with the
    identity a restore owns (target namespace, agent, restoring actor, an `imported`
    source class, a fresh version, no tombstone). Restore is an import by the restorer;
    the row keeps its attestation and provenance."""
    rows: list[dict[str, Any]] = []
    for i, m in enumerate(memories):
        agent = rebind_agent or m.agent
        source = store.MemoryRow.from_snapshot(
            m, org=org, namespace=target_namespace, agent=agent, value=m.value
        )
        rows.append(
            store.build_memory_version_row(
                source,
                org=org,
                namespace=target_namespace,
                agent=agent,
                actor=actor or m.actor,
                source_class="imported",
                version=version_base + i,
                tombstoned_at=None,
                content_sha256=m.content_sha256 or "0",
            )
        )
    return rows


async def insert_version_rows(rows: Sequence[Mapping[str, Any]], *, ch: Any) -> int:
    """Commit restored version rows through the typed loom.memory insert."""
    return await store.insert_memory_versions(rows, ch=ch)
