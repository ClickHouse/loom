"""Reads of loom.memory heads: point lookups by key and by id, bulk reads by
key, the namespace list and count, the change watermark and the
entity-overlap neighbours. Outputs are `MemoryRow`s or plain row dicts.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from server.core.hashing import served_content_sha256
from server.core.repo_base import _CHClient, _default_client, query_all_sliced
from server.core.tag_sql import TagFilter, tag_where
from server.identity.domain.read_scope import ReadFilters, scope_and, scope_where
from server.memory.domain.delete_marks import DELETE_VERSION_TRACE
from server.memory.domain.expire_reasons import SWEEP_REASON_PREFIX
from server.memory.domain.identity_columns import IdentityColumn
from server.memory.domain.temporal_sql import as_of_predicate, served_status_sql
from server.memory.domain.transcript_rows import TRANSCRIPT_ROW_SQL
from server.memory.store.rows import Identity, MemoryRow, _hydrate, parse_memory_value
from server.memory.store.sql import (
    _IDENTITY_KEY_TUPLE,
    _MEMORY_FINAL_PROJECTION,
    _SEQUENTIAL,
    SERVED_VALUE,
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
    (update_by_id's EXPIRED). Under `as_of`, a replay serves expired rows
    (it asks about a past instant), except a 'high' row past its retention
    window, which stays hidden.

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
    elif not include_expired:
        # An as_of replay reads past expiry, but never a 'high' row past its retention window.
        tomb_clause += (
            " AND NOT (sensitivity = 'high' AND expires_at IS NOT NULL AND expires_at <= now())"
        )
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


# Each id's newest version, without FINAL: memory_id is found by its bloom-filter skip
# index, which FINAL re-expands to every granule of the key range.
_STATUSES_SQL = """
        SELECT toString(memory_id) AS id,
               argMax(status, version) AS status,
               argMax(superseded_by, version) AS superseded_by,
               -- argMax skips NULLs: take it over the flag, or a restored row reads deleted.
               argMax(tombstoned_at IS NOT NULL, version) AS deleted
        FROM loom.memory
        PREWHERE org = {org:String} AND namespace = {ns:String}
        WHERE memory_id IN {ids:Array(UUID)}%s
        GROUP BY memory_id
        SETTINGS select_sequential_consistency = 1
        """


async def statuses_by_ids(
    org: str, namespace: str, ids: list[str], *, scope: ReadFilters, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """[{memory_id, status, superseded_by}] for the ids among `ids` the namespace holds
    inside `scope`, read strongly; a tombstoned row's status is `deleted`. An id whose
    row a write of the same key replaced under another id (two first writes of a key
    racing each mint one) drops out once the table's parts merge, as a read by id
    answers found=false."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "ids": ids}
    sql = _STATUSES_SQL % scope_and(scope, params)
    return [
        {
            "memory_id": str(r["id"]),
            "status": "deleted" if r["deleted"] else str(r["status"]),
            "superseded_by": str(r["superseded_by"] or ""),
        }
        for r in await query_all_sliced(ch, sql, params, "ids")
    ]


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
    include_tombstoned: bool = False,
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
    `include_tombstoned`: a deleted row is returned too, as get_by_key's.
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
    where = ["created_at = {ca:DateTime}", "memory_id = {mid:UUID}"]
    if not include_tombstoned:
        where.append("tombstoned_at IS NULL")
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


