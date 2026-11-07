"""The memory rows a namespace snapshot reads and a restore writes back.

A snapshot selects every persisted loom.memory column except the identity of the
namespace it came from (org/namespace are the payload's); a restore re-emits each
snapshot row through the version-row builder (the single loom.memory writer) and
commits them with the typed insert, which refuses a row that is not a full version
row. Both column lists derive from the persisted columns, so a new column lands in
both at once (the registry test compares them to the schema)."""

from __future__ import annotations

import json
from collections.abc import Container, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import server.memory.store as store
from server.core import pg
from server.core.datetimes import fits_datetime_column
from server.core.errors import PreconditionFailedError, ValidationError
from server.core.hashing import hash_text
from server.memory.domain.redaction_lineage import is_content_hash
from server.memory.store import pg_memory
from server.memory.store.rows import identity_key
from server.settings import settings


def persisted_columns() -> tuple[str, ...]:
    """Every loom.memory column a version row carries, in schema order."""
    return store.MEMORY_PERSISTED_COLS


SNAPSHOT_COLUMNS: tuple[str, ...] = tuple(
    c for c in persisted_columns() if c not in ("org", "namespace", "memory_id")
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
    """The namespace's live loom.memory rows, oldest first, with every column
    `snapshot_fields` reads. Under Postgres, the heads the authority holds (every
    persisted column): ClickHouse may not have a committed delete yet, or may never (a
    parked delivery)."""
    if pg.enabled():
        heads: list[dict[str, Any]] = []
        after = None
        while page := await pg_memory.live_heads_in(org, namespace, after=after):
            heads += page
            after = identity_key(page[-1])
        return sorted(heads, key=lambda r: r["created_at"])
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
                # Not nullable: a payload without one lands the memory's created_at.
                updated_at=m.updated_at or source.created_at,
                last_reinforced_at=m.last_reinforced_at or source.created_at,
            )
        )
    return rows


async def without_erased(
    org: str, namespace: str, memories: Sequence[Any]
) -> tuple[list[Any], int]:
    """The snapshot's memories a restore into `namespace` writes, and how many it leaves
    out: under Postgres, those an erasure removed from it while their deletion evidence
    is kept, whose rows cannot be told from a stale writer's."""
    if not pg.enabled() or not memories:
        return list(memories), 0
    erased = await pg_memory.erased_ids(org, namespace, [m.memory_id for m in memories])
    kept = [m for m in memories if m.memory_id not in erased]
    return kept, len(memories) - len(kept)


async def insert_version_rows(rows: Sequence[Mapping[str, Any]], *, ch: Any) -> bool:
    """Commit restored version rows; whether some are not yet published. Under Postgres
    they commit in batches of LOOM_PG_RESTORE_BATCH_ROWS; once one's publication is
    pending, the rest are only committed, and the delivery worker publishes them."""
    if not pg.enabled():
        await store.insert_memory_versions(rows, ch=ch)
        return False
    publish_now = True
    step = settings.pg_restore_batch_rows
    for start in range(0, len(rows), step):
        try:
            await store.insert_memory_versions(
                rows[start : start + step], ch=ch, publish_now=publish_now
            )
        except pg_memory.PublicationPending:
            publish_now = False
        except pg_memory.MemoryErased:
            raise PreconditionFailedError(
                "an erasure that removed some of the snapshot's memories from this namespace "
                "landed while it was restored; restore it again to leave them out"
            ) from None
    return not publish_now


def checked_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A restore's rows with every memory id in the server's UUID spelling, refused with
    422 where a loom.memory column cannot hold a value: checked before the restore writes
    anything. Under Postgres, text and floats the authority cannot store are refused too
    (`pg_memory.encode_row`); a duplicate_of that is not an id stays as ClickHouse keeps it.
    """
    out = []
    for row in rows:
        r = dict(row)
        r["conflict_with"] = [_uuid("conflict_with", i) for i in r.get("conflict_with") or []]
        if r.get("cross_promoted_from_memory_id"):
            r["cross_promoted_from_memory_id"] = _uuid(
                "cross_promoted_from_memory_id", r["cross_promoted_from_memory_id"]
            )
        if r.get("duplicate_of"):
            try:
                r["duplicate_of"] = str(UUID(str(r["duplicate_of"])))
            except ValueError:
                if pg.enabled():  # the authority follows it as a memory id
                    raise ValidationError("duplicate_of is not a memory id") from None
        sha = r.get("content_sha256") or ""
        if sha not in ("", "0") and not is_content_hash(sha):
            raise ValidationError("content_sha256 is not 64 lowercase hex characters")
        _json_object(r.get("value_json"))
        for col, v in r.items():
            if isinstance(v, datetime) and not fits_datetime_column(v):
                raise ValidationError(f"{col} is outside 1970-2106, which loom.memory cannot hold")
        if pg.enabled():
            pg_memory.encode_row(r)  # as the commit encodes it: refuses what Postgres cannot store
        out.append(r)
    return out


def _uuid(col: str, value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise ValidationError(f"{col} is not a memory id") from None


def _json_object(value: Any) -> None:
    """The JSON column holds an object; NaN and Infinity are not JSON to it."""
    try:
        parsed = json.loads(value, parse_constant=_not_json)
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict):
        raise ValidationError("value_json is not a JSON object")


def _not_json(token: str) -> Any:
    raise ValueError(token)
