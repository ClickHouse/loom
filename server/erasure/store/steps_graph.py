"""Erasure steps for the substrates derived from memory: source and graph
links, the entity graph and its vectors, summaries, records, and the answer
cache, precedents and communities that quote memory text."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, NamedTuple

from server.core.ch_params import FIELD_BYTES, chunks
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import (
    delete_scoped,
    namespaces_of,
    tombstone_scoped,
)
from server.erasure.store.predicates import (
    COMMUNITY_ROWS_IN,
    CONFLICT_RECORD_IDS_IN,
    NS_EQ,
    NS_IN,
    PARENT_KEYS_IN,
    PRECEDENT_KEYS_IN,
    RECORD_IDS_IN,
    UID,
    USER_COMMUNITY_ENTITIES,
    USER_KEYS_SUBQ,
    USER_RECORDS_SUBQ,
    WINDOW_KEYS_IN,
)
from server.erasure.store.scope_reads import (
    hashes,
    slices_or_one_empty,
    user_key_pairs,
    user_keys,
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
    different one — both sides of the (namespace, memory_key) identity."""
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
    return total


def _graph_owned_only_sql(link_table: str, cols: tuple[str, ...]) -> str:
    """The hashes of the graph keys whose every linking memory belongs to the user. A
    triple or edge shared with another user's memory stays: the other user still knows
    it. `cols` names the graph key columns as they appear on both the graph table and
    its link table."""
    key = ", ".join(cols)
    return (
        f"SELECT cityHash64(namespace, {key}) AS h FROM {link_table} FINAL "
        "WHERE org = {org:String} "
        f"GROUP BY namespace, {key} "
        f"HAVING countIf((namespace, memory_key) IN {USER_KEYS_SUBQ}) > 0 "
        f"AND countIf((namespace, memory_key) NOT IN {USER_KEYS_SUBQ}) = 0"
    )


_GRAPH_LINKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("loom.entity_triples", "loom.entity_triple_memories", ("subject", "relation", "object")),
    ("loom.entity_edges", "loom.entity_edge_memories", ("entity_a", "entity_b")),
)


async def delete_entity_graph_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Delete the triples and edges only this user's memories carried. Must
    run BEFORE `graph_links.delete`: ownership is read from the link tables.

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
    ch = ch or _default_client()
    keys = await user_key_pairs(org, user_id, ch=ch)
    total = 0
    for t in ROW_LINK_TABLES:
        total += await delete_scoped(t, org=org, scope_where=UID, params={"uid": user_id}, ch=ch)
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


# Summaries synthesised FROM an erased user's rows. A consolidated topic
# memory (or merged canonical row) lives under the identity that ran the
# consolidation — often `user_id='-'` — and carries its sources' content
# verbatim by design, tagged `src:<key>`. The per-user tombstone above never
# reaches it. A `src:` tag names a key of the summary's own namespace, as consolidation
# writes it (a copy into another namespace keeps the tags, and is not the user's there);
# the next dense pass rebuilds a retired summary from the surviving sources. The user's
# tags let the skip index prune; their (namespace, tag) pairs decide.
_CONSOLIDATED = (
    "has(tags, 'consolidated') AND hasAny(tags, {src:Array(String)}) "
    "AND hasAny(arrayMap(t -> (namespace, t), tags), {pairs:Array(Tuple(String, String))})"
)


def _consolidated_params(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """The bound values of `_CONSOLIDATED` for a slice of the user's (namespace, key)."""
    tagged = [(ns, "src:" + key) for ns, key in pairs]
    return {"src": sorted({tag for _, tag in tagged}), "pairs": tagged}


async def tombstone_consolidated_dependents_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """The consolidated rows the user's memories fed, under another identity. Their code
    links go too: a link's `suggestion` holds a rewrite of the row's text, and this
    tombstone is a mutation, so the link queue never sees it."""
    ch = ch or _default_client()
    # Pairs are measured without the `src:` prefix they are bound with: half a field
    # leaves room for it.
    parts = list(chunks(await user_key_pairs(org, user_id, ch=ch), limit=FIELD_BYTES // 2))
    total = 0
    for t in ("loom.memory", "loom.memory_embeddings"):
        for part in parts:
            total += await tombstone_scoped(
                t, org=org, scope_where=_CONSOLIDATED, params=_consolidated_params(part), ch=ch
            )
    # after the tombstones, as every link delete: a link written meanwhile for a row read
    # as live goes too
    rows: set[tuple[str, str]] = set()
    for part in parts:
        found = await ch.query_all(
            "SELECT DISTINCT namespace, toString(memory_id) AS id FROM loom.memory "
            f"WHERE org = {{org:String}} AND {_CONSOLIDATED}",
            params={"org": org, **_consolidated_params(part)},
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
    ch = ch or _default_client()
    total = 0
    for part in chunks(await user_keys(org, user_id, ch=ch)):
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
    """Precedent answers whose window or citations held any of the user's keys."""
    ch = ch or _default_client()
    total = 0
    for part in chunks(await user_keys(org, user_id, ch=ch)):
        total += await delete_scoped(
            "loom.memory_ask_precedents",
            org=org,
            scope_where=PRECEDENT_KEYS_IN,
            params={"keys": part},
            ch=ch,
        )
    return total


async def delete_entity_communities_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Community rows for (or labelled by) the entities only this user's
    memories carried. Must run BEFORE `graph_links.delete`: ownership is read
    from `entity_edge_memories`."""
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
