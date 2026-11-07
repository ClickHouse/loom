"""The entity extractor's reads and writes: memories whose `entities` is
still empty, the version re-emits that set it on both tables, and the heal
for a mirror that missed one.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from server.core.hashing import hash_text
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.redaction_lineage import is_content_hash
from server.memory.store.embeddings import retire_copies_under_unembedded_heads
from server.memory.store.inserts import insert_embedding_rows
from server.memory.store.lifecycle import insert_versions_over_heads
from server.memory.store.pg_memory import AdmissionContendedError, MemoryErased
from server.memory.store.reads import memory_rows_by_identity_keys
from server.memory.store.rows import (
    EMBEDDING_PERSISTED_COLS,
    IdentityKey,
    MemoryRow,
    build_embedding_row,
    build_memory_version_row,
    identity_key,
    next_version,
    reemit_version,
    version_over,
)
from server.memory.store.sql import _IDENTITY_KEY_TUPLE
from server.settings import settings


async def find_unentitied_keys(
    *,
    batch_size: int,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    since_seconds: int | None = None,
    per_namespace: int | None = None,
    exclude_orgs: frozenset[str] = frozenset(),
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Phase one of the entity extractor's candidate read:
    the identity keys of live memories whose entities[] is still empty,
    newest first — the same argMax aggregate the worker runs, without
    the `value` column (70% of a whole-table read that returned keys).
    `since_seconds` narrows it to memories created in that window (the
    worker's every pass); `None` is the whole table (its slow clock, which
    fills the backlog queue)."""
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"lim": int(batch_size)}
    if org_filter is not None:
        extra_where += " AND org = {org_filter:String}"
        params["org_filter"] = org_filter
    if namespace_filter is not None:
        extra_where += " AND namespace = {ns_filter:String}"
        params["ns_filter"] = namespace_filter
    if exclude_orgs:  # orgs whose model calls are switched off keep their rows for later
        extra_where += " AND org NOT IN {exclude_orgs:Array(String)}"
        params["exclude_orgs"] = sorted(exclude_orgs)
    if since_seconds is not None and since_seconds > 0:
        extra_where += " AND m.created_at > now() - toIntervalSecond({since_s:UInt32})"
        params["since_s"] = int(since_seconds)
    # The whole-table backlog read is fair across namespaces and oldest
    # first. The windowed read (since_seconds) already takes every pass's new
    # rows, so newest-first here would only re-read the recent end and leave a
    # small namespace's old rows behind a large one's indefinitely.
    # `per_namespace` caps each namespace's share of one refill.
    order = "DESC"
    per_ns_sql = ""
    if per_namespace is not None and per_namespace > 0:
        order = "ASC"
        per_ns_sql = "LIMIT {per_ns:UInt32} BY org, namespace"
        params["per_ns"] = int(per_namespace)
    # No FINAL: argMax(..., version) gives the latest-version state without a
    # pre-merge; the worker is idempotent on races. WHERE columns are
    # alias-qualified (m.memory_type) so CH doesn't bind them to the SELECT
    # aggregate alias of the same name — that raises ILLEGAL_AGGREGATION.
    return await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key
        FROM loom.memory AS m
        WHERE m.memory_type IN ('semantic', 'procedural', 'preference')
          {extra_where}
        GROUP BY org, namespace, agent, user_id, session_id, memory_key
        HAVING argMax(empty(m.entities), version) = 1
           AND argMax(m.tombstoned_at IS NULL, version) = 1
           AND argMax(m.redacted_at IS NULL, version) = 1
           -- a high value is never sent to the extractor's model
           AND argMax(m.sensitivity, version) != 'high'
        ORDER BY argMax(m.created_at, version) {order}
        {per_ns_sql}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def read_unentitied_rows(
    keys: list[dict[str, Any]], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Phase two: type, value and writer (`actor`, whom the extraction's model
    call is for) for the keys phase one found, under FINAL
    with the sort key in PREWHERE, the emptiness and liveness re-checked
    after the merge so a row entitied
    or tombstoned since the keys were read is not extracted again."""
    if not keys:
        return []
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT org, namespace, agent, user_id, session_id, memory_key,
               memory_type, value, actor, version,
               toStringCutToZero(content_sha256) AS content_sha256,
               redacted_at, created_at
        FROM loom.memory FINAL
        PREWHERE (org, namespace, agent, user_id, session_id, memory_key)
                 IN {keys:Array(Tuple(String, String, String, String, String, String))}
        WHERE tombstoned_at IS NULL
          AND redacted_at IS NULL
          AND empty(entities)
          AND sensitivity != 'high'
          AND memory_type IN ('semantic', 'procedural', 'preference')
        ORDER BY created_at DESC
        """,
        params={"keys": [identity_key(k) for k in keys]},
    )
    return [{k: v for k, v in r.items() if k != "created_at"} for r in rows]


