"""Erasure steps for the substrates derived from memory: source and graph
links, the entity graph and its vectors, summaries, records, and the answer
cache, precedents and communities that quote memory text; and `memory.retire`, the
user's loom.memory rows after them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple

from server.core.ch import sequential_reads
from server.core.ch_params import chunks
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import (
    delete_scoped,
    namespaces_of,
    purge_mode,
    purge_scope,
    tombstone_scoped,
)
from server.erasure.store.predicates import (
    COMMUNITY_ROWS_IN,
    CONFLICT_RECORD_IDS_IN,
    CONSOLIDATED_IN,
    MEMORY_KEYS_IN,
    MEMORY_NS_EQ,
    NS_EQ,
    NS_IN,
    PARENT_KEYS_IN,
    PRECEDENT_KEYS_IN,
    RECORD_IDS_IN,
    SUMMARY_IDS_IN,
    SUMMARY_VECTORS_IN,
    UID,
    USER_COMMUNITY_ENTITIES,
    USER_GRAPH_KEYS,
    USER_RECORDS_SUBQ,
    WINDOW_KEYS_IN,
)
from server.erasure.store.scope_reads import (
    hashes,
    slices_or_one_empty,
    summary_slices,
    user_key_pairs,
    user_quoted_keys,
    user_summary_rows,
)
from server.erasure.store.steps_memory import (
    delete_memory_versions_by_user,
    tombstone_memory_chunks_by_user,
    tombstone_shared_by_user,
    tombstone_shared_embeddings_by_user,
)

# ---------------------------------------------------------------------------
# Derived substrates that outlive the memory step. Each helper mutates in
# both scopes; user scope reaches tables without a user_id column through
# the parent memory keys (tombstoned rows included —
# the memory step runs first and these must follow it, not escape it).
# ---------------------------------------------------------------------------

# Fact → source-turn links and chunk → source links. All carry tombstoned_at.
_LINK_TABLES_TOMBSTONE = (
    "loom.memory_belief_sources",
    "loom.memory_chunk_sources",
)

# The entity graph itself: edges and triples carry the namespace's subjects,
# relations and objects verbatim and have no soft-delete column (invalidation
# is valid_to/invalid_at). MergeTree → ALTER DELETE. No user_id column: the
# user saga reaches them through the link tables, as it reaches
# entity_communities, precedents and the response cache.
# `entity_degree` (the SummingMergeTree the edge view feeds) carries every
# entity's name. Same shape, same step.
_GRAPH_TABLES_DELETE = (
    "loom.entity_edges",
    "loom.entity_triples",
    "loom.entity_degree",
)
# Per-session / per-entity summaries: carry user_id and tombstoned_at.
_SUMMARY_TABLES = ("loom.session_summaries", "loom.entity_summaries")


async def tombstone_source_links_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t in _LINK_TABLES_TOMBSTONE:
        total += await tombstone_scoped(
            t, org=org, scope_where="namespace = {ns:String}", params={"ns": namespace}, ch=ch
        )
    return total


async def tombstone_source_links_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    ch = ch or _default_client()
    keys = await user_key_pairs(org, user_id, ch=ch)
    total = 0
    for t in _LINK_TABLES_TOMBSTONE:
        for part in chunks(keys):
            total += await tombstone_scoped(
                t, org=org, scope_where=PARENT_KEYS_IN, params={"keys": part}, ch=ch
            )
    return total


# The row-link tables a build older than their migrations did not know, so its
# erasures left them (`graph_holders._ROW_LINKS_MIGRATION` and
# `projections.EDGE_ROW_LINKS_MIGRATION`).
# `loom erasure-re-verify --completed-since` checks a completed saga's scope for
# links whose memory has no live row (rolling-upgrades rule 14). A link of a live
# memory is not an orphan, and neither is one of a memory written after the
# erasure finished: that data stays, even once memory.delete tombstones it (it
# can still be restored).
ROW_LINK_TABLES = ("loom.entity_triple_rows", "loom.entity_edge_rows")
_KEY_LINK_TABLES = ("loom.entity_triple_memories", "loom.entity_edge_memories")
# Entity-graph link tables: no soft-delete column → ALTER DELETE. A user's key
# links are found by the user's keys, row links by the writer they carry, which
# outlives the row.
_LINK_TABLES_DELETE = _KEY_LINK_TABLES + ROW_LINK_TABLES


async def delete_graph_links_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t in _LINK_TABLES_DELETE:
        total += await delete_scoped(
            t, org=org, scope_where="namespace = {ns:String}", params={"ns": namespace}, ch=ch
        )
    return total


class OrphanLinks(NamedTuple):
    """The predicates of a row-link table's orphan links and their params."""

    scope: str  # a link in scope
    where: str  # an orphan link in scope; for reads only: it holds subqueries
    kept: str  # the ids of the memories in scope that keep their links
    params: dict[str, Any]


