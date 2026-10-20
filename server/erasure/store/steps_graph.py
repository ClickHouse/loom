"""Erasure steps for the substrates derived from memory: source and graph
links, the entity graph and its vectors, summaries, records, and the answer
cache, precedents and communities that quote memory text."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple

from server.core.repo_base import _CHClient, _default_client, param_slices
from server.erasure.store.mutations import (
    count_then_mutate,
    delete_scoped,
    namespaces_of,
    tombstone_scoped,
)
from server.erasure.store.predicates import (
    NS_EQ,
    NS_IN,
    UID,
    USER_COMMUNITY_ROWS,
    USER_CONFLICT_RECORD_IDS,
    USER_KEYS_SUBQ,
    USER_PARENT_KEYS,
    USER_PRECEDENT_KEYS,
    USER_RECORD_IDS,
    USER_WINDOW_KEYS,
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

# Entity-graph link tables: no soft-delete column → ALTER DELETE.
# Each with how a user's links are found: a key link by the user's keys, a row
# link by the writer it carries, which outlives the row.
_LINK_TABLES_DELETE = (
    ("loom.entity_triple_memories", USER_PARENT_KEYS),
    ("loom.entity_triple_rows", UID),
    ("loom.entity_edge_memories", USER_PARENT_KEYS),
    ("loom.entity_edge_rows", UID),
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
    total = 0
    for t in _LINK_TABLES_TOMBSTONE:
        total += await tombstone_scoped(
            t, org=org, scope_where=USER_PARENT_KEYS, params={"uid": user_id}, ch=ch
        )
    return total


async def delete_graph_links_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for t, _ in _LINK_TABLES_DELETE:
        total += await delete_scoped(
            t, org=org, scope_where="namespace = {ns:String}", params={"ns": namespace}, ch=ch
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
        for ids in param_slices(sorted(str(r["id"]) for r in found)):
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
    different one — both sides of the (namespace, memory_key) identity."""
    return await delete_scoped(
        "loom.entity_vectors",
        org=org,
        scope_where=f"(user_id = {{uid:String}} OR {USER_PARENT_KEYS})",
        params={"uid": user_id},
        lightweight=True,
        ch=ch,
    )


def _graph_owned_only_where(link_table: str, cols: str) -> str:
    """Rows of a graph table whose every linking memory belongs to the user.
    A triple or edge shared with another user's memory
    stays: the other user still knows it. `cols` names the graph key
    columns as they appear on both the graph table and its link table."""
    return (
        f"org = {{org:String}} AND (namespace, {cols}) IN ("
        f"SELECT namespace, {cols} FROM {link_table} FINAL "
        f"WHERE org = {{org:String}} "
        f"GROUP BY namespace, {cols} "
        f"HAVING countIf((namespace, memory_key) IN {USER_KEYS_SUBQ}) > 0 "
        f"AND countIf((namespace, memory_key) NOT IN {USER_KEYS_SUBQ}) = 0)"
    )


_GRAPH_LINKS: tuple[tuple[str, str, str], ...] = (
    ("loom.entity_triples", "loom.entity_triple_memories", "subject, relation, object"),
    ("loom.entity_edges", "loom.entity_edge_memories", "entity_a, entity_b"),
)


async def delete_entity_graph_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Delete the triples and edges only this user's memories carried. Must
    run BEFORE `graph_links.delete`: ownership is read from the link tables."""
    ch = ch or _default_client()
    total = 0
    for table, link_table, cols in _GRAPH_LINKS:
        where = _graph_owned_only_where(link_table, cols)
        total += await count_then_mutate(
            table,
            count_where=where,
            mutation="DELETE",
            mutation_where=where,
            params={"org": org, "uid": user_id},
            final=False,
            ch=ch,
        )
    return total


async def delete_graph_links_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    total = 0
    for t, user_scope in _LINK_TABLES_DELETE:
        total += await delete_scoped(
            t, org=org, scope_where=user_scope, params={"uid": user_id}, ch=ch
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


# Summaries synthesised FROM an erased user's rows. A consolidated topic
# memory (or merged canonical row) lives under the identity that ran the
# consolidation — often `user_id='-'` — and carries its sources' content
# verbatim by design, tagged `src:<key>`. The per-user tombstone above never
# reaches it. Any overlap with the erased user's keys retires the summary;
# the next dense pass rebuilds one from the surviving sources.
USER_SRC_TAGS = (
    "(SELECT groupArray(concat('src:', memory_key)) FROM loom.memory "
    "WHERE org = {org:String} AND user_id = {uid:String})"
)


_CONSOLIDATED = f"has(tags, 'consolidated') AND hasAny(tags, {USER_SRC_TAGS})"


async def tombstone_consolidated_dependents_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """The consolidated rows the user's memories fed, under another identity. Their code
    links go too: a link's `suggestion` holds a rewrite of the row's text, and this
    tombstone is a mutation, so the link queue never sees it."""
    total = 0
    for t in ("loom.memory", "loom.memory_embeddings"):
        total += await tombstone_scoped(
            t, org=org, scope_where=_CONSOLIDATED, params={"uid": user_id}, ch=ch
        )
    # after the tombstones, as every link delete: a link written meanwhile for a row read
    # as live goes too
    total += await delete_scoped(
        "loom.memory_code_links",
        org=org,
        scope_where=(
            "memory_id IN (SELECT memory_id FROM loom.memory "
            f"WHERE org = {{org:String}} AND {_CONSOLIDATED})"
        ),
        params={"uid": user_id},
        ch=ch,
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
    through the record ids. Fields/conflicts first so the record subquery
    still sees the (tombstoned or not) parent rows.

    The records' version history (loom.memory_record_versions) is deleted,
    not tombstoned, in both scopes: a tombstone is an ALTER UPDATE, which the
    mirror's view never sees, so the history would keep serving erased
    records."""
    total = 0
    for t, scope in (
        ("loom.memory_record_fields", USER_RECORD_IDS),
        ("loom.memory_record_conflicts", USER_CONFLICT_RECORD_IDS),
    ):
        total += await tombstone_scoped(
            t, org=org, scope_where=scope, params={"uid": user_id}, ch=ch
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
    """Cached answers whose rendered window held any of the user's memory keys."""
    return await delete_scoped(
        "loom.llm_response_cache",
        org=org,
        scope_where=USER_WINDOW_KEYS,
        params={"uid": user_id},
        ch=ch,
    )


async def delete_ask_precedents_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Precedent answers whose window or citations held any of the user's keys."""
    return await delete_scoped(
        "loom.memory_ask_precedents",
        org=org,
        scope_where=USER_PRECEDENT_KEYS,
        params={"uid": user_id},
        ch=ch,
    )


async def delete_entity_communities_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Community rows for (or labelled by) the entities only this user's
    memories carried. Must run BEFORE `graph_links.delete`: ownership is read
    from `entity_edge_memories`."""
    return await delete_scoped(
        "loom.entity_communities",
        org=org,
        scope_where=USER_COMMUNITY_ROWS,
        params={"uid": user_id},
        ch=ch,
    )