async def key_heads_across_sessions(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    memory_keys: Sequence[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The newest version of each of `memory_keys` across the sessions of the identity
    `(org, namespace, agent, user_id)` that `scope` may read, tombstones and every status
    included: `memory_key`, `deleted`, `head_status`, `head_sha256`, `head_sensitivity`.
    Sequentially consistent, since a write decides on the answer."""
    if not memory_keys:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "agent": agent,
        "uid": user_id,
        "keys": sorted(set(memory_keys)),
    }
    in_scope = scope_and(scope, params)
    return await ch.query_all(
        f"""
        SELECT memory_key,
               argMax(tombstoned_at IS NOT NULL, version) AS deleted,
               argMax(status, version) AS head_status,
               argMax(content_sha256, version) AS head_sha256,
               argMax(sensitivity, version) AS head_sensitivity
        FROM loom.memory
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND agent = {{agent:String}}
          AND user_id = {{uid:String}}
          AND memory_key IN {{keys:Array(String)}}{in_scope}
        GROUP BY memory_key
        {_SEQUENTIAL}
        """,
        params=params,
    )


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


async def session_writers(
    keys: Sequence[tuple[str, str, str, str, str]], *, ch: _CHClient | None = None
) -> dict[tuple[str, str, str, str, str], str]:
    """The principal writer (`actor`) of each session's newest live memory version, by
    (org, namespace, agent, user_id, session_id): whom a job's model call over the
    session is for. FINAL, so a deleted memory or a replaced version names no one, and
    by `version`, which every write stamps (an update keeps `created_at`). A pipeline's
    own actor names no principal and is passed over; a session whose memories record
    none is absent."""
    from server.core.ch_params import chunks
    from server.memory.domain.provenance import CLI_ACTOR_PREFIX, PIPELINE_ACTORS

    if not keys:
        return {}
    ch = ch or _default_client()
    out: dict[tuple[str, str, str, str, str], str] = {}
    for part in chunks(sorted({tuple(str(x) for x in k) for k in keys})):
        out.update(await _session_writers(ch, part, PIPELINE_ACTORS, CLI_ACTOR_PREFIX))
    return out


async def _session_writers(
    ch: _CHClient, keys: list[tuple[str, ...]], pipeline: frozenset[str], cli: str
) -> dict[tuple[str, str, str, str, str], str]:
    rows = await ch.query_all(
        """
        SELECT org, namespace, agent, user_id, session_id,
               argMaxIf(actor, version,
                        actor != '' AND NOT has({pipeline:Array(String)}, actor)
                        AND NOT startsWith(actor, {cli:String})) AS writer
        FROM loom.memory FINAL
        WHERE (org, namespace, agent, user_id, session_id)
              IN {keys:Array(Tuple(String, String, String, String, String))}
          AND tombstoned_at IS NULL
        GROUP BY org, namespace, agent, user_id, session_id
        """,
        params={"keys": keys, "pipeline": sorted(pipeline), "cli": cli},
    )
    out: dict[tuple[str, str, str, str, str], str] = {}
    for r in rows:
        if writer := str(r.get("writer") or ""):
            out[(r["org"], r["namespace"], r["agent"], r["user_id"], r["session_id"])] = writer
    return out


async def get_many_by_keys(
    identity: Identity,
    memory_keys: list[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
    strong: bool = False,
    exclude_high_sensitivity: bool = False,
) -> list[MemoryRow]:
    """Bulk read: one CH round-trip for N keys under one identity tuple.

    Used by memory.bulk_get, and by procedure.match and procedure.list for
    their candidate bodies. Tombstoned rows are filtered out. Empty
    `memory_keys` → empty result, no CH call. `exclude_high_sensitivity`
    (the procedure, search-body and extraction-prompt reads) leaves 'high'
    rows out of the locate, so under an open axis the newest other row of
    the key answers instead.
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
    # With any axis open, several identities may hold the same key: the
    # newest row per key answers, the way `get_by_key`'s wildcard read orders
    # (version DESC, created_at DESC) and takes one.
    one_per_key = (
        "ORDER BY memory_key ASC, version DESC, created_at DESC LIMIT 1 BY memory_key"
        if not (agent_clause and user_clause and session_clause)
        else ""
    )
    session_clause += scope_and(scope, params)
    if exclude_high_sensitivity:
        session_clause += " AND sensitivity != 'high'"
    if not strong:
        # This read leaves out a 'high' row past its retention window, before one row
        # per key is picked, so it hides no other identity's live row of the key. A
        # strong read feeds a write (memory.touch may extend that window) and still sees
        # it.
        session_clause += (
            " AND NOT (sensitivity = 'high' AND expires_at IS NOT NULL AND expires_at <= now())"
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
            created_at, updated_at, expires_at, on_expire, expire_reason,
            valid_at, valid_to, temporal_anchor, temporal_confidence,
            status, run_id, source_trace, char_limit, tags, entities, content_sha256,
            duplicate_of, superseded_by, recall_count, last_recalled_at,
            verified_by, verified_at, verified_sha256, sensitivity, actor, source_class,
            lock_mode, lock_reason, lock_until, locked_by, locked_at
        FROM loom.memory
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
        LIMIT 1 BY memory_id, version
    """


async def high_keys(
    org: str, namespace: str, keys: Sequence[str], *, ch: _CHClient | None = None
) -> set[str]:
    """The keys among `keys` with any version labelled 'high', live or deleted, under any
    memory id (a key deleted and written again gets a new one): the raw chunks and digests
    behind such a row may hold its text. memory_key follows the identity columns in
    loom.memory_versions' sort key, so this reads the namespace's memory_key and
    sensitivity columns."""
    if not keys:
        return set()
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT DISTINCT memory_key
        FROM loom.memory_versions
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)}
          AND sensitivity = 'high'
        """,
        params={"org": org, "ns": namespace, "keys": sorted(set(keys))},
    )
    return {str(r["memory_key"]) for r in rows}


async def high_memory_ids(
    org: str,
    namespace: str,
    memory_key: str,
    memory_ids: Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> set[str]:
    """The ids among `memory_ids` of rows of `memory_key` with any version labelled 'high'.
    The id filter is on the UUID column itself, so the memory_id_skip index applies; the
    output is aliased `mid`, since `AS memory_id` would compare the String alias against
    the UUIDs and match nothing."""
    if not memory_ids:
        return set()
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT DISTINCT toString(memory_id) AS mid
        FROM loom.memory_versions
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key = {key:String}
          AND memory_id IN {ids:Array(UUID)}
          AND sensitivity = 'high'
        """,
        params={"org": org, "ns": namespace, "key": memory_key, "ids": sorted(set(memory_ids))},
    )
    return {str(r["mid"]) for r in rows}


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
        SELECT entities, sensitivity
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
    # A row labelled high after the extractor ran keeps entities mined from its value.
    if source is None or source.get("sensitivity") == "high":
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
          -- A 'high' row is readable by key only, not as a neighbour's excerpt.
          AND sensitivity != 'high'
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
# The value of a 'high' row is not searched: it is readable by key only.
CONTAINS_PREDICATE = (
    "(positionCaseInsensitiveUTF8(memory_key, {contains:String}) > 0"
    " OR (sensitivity != 'high' AND positionCaseInsensitiveUTF8(value, {contains:String}) > 0))"
)


def _identity_where(identity: Identity, where: list[str], params: dict[str, Any]) -> None:
    """The identity axes `list_by_namespace` and `count_by_namespace` share: `*` on an axis
    drops it from WHERE."""
    if identity.agent != "*":
        where.append("agent = {agent:String}")
        params["agent"] = identity.agent
    if identity.user_id != "*":
        where.append("user_id = {uid:String}")
        params["uid"] = identity.user_id
    if identity.session_id != "*":
        where.append("session_id = {sid:String}")
        params["sid"] = identity.session_id


async def list_by_namespace(
    identity: Identity,
    *,
    scope: ReadFilters,
    memory_type: str | None = None,
    pinned_only: bool = False,
    key_prefix: str | None = None,
    source_trace: str | None = None,
    tag_filter: TagFilter | None = None,
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
    exclude_transcripts: bool = False,
    exclude_memory_types: Sequence[str] = (),
) -> list[MemoryRow]:
    """List active memories under (org, namespace, agent, user, session).

    Filters:
      - memory_type: exact match (e.g. "preference", "procedural")
      - pinned_only: only rows where pinned=1
      - key_prefix:  startsWith(memory_key, prefix) — for hierarchical key spaces
      - tag_filter:  its tags matched as its `match` says: any one or every one in
                     any case, or any one exactly as memory.search matches
      - contains:    case-insensitive substring of the value or the key — the
                     keyword filter a browse page's search box applies; a
                     'high' row matches by its key only
      - exclude_transcripts: no trajectory transcript rows (`TRANSCRIPT_ROW_SQL`)
      - exclude_memory_types: none of these types

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
    _identity_where(identity, where, params)
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
    if tag_filter is not None:
        where.append(tag_where(tag_filter, params))
    if contains:
        where.append(CONTAINS_PREDICATE)
        params["contains"] = contains
    if exclude_transcripts:
        where.append("NOT " + TRANSCRIPT_ROW_SQL)
    if exclude_memory_types:
        where.append("memory_type NOT IN {xmt:Array(String)}")
        params["xmt"] = list(exclude_memory_types)
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
            evidence_count, recall_count, last_recalled_at, sensitivity,
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
            evidence_count, recall_count, last_recalled_at, sensitivity,
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


async def versions_above(
    org: str,
    namespace: str,
    rows: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str, str, str], list[tuple[int, bool, int]]]:
    """Every committed version of each row's key (agent, user_id, session_id, memory_key)
    above the lowest version among `rows`, newest first, with whether it is a delete's
    tombstone (a blacklist sweep's is not one) and, for memory.delete_version's tombstone,
    the version it removed (0 otherwise). From loom.memory_versions, which keeps
    every version (a merge of loom.memory keeps only the newest). Read with sequential
    consistency, since the answer feeds the write."""
    if not rows:
        return {}
    ch = ch or _default_client()
    keys = sorted({(r["agent"], r["user_id"], r["session_id"], r["memory_key"]) for r in rows})
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "keys": keys,
        "v": min(int(r["version"]) for r in rows),
        "sweep": SWEEP_REASON_PREFIX,
        "dv": DELETE_VERSION_TRACE,
    }
    found = await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key,
               arrayReverseSort(groupArray((version, deleted, target))) AS versions
        FROM (
            SELECT agent, user_id, session_id, memory_key, version,
                   tombstoned_at IS NOT NULL
                       AND NOT startsWith(expire_reason, {{sweep:String}}) AS deleted,
                   if(
                       tombstoned_at IS NOT NULL AND startsWith(source_trace, {{dv:String}}),
                       toUInt64OrZero(substring(source_trace, length({{dv:String}}) + 1)),
                       0
                   ) AS target
            FROM loom.memory_versions
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND (agent, user_id, session_id, memory_key)
                  IN {{keys:Array(Tuple(String, String, String, String))}}
              AND version > {{v:UInt64}}
        )
        GROUP BY agent, user_id, session_id, memory_key
        {_SEQUENTIAL if read_your_writes_enabled() else ""}
        """,
        params=params,
    )
    return {
        (f["agent"], f["user_id"], f["session_id"], f["memory_key"]): [
            (int(v), bool(d), int(t)) for v, d, t in f["versions"]
        ]
        for f in found
    }


async def live_keys(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The identity and key of every live row of the namespace, as one statement reads
    them: the rows memory.list serves with every identity at "*" and superseded and
    duplicate rows included (a proposed row, on trial, is not). At most `limit` + 1, so a
    caller can tell it was cut."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "limit": int(limit) + 1}
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
        "status != 'proposed'",
        *scope_where(scope, params),
    ]
    return await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY agent, user_id, session_id, memory_key
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )


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
    tag_filter: TagFilter | None = None,
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
    _identity_where(identity, where, params)
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
    if tag_filter is not None:
        where.append(tag_where(tag_filter, params))
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
                -- Events other than recalls carry the epoch here.
                nullIf(max(last_recalled_at), toDateTime(0)) AS last_recalled_at
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
    return _with_usage(row, totals)


async def overlay_usage_totals_many(
    rows: list[MemoryRow], *, ch: _CHClient | None = None
) -> list[MemoryRow]:
    """`_overlay_usage_totals` for rows of one namespace, in one statement."""
    if not rows:
        return rows
    ch = ch or _default_client()
    try:
        totals = await ch.query_all(
            """
            SELECT
                memory_id,
                sum(evidence_count) AS evidence_count,
                sum(recall_count) AS recall_count,
                -- Events other than recalls carry the epoch here.
                nullIf(max(last_recalled_at), toDateTime(0)) AS last_recalled_at
            FROM loom.memory_usage_totals
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND memory_id IN {mids:Array(UUID)}
            GROUP BY memory_id
            """,
            params={
                "org": rows[0].org,
                "ns": rows[0].namespace,
                "mids": [str(r.memory_id) for r in rows],
            },
        )
    except Exception:  # noqa: BLE001 — usage totals are optional; keep the rows
        return rows
    by_id = {str(t["memory_id"]): t for t in totals}
    return [_with_usage(r, by_id.get(str(r.memory_id))) for r in rows]


def _with_usage(row: MemoryRow, totals: dict[str, Any] | None) -> MemoryRow:
    """The row with the usage totals' counters where they are non-zero."""
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


# The memory rows the inventory counts: not tombstoned and in a served status
# (`served_status_sql`, as the search legs read it: not a duplicate, superseded,
# proposed or rejected row). Expiry and validity follow `memory.list` and `memory.get`:
# a row past `expires_at` or `valid_to` is served until the sweep, so it counts.
_SERVED_ROWS = f"tombstoned_at IS NULL AND {served_status_sql()}"


async def live_tally(
    org: str, namespace: str, *, scope: ReadFilters, ch: _CHClient | None = None
) -> tuple[int, datetime | None]:
    """The namespace's memory rows inside `scope` (`_SERVED_ROWS`), and the newest write
    among them."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace}
    row = await ch.query_one(
        "SELECT count() AS n, if(n = 0, NULL, max(updated_at)) AS latest FROM loom.memory FINAL "
        f"WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {_SERVED_ROWS}"
        + scope_and(scope, params),
        params,
    )
    return (int(row["n"]), row["latest"]) if row else (0, None)


# Identity column → the rows that name one. `user_id` '-' is the unset identity, never
# a user; `actor` '' is a row written before it was stamped.
_NAMED: dict[IdentityColumn, str] = {
    "agent": "agent != ''",
    "actor": "actor != ''",
    "user_id": "user_id NOT IN ('', '-')",
}


async def identity_tally(
    org: str,
    namespace: str,
    column: IdentityColumn,
    *,
    scope: ReadFilters,
    limit: int,
    ch: _CHClient | None = None,
) -> tuple[int, list[tuple[str, int]]]:
    """Distinct values of `column` (agent, actor or user_id) on the rows `live_tally`
    counts, and the `limit` that carry the most rows."""
    ch = ch or _default_client()
    # The total rides on the listed rows, so at least one is read.
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": max(1, limit)}
    rows = await ch.query_all(
        f"SELECT {column} AS name, count() AS n, count() OVER () AS total "
        "FROM loom.memory FINAL "
        f"WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {_SERVED_ROWS} "
        f"AND {_NAMED[column]}{scope_and(scope, params)} "
        f"GROUP BY {column} ORDER BY n DESC, name LIMIT {{lim:UInt32}}",
        params,
    )
    total = int(rows[0]["total"]) if rows else 0
    return total, [(str(r["name"]), int(r["n"])) for r in rows][:limit]


# A memory row within its namespace: (agent, user_id, session_id, memory_key).
RowKey = tuple[str, str, str, str]


async def live_revisions(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    memory_ids: Sequence[str],
    identities: Sequence[RowKey],
    hash_max_bytes: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The live heads of the rows named by `memory_ids` or by full identity
    (agent, user_id, session_id, memory_key), with the identifiers a citation needs
    (memory_id, version, content hash) and no content, inside the caller's read `scope`.
    The hash is the one memory.get serves (`_served_hashes`), for a row whose value is at
    most `hash_max_bytes`. A high-sensitivity row gets none: a short secret's hash can be
    guessed, and these reports are unaudited reads, so it is left out as memory.list
    leaves it out."""
    if not memory_ids and not identities:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "ids": list(memory_ids),
        "identities": list(identities),
        # Each id and identity names one row.
        "cap": len(memory_ids) + len(identities),
    }
    # The id is selected under another name: an alias named memory_id would shadow the
    # column in WHERE.
    rows = await ch.query_all(
        f"""
        SELECT memory_key, toString(memory_id) AS id, version, sensitivity, value_len,
               agent, user_id, session_id, memory_type, source_class, updated_at
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL
          AND (memory_id IN {{ids:Array(UUID)}}
               OR (agent, user_id, session_id, memory_key)
                  IN {{identities:Array(Tuple(String, String, String, String))}})
          {scope_and(scope, params)}
        LIMIT {{cap:UInt32}}
        """,
        params=params,
    )
    hashable = [
        r for r in rows if r["sensitivity"] != "high" and int(r["value_len"]) <= hash_max_bytes
    ]
    hashes = await _served_hashes(org, namespace, hashable, hash_max_bytes, ch)
    for r in rows:
        r["memory_id"] = r.pop("id")
        del r["sensitivity"], r["value_len"]
        r["content_sha256"] = hashes.get(_row_key(r), "")
    return rows