def _orphan_row_links(
    table: str, scope_type: str, scope_value: str | Sequence[str], verified_at: datetime
) -> OrphanLinks:
    """The orphan row links of `table` in scope. `verified_at` is when the saga's census
    ran."""
    if scope_type == "namespace":
        scope = NS_IN
        memories = f"org = {{org:String}} AND {NS_IN}"
        params: dict[str, Any] = {"ns": namespaces_of(scope_value)}
    else:
        # A link carries its writer, and another user may own the memory now, so
        # any memory of the org keeps its link. Only the memories this user's links
        # name are read, not every memory of the org.
        scope = UID
        memories = (
            "org = {org:String} AND memory_id IN "
            f"(SELECT memory_id FROM {table} WHERE org = {{org:String}} AND {UID})"
        )
        params = {"uid": str(scope_value)}
    live_ids = f"SELECT memory_id FROM loom.memory FINAL WHERE {memories} AND tombstoned_at IS NULL"
    # DateTime keeps whole seconds, so a row from the census's own second counts
    # as later: keeping one orphan link is safer than deleting a later write's.
    later_ids = (
        f"SELECT memory_id FROM loom.memory WHERE {memories} "
        "AND updated_at >= {verified_at:DateTime}"
    )
    where = f"{scope} AND memory_id NOT IN ({live_ids}) AND memory_id NOT IN ({later_ids})"
    kept = f"{live_ids} UNION ALL {later_ids}"
    return OrphanLinks(scope, where, kept, {**params, "verified_at": verified_at})


