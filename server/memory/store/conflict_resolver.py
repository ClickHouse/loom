"""The conflict resolver's statements: its queue, its stamp, the cluster it judges
a row in, and the erasure check it makes before each write.

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

import asyncio
import calendar
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from server.core.naming import SENTINEL, is_anonymous_user, session_spellings
from server.core.repo_base import _CHClient, _default_client
from server.memory.store.embeddings import live_embedding_rows_for_key, reemit_rows

# Aware: a naive datetime encodes as host time, which east of UTC is before 1970.
OWED_MARK = datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
JUDGED_MARK = datetime(1970, 1, 1, 0, 0, 2, tzinfo=UTC)
# An older build's lost mark is owed once its own settle had this long to land.
LEGACY_MARK_GRACE_SECONDS = 60  # not a setting: an older build's settle timing

_QUEUE_COLUMNS = """org, namespace, agent, user_id, session_id, memory_key, memory_type,
               embedding_model, embedding, version, content_sha256, status"""

# Live, private, embedded rows; the minmax index on conflict_checked_at prunes the rest.
_LIVE_VECTOR = """tombstoned_at IS NULL
          AND scope = 'private'
          AND status IN ('active', 'conflicted')
          AND embedding_dim > 0"""

# The failure_reason of an abandoned erasure saga, which never runs again
# (erasure.domain.failure_reason.ABANDONED_REASON_PREFIX; this store may not import it).
ERASURE_ABANDONED_PREFIX = "max_attempts ("

# The orgs with an erasure a write waits for (an abandoned saga never runs again). The
# projection repair's scan reads it too.
ERASING_ORGS_SQL = f"""
              SELECT org FROM loom.erasure_requests FINAL
              WHERE status IN ('queued', 'in_progress', 'failed')
                AND NOT startsWith(failure_reason, '{ERASURE_ABANDONED_PREFIX}')"""
# Rows the resolver leaves out of its queue, so they hold no place in it: a namespace
# soft-deleted by namespace.delete (its data stays in place; an unregistered namespace
# has no directory row and is live), an org with an erasure queued, running or failed
# and due a retry (its saga deletes what the resolver would write a new version of),
# and the namespaces in `capped` (over their daily cost cap, read by the worker).
_NOT_SKIPPED = f"""(org, namespace) NOT IN (
              SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active)
          AND org NOT IN ({ERASING_ORGS_SQL})
          AND (org, namespace) NOT IN {{capped:Array(Tuple(String, String))}}"""

# An owed row older than `max_age` days is no longer read: it stays as it was written.
# The table is partitioned by month of created_at, so older months are pruned.
_OWED = f"""conflict_checked_at < toDateTime(2, 'UTC')
          AND created_at > now() - INTERVAL {{max_age:UInt32}} DAY
          AND (toUnixTimestamp(conflict_checked_at) = 1
               OR created_at < now() - INTERVAL {LEGACY_MARK_GRACE_SECONDS} SECOND)
          AND {_LIVE_VECTOR}
          AND {_NOT_SKIPPED}"""

_UNJUDGED = f"""status = 'conflicted'
          AND (conflict_checked_at IS NULL OR conflict_checked_at > toDateTime(2, 'UTC'))
          AND {_LIVE_VECTOR}
          AND {_NOT_SKIPPED}"""


def _queued_pairs(where: str) -> str:
    """The (org, namespace) pairs with any version matching `where`: a superset of the
    pairs the latest versions hold. Read without FINAL, where the skip indexes prune
    (under FINAL every granule of a key range is read back)."""
    return f"SELECT DISTINCT org, namespace FROM loom.memory_embeddings WHERE {where}"


def _in_turns(where: str, *, newest_first: bool = False, held: bool = False) -> str:
    """The vector rows matching `where`, in turns across (org, namespace), each
    namespace's rows oldest first (newest first with `newest_first`), so one namespace's
    backlog cannot hold the others back; within a turn the namespaces come in an order
    drawn from the pass's `seed`. Read strongly: a stamp a replica lacks is a row paid
    for twice."""
    order = "created_at DESC" if newest_first else "created_at"
    if held:
        where += """ AND (org, namespace, agent, user_id, session_id, memory_key) NOT IN
            {held:Array(Tuple(String, String, String, String, String, String))}"""
    return f"""
        SELECT {_QUEUE_COLUMNS}
        FROM loom.memory_embeddings FINAL
        WHERE (org, namespace, agent, user_id, session_id, embedding_model, memory_key) IN (
            SELECT org, namespace, agent, user_id, session_id, embedding_model, memory_key
            FROM (
                SELECT org, namespace, agent, user_id, session_id, embedding_model,
                       memory_key, created_at,
                       row_number() OVER (PARTITION BY org, namespace ORDER BY {order})
                           AS turn
                FROM loom.memory_embeddings FINAL
                WHERE (org, namespace) IN ({_queued_pairs(where)})
                  AND {where}
            )
            ORDER BY turn, cityHash64(org, namespace, {{seed:UInt64}}), created_at
            LIMIT {{lim:UInt32}}
        )
          AND {where}
        ORDER BY created_at ASC
        SETTINGS select_sequential_consistency = 1
        """


def _capped(capped: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(str(o), str(n)) for o, n in capped]


async def queued_namespaces(
    *, recheck: bool = True, max_age_days: int = 30, ch: _CHClient | None = None
) -> list[tuple[str, str]]:
    """The (org, namespace) pairs that may hold owed rows, and with `recheck` unjudged
    `conflicted` rows, the queue would read: the pairs the worker checks against their
    cost caps. A superset (any version counts): an extra pair is only checked."""
    ch = ch or _default_client()
    sql = _queued_pairs(_OWED)
    if recheck:
        sql += f" UNION DISTINCT {_queued_pairs(_UNJUDGED)}"
    rows = await ch.query_all(sql, {"capped": [], "max_age": int(max_age_days)})
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def owed_rows(
    *,
    limit: int,
    capped: Sequence[tuple[str, str]] = (),
    held: Sequence[tuple[str, ...]] = (),
    seed: int = 0,
    max_age_days: int = 30,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Rows whose write the resolver owes a judgement, in turns across namespaces (in an
    order drawn from `seed` within a turn), each namespace's newest first: under its cost
    cap a busy namespace may never drain, and its latest writes are the ones a read
    needs judged. None of a namespace in `capped`, none older than `max_age_days`."""
    ch = ch or _default_client()
    params = {
        "lim": int(limit),
        "capped": _capped(capped),
        "seed": int(seed),
        "max_age": int(max_age_days),
    }
    if held:
        params["held"] = list(held)
    return await ch.query_all(_in_turns(_OWED, newest_first=True, held=bool(held)), params)