def _row_key(r: dict[str, Any]) -> tuple[str, str, str, str, int]:
    return (r["agent"], r["user_id"], r["session_id"], r["memory_key"], r["version"])


async def _served_hashes(
    org: str, namespace: str, revisions: list[dict[str, Any]], max_bytes: int, ch: _CHClient
) -> dict[tuple[str, str, str, str, int], str]:
    """The hash memory.get serves for each revision, by its row key and version: the hash
    of the value it serves, not the stored column, which can differ for an object (the
    JSON column drops a null field). The value is read by each row's whole sorting key
    (the primary index prunes to their granules), hashed here and never returned. A row
    revised between the two reads gets none rather than another revision's."""
    if not revisions:
        return {}
    keys = [(r["agent"], r["user_id"], r["session_id"], r["memory_key"]) for r in revisions]
    rows = await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key, version, {SERVED_VALUE} AS value
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL
          AND (agent, user_id, session_id, memory_key)
              IN {{keys:Array(Tuple(String, String, String, String))}}
          AND value_len <= {{max:UInt32}}
        LIMIT {{cap:UInt32}}
        """,
        params={"org": org, "ns": namespace, "keys": keys, "cap": len(keys), "max": max_bytes},
    )
    # Decoded as memory.get decodes it: a string value is stored raw, not as JSON.
    return {_row_key(r): served_content_sha256(parse_memory_value(r["value"])) for r in rows}
