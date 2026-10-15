"""The entity extractor's reads and writes: memories whose `entities` is
still empty, the version re-emits that set it on both tables, and the heal
for a mirror that missed one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.temporal_capture import read_bases, row_key
from server.memory.store.embeddings import retire_copies_under_unembedded_heads
from server.memory.store.inserts import insert_embedding_rows, insert_memory_versions
from server.memory.store.reads import memory_rows_by_identity_keys
from server.memory.store.rows import (
    EMBEDDING_PERSISTED_COLS,
    build_embedding_row,
    build_memory_version_row,
    next_version,
    reemit_version,
)
from server.memory.store.sql import _IDENTITY_KEY_TUPLE, _identity_key


async def find_unentitied_keys(
    *,
    batch_size: int,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    since_seconds: int | None = None,
    per_namespace: int | None = None,
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
        ORDER BY argMax(m.created_at, version) {order}
        {per_ns_sql}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def read_unentitied_rows(
    keys: list[dict[str, Any]], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Phase two: type and value for the keys phase one found, under FINAL
    with the sort key in PREWHERE, the emptiness and liveness re-checked
    after the merge so a row entitied
    or tombstoned since the keys were read is not extracted again."""
    if not keys:
        return []
    ch = ch or _default_client()
    tuples = [
        (
            str(k["org"]),
            str(k["namespace"]),
            str(k["agent"]),
            str(k["user_id"]),
            str(k["session_id"]),
            str(k["memory_key"]),
        )
        for k in keys
    ]
    rows = await ch.query_all(
        """
        SELECT org, namespace, agent, user_id, session_id, memory_key,
               memory_type, value, created_at
        FROM loom.memory FINAL
        PREWHERE (org, namespace, agent, user_id, session_id, memory_key)
                 IN {keys:Array(Tuple(String, String, String, String, String, String))}
        WHERE tombstoned_at IS NULL
          AND empty(entities)
          AND memory_type IN ('semantic', 'procedural', 'preference')
        ORDER BY created_at DESC
        """,
        params={"keys": tuples},
    )
    return [{k: v for k, v in r.items() if k != "created_at"} for r in rows]


async def find_unentitied_memories(
    *,
    batch_size: int,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    since_seconds: int | None = None,
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
        ch=ch,
    )
    return await read_unentitied_rows(keys, ch=ch)


# Visibility retry for the mirror row after an async dual write.
ENTITY_MIRROR_RETRY_DELAY_S = 0.3


_MIRROR_KEY_TUPLE = "Array(Tuple(String, String, String, String, String, String, String, String))"


async def _mirror_rows_for_keys(
    ch: _CHClient, keys: Sequence[tuple[str, str, str, str, str, str]]
) -> list[dict[str, Any]]:
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
        params={"keys": [tuple(str(x) for x in k) for k in keys]},
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


async def apply_entities_batch(
    items: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """Set `entities` on both tables for a pass's worth of rows by re-emitting
    bumped VERSION rows through the canonical builders, in five statements
    whatever N is. Never a mutation: a two-table mutation loop is
    non-atomic (a failed second leg leaves the tables disagreeing) and
    version-invisible (a writer re-emitting from a pre-mutation snapshot
    resurrects the old entities under FINAL). Each item carries the identity columns,
    `memory_key` and `entities`. One FINAL read of the live memory rows by
    exact key; one `loom.memory` insert of every version row; the mirror
    rows of every key in two statements (`_mirror_rows_for_keys`); one
    `memory_embeddings` insert. Keys whose memory row exists but whose
    mirror is not yet visible (the dual write lands the embedding row
    asynchronously) get one bounded retry (`ENTITY_MIRROR_RETRY_DELAY_S`),
    together, before the insert; `heal_divergent_entities` closes the rest.
    One version for the batch: each key's rows are independent and every
    row of the batch is newer than any snapshot taken before the pass wrote.
    Returns the rows written per table."""
    wanted: dict[tuple[str, str, str, str, str, str], list[str]] = {}
    for it in items:
        wanted[_identity_key(it)] = list(it["entities"])
    written = {"memory": 0, "memory_embeddings": 0}
    if not wanted:
        return written
    ch = ch or _default_client()
    version = next_version()
    keys = list(wanted)
    existing = await memory_rows_by_identity_keys(keys, ch=ch)
    mem_rows = [
        build_memory_version_row(existing[k], entities=wanted[k], version=version)
        for k in keys
        if k in existing
    ]
    if mem_rows:
        landed: set[tuple[str, ...]] = set()

        async def _landed(rows: Any) -> None:
            landed.update(_identity_key(r) for r in rows)

        # Over the heads read here: what this pass did not change is the journal's.
        bases = {row_key(r): _base(existing[_identity_key(r)]) for r in mem_rows}
        with read_bases(bases):
            written["memory"] = await insert_memory_versions(mem_rows, ch=ch, alongside=[_landed])
        keys = [k for k in keys if k in landed]  # a row the journal left out: no mirror
    # A key whose head keeps no vector ('high', unsearchable) gets no mirror copy.
    keys = [k for k in keys if k in existing and _embedded(existing[k])]
    emb_rows = await _mirror_rows_for_keys(ch, keys)
    seen = {_identity_key(r) for r in emb_rows}
    missing = [k for k in keys if k in existing and k not in seen]
    if missing:
        await asyncio.sleep(ENTITY_MIRROR_RETRY_DELAY_S)
        emb_rows.extend(await _mirror_rows_for_keys(ch, missing))
    if emb_rows:
        copies = [
            build_embedding_row(
                **(
                    {k: v for k, v in r.items() if k in EMBEDDING_PERSISTED_COLS}
                    | {"entities": list(wanted[_identity_key(r)]), "version": reemit_version(r)}
                )
            )
            for r in emb_rows
        ]
        await insert_embedding_rows(copies, ch=ch)
        await retire_copies_under_unembedded_heads(copies, ch=ch)
        written["memory_embeddings"] = len(emb_rows)
    return written


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
    entities_by_key = {_identity_key(pr): list(pr["entities"] or []) for pr in pairs}
    emb_rows = await ch.query_all(
        f"""
        SELECT * FROM loom.memory_embeddings FINAL
        WHERE (org, namespace, agent, user_id, session_id, memory_key)
              IN {{keys:{_IDENTITY_KEY_TUPLE}}}
          AND tombstoned_at IS NULL
        """,
        params={"keys": list(entities_by_key)},
    )
    emb_rows = [r for r in emb_rows if _identity_key(r) in entities_by_key]
    if not emb_rows:
        return 0
    copies = [
        build_embedding_row(
            **(
                {k: v for k, v in r.items() if k in EMBEDDING_PERSISTED_COLS}
                | {"entities": entities_by_key[_identity_key(r)], "version": reemit_version(r)}
            )
        )
        for r in emb_rows
    ]
    await insert_embedding_rows(copies, ch=ch)
    await retire_copies_under_unembedded_heads(copies, ch=ch)
    return len(emb_rows)


def _base(head: Any) -> dict[str, Any]:
    return build_memory_version_row(head, version=int(head.version))
