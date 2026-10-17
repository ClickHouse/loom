"""Reads of loom.memory heads: point lookups by key and by id, bulk reads by
key, the namespace list and count, the change watermark and the
entity-overlap neighbours. Outputs are `MemoryRow`s or plain row dicts.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and, scope_where
from server.memory.domain.temporal_sql import as_of_predicate
from server.memory.store.rows import Identity, MemoryRow, _hydrate
from server.memory.store.sql import (
    _IDENTITY_KEY_TUPLE,
    _MEMORY_FINAL_PROJECTION,
    _SEQUENTIAL,
    _identity_key,
    _is_full_row,
    read_your_writes_enabled,
)

# The key columns a bulk read locates its heads by before fetching the rows
# without FINAL.
_LOCATE_COLUMNS = "memory_id, version, agent, user_id, session_id, memory_key"


# The strong bulk read's fetch — the whole row by its exact (memory_id,
# version), without FINAL, for a caller that writes from it.
_BULK_FETCH_WHOLE_SQL = (
    _MEMORY_FINAL_PROJECTION.replace("FROM loom.memory FINAL", "FROM loom.memory")
    + """
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
        LIMIT 1 BY memory_id, version"""
)


async def _point_lookup(
    ch: _CHClient, sql: str, params: dict[str, Any], *, fresh: bool, strong: bool = False
) -> dict[str, Any] | None:
    """One point lookup; on a miss, one more with sequential consistency when
    `fresh` says the row may have been written moments ago.

    `strong` reads with sequential consistency FIRST. A miss is not the only
    way a lagging replica lies: a row that was just re-versioned can come
    back as its previous version — a hit, so the fresh-on-miss retry never
    fires. A read-modify-write that trusts that hit re-applies work already
    folded into the newer version, and the content-hash precondition that
    should catch it is checked with the same lagging read and passes. Pay
    the metadata round-trip where the answer feeds a write."""
    if strong and read_your_writes_enabled():
        return await ch.query_one(sql.rstrip() + "\n        " + _SEQUENTIAL + "\n", params=params)
    row = await ch.query_one(sql, params=params)
    if row is None and fresh and read_your_writes_enabled():
        row = await ch.query_one(sql.rstrip() + "\n        " + _SEQUENTIAL + "\n", params=params)
    return row


async def get_by_key(
    identity: Identity,
    memory_key: str,
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
    as_of: datetime | None = None,
    include_tombstoned: bool = False,
    include_usage_totals: bool = False,
    fresh: bool = False,
    strong: bool = False,
    include_expired: bool = True,
) -> MemoryRow | None:
    """Return the active memory at this identity + key, or None.

    `include_expired=False` hides a row whose `expires_at` has passed, the
    way search does; the default keeps expired rows visible because most
    callers are write paths that must carry the row's columns
    forward (set, touch, lock, delete) or decide about its expiry
    (update_by_id's EXPIRED). Ignored under `as_of`: a replay read asks
    about a past instant, not about now.

    FINAL because loom.memory is ReplacingMergeTree(version).

    `include_tombstoned=True` keeps tombstoned rows so callers
    (memory.restore) can rebuild a full version row via
    build_memory_version_row.

    `as_of` restricts to rows valid as of that time (replay). The OR form
    (not coalesce) keeps the `created_at` index prune fast.

    `scope`: the request's read scope (`identity.domain.read_scope`); a
    write path that reads the row it replaces passes `WHOLE_NAMESPACE`.
    """
    ch = ch or _default_client()
    as_of_clause = "AND " + as_of_predicate("valid_time") if as_of is not None else ""
    tomb_clause = "" if include_tombstoned else "AND tombstoned_at IS NULL"
    if not include_expired and as_of is None:
        tomb_clause += " AND (expires_at IS NULL OR expires_at > now())"
    params: dict[str, Any] = {
        "org": identity.org,
        "ns": identity.namespace,
        "agent": identity.agent,
        "uid": identity.user_id,
        "sid": identity.session_id,
        "key": memory_key,
    }
    if as_of is not None:
        params["as_of"] = as_of
    # Wildcard agent ⇒ namespace-wide lookup, matching how memory.list /
    # memory.search treat `*` ("drop the per-agent filter"). Used by the
    # dashboard search re-fetch to hydrate a hit whose writer agent the caller
    # doesn't know. Ambiguity across agents sharing a key is resolved to the
    # most-recently-written row.
    if identity.agent == "*":
        identity_clause = ""
        order_clause = "ORDER BY version DESC, created_at DESC"
    else:
        identity_clause = (
            "AND agent = {agent:String} AND user_id = {uid:String} AND session_id = {sid:String}"
        )
        order_clause = ""
    identity_clause += scope_and(scope, params)
    # The sort key goes in PREWHERE, applied per
    # part before the FINAL merge, so only the granules that hold the key
    # read their remaining ~60 columns. Sort-key columns are identical across
    # a row's versions, so the filter cannot drop a newer version or
    # resurrect an older one; `tombstoned_at`, `expires_at` and the as-of
    # window are not key columns and stay in WHERE, after the merge.
    post_clauses = " ".join(c for c in (tomb_clause, as_of_clause) if c)
    where_clause = f"WHERE {post_clauses[4:]}" if post_clauses.startswith("AND ") else ""
    # A point read's cost is the value column of the granule that holds the
    # row, whatever the statement shape (the same row read with or without
    # FINAL, located first or not, costs one granule); the table's granule is
    # small. One statement.
    row = await _point_lookup(
        ch,
        f"""{_MEMORY_FINAL_PROJECTION}
        PREWHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND memory_key = {{key:String}}
          {identity_clause}
        {where_clause}
        {order_clause}
        LIMIT 1
        """,
        params,
        fresh=fresh,
        strong=strong,
    )
    if row is None:
        return None
    hydrated = _hydrate(row)
    return await _overlay_usage_totals(hydrated, ch) if include_usage_totals else hydrated


async def get_by_id(
    memory_id: str,
    *,
    scope: ReadFilters,
    org: str | None = None,
    ch: _CHClient | None = None,
    as_of: datetime | None = None,
    include_usage_totals: bool = False,
    fresh: bool = False,
    strong: bool = False,
) -> MemoryRow | None:
    """Lookup by surrogate ID. Used by ops that address memories by their
    UUID rather than the (identity, key) tuple — `memory.update_by_id`,
    conflict-resolution flows, audit-log replays.

    `org` (recommended on user-facing paths): when supplied, filters
    the lookup by `org` as well as memory_id. Required for any caller
    that doesn't already trust the memory_id came from the same tenant
    — without it, a leaked memory_id from one tenant could surface
    another tenant's row.

    `as_of`: bi-temporal filter — see `get_by_key`. `fresh`: the row may have
    been written moments ago — on a miss, read once more with sequential
    consistency (see `_point_lookup`).

    `scope`: a row outside the read scope is not found (None), never refused.
    """
    ch = ch or _default_client()
    params: dict[str, Any] = {"mid": memory_id}
    org_pred = ""
    if org is not None:
        org_pred = "AND org = {org:String}"
        params["org"] = org
    org_pred += scope_and(scope, params)
    # Discover the row's org, namespace, created_at and memory_key without
    # FINAL, then read by them. loom.memory is sorted by (org, namespace,
    # agent, user_id, session_id, memory_key); memory_id is not in the sort
    # key and is found by its bloom-filter skip index, but under FINAL the
    # skip-index selection is re-expanded to every granule that shares the primary-key range
    # (PrimaryKeyExpand) — every column of most granules, ~300 MiB for one
    # row. The key columns never change across a memory's versions, so any
    # version answers phase 1; phase 2 reads under FINAL where the key leaves
    # nothing to expand (2 granules).
    key = await _point_lookup(
        ch,
        f"""
        SELECT org, namespace, created_at, memory_key
        FROM loom.memory
        WHERE memory_id = {{mid:UUID}} {org_pred}
        LIMIT 1
        """,
        params,
        fresh=fresh,
        strong=strong,
    )
    if key is None:
        return None
    # The three sort-key columns the key read returned go in PREWHERE (per
    # part, before the FINAL merge); created_at, memory_id and the tombstone
    # are not key columns and stay in WHERE.
    prewhere = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "memory_key = {k:String}",
    ]
    where = [
        "created_at = {ca:DateTime}",
        "memory_id = {mid:UUID}",
        "tombstoned_at IS NULL",
    ]
    params = {
        **params,
        "org": str(key["org"]),
        "ns": str(key["namespace"]),
        "ca": key["created_at"],
        "k": str(key["memory_key"]),
    }
    if as_of is not None:
        where.append(as_of_predicate("valid_time"))
        params["as_of"] = as_of

    row = await _point_lookup(
        ch,
        f"""{_MEMORY_FINAL_PROJECTION}
        PREWHERE {" AND ".join(prewhere)}
        WHERE {" AND ".join(where)}
        LIMIT 1
        """,
        params,
        fresh=fresh,
        strong=strong,
    )
    if row is None:
        return None
    hydrated = _hydrate(row)
    return await _overlay_usage_totals(hydrated, ch) if include_usage_totals else hydrated


async def memory_rows_by_identity_keys(
    keys: Sequence[tuple[str, str, str, str, str, str]],
    *,
    ch: _CHClient | None = None,
    sequential: bool = False,
) -> dict[tuple[str, str, str, str, str, str], MemoryRow]:
    """`get_by_key` for N exact identity keys in one statement: the same
    projection under FINAL, the full sort key in PREWHERE, tombstones
    filtered after the merge. Keyed by the six-tuple; a key with no live row
    is absent. `sequential` reads with sequential consistency (a write
    decides on the answer)."""
    if not keys:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""{_MEMORY_FINAL_PROJECTION}
        PREWHERE (org, namespace, agent, user_id, session_id, memory_key)
                 IN {{keys:{_IDENTITY_KEY_TUPLE}}}
        WHERE tombstoned_at IS NULL
        {_SEQUENTIAL if sequential else ""}
        """,
        params={"keys": [tuple(str(x) for x in k) for k in keys]},
    )
    return {_identity_key(r): _hydrate(r) for r in rows}