async def count_orphan_row_links(
    org: str,
    scope_type: str,
    scope_value: str | Sequence[str],
    *,
    verified_at: datetime,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Orphan row links in scope, per table."""
    ch = ch or _default_client()
    out: dict[str, int] = {}
    for table in ROW_LINK_TABLES:
        orphans = _orphan_row_links(table, scope_type, scope_value, verified_at)
        # A memory written a moment ago on another replica must count as live.
        row = await ch.query_one(
            f"SELECT count() AS n FROM {table} WHERE org = {{org:String}} AND {orphans.where} "
            "SETTINGS select_sequential_consistency = 1",
            params={"org": org, **orphans.params},
        )
        out[table] = int((row or {}).get("n") or 0)
    return out


async def _live_or_written_since(
    org: str, orphans: OrphanLinks, ids: list[str], *, ch: _CHClient
) -> set[str]:
    """Of the memories `ids`, those that keep their links now (`orphans.kept`: live, or
    written since the census). A memory written after this read is not seen: its links
    go with the DELETE that follows."""
    rows = await ch.query_all(
        f"SELECT DISTINCT toString(memory_id) AS id FROM ({orphans.kept}) "
        "WHERE memory_id IN {ids:Array(UUID)} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, **orphans.params, "ids": ids},
    )
    return {str(r["id"]) for r in rows}


async def delete_orphan_row_links(
    org: str,
    scope_type: str,
    scope_value: str | Sequence[str],
    *,
    verified_at: datetime,
    ch: _CHClient | None = None,
) -> int:
    """Delete the orphan row links in scope. The DELETE names their memory ids as
    parameters: a mutation with a subquery is refused on a replicated table. Each slice
    of ids is read again right before its DELETE, and a memory live or written since
    `verified_at` keeps its link; only a write landing between that read and its DELETE
    can still lose one."""
    ch = ch or _default_client()
    total = 0
    for table in ROW_LINK_TABLES:
        orphans = _orphan_row_links(table, scope_type, scope_value, verified_at)
        found = await ch.query_all(
            f"SELECT DISTINCT toString(memory_id) AS id FROM {table} "
            f"WHERE org = {{org:String}} AND {orphans.where} "
            "SETTINGS select_sequential_consistency = 1",
            params={"org": org, **orphans.params},
        )
        for ids in chunks(sorted(str(r["id"]) for r in found)):
            kept = await _live_or_written_since(org, orphans, ids, ch=ch)
            still_orphans = [memory_id for memory_id in ids if memory_id not in kept]
            if not still_orphans:
                continue
            total += await delete_scoped(
                table,
                org=org,
                scope_where=f"{orphans.scope} AND memory_id IN {{ids:Array(UUID)}}",
                params={**orphans.params, "ids": still_orphans},
                ch=ch,
            )
    return total


async def delete_entity_graph_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t in _GRAPH_TABLES_DELETE:
        total += await delete_scoped(
            t, org=org, scope_where="namespace = {ns:String}", params={"ns": namespace}, ch=ch
        )
    return total


# The entity vectors an erased memory's `entities` were embedded into.
# `loom.entity_vectors` carries the memory's key and each entity's text
# verbatim and is read by the entity boost on every search. A
# ReplacingMergeTree with
# `tombstoned_at`, but a derived table with nothing to restore: a lightweight
# DELETE in every mode, not a tombstone that keeps the text in the parts.
async def delete_entity_vectors_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.entity_vectors",
        org=org,
        scope_where=NS_EQ,
        params={"ns": namespace},
        lightweight=True,
        ch=ch,
    )


async def delete_entity_vectors_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """The rows carry the memory's `user_id` (written from the same input),
    and the parent-key match reaches a row whose memory carried it under a
    different one — both sides of the (namespace, memory_key) identity. A summary of
    the user's memories is matched by its agent, user and key."""
    ch = ch or _default_client()
    keys = await user_key_pairs(org, user_id, ch=ch)
    total = 0
    for part in slices_or_one_empty(keys):
        total += await delete_scoped(
            "loom.entity_vectors",
            org=org,
            scope_where=f"(user_id = {{uid:String}} OR {PARENT_KEYS_IN})",
            params={"uid": user_id, "keys": part},
            lightweight=True,
            ch=ch,
        )
    for rows in chunks((await user_summary_rows(org, user_id, ch=ch)).identities):
        total += await delete_scoped(
            "loom.entity_vectors",
            org=org,
            scope_where=SUMMARY_VECTORS_IN,
            params={"rows": rows},
            lightweight=True,
            ch=ch,
        )
    return total


def _graph_owned_only_sql(link_table: str, cols: tuple[str, ...]) -> str:
    """The hashes of the graph keys whose every linking memory belongs to the user, or is
    a summary of the user's memories. A triple or edge shared with another user's memory
    stays: the other user still knows it. `cols` names the graph key columns as they
    appear on both the graph table and its link table."""
    key = ", ".join(cols)
    return (
        f"SELECT cityHash64(namespace, {key}) AS h FROM {link_table} FINAL "
        "WHERE org = {org:String} "
        f"GROUP BY namespace, {key} "
        f"HAVING countIf({USER_GRAPH_KEYS}) > 0 AND countIf(NOT {USER_GRAPH_KEYS}) = 0"
    )


_GRAPH_LINKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("loom.entity_triples", "loom.entity_triple_memories", ("subject", "relation", "object")),
    ("loom.entity_edges", "loom.entity_edge_memories", ("entity_a", "entity_b")),
)