async def unjudged_conflicted_rows(
    *,
    limit: int,
    capped: Sequence[tuple[str, str]] = (),
    held: Sequence[tuple[str, ...]] = (),
    seed: int = 0,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """`conflicted` rows no resolver has judged (flags raised by cosine alone, or by an
    older build's judge), in turns across namespaces as `owed_rows`; none of a namespace
    in `capped`."""
    ch = ch or _default_client()
    params = {"lim": int(limit), "capped": _capped(capped), "seed": int(seed)}
    if held:
        params["held"] = list(held)
    return await ch.query_all(_in_turns(_UNJUDGED, held=bool(held)), params)


async def under_erasure(org: str, *, ch: _CHClient | None = None) -> bool:
    """Whether `org` has an erasure the queue leaves it out for: read again right
    before each write, since one can be requested while a row's call is in flight."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT count() AS n FROM ({ERASING_ORGS_SQL}) WHERE org = {{org:String}}"
        " SETTINGS select_sequential_consistency = 1",
        {"org": org},
    )
    return bool(rows and int(rows[0]["n"]))


def _same_write(row: Mapping[str, Any], judged: Mapping[str, Any]) -> bool:
    sha = str(judged.get("content_sha256") or "")
    if sha:
        return str(row.get("content_sha256") or "") == sha
    # A vector row with no write hash (rows written before 038_embedding_write_sha.sql):
    # compare the version judged instead.
    return int(row.get("version") or 0) == int(judged.get("version") or 0)


def _owed(row: Mapping[str, Any]) -> bool:
    mark = row.get("conflict_checked_at")
    return mark is not None and calendar.timegm(mark.utctimetuple()) < 2


async def stamp_judged(
    judged: Mapping[str, Any], *, keep_owed: bool = False, ch: _CHClient | None = None
) -> int:
    """Stamp the judged write's vector rows `JUDGED_MARK` (`judged`: a queue row); with
    `keep_owed`, a row still owed its own turn keeps its mark. Nothing under erasure."""
    ch = ch or _default_client()
    if await under_erasure(str(judged["org"]), ch=ch):
        return 0
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
    if keep_owed:
        own = [r for r in own if not _owed(r)]
    return await reemit_rows(own, {"conflict_checked_at": JUDGED_MARK}, ch=ch)


# The row's subject's live vectors of its type and its embedding model, its own left out:
# two models' vectors of one dimension are not comparable. What would drop a candidate
# later is applied here, before a LIMIT, so closer rows of another type, expired or
# superseded ones cannot take every place. Sensitivity needs no predicate: a
# write labelled 'high' retires the vectors of the version it replaces.
_LIVE_VECTORS = """
        SELECT agent, user_id, session_id, memory_key,
               cosineDistance(embedding, {emb:Array(Float32)}) AS dist
        FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND user_id IN {users:Array(String)} AND ({anon:UInt8} = 0 OR agent = {a:String})
          -- The row itself, by its full identity: a twin under the other spelling of no
          -- user or no session is another row, and a peer.
          AND NOT (agent = {a:String} AND user_id = {u:String} AND session_id = {s:String}
                   AND memory_key = {k:String})
          AND scope = 'private' AND tombstoned_at IS NULL
          AND status IN ('active', 'conflicted') AND memory_type = {mt:String}
          AND (expires_at IS NULL OR expires_at > now())
          AND (valid_to IS NULL OR valid_to > now())
          AND embedding_model = {model:String}
          AND embedding_dim = {dim:UInt16} AND embedding.size0 = {dim:UInt16}"""


def _nearest_sql() -> str:
    """The row's nearest vectors in its subject: the same user (the anonymous user is
    one subject per agent), its own vector left out. A named user's other sessions are
    peers like any other; the anonymous user's are separate tasks, so only its own
    session is read (a peer the row flags comes through `_ANCHORED_SQL`; one that flags
    only the row, when the row's side of the mark was lost, waits for a person)."""
    from server.core.vectors.search_settings import vector_search_settings

    return (
        _LIVE_VECTORS
        + """
          AND ({anon:UInt8} = 0 OR session_id IN {own_sessions:Array(String)})
        ORDER BY dist ASC
        LIMIT {lim:UInt32}
        """
        + vector_search_settings(table="loom.memory_embeddings")
    )


# The candidates' rows, joined by the full identity (a key alone repeats across sessions),
# within the row's subject like every read of the cluster. A pinned row is no peer: no
# verdict may change it, so judging it is a paid call for nothing.
_PEER_ROWS_SQL = """
        SELECT toString(memory_id) AS memory_id, org, namespace, agent, user_id, session_id,
               memory_key, toString(value) AS value, pinned, valid_at, created_at, updated_at,
               version, redacted_at, toString(content_sha256) AS content_sha256, status,
               arrayMap(x -> toString(x), conflict_with) AS conflict_with
        FROM loom.memory FINAL
        PREWHERE org = {org:String} AND namespace = {ns:String}
          AND (agent, user_id, session_id, memory_key)
              IN {ids:Array(Tuple(String, String, String, String))}
        WHERE user_id IN {users:Array(String)} AND ({anon:UInt8} = 0 OR agent = {a:String})
          AND memory_type = {mt:String}
          AND tombstoned_at IS NULL
          AND NOT pinned
          AND status IN ('active', 'conflicted')
          AND sensitivity != 'high'
          AND (expires_at IS NULL OR expires_at > now())
          AND (valid_to IS NULL OR valid_to > now())
        SETTINGS select_sequential_consistency = 1
        """


# The row's flagged peers (read from its head: it can hold thousands) and, for a named
# user, the nearest rows of its key in other sessions, bounded, with their distance: they
# join the cluster whatever the cutoff or the floor, since the row may be owed for them
# alone. Only the row's subject: a cosine flag can name another user's row, whose text
# must not reach the prompt.
_FLAGGED_IDENTITIES_SQL = """
                  SELECT agent, user_id, session_id, memory_key FROM loom.memory FINAL
                  WHERE org = {org:String} AND namespace = {ns:String} AND memory_id IN (
                    SELECT arrayJoin(conflict_with) FROM loom.memory FINAL
                    WHERE org = {org:String} AND namespace = {ns:String} AND agent = {a:String}
                      AND user_id = {u:String} AND session_id = {s:String}
                      AND memory_key = {k:String})"""
_ANCHORED_SQL = (
    "SELECT * FROM ("
    + _LIVE_VECTORS
    + """
          AND (agent, user_id, session_id, memory_key) IN ("""
    + _FLAGGED_IDENTITIES_SQL
    + """)
        ORDER BY dist ASC
        LIMIT {lim:UInt32})
        UNION ALL"""
    + _LIVE_VECTORS
    + """
          AND memory_key = {k:String} AND {anon:UInt8} = 0
        ORDER BY dist ASC
        LIMIT {same_lim:UInt32}
        SETTINGS select_sequential_consistency = 1
        """
)


def key_identity(r: Any) -> tuple[str, str, str, str]:
    """A row's (agent, user_id, session_id, memory_key), from a dict or a row object: its
    key within (org, namespace)."""
    cols = ("agent", "user_id", "session_id", "memory_key")
    if isinstance(r, Mapping):
        return tuple(str(r[c]) for c in cols)  # type: ignore[return-value]
    return tuple(str(getattr(r, c)) for c in cols)  # type: ignore[return-value]


def _subject_params(user_id: str) -> dict[str, Any]:
    """The cluster reads' subject: a named user's rows, or for the anonymous user (either
    spelling, '-' or a user_id sent empty) the rows of the same agent."""
    if is_anonymous_user(user_id):
        return {"users": [SENTINEL, ""], "anon": 1}
    return {"users": [user_id], "anon": 0}


class Cluster(NamedTuple):
    """A row's peers for one call, and its flagged peers that could join but did not fit."""

    peers: list[dict[str, Any]]
    flags_left: frozenset[str]


async def cluster_peers(
    row: Mapping[str, Any],
    *,
    floor: float,
    limit: int,
    prefer: frozenset[str] = frozenset(),
    shown: frozenset[str] = frozenset(),
    ch: _CHClient | None = None,
) -> Cluster:
    """Up to `limit` live peers of the queue row `row`, each with `similarity`: its flags
    (`prefer`, those not `shown` before first) and a named user's other sessions of its
    key take places ahead of the nearest rows, so the pairs it is owed for are always
    judged."""
    ch = ch or _default_client()
    emb = [float(x) for x in (row.get("embedding") or [])]
    if not emb:
        return Cluster([], frozenset())
    params: dict[str, Any] = {
        "org": str(row["org"]),
        "ns": str(row["namespace"]),
        "a": str(row["agent"]),
        "u": str(row["user_id"]),
        "s": str(row["session_id"]),
        "k": str(row["memory_key"]),
        "mt": str(row["memory_type"]),
        **_subject_params(str(row["user_id"])),
        "own_sessions": session_spellings(str(row["session_id"])),
        "emb": emb,
        "model": str(row["embedding_model"]),
        "dim": len(emb),
        "lim": 4 * max(int(limit), 1),
    }
    half = max(int(limit) // 2, 1)
    near = [c for c in await ch.query_all(_nearest_sql(), params=params) if 1 - c["dist"] >= floor]
    anchored = await ch.query_all(_ANCHORED_SQL, params={**params, "same_lim": half})
    above_floor = {key_identity(c) for c in near}
    found = {key_identity(c): c for c in [*anchored, *near]}
    if not found:
        return Cluster([], frozenset())
    rows = await ch.query_all(_PEER_ROWS_SQL, params={**params, "ids": list(found)})
    peers = [
        {**dict(r), "similarity": 1.0 - float(found[key_identity(r)]["dist"])}
        for r in rows
        if key_identity(r) in found
    ]
    peers.sort(key=lambda p: (-p["similarity"], str(p["memory_key"])))
    key = str(row["memory_key"])
    flagged = [p for p in peers if str(p["memory_id"]) in prefer]
    flagged.sort(key=lambda p: str(p["memory_id"]) in shown)  # not judged before first
    taken = {str(p["memory_id"]) for p in flagged}
    same_key = [p for p in peers if str(p["memory_id"]) not in taken and p["memory_key"] == key]
    same_key = same_key[:half]
    taken |= {str(p["memory_id"]) for p in same_key}
    rest = [p for p in peers if str(p["memory_id"]) not in taken and key_identity(p) in above_floor]
    kept = (flagged + same_key + rest)[: max(int(limit), 0)]
    kept.sort(key=lambda p: (-p["similarity"], str(p["memory_key"])))
    left = {str(p["memory_id"]) for p in flagged} - {str(p["memory_id"]) for p in kept}
    return Cluster(kept, frozenset(left))


# The ids the row's head flags whose row is no longer live (deleted, retired, rejected,
# expired or ended): read from the head in ClickHouse, since it can flag thousands.
_DEAD_FLAGS_SQL = """
        WITH flags AS (
            SELECT arrayJoin(conflict_with) AS id FROM loom.memory FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND agent = {a:String}
              AND user_id = {u:String} AND session_id = {s:String} AND memory_key = {k:String})
        SELECT toString(id) AS id FROM flags
        WHERE id NOT IN (
            SELECT memory_id FROM loom.memory FINAL
            PREWHERE org = {org:String} AND namespace = {ns:String}
            WHERE memory_id IN (SELECT id FROM flags)
              AND tombstoned_at IS NULL
              AND status IN ('active', 'conflicted')
              AND (expires_at IS NULL OR expires_at > now())
              AND (valid_to IS NULL OR valid_to > now()))
        SETTINGS select_sequential_consistency = 1
        """


async def dead_flags(
    org: str, namespace: str, identity: tuple[str, str, str, str], *, ch: _CHClient | None = None
) -> list[str]:
    """The ids the head of `identity` (agent, user_id, session_id, memory_key) flags whose
    row is no longer live, read strongly."""
    ch = ch or _default_client()
    a, u, s, k = identity
    rows = await ch.query_all(
        _DEAD_FLAGS_SQL, {"org": org, "ns": namespace, "a": a, "u": u, "s": s, "k": k}
    )
    return [str(r["id"]) for r in rows]


# Ids bind as UUIDs: `memory_id` itself, not a cast of it, is what memory_id_skip prunes
# (and no `toString(memory_id) AS memory_id` alias, which the WHERE would read instead).
_PROTECTED_SQL = """
        SELECT memory_id
        FROM loom.memory FINAL
        PREWHERE org = {org:String} AND namespace = {ns:String}
        WHERE memory_id IN {ids:Array(UUID)} AND sensitivity = 'high'
        SETTINGS select_sequential_consistency = 1
        """


async def protected_ids(
    org: str, namespace: str, ids: list[str], *, ch: _CHClient | None = None
) -> set[str]:
    """The ids among `ids` whose current row is labelled 'high', read strongly: such a
    row's text never goes to the model, whatever was read before."""
    if not ids:
        return set()
    ch = ch or _default_client()
    rows = await ch.query_all(
        _PROTECTED_SQL, {"org": org, "ns": namespace, "ids": [str(i) for i in ids]}
    )
    return {str(r["memory_id"]) for r in rows}


# Each row's history, oldest first: (version, holds the asked hash, superseded).
_HISTORY_SQL = """
        SELECT toString(memory_id) AS memory_id,
               arraySort(x -> x.1, groupArray((version, held, superseded))) AS run
        FROM (
            SELECT memory_id, version,
                   (toString(memory_id), toString(content_sha256))
                       IN {rows:Array(Tuple(String, String))} AS held,
                   status = 'superseded' AS superseded
            FROM loom.memory_versions
            PREWHERE org = {org:String} AND namespace = {ns:String}
            WHERE memory_id IN {ids:Array(UUID)}
        )
        GROUP BY memory_id
        HAVING countIf(held) > 0
        SETTINGS select_sequential_consistency = 1
        """

# Some rows' Undos (`memory.unsupersede` records); a record without the restored hash
# and the restore's version is not read.
_UNDOS_SQL = """
        SELECT JSONExtractString(subject, 'memory_id') AS memory_id,
               JSONExtractString(subject, 'superseded_by') AS superseded_by,
               JSONExtractString(subject, 'content_sha256') AS content_sha256,
               JSONExtractUInt(subject, 'version') AS version
        FROM loom.audit_log
        WHERE org = {org:String} AND namespace = {ns:String}
          AND event_type = 'memory.unsupersede'
          AND JSONExtractString(subject, 'memory_id') IN {ids:Array(String)}
          AND JSONExtractString(subject, 'content_sha256') != ''
          AND JSONExtractUInt(subject, 'version') > 0
        SETTINGS select_sequential_consistency = 1
        """


# Whether an Undo record names this row's restore at this version (and, when given, the
# memory it was retired behind).
_UNDO_RECORDED_SQL = """
        SELECT count() > 0 AS recorded
        FROM loom.audit_log
        WHERE org = {org:String} AND namespace = {ns:String}
          AND event_type = 'memory.unsupersede'
          AND JSONExtractString(subject, 'memory_id') = {id:String}
          AND JSONExtractUInt(subject, 'version') = {v:UInt64}%s
        SETTINGS select_sequential_consistency = 1
        """


async def undo_recorded(
    org: str,
    namespace: str,
    memory_id: str,
    version: int,
    superseded_by: str = "",
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether a `memory.unsupersede` record names `memory_id` restored at `version`
    (behind `superseded_by`, when given): that version is that Undo's restore."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "id": memory_id, "v": int(version)}
    behind = ""
    if superseded_by:
        behind = "\n          AND JSONExtractString(subject, 'superseded_by') = {by:String}"
        params["by"] = superseded_by
    rows = await ch.query_all(_UNDO_RECORDED_SQL % behind, params)
    return bool(rows and rows[0]["recorded"])


class Undo(NamedTuple):
    """A person's Undo of `memory_id`'s retirement behind `superseded_by`: the value it
    restored and the version the restore wrote."""

    memory_id: str
    superseded_by: str
    content_sha256: str
    version: int


class Asserted(NamedTuple):
    """`asserted_versions`' answer: {memory_id: when its current value was asserted, in
    ns} and the rows' Undos."""

    asserted: dict[str, int]
    undos: list[Undo]


def _run_start(run: list[tuple[int, bool, bool]], undo_versions: set[int]) -> int | None:
    """The first version of the current run of versions holding the hash: a restatement
    after a retirement starts a new run, an Undo's version does not."""
    start: int | None = None
    after_retirement = False
    for version, held, superseded in run:
        if not held:
            start = None
        elif start is None or (
            after_retirement and not superseded and version not in undo_versions
        ):
            start = version
        after_retirement = held and superseded
    return start


async def asserted_versions(
    org: str, namespace: str, rows: list[tuple[str, str]], *, ch: _CHClient | None = None
) -> Asserted:
    """`asserted`: {memory_id: the first version of the current run of history rows
    holding `sha`} for each (memory_id, sha) in `rows`: when the row's current value was
    asserted, in wall-clock ns. A metadata copy (touch, pin, tags) keeps the hash, so it
    does not move it; a value that came back, or that its writer restated after a
    retirement no one undid, is asserted then. An id the history does not hold is left
    out. `undos`: the rows' Undos."""
    if not rows:
        return Asserted({}, [])
    ch = ch or _default_client()
    ids = [str(i) for i, _ in rows]
    params = {
        "org": org,
        "ns": namespace,
        "ids": ids,
        "rows": [(str(i), str(h)) for i, h in rows],
    }
    history, found = await asyncio.gather(
        ch.query_all(_HISTORY_SQL, params),
        ch.query_all(_UNDOS_SQL, {"org": org, "ns": namespace, "ids": ids}),
    )
    undos = [
        Undo(
            str(u["memory_id"]),
            str(u["superseded_by"]),
            str(u["content_sha256"]),
            int(u["version"]),
        )
        for u in found
    ]
    undone: dict[str, set[int]] = {}
    for u in undos:
        undone.setdefault(u.memory_id, set()).add(u.version)
    out: dict[str, int] = {}
    for h in history:
        run = [(int(v), bool(held), bool(sup)) for v, held, sup in h["run"]]
        start = _run_start(run, undone.get(str(h["memory_id"]), set()))
        if start is not None:
            out[str(h["memory_id"])] = start
    return Asserted(out, undos)


__all__ = [
    "ERASING_ORGS_SQL",
    "JUDGED_MARK",
    "OWED_MARK",
    "Asserted",
    "Undo",
    "asserted_versions",
    "cluster_peers",
    "dead_flags",
    "key_identity",
    "owed_rows",
    "protected_ids",
    "queued_namespaces",
    "stamp_judged",
    "under_erasure",
    "undo_recorded",
    "unjudged_conflicted_rows",
]