async def key_holds_established_fact(
    org: str, namespace: str, memory_key: str, *, ch: _CHClient | None = None
) -> bool:
    """Whether any identity of the namespace holds `memory_key`
    as a live, accepted fact (active, conflicted or duplicate) — the guard a
    proposal write takes. Read with sequential consistency: it gates a write."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT count() AS n
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key = {{key:String}} AND tombstoned_at IS NULL
          AND status IN ('active', 'conflicted', 'duplicate')
        {_SEQUENTIAL if read_your_writes_enabled() else ""}
        """,
        params={"org": org, "ns": namespace, "key": memory_key},
    )
    return int((row or {}).get("n") or 0) > 0


async def actors_by_key(
    org: str,
    keys: Sequence[tuple[str, str]],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str], list[dict[str, str]]]:
    """The live rows under each (namespace, memory_key), read inside `scope`, as
    `{agent, user_id, session_id, actor}`: one entry per row, so a caller labels a recalled
    item with the writer of the row it came from, not every writer of the key. Rows with
    no recorded actor are left out."""
    if not keys:
        return {}
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "keys": sorted({(str(n), str(k)) for n, k in keys})}
    where = [
        "org = {org:String}",
        "(namespace, memory_key) IN {keys:Array(Tuple(String, String))}",
        "tombstoned_at IS NULL",
        "actor != ''",
        *scope_where(scope, params),
    ]
    rows = await ch.query_all(
        f"""
        SELECT namespace, memory_key, agent, user_id, session_id, actor
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        """,
        params=params,
    )
    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    for r in rows:
        row = {c: str(r[c]) for c in ("agent", "user_id", "session_id", "actor")}
        out.setdefault((str(r["namespace"]), str(r["memory_key"])), []).append(row)
    return out