async def find_unentitied_memories(
    *,
    batch_size: int,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    since_seconds: int | None = None,
    exclude_orgs: frozenset[str] = frozenset(),
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Memories whose entities[] array is still empty: phase one then phase
    two. The worker drives the phases itself so it can keep
    a queue of keys between passes; this is the one-shot form for callers
    that want the rows now."""
    keys = await find_unentitied_keys(
        batch_size=batch_size,
        org_filter=org_filter,
        namespace_filter=namespace_filter,
        since_seconds=since_seconds,
        exclude_orgs=exclude_orgs,
        ch=ch,
    )
    return await read_unentitied_rows(keys, ch=ch)


# Visibility retry for the mirror row after an async dual write.
ENTITY_MIRROR_RETRY_DELAY_S = 0.3


_MIRROR_KEY_TUPLE = "Array(Tuple(String, String, String, String, String, String, String, String))"


async def _mirror_rows_for_keys(ch: _CHClient, keys: Sequence[IdentityKey]) -> list[dict[str, Any]]:
    """Every live `memory_embeddings` row of the given identity keys in two
    statements whatever N is: discover the `(scope, embedding_model)`
    siblings without FINAL — narrow
    columns, a superset — then read the full rows under FINAL by the full
    sort key, so no identity's whole range is read with its vectors."""
    if not keys:
        return []
    sib = await ch.query_all(
        f"""
        SELECT DISTINCT org, namespace, agent, user_id, session_id, memory_key,
                        scope, embedding_model
        FROM loom.memory_embeddings
        WHERE (org, namespace, agent, user_id, session_id, memory_key)
              IN {{keys:{_IDENTITY_KEY_TUPLE}}}
        """,
        params={"keys": list(keys)},
    )
    if not sib:
        return []
    full = [
        (
            str(r["org"]),
            str(r["namespace"]),
            str(r["agent"]),
            str(r["user_id"]),
            str(r["session_id"]),
            str(r["scope"]),
            str(r["embedding_model"]),
            str(r["memory_key"]),
        )
        for r in sib
    ]
    return await ch.query_all(
        f"""
        SELECT * FROM loom.memory_embeddings FINAL
        PREWHERE (org, namespace, agent, user_id, session_id, scope,
                  embedding_model, memory_key)
                 IN {{keys:{_MIRROR_KEY_TUPLE}}}
        WHERE tombstoned_at IS NULL
        """,
        params={"keys": full},
    )


def _embedded(row: Any) -> bool:
    """A head that may keep vectors: anything but 'high' (profile rows keep theirs)."""
    return str(row.sensitivity) != "high"


class Extracted(NamedTuple):
    """The entities extracted from a key's head, and that head's version and content hash."""

    version: int
    content_sha256: str
    entities: list[str]


def _still_extracted(row: MemoryRow, read: Extracted) -> bool:
    """Whether a head still holds the text its entities were extracted from, and no
    entities of its own: the version read, or a later copy of the same content (a lock, a
    touch), unredacted and not 'high'."""
    same = row.version == read.version or (
        is_content_hash(read.content_sha256) and row.content_sha256 == read.content_sha256
    )
    return same and not row.entities and row.redacted_at is None and _embedded(row)


async def apply_entities_batch(
    items: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """Set `entities` on both tables by re-emitting version rows, never a mutation (a
    two-table mutation loop is non-atomic). Each item carries the identity columns and
    the `version` and `content_sha256` of the head its `entities` were extracted from.

    Each head that still holds that text takes them (`_still_extracted`), at the version
    drawn before the read or above the head when that is newer, and its search documents
    follow under the commit's lease (`_copy_mirrors`). Under Postgres a head written or
    erased since the read refuses the commit, which is read again without it. Returns the
    rows written per table."""
    wanted = {
        identity_key(it): Extracted(int(it["version"]), it["content_sha256"], list(it["entities"]))
        for it in items
    }
    ch = ch or _default_client()
    # Only a Postgres commit refuses a moved head; on ClickHouse this runs once.
    for attempt in range(1, settings.pg_admission_rounds + 1):
        # Drawn before the read, then lifted above the head read (`version_over`).
        version = next_version()
        heads = await memory_rows_by_identity_keys(list(wanted), ch=ch, sequential=True)
        existing = {k: row for k, row in heads.items() if _still_extracted(row, wanted[k])}
        if not existing:
            break
        rows = [
            build_memory_version_row(
                row, entities=wanted[k].entities, version=version_over(row, version)
            )
            for k, row in existing.items()
        ]
        try:
            copied = await insert_versions_over_heads(
                rows,
                list(existing.values()),
                ch=ch,
                then=functools.partial(_copy_mirrors, wanted, existing, ch),
            )
            return {"memory": len(rows), "memory_embeddings": copied}
        except (AdmissionContendedError, MemoryErased):
            # The next read leaves out the heads written or erased since this one.
            if attempt == settings.pg_admission_rounds:
                raise
    return {"memory": 0, "memory_embeddings": 0}


async def _copy_mirrors(
    wanted: Mapping[IdentityKey, Extracted],
    existing: Mapping[IdentityKey, MemoryRow],
    ch: _CHClient,
) -> int:
    """Copy the entities onto the search documents of the heads written that show their
    content (`_shows`); a mirror not yet visible (the dual write lands it asynchronously)
    gets one retry, and `heal_divergent_entities` closes the rest, copying the head's
    entities onto every document of the key. Returns the rows written."""
    keys = list(existing)
    emb_rows = await _mirror_rows_for_keys(ch, keys)
    seen = {identity_key(r) for r in emb_rows}
    if missing := [k for k in keys if k not in seen]:
        await asyncio.sleep(ENTITY_MIRROR_RETRY_DELAY_S)
        emb_rows += await _mirror_rows_for_keys(ch, missing)
    emb_rows = [r for r in emb_rows if _shows(r, existing[identity_key(r)].content_sha256)]
    if not emb_rows:
        return 0
    copies = [
        build_embedding_row(
            **(
                {k: v for k, v in r.items() if k in EMBEDDING_PERSISTED_COLS}
                | {"entities": wanted[identity_key(r)].entities, "version": reemit_version(r)}
            )
        )
        for r in emb_rows
    ]
    await insert_embedding_rows(copies, ch=ch)
    await retire_copies_under_unembedded_heads(copies, ch=ch)
    return len(emb_rows)


def _shows(mirror: Mapping[str, Any], head_sha256: str) -> bool:
    """Whether a search document shows the head's content: its hash is the head's, or it
    was stored without one. Not its version: a fill or a metadata copy sits above it. A
    newer write's document keeps its own entities."""
    sha = hash_text(mirror.get("content_sha256"))
    return not is_content_hash(sha) or sha == head_sha256


async def heal_divergent_entities(*, limit: int = 200, ch: _CHClient | None = None) -> int:
    """Re-emit embedding rows whose `entities` disagree with loom.memory.

    loom.memory is the source of truth for entities; loom.memory_embeddings
    is the mirror search reads. The `loom.integrity.divergent_entities`
    gauge counts pairs that disagree and should read 0; a mirror can miss the
    entity update when it is not yet visible to apply_entities_batch (see there).
    This is the self-heal: find the divergent pairs, re-emit
    each mirror row with memory's entities at its version + 1. Bounded per
    pass; idempotent; returns rows re-emitted."""
    ch = ch or _default_client()
    pairs = await ch.query_all(
        """
        SELECT m.org AS org, m.namespace AS namespace, m.agent AS agent,
               m.user_id AS user_id, m.session_id AS session_id,
               m.memory_key AS memory_key, m.entities AS entities
        FROM (SELECT org, namespace, agent, user_id, session_id, memory_key,
                     entities, arraySort(entities) AS ents
              FROM loom.memory FINAL
              WHERE tombstoned_at IS NULL AND sensitivity != 'high') AS m
        INNER JOIN (SELECT org, namespace, agent, user_id, session_id,
                           memory_key, arraySort(entities) AS ents
                    FROM loom.memory_embeddings FINAL
                    WHERE tombstoned_at IS NULL) AS e
          ON m.org = e.org AND m.namespace = e.namespace AND m.agent = e.agent
             AND m.user_id = e.user_id AND m.session_id = e.session_id
             AND m.memory_key = e.memory_key
        WHERE m.ents != e.ents
        LIMIT {lim:UInt32}
        """,
        params={"lim": int(limit)},
    )
    if not pairs:
        return 0
    # One keyed FINAL read for every pair and one insert, not a read and an
    # insert per pair.
    entities_by_key = {identity_key(pr): list(pr["entities"] or []) for pr in pairs}
    emb_rows = await ch.query_all(
        f"""
        SELECT * FROM loom.memory_embeddings FINAL
        WHERE (org, namespace, agent, user_id, session_id, memory_key)
              IN {{keys:{_IDENTITY_KEY_TUPLE}}}
          AND tombstoned_at IS NULL
        """,
        params={"keys": list(entities_by_key)},
    )
    emb_rows = [r for r in emb_rows if identity_key(r) in entities_by_key]
    if not emb_rows:
        return 0
    copies = [
        build_embedding_row(
            **(
                {k: v for k, v in r.items() if k in EMBEDDING_PERSISTED_COLS}
                | {"entities": entities_by_key[identity_key(r)], "version": reemit_version(r)}
            )
        )
        for r in emb_rows
    ]
    await insert_embedding_rows(copies, ch=ch)
    await retire_copies_under_unembedded_heads(copies, ch=ch)
    return len(emb_rows)
