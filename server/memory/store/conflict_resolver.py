"""The conflict resolver's statements: its queue, its stamp, the cluster it judges
a row in, and the erasure check it makes before acting.

The queue lives on `loom.memory_embeddings.conflict_checked_at`, the row a write
already emits, so every replica reads the same queue and a write's mark goes with
its content (`content_sha256`):

- NULL: nothing owed; the write had no peer near enough.
- `OWED_MARK` (1970-01-01 00:00:01): the resolver owes this write a judgement.
- `JUDGED_MARK` (1970-01-01 00:00:02): the resolver judged this write.
- the epoch itself (00:00:00): a judgement an older build deferred and lost; owed.
- any real time: a verdict an older build stamped; owed only while the row is
  `conflicted` (the re-check of flags this build did not raise).

An older build reads only the epoch as its own pending mark (`toUnixTimestamp(...)
= 0`), so neither of this build's marks enters its sweep or its `pending` read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows

# Aware: a naive datetime encodes as host time, which east of UTC is before 1970.
OWED_MARK = datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
JUDGED_MARK = datetime(1970, 1, 1, 0, 0, 2, tzinfo=UTC)
# An older build's lost mark is owed once its own settle had this long to land.
LEGACY_MARK_GRACE_SECONDS = 60

_QUEUE_COLUMNS = """org, namespace, agent, user_id, session_id, memory_key, memory_type,
               embedding, version, content_sha256, status"""

# Live, private, embedded rows; the minmax index on conflict_checked_at prunes the rest.
_LIVE_VECTOR = """tombstoned_at IS NULL
          AND scope = 'private'
          AND status IN ('active', 'conflicted')
          AND embedding_dim > 0"""

# The failure_reason of an abandoned erasure saga, which never runs again
# (erasure.store.ledger.ABANDONED_REASON_PREFIX; this store may not import it).
ERASURE_ABANDONED_PREFIX = "max_attempts ("

# Rows the resolver leaves out of its queue, so they hold no place in it: a namespace
# soft-deleted by namespace.delete (its data stays in place; an unregistered namespace
# has no directory row and is live), an org with an erasure queued, running or failed
# and due a retry (its saga deletes what the resolver would write a new version of),
# and the namespaces in `capped` (over their daily cost cap, read by the worker).
_NOT_SKIPPED = f"""(org, namespace) NOT IN (
              SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active)
          AND org NOT IN (
              SELECT org FROM loom.erasure_requests FINAL
              WHERE status IN ('queued', 'in_progress', 'failed')
                AND NOT startsWith(failure_reason, '{ERASURE_ABANDONED_PREFIX}'))
          AND (org, namespace) NOT IN {{capped:Array(Tuple(String, String))}}"""

_OWED = f"""conflict_checked_at < toDateTime(2, 'UTC')
          AND (toUnixTimestamp(conflict_checked_at) = 1
               OR created_at < now() - INTERVAL {LEGACY_MARK_GRACE_SECONDS} SECOND)
          AND {_LIVE_VECTOR}
          AND {_NOT_SKIPPED}"""

_UNJUDGED = f"""status = 'conflicted'
          AND (conflict_checked_at IS NULL OR conflict_checked_at > toDateTime(2, 'UTC'))
          AND {_LIVE_VECTOR}
          AND {_NOT_SKIPPED}"""


def _in_turns(where: str) -> str:
    """The vector rows matching `where`, in turns across (org, namespace): every
    namespace's oldest row, then every namespace's second oldest, and so on, so one
    namespace's backlog cannot hold the others back. Only the identity is ranked; the
    vectors are read for the picked rows alone."""
    return f"""
        SELECT {_QUEUE_COLUMNS}
        FROM loom.memory_embeddings FINAL
        WHERE (org, namespace, agent, user_id, session_id, embedding_model, memory_key) IN (
            SELECT org, namespace, agent, user_id, session_id, embedding_model, memory_key
            FROM (
                SELECT org, namespace, agent, user_id, session_id, embedding_model,
                       memory_key, created_at,
                       row_number() OVER (PARTITION BY org, namespace ORDER BY created_at)
                           AS turn
                FROM loom.memory_embeddings FINAL
                WHERE {where}
            )
            ORDER BY turn, created_at
            LIMIT {{lim:UInt32}}
        )
          AND {where}
        ORDER BY created_at ASC
        """


def _capped(capped: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(str(o), str(n)) for o, n in capped]


async def queued_namespaces(*, ch: _CHClient | None = None) -> list[tuple[str, str]]:
    """The (org, namespace) pairs that hold owed or unjudged `conflicted` rows the
    queue would read: the pairs the worker checks against their cost caps."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT org, namespace
        FROM loom.memory_embeddings FINAL
        WHERE (({_OWED}) OR ({_UNJUDGED}))
        """,
        {"capped": []},
    )
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def owed_rows(
    *, limit: int, capped: Sequence[tuple[str, str]] = (), ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Rows whose write the resolver owes a judgement, in turns across namespaces;
    none of a namespace in `capped`."""
    ch = ch or _default_client()
    return await ch.query_all(_in_turns(_OWED), {"lim": int(limit), "capped": _capped(capped)})