async def delete_entity_graph_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Delete the triples and edges only this user's memories (or summaries of them)
    carried. Must run BEFORE `graph_links.delete` (which runs it again first):
    ownership is read from the link tables.

    The mutation names each key by its hash, computed by ClickHouse on both sides:
    nothing bounds a stored entity name, and one name can outgrow a form field."""
    ch = ch or _default_client()
    total = 0
    for table, link_table, cols in _GRAPH_LINKS:
        rows = await ch.query_all(
            _graph_owned_only_sql(link_table, cols), params={"org": org, "uid": user_id}
        )
        scope = f"cityHash64(namespace, {', '.join(cols)}) IN {{owned:Array(UInt64)}}"
        for part in chunks(hashes(rows)):
            total += await delete_scoped(
                table, org=org, scope_where=scope, params={"owned": part}, ch=ch
            )
    return total


async def delete_graph_links_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """The user's links, and those of the summaries of the user's memories: by key, and
    by the summary's id for a row link, which carries the summary writer's user. First
    the graph rows and communities those links alone hold, read from the links, so a
    run the census reopens leaves none of them with no link that names it; this step's
    total counts the links only."""
    ch = ch or _default_client()
    await delete_entity_graph_by_user(org, user_id, ch=ch)
    await delete_entity_communities_by_user(org, user_id, ch=ch)
    total = 0
    summaries = await user_summary_rows(org, user_id, ch=ch)
    keys = await user_key_pairs(org, user_id, ch=ch) + summaries.keys
    for t in ROW_LINK_TABLES:
        total += await delete_scoped(t, org=org, scope_where=UID, params={"uid": user_id}, ch=ch)
        for ids in chunks(summaries.ids):
            total += await delete_scoped(
                t,
                org=org,
                scope_where=SUMMARY_IDS_IN,
                params={"ids": ids},
                ch=ch,
            )
    for t in _KEY_LINK_TABLES:
        for part in chunks(keys):
            total += await delete_scoped(
                t, org=org, scope_where=PARENT_KEYS_IN, params={"keys": part}, ch=ch
            )
    return total


async def tombstone_summaries_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t in _SUMMARY_TABLES:
        total += await tombstone_scoped(
            t, org=org, scope_where="namespace = {ns:String}", params={"ns": namespace}, ch=ch
        )
    return total


async def tombstone_summaries_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t in _SUMMARY_TABLES:
        total += await tombstone_scoped(
            t, org=org, scope_where="user_id = {uid:String}", params={"uid": user_id}, ch=ch
        )
    return total


async def _retire_summaries(
    org: str, user_id: str, *, ch: _CHClient
) -> tuple[int, list[dict[str, Any]]]:
    """The summaries' loom.memory_embeddings and loom.memory rows, and their code links:
    a link's `suggestion` holds a rewrite of the row's text, and this is a mutation, so
    the link queue never sees it. The loom.memory rows are only tombstoned, even in a
    purge, so no new link or read finds them live while the steps that read their keys
    still can. Returns the count and the bound slices."""
    slices = await summary_slices(org, user_id, ch=ch)
    total = 0
    for params in slices:
        total += await tombstone_scoped(
            "loom.memory_embeddings", org=org, scope_where=CONSOLIDATED_IN, params=params, ch=ch
        )
    with purge_scope(False):
        for params in slices:
            total += await tombstone_scoped(
                "loom.memory", org=org, scope_where=CONSOLIDATED_IN, params=params, ch=ch
            )
    # after the tombstones, as every link delete: a link written meanwhile for a row read
    # as live goes too
    rows: set[tuple[str, str]] = set()
    for params in slices:
        found = await ch.query_all(
            "SELECT DISTINCT namespace, toString(memory_id) AS id FROM loom.memory "
            f"WHERE org = {{org:String}} AND {CONSOLIDATED_IN}",
            params={"org": org, **params},
        )
        rows.update((str(r["namespace"]), str(r["id"])) for r in found)
    for part in chunks(sorted(rows)):
        total += await delete_scoped(
            "loom.memory_code_links",
            org=org,
            scope_where="(namespace, memory_id) IN {rows:Array(Tuple(String, UUID))}",
            params={"rows": part},
            ch=ch,
        )
    return total, slices


async def tombstone_consolidated_dependents_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """The consolidated rows the user's memories fed, under another identity. In a
    purge their loom.memory rows are only tombstoned: the cache steps read their keys,
    and `memory.retire` deletes them."""
    total, _ = await _retire_summaries(org, user_id, ch=ch or _default_client())
    return total


_SUMMARY_DERIVED_STEPS = (
    tombstone_shared_embeddings_by_user,
    tombstone_shared_by_user,
    tombstone_memory_chunks_by_user,
    delete_entity_vectors_by_user,
    delete_graph_links_by_user,
    delete_memory_versions_by_user,
)


async def retire_memory_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """After every step that finds rows by the user's memory keys. In a purge,
    `_purge_memory_by_user`. Otherwise it retires what a write made live since the first
    steps: the summaries built from the user's memories (while the user's rows still
    name them), their shared copies, the user's rows, then the cached answers and
    precedents that quote either."""
    ch = ch or _default_client()
    if purge_mode():
        return await _purge_memory_by_user(org, user_id, ch=ch)
    total, _ = await _retire_summaries(org, user_id, ch=ch)
    # a copy promoted since `shared.tombstone`: the census does not count loom.shared
    total += await tombstone_shared_embeddings_by_user(org, user_id, ch=ch)
    total += await tombstone_shared_by_user(org, user_id, ch=ch)
    # the rows written since memory.tombstone are stamped before the answers go, so no
    # new answer quotes them in between
    total += await tombstone_scoped(
        "loom.memory", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )
    return total + await _delete_quoting_answers(org, user_id, ch=ch)


async def _delete_quoting_answers(org: str, user_id: str, *, ch: _CHClient) -> int:
    total = await delete_llm_response_cache_by_user(org, user_id, ch=ch)
    total += await delete_ask_precedents_by_user(org, user_id, ch=ch)
    return total + await delete_code_answers_by_user(org, user_id, ch=ch)


async def _purge_memory_by_user(org: str, user_id: str, *, ch: _CHClient) -> int:
    """`memory.retire` in a purge. A summary written since the steps that delete what
    summaries fed goes with what it fed, before the rows that name it go: after the
    purge nothing, not even the census, finds them."""
    with purge_scope(False):
        await tombstone_scoped(
            "loom.memory", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
        )
    total, slices = await _retire_summaries(org, user_id, ch=ch)
    # the answers before the history goes, as in the saga
    total += await _delete_quoting_answers(org, user_id, ch=ch)
    for step in _SUMMARY_DERIVED_STEPS:
        total += await step(org, user_id, ch=ch)
    for params in slices:
        # counted at their tombstone in this saga; one tombstoned before it is not
        await delete_scoped(
            "loom.memory", org=org, scope_where=CONSOLIDATED_IN, params=params, ch=ch
        )
    total += await tombstone_scoped(
        "loom.memory", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )
    return total


async def tombstone_records_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t in ("loom.memory_records", "loom.memory_record_fields", "loom.memory_record_conflicts"):
        total += await tombstone_scoped(
            t, org=org, scope_where="namespace = {ns:String}", params={"ns": namespace}, ch=ch
        )
    total += await delete_scoped(
        "loom.memory_record_versions",
        org=org,
        scope_where="namespace = {ns:String}",
        params={"ns": namespace},
        ch=ch,
    )
    return total


async def tombstone_records_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Records carry user_id; their fields and conflicts do not — reach them
    through the record ids, read before the records are tombstoned.

    The records' version history (loom.memory_record_versions) is deleted,
    not tombstoned, in both scopes: a tombstone is an ALTER UPDATE, which the
    mirror's view never sees, so the history would keep serving erased
    records."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT toString(record_id) AS id FROM {USER_RECORDS_SUBQ}",
        params={"org": org, "uid": user_id},
    )
    ids = sorted(str(r["id"]) for r in rows)
    total = 0
    for t, scope in (
        ("loom.memory_record_fields", RECORD_IDS_IN),
        ("loom.memory_record_conflicts", CONFLICT_RECORD_IDS_IN),
    ):
        for part in chunks(ids):
            total += await tombstone_scoped(
                t, org=org, scope_where=scope, params={"ids": part}, ch=ch
            )
    total += await tombstone_scoped(
        "loom.memory_records",
        org=org,
        scope_where="user_id = {uid:String}",
        params={"uid": user_id},
        ch=ch,
    )
    total += await delete_scoped(
        "loom.memory_record_versions",
        org=org,
        scope_where="user_id = {uid:String}",
        params={"uid": user_id},
        ch=ch,
    )
    return total


async def delete_ask_precedents_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Precedent answers quote the namespace's memories; no user axis, no
    soft-delete column → ALTER DELETE."""
    return await delete_scoped(
        "loom.memory_ask_precedents",
        org=org,
        scope_where="namespace = {ns:String}",
        params={"ns": namespace},
        ch=ch,
    )