async def get_many_by_keys(
    identity: Identity,
    memory_keys: list[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
    strong: bool = False,
) -> list[MemoryRow]:
    """Bulk read: one CH round-trip for N keys under one identity tuple.

    Used by memory.bulk_get. Tombstoned rows are filtered out. Empty
    `memory_keys` → empty result, no CH call.
    """
    if not memory_keys:
        return []
    ch = ch or _default_client()
    # agent="*" is the documented wildcard — fan out across agents (mirrors
    # search_sql.py); procedure.match relies on it. The "-" sentinel stays a
    # literal match (it's a real stored agent value, not a wildcard).
    params: dict[str, Any] = {
        "org": identity.org,
        "ns": identity.namespace,
        "keys": memory_keys,
    }
    agent_clause = ""
    wildcard_agent = identity.agent == "*"
    if not wildcard_agent:
        agent_clause = "AND agent = {agent:String}"
        params["agent"] = identity.agent
    # user_id="*" opens the user axis the same way: memory.demonstrate
    # resolves examples written by every user. Under the wildcard agent the
    # unset ("-") user and session
    # axes open too, as `get_by_key` reads them — agent="*" is the
    # namespace-wide lookup, so a row written under a real user_id or
    # session_id is found by bulk_get as it is by memory.get. A pinned
    # (non-sentinel) user_id or session_id still filters.
    user_clause = ""
    if identity.user_id != "*" and not (wildcard_agent and identity.user_id == "-"):
        user_clause = "AND user_id = {uid:String}"
        params["uid"] = identity.user_id
    # session_id="*" is the same wildcard on the session axis:
    # the extraction pipeline resolves the rows its vector hits name across
    # every earlier session of the identity, not only the one being written.
    session_clause = ""
    if identity.session_id != "*" and not (wildcard_agent and identity.session_id == "-"):
        session_clause = "AND session_id = {sid:String}"
        params["sid"] = identity.session_id
    session_clause += scope_and(scope, params)
    # With any axis open, several identities may hold the same key: the
    # newest row per key answers, the way `get_by_key`'s wildcard read orders
    # (version DESC, created_at DESC) and takes one.
    one_per_key = (
        "ORDER BY memory_key ASC, version DESC, created_at DESC LIMIT 1 BY memory_key"
        if not (agent_clause and user_clause and session_clause)
        else ""
    )
    # Locate the heads (key columns only) under
    # FINAL, then fetch the rows by their exact (memory_id, version) without
    # FINAL — 19 MB → about 1 MB for twenty keys on a namespace of 5 KB values.
    body = f"""
        SELECT {_LOCATE_COLUMNS}, created_at
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}}
          AND tombstoned_at IS NULL
          {agent_clause}
          {user_clause}
          {session_clause}
        {one_per_key}
    """
    # `strong`: the heads feed a write, so locate them with
    # sequential consistency first, as `_point_lookup` does for one key.
    if strong and read_your_writes_enabled():
        rows = await ch.query_all(f"{body}\n            {_SEQUENTIAL}", params=params)
    else:
        rows = await ch.query_all(body, params=params)
    # A bulk read that follows bulk writes (the OKF exporter, set_from_messages
    # → bulk_get) can miss SOME keys on a lagging replica. Same rule as
    # _point_lookup: when fewer distinct keys came back than were asked for,
    # read once more with sequential consistency. Keys the caller never wrote
    # cost that one extra read; correctness of the ones it did wins.
    wanted = {k for k in memory_keys if k}
    if len({r["memory_key"] for r in rows}) < len(wanted) and read_your_writes_enabled():
        again = await ch.query_all(f"{body}\n            {_SEQUENTIAL}", params=params)
        # A consistent read can only add rows; never let it shrink the answer.
        if len(again) >= len(rows):
            rows = again
    if not rows or all(_is_full_row(r) for r in rows):
        return [_hydrate(r) for r in rows]
    # `strong`: the heads feed a write — memory.touch writes
    # a version row from each — so the fetch takes whole rows (every column
    # the builder carries forward), never bulk_get's subset: a missing column
    # is zeroed or refused. Same pairs, no FINAL.
    fetch_sql = _BULK_FETCH_WHOLE_SQL + f"\n        {_SEQUENTIAL}" if strong else _BULK_FETCH_SQL
    fetched = await ch.query_all(
        fetch_sql,
        params={
            "org": identity.org,
            "ns": identity.namespace,
            "pairs": [(str(r["memory_id"]), int(r["version"])) for r in rows],
        },
    )
    order = {(str(r["memory_id"]), int(r["version"])): i for i, r in enumerate(rows)}
    fetched.sort(key=lambda r: order.get((str(r["memory_id"]), int(r["version"])), len(order)))
    return [_hydrate(r) for r in fetched]


# The bulk read's second statement — the located
# (memory_id, version) pairs, read without FINAL. (memory_key, version) is
# not unique in a namespace: the same key lives under several identities, and
# one write can stamp one version on rows of several identities
# (apply_entities_batch). memory_id names one identity's key, so a pair names
# one write and `LIMIT 1 BY` only guards an insert that was retried.
_BULK_FETCH_SQL = """
        SELECT
            memory_id,
            org, namespace, agent, user_id, session_id,
            memory_key, memory_type, value, confidence, version, pinned,
            created_at, updated_at, expires_at, valid_at, status, run_id, source_trace,
            char_limit, tags, entities, content_sha256,
            verified_by, verified_at, verified_sha256, actor, source_class
        FROM loom.memory
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
        LIMIT 1 BY memory_id, version
    """


async def find_related_by_entities(
    identity: Identity,
    memory_key: str,
    *,
    scope: ReadFilters,
    top_k: int = 10,
    min_overlap: int = 1,
    ch: _CHClient | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (source_entities, related_rows) for `memory.related`.

    Two-step query: first fetch the source row's entities, then scan
    the namespace for active rows that intersect the source entities,
    ranked by overlap count desc.

    Returns:
      - source_entities: the entity list on the source row (may be []
        if the extractor hasn't processed it or is disabled).
      - related_rows: list of {memory_key, memory_type, content_excerpt,
        overlap_count, shared_entities} for matches. Self is excluded.

    If source_entities is empty, related_rows is also empty (no query).
    Both statements stay inside `scope`.
    """
    ch = ch or _default_client()
    source_params: dict[str, Any] = {
        "org": identity.org,
        "ns": identity.namespace,
        "agent": identity.agent,
        "uid": identity.user_id,
        "sid": identity.session_id,
        "key": memory_key,
    }
    source_scope = scope_and(scope, source_params)
    source = await ch.query_one(
        f"""
        SELECT entities
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND agent = {{agent:String}}
          AND user_id = {{uid:String}}
          AND session_id = {{sid:String}}
          AND memory_key = {{key:String}}
          AND tombstoned_at IS NULL
          {source_scope}
        LIMIT 1
        """,
        params=source_params,
    )
    if source is None:
        return ([], [])
    source_entities: list[str] = list(source.get("entities") or [])
    if not source_entities:
        return ([], [])

    # Rank on the columns that rank. The namespace scan reads `entities` (and
    # the key columns) for every candidate (about 1.4 MB), and the excerpts of
    # the top_k winners alone are read afterwards by their exact (memory_id,
    # version), from the stored 2 KB preview, never the
    # value column of every candidate (66 MB for four hits).
    related_params: dict[str, Any] = {
        "org": identity.org,
        "ns": identity.namespace,
        "ents": source_entities,
        "key": memory_key,
        "min_overlap": int(min_overlap),
        "top_k": int(top_k),
    }
    related_scope = scope_and(scope, related_params)
    rows = await ch.query_all(
        f"""
        SELECT
            memory_id,
            memory_key,
            memory_type,
            version,
            arrayIntersect(entities, {{ents:Array(String)}}) AS shared_entities,
            length(arrayIntersect(entities, {{ents:Array(String)}})) AS overlap_count
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND tombstoned_at IS NULL
          AND memory_key != {{key:String}}
          AND hasAny(entities, {{ents:Array(String)}})
          AND length(arrayIntersect(entities, {{ents:Array(String)}})) >= {{min_overlap:UInt32}}
          {related_scope}
        ORDER BY overlap_count DESC, created_at DESC
        LIMIT {{top_k:UInt32}}
        """,
        params=related_params,
    )
    if not rows or all("content_excerpt" in r for r in rows):
        return (source_entities, rows)
    excerpts = await ch.query_all(
        """
        SELECT memory_id, version, substring(value_preview, 1, 1200) AS content_excerpt
        FROM loom.memory
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
        LIMIT 1 BY memory_id, version
        """,
        params={
            "org": identity.org,
            "ns": identity.namespace,
            "pairs": [(str(r["memory_id"]), int(r["version"])) for r in rows],
        },
    )
    by_pair = {(str(e["memory_id"]), int(e["version"])): e["content_excerpt"] for e in excerpts}
    return (
        source_entities,
        [
            {**r, "content_excerpt": by_pair.get((str(r["memory_id"]), int(r["version"])), "")}
            for r in rows
        ],
    )


# OFFSET pages need a total order: created_at is whole-second (a bulk write
# lands many rows in one second) and memory_key repeats across identities under
# a `*` axis. (memory_key, agent, user_id, session_id) is unique under FINAL with
# org + namespace fixed, so it breaks every tie.
_LIST_TIE = "memory_key, agent, user_id, session_id"
_LIST_ORDER = {
    "memory_key_asc": f"ORDER BY {_LIST_TIE}",
    "created_at_desc": f"ORDER BY created_at DESC, {_LIST_TIE}",
    "created_at_asc": f"ORDER BY created_at ASC, {_LIST_TIE}",  # anchor_first tier
}


# The keyword filter of a list: the value or the key holds the text, any case. A scan of
# the namespace's values (no text index on `value`), so it is offered on the namespace-scoped
# list only, where the primary key bounds the read to one namespace.
CONTAINS_PREDICATE = (
    "(positionCaseInsensitiveUTF8(value, {contains:String}) > 0"
    " OR positionCaseInsensitiveUTF8(memory_key, {contains:String}) > 0)"
)


def tag_filter(tags: Sequence[str], *, any_case: bool = False) -> tuple[str, list[str]]:
    """The hard OR tag predicate and its values: exact, or with `any_case` the row's tags
    and the asked ones compared lowercased."""
    if any_case:
        return "hasAny(arrayMap(t -> lower(t), tags), {tags:Array(String)})", [
            str(t).lower() for t in tags
        ]
    return "hasAny(tags, {tags:Array(String)})", [str(t) for t in tags]


async def list_by_namespace(
    identity: Identity,
    *,
    scope: ReadFilters,
    memory_type: str | None = None,
    pinned_only: bool = False,
    key_prefix: str | None = None,
    source_trace: str | None = None,
    tags: Sequence[str] | None = None,
    tags_any_case: bool = False,
    contains: str | None = None,
    order_by: str = "created_at_desc",
    limit: int = 200,
    offset: int = 0,
    include_tombstoned: bool = False,
    as_of: datetime | None = None,
    ch: _CHClient | None = None,
    preview: bool = False,
    exclude_high_sensitivity: bool = False,
    include_duplicates: bool = True,
    include_superseded: bool = True,
    include_proposed: bool = True,
) -> list[MemoryRow]:
    """List active memories under (org, namespace, agent, user, session).

    Filters:
      - memory_type: exact match (e.g. "preference", "procedural")
      - pinned_only: only rows where pinned=1
      - key_prefix:  startsWith(memory_key, prefix) — for hierarchical key spaces
      - tags:        hasAny(tags, [...]) — the OR filter memory.search applies
      - contains:    case-insensitive substring of the value or the key — the
                     keyword filter a browse page's search box applies

    Ordering:
      - "created_at_desc" (default) — recent-first feed shape
      - "memory_key_asc"             — stable alphabetical listing shape

    Bounded by `limit` (default 200) — not unbounded. Every order ends in
    (memory_key, agent, user_id, session_id), so OFFSET pages neither skip
    nor repeat a row.
    """
    ch = ch or _default_client()
    # Wildcard `"*"` on any identity-tuple axis drops that axis from WHERE
    # (the dashboard's "everything in this namespace" view); exact-match
    # is the default so agent reads stay isolated to their own slice.
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
    ]
    if not include_tombstoned:
        where.append("tombstoned_at IS NULL")
    params: dict[str, Any] = {
        "org": identity.org,
        "ns": identity.namespace,
        "limit": int(limit),
        "offset": int(offset),
    }
    if exclude_high_sensitivity:
        # Recency reads (startup pack) are casual retrieval; a 'high' row is
        # reachable by key only.
        where.append("sensitivity != 'high'")
    if not include_duplicates:
        # A duplicate's canonical is in the same scan.
        where.append("status != 'duplicate'")
    if not include_superseded:
        # A retired row is served only when asked.
        where.append("status != 'superseded'")
    if not include_proposed:
        # A fact on trial is served only when asked.
        where.append("status != 'proposed'")
    if identity.agent != "*":
        where.append("agent = {agent:String}")
        params["agent"] = identity.agent
    if identity.user_id != "*":
        where.append("user_id = {uid:String}")
        params["uid"] = identity.user_id
    if identity.session_id != "*":
        where.append("session_id = {sid:String}")
        params["sid"] = identity.session_id
    where.extend(scope_where(scope, params))
    if memory_type is not None:
        where.append("memory_type = {mt:String}")
        params["mt"] = memory_type
    if pinned_only:
        where.append("pinned = 1")
    if key_prefix is not None:
        where.append("startsWith(memory_key, {prefix:String})")
        params["prefix"] = key_prefix
    if source_trace is not None:
        where.append("source_trace = {st:String}")
        params["st"] = source_trace
    if tags:
        clause, params["tags"] = tag_filter(tags, any_case=tags_any_case)
        where.append(clause)
    if contains:
        where.append(CONTAINS_PREDICATE)
        params["contains"] = contains
    if as_of is not None:
        where.append(as_of_predicate("valid_time"))
        params["as_of"] = as_of

    order_clause = _LIST_ORDER.get(order_by)
    if order_clause is None:
        raise ValueError(
            f"unknown order_by: {order_by!r}; use 'created_at_desc', 'created_at_asc' "
            "or 'memory_key_asc'",
        )

    # A list reads the stored 2 KB preview and the
    # value's length, never the value column (54 MB per page of 100 on a
    # namespace of large values); a caller that needs the whole value asks
    # for it.
    if preview:
        # Locate, then read — see _locate_then_preview.
        return await _locate_then_preview(
            ch, identity, where=where, order_clause=order_clause, params=params
        )
    rows = await ch.query_all(
        f"""
        SELECT
            memory_id,
            org, namespace, agent, user_id, session_id,
            memory_key, memory_type, value, 0 AS value_truncated, confidence, version, pinned,
            char_limit,
            created_at, updated_at, expires_at, valid_at, tombstoned_at,
            status, run_id, source_trace, tags, entities,
            content_sha256, verified_by, verified_at, verified_sha256, actor, source_class
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        {order_clause}
        LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
        """,
        params=params,
    )
    return [_hydrate(r) for r in rows]


async def _locate_then_preview(
    ch: _CHClient,
    identity: Identity,
    *,
    where: list[str],
    order_clause: str,
    params: dict[str, Any],
) -> list[MemoryRow]:
    """The preview page in two statements. The page's ORDER BY is not the
    sort key, so under FINAL one statement sees every version of every key
    before it keeps a page — and `value_preview` would be most of what it
    reads (3.8 MB of 4.7 MB, 2,962 rows for 101). The key and card columns
    locate the page (0.7 MB); the previews come by (memory_id, version)
    pairs without FINAL, from the page's own granules."""
    located = await ch.query_all(
        f"""
        SELECT
            memory_id,
            org, namespace, agent, user_id, session_id,
            memory_key, memory_type, value_len > 2048 AS value_truncated,
            confidence, version, pinned, char_limit,
            created_at, updated_at, expires_at, valid_at, tombstoned_at,
            status, run_id, source_trace, tags, entities,
            content_sha256, verified_by, verified_at, verified_sha256, actor, source_class
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        {order_clause}
        LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
        """,
        params=params,
    )
    if not located:
        return []
    pairs = [(str(r["memory_id"]), int(r["version"])) for r in located]
    previews = await ch.query_all(
        """
        SELECT memory_id, version, value_preview
        FROM loom.memory
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
        LIMIT 1 BY memory_id, version
        """,
        params={"org": identity.org, "ns": identity.namespace, "pairs": pairs},
    )
    by_pair = {(str(p["memory_id"]), int(p["version"])): p["value_preview"] for p in previews}
    out: list[MemoryRow] = []
    for r in located:
        row = dict(r)
        row["value"] = by_pair.get((str(r["memory_id"]), int(r["version"])))
        out.append(_hydrate(row))
    return out


async def namespace_version(org: str, namespace: str, *, ch: _CHClient | None = None) -> int:
    """The namespace's newest memory version — the change watermark for
    pollers. Every write path inserts a row with a
    higher `version`, so `max(version)` moves on any set, update, delete,
    pin or restore. No FINAL: the maximum over every version row IS the
    newest. 0 for an empty namespace."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT max(version) AS v
        FROM loom.memory
        WHERE org = {org:String} AND namespace = {ns:String}
        """,
        params={"org": org, "ns": namespace},
    )
    return int((rows[0] if rows else {}).get("v") or 0)


async def count_by_namespace(
    identity: Identity,
    *,
    scope: ReadFilters,
    memory_type: str | None = None,
    pinned_only: bool = False,
    key_prefix: str | None = None,
    source_trace: str | None = None,
    tags: Sequence[str] | None = None,
    tags_any_case: bool = False,
    contains: str | None = None,
    include_tombstoned: bool = False,
    as_of: datetime | None = None,
    created_since: datetime | None = None,
    ch: _CHClient | None = None,
    include_duplicates: bool = True,
    include_superseded: bool = True,
    include_proposed: bool = True,
) -> int:
    """Total rows matching the same scope as `list_by_namespace` — the true
    count behind a (possibly truncated) list page, so the UI can show
    "N of TOTAL" rather than "N of <page cap>". WHERE mirrors
    `list_by_namespace` exactly; keep the two in sync.
    """
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
    ]
    if not include_tombstoned:
        where.append("tombstoned_at IS NULL")
    params: dict[str, Any] = {
        "org": identity.org,
        "ns": identity.namespace,
    }
    if not include_duplicates:
        where.append("status != 'duplicate'")  # as list_by_namespace
    if not include_superseded:
        where.append("status != 'superseded'")  # as list_by_namespace
    if not include_proposed:
        where.append("status != 'proposed'")  # as list_by_namespace
    if identity.agent != "*":
        where.append("agent = {agent:String}")
        params["agent"] = identity.agent
    if identity.user_id != "*":
        where.append("user_id = {uid:String}")
        params["uid"] = identity.user_id
    if identity.session_id != "*":
        where.append("session_id = {sid:String}")
        params["sid"] = identity.session_id
    where.extend(scope_where(scope, params))
    if memory_type is not None:
        where.append("memory_type = {mt:String}")
        params["mt"] = memory_type
    if pinned_only:
        where.append("pinned = 1")
    if key_prefix is not None:
        where.append("startsWith(memory_key, {prefix:String})")
        params["prefix"] = key_prefix
    if source_trace is not None:
        where.append("source_trace = {st:String}")
        params["st"] = source_trace
    if tags:
        clause, params["tags"] = tag_filter(tags, any_case=tags_any_case)
        where.append(clause)
    if contains:
        where.append(CONTAINS_PREDICATE)
        params["contains"] = contains
    if as_of is not None:
        where.append(as_of_predicate("valid_time"))
        params["as_of"] = as_of
    if created_since is not None:
        where.append("created_at >= {since:DateTime}")
        params["since"] = created_since
    row = await ch.query_one(
        f"SELECT count() AS n FROM loom.memory FINAL WHERE {' AND '.join(where)}",
        params=params,
    )
    return int(row["n"]) if row else 0


async def _overlay_usage_totals(row: MemoryRow, ch: _CHClient) -> MemoryRow:
    """Overlay append-only usage counters when the totals table has data.

    Hot paths write `memory_usage_events`; the SummingMergeTree totals
    become authoritative over the row's own counters once non-zero.
    """
    try:
        totals = await ch.query_one(
            """
            SELECT
                sum(evidence_count) AS evidence_count,
                sum(recall_count) AS recall_count,
                max(last_recalled_at) AS last_recalled_at
            FROM loom.memory_usage_totals
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND memory_id = {mid:UUID}
            """,
            params={
                "org": row.org,
                "ns": row.namespace,
                "mid": row.memory_id,
            },
        )
    except Exception:  # noqa: BLE001 — usage totals are optional; keep the row
        return row
    if not totals:
        return row

    updates: dict[str, Any] = {}
    usage_evidence = int(totals.get("evidence_count") or 0)
    usage_recall = int(totals.get("recall_count") or 0)
    usage_last_recalled = totals.get("last_recalled_at")
    if usage_evidence > 0:
        updates["evidence_count"] = usage_evidence
    if usage_recall > 0:
        updates["recall_count"] = usage_recall
    if usage_last_recalled:
        updates["last_recalled_at"] = usage_last_recalled
    return row.model_copy(update=updates) if updates else row