async def unjudged_conflicted_rows(
    *, limit: int, capped: Sequence[tuple[str, str]] = (), ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """`conflicted` rows no resolver has judged (flags raised by cosine alone, or by an
    older build's judge), in turns across namespaces; none of a namespace in `capped`."""
    ch = ch or _default_client()
    return await ch.query_all(_in_turns(_UNJUDGED), {"lim": int(limit), "capped": _capped(capped)})


def _same_write(row: Mapping[str, Any], judged: Mapping[str, Any]) -> bool:
    sha = str(judged.get("content_sha256") or "")
    if sha:
        return str(row.get("content_sha256") or "") == sha
    # A row from before content hashes: the version judged.
    return int(row.get("version") or 0) == int(judged.get("version") or 0)


async def stamp_judged(judged: Mapping[str, Any], *, ch: _CHClient | None = None) -> int:
    """Stamp the vector rows of the judged write (`judged`: a queue row) `JUDGED_MARK`.
    A newer write's row keeps its own mark."""
    ch = ch or _default_client()
    rows = await live_embedding_rows_for_key(
        ch,
        org=str(judged["org"]),
        namespace=str(judged["namespace"]),
        agent=str(judged["agent"]),
        user_id=str(judged["user_id"]),
        session_id=str(judged["session_id"]),
        memory_key=str(judged["memory_key"]),
    )
    own = [r for r in rows if str(r.get("scope")) == "private" and _same_write(r, judged)]
    return await reemit_rows(own, {"conflict_checked_at": JUDGED_MARK}, ch=ch)


def _nearest_sql() -> str:
    """The row's nearest vectors in its subject: the same user (the anonymous user is
    one subject per agent), any session, its own vector left out. A same-key row of
    another session is a peer like any other."""
    from server.core.vectors.search_settings import vector_search_settings

    return """
        SELECT agent, user_id, session_id, memory_key,
               cosineDistance(embedding, {emb:Array(Float32)}) AS dist
        FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND user_id = {u:String} AND (user_id != '-' OR agent = {a:String})
          AND NOT (agent = {a:String} AND session_id = {s:String} AND memory_key = {k:String})
          AND scope = 'private'
          AND tombstoned_at IS NULL
          AND status IN ('active', 'conflicted')
          -- What would drop a candidate later is applied here, before the LIMIT, so
          -- closer rows of another type (or expired ones) cannot take every place.
          AND memory_type = {mt:String}
          AND (expires_at IS NULL OR expires_at > now())
          AND (valid_to IS NULL OR valid_to > now())
          AND embedding_dim = {dim:UInt16}
          AND embedding.size0 = {dim:UInt16}
        ORDER BY dist ASC
        LIMIT {lim:UInt32}
        """ + vector_search_settings(table="loom.memory_embeddings")


# The candidates' rows, joined by the full identity: a key alone repeats across sessions.
_PEER_ROWS_SQL = """
        SELECT toString(memory_id) AS memory_id, agent, user_id, session_id, memory_key,
               toString(value) AS value, pinned, valid_at, created_at, updated_at, version,
               toString(content_sha256) AS content_sha256, status,
               arrayMap(x -> toString(x), conflict_with) AS conflict_with
        FROM loom.memory FINAL
        PREWHERE org = {org:String} AND namespace = {ns:String}
          AND (agent, user_id, session_id, memory_key)
              IN {ids:Array(Tuple(String, String, String, String))}
        WHERE memory_type = {mt:String}
          AND tombstoned_at IS NULL
          AND status IN ('active', 'conflicted')
          AND sensitivity != 'high'
          AND (expires_at IS NULL OR expires_at > now())
          AND (valid_to IS NULL OR valid_to > now())
        SETTINGS select_sequential_consistency = 1
        """


# The row's flagged peers, by id, with their distance: they join the cluster whatever
# the vector cutoff or the floor.
_FLAGGED_SQL = """
        SELECT e.agent AS agent, e.user_id AS user_id, e.session_id AS session_id,
               e.memory_key AS memory_key,
               cosineDistance(e.embedding, {emb:Array(Float32)}) AS dist
        FROM loom.memory_embeddings AS e FINAL
        WHERE e.org = {org:String} AND e.namespace = {ns:String}
          AND (e.agent, e.user_id, e.session_id, e.memory_key) IN (
              SELECT agent, user_id, session_id, memory_key FROM loom.memory FINAL
              WHERE org = {org:String} AND namespace = {ns:String}
                AND toString(memory_id) IN {flagged:Array(String)})
          AND e.scope = 'private' AND e.tombstoned_at IS NULL
          AND e.embedding_dim = {dim:UInt16} AND e.embedding.size0 = {dim:UInt16}
        SETTINGS select_sequential_consistency = 1
        """


def _ident(r: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))