async def delete_llm_response_cache_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Exact-prompt response cache rows quote the namespace's memories in
    their answer text; org/namespace ride the row for this purpose only (the
    key is the prompt). No soft-delete column → ALTER DELETE, as for
    precedents. The table is small (a 7-day TTL)."""
    return await delete_scoped(
        "loom.llm_response_cache",
        org=org,
        scope_where="namespace = {ns:String}",
        params={"ns": namespace},
        ch=ch,
    )


async def delete_code_answers_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Cached code.ask answers that quoted the namespace's memories, in whatever code
    namespace they were asked; a tombstone version an older build wrote mirrors the
    text, so every version goes. A cache: the next ask synthesizes again. The count that
    decides whether to delete reads sequentially, so it sees an answer another replica
    just stored. A lightweight delete: the table carries a vector column a mutation would
    rewrite."""
    with sequential_reads():
        return await delete_scoped(
            "loom.code_answers",
            org=org,
            scope_where=MEMORY_NS_EQ,
            params={"ns": namespace},
            ch=ch,
            lightweight=True,
        )


async def delete_entity_communities_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Community labels are computed over a namespace's entity graph and name
    its entities; no user axis, no soft-delete column → ALTER DELETE."""
    return await delete_scoped(
        "loom.entity_communities",
        org=org,
        scope_where="namespace = {ns:String}",
        params={"ns": namespace},
        ch=ch,
    )


async def delete_llm_response_cache_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Cached answers whose rendered window held any of the user's memory keys, or the
    key of a summary built from them."""
    ch = ch or _default_client()
    total = 0
    for part in chunks(await user_quoted_keys(org, user_id, ch=ch)):
        total += await delete_scoped(
            "loom.llm_response_cache",
            org=org,
            scope_where=WINDOW_KEYS_IN,
            params={"keys": part},
            ch=ch,
        )
    return total