async def cluster_peers(
    row: Mapping[str, Any],
    *,
    floor: float,
    limit: int,
    prefer: frozenset[str] = frozenset(),
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Up to `limit` live peers of the queue row `row`: its memory_type, its subject,
    cosine >= `floor`, nearest first, each with `similarity`. Peers named in `prefer`
    (the row's current flags) are read by id, whatever the cutoff and the floor, and
    take places first, so a re-check judges them."""
    ch = ch or _default_client()
    emb = [float(x) for x in (row.get("embedding") or [])]
    if not emb:
        return []
    params: dict[str, Any] = {
        "org": str(row["org"]),
        "ns": str(row["namespace"]),
        "a": str(row["agent"]),
        "u": str(row["user_id"]),
        "s": str(row["session_id"]),
        "k": str(row["memory_key"]),
        "mt": str(row["memory_type"]),
        "emb": emb,
        "dim": len(emb),
        "lim": 4 * max(int(limit), 1),
    }
    near = [c for c in await ch.query_all(_nearest_sql(), params=params) if 1 - c["dist"] >= floor]
    if prefer:
        flagged = await ch.query_all(_FLAGGED_SQL, params={**params, "flagged": sorted(prefer)})
        seen = {_ident(c) for c in near}
        near += [c for c in flagged if _ident(c) not in seen]
    if not near:
        return []
    rows = await ch.query_all(_PEER_ROWS_SQL, params={**params, "ids": [_ident(c) for c in near]})
    by_ident = {_ident(r): dict(r) for r in rows}
    peers = [
        {**by_ident[_ident(c)], "similarity": 1.0 - float(c["dist"])}
        for c in near
        if _ident(c) in by_ident
    ]
    peers.sort(key=lambda p: (str(p["memory_id"]) not in prefer, -p["similarity"]))
    kept = peers[: max(int(limit), 0)]
    kept.sort(key=lambda p: (-p["similarity"], str(p["memory_key"])))
    return kept


async def protected_ids(org: str, ids: list[str], *, ch: _CHClient | None = None) -> set[str]:
    """The ids among `ids` whose current row is labelled 'high', read strongly: such a
    row's text never goes to the model, whatever was read before."""
    if not ids:
        return set()
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id
        FROM loom.memory FINAL
        PREWHERE org = {org:String}
        WHERE toString(memory_id) IN {ids:Array(String)} AND sensitivity = 'high'
        SETTINGS select_sequential_consistency = 1
        """,
        {"org": org, "ids": [str(i) for i in ids]},
    )
    return {str(r["memory_id"]) for r in rows}


async def asserted_versions(
    org: str, namespace: str, rows: list[tuple[str, str]], *, ch: _CHClient | None = None
) -> dict[str, int]:
    """{memory_id: the smallest version of its history rows holding `sha`} for each
    (memory_id, sha) in `rows`: when the row's current value was first asserted, in
    wall-clock ns. A metadata copy (touch, pin, tags) keeps the hash, so it does not
    move it. An id the history does not hold is left out."""
    if not rows:
        return {}
    ch = ch or _default_client()
    found = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id, min(version) AS asserted
        FROM loom.memory_versions
        PREWHERE org = {org:String} AND namespace = {ns:String}
        WHERE (toString(memory_id), toString(content_sha256))
              IN {rows:Array(Tuple(String, String))}
        GROUP BY memory_id
        SETTINGS select_sequential_consistency = 1
        """,
        {"org": org, "ns": namespace, "rows": [(str(i), str(h)) for i, h in rows]},
    )
    return {str(r["memory_id"]): int(r["asserted"]) for r in found}


__all__ = [
    "JUDGED_MARK",
    "OWED_MARK",
    "asserted_versions",
    "cluster_peers",
    "owed_rows",
    "protected_ids",
    "queued_namespaces",
    "stamp_judged",
    "unjudged_conflicted_rows",
]