async def delete_ask_precedents_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Precedent answers whose window or citations held any of the user's keys, or the
    key of a summary built from them."""
    ch = ch or _default_client()
    total = 0
    for part in chunks(await user_quoted_keys(org, user_id, ch=ch)):
        total += await delete_scoped(
            "loom.memory_ask_precedents",
            org=org,
            scope_where=PRECEDENT_KEYS_IN,
            params={"keys": part},
            ch=ch,
        )
    return total


async def delete_code_answers_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Cached code.ask answers whose memory excerpts held any of the user's keys or a
    summary built from them, read sequentially as for a namespace."""
    ch = ch or _default_client()
    total = 0
    with sequential_reads():
        for part in chunks(await user_quoted_keys(org, user_id, ch=ch)):
            total += await delete_scoped(
                "loom.code_answers",
                org=org,
                scope_where=MEMORY_KEYS_IN,
                params={"keys": part},
                ch=ch,
                lightweight=True,
            )
    return total


async def delete_entity_communities_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Community rows for (or labelled by) the entities only this user's memories (or
    summaries of them) carried. Must run BEFORE `graph_links.delete` (which runs it
    again first): ownership is read from `entity_edge_memories`."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT cityHash64(namespace, entity) AS h FROM {USER_COMMUNITY_ENTITIES}",
        params={"org": org, "uid": user_id},
    )
    total = 0
    for part in chunks(hashes(rows)):
        total += await delete_scoped(
            "loom.entity_communities",
            org=org,
            scope_where=COMMUNITY_ROWS_IN,
            params={"entities": part},
            ch=ch,
        )
    return total
