"""Statements over the bi-temporal journal and its projections.

`memory_temporal_commits` is the authority; `memory_temporal_assertions` and the
watermark and vector tables are projections of it. Every statement that
reads or writes those tables lives here. The publisher and the cutoff read run
inside `sequential_reads()`, so they see every acknowledged commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and, scope_where
from server.memory.store.redaction import REDACTED_VALUE

COMMITS = "loom.memory_temporal_commits"
ASSERTIONS = "loom.memory_temporal_assertions"
WATERMARKS = "loom.memory_temporal_watermarks"
VECTORS = "loom.memory_temporal_vectors"

# A commit is part of the journal only if it landed within this long of the time
# its publisher stamped; a publisher that sees its own insert land later withdraws
# it and fails the write. Until a sequence is that old its insert may still land,
# so reads cut below it (`stable_prefix`).
SETTLE_S = 120
_ON_TIME = f"inserted_at <= recorded_at + INTERVAL {SETTLE_S} SECOND"


@dataclass(frozen=True)
class Erased:
    """When erasures of a namespace, and of users, were requested (epoch µs):
    content recorded before them is never served, whatever a failed or overtaken
    cleanup left in a table."""

    namespace_us: int = 0
    users: tuple[tuple[str, int], ...] = ()


NOT_ERASED = Erased()


def _not_erased(col: str, e: Erased, params: dict[str, Any]) -> str:
    """` AND …`: `col` (a recording time) is not before an erasure of its scope."""
    out = ""
    if e.namespace_us:
        out += f" AND toUnixTimestamp64Micro({col}) >= {{er_ns:Int64}}"
        params["er_ns"] = e.namespace_us
    if e.users:
        out += (
            f" AND NOT arrayExists(x -> x.1 = user_id AND toUnixTimestamp64Micro({col}) < x.2, "
            "{er_users:Array(Tuple(String, Int64))})"
        )
        params["er_users"] = list(e.users)
    return out


async def high_after(
    org: str, namespace: str, after_seq: int, *, ch: _CHClient | None = None
) -> set[tuple[str, str, str, str]]:
    """Identities a commit past `after_seq` (not yet projected) gave a 'high'
    assertion: withheld from a non-key read until the projection says otherwise.
    Read from the payloads, so no tail bound leaves one out."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT agent, user_id, session_id, memory_key FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND commit_seq > {lo:UInt64} "
        "AND arrayExists(a -> JSONExtractString(a, 'sensitivity') = 'high', "
        "JSONExtractArrayRaw(assertions))",
        params={"org": org, "ns": namespace, "lo": after_seq},
    )
    return {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))
        for r in rows
    }


# A batch's identities, for `(agent, user_id, session_id, memory_key) IN ...`.
_IDENT = "(agent, user_id, session_id, memory_key)"
_KEYS = "{keys:Array(Tuple(String, String, String, String))}"

COMMIT_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "scope",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_id",
    "commit_seq",
    "commit_id",
    "parent_commit_id",
    "publisher_id",
    "recorded_at",
    "request_id",
    "idempotency_key",
    "request_sha256",
    "request_slot",
    "operation",
    "change_kind",
    "payload_schema",
    "payload_sha256",
    "actor",
    "source_class",
    "source_resource_id",
    "erasure_generation",
    "retired_assertion_ids",
    "assertions",
    "retain_until",
    "row_version",
    "restated_assertion_id",
)

ASSERTION_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "scope",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_id",
    "assertion_id",
    "parent_assertion_id",
    "origin_assertion_id",
    "born_seq",
    "born_at",
    "retired_seq",
    "retired_at",
    "projected_seq",
    "source_version",
    "memory_type",
    "value",
    "value_sha256",
    "embed_input_sha256",
    "temporal_kind",
    "validity_basis",
    "valid_from",
    "valid_to",
    "valid_text",
    "valid_precision",
    "valid_tz",
    "modality",
    "status",
    "confidence",
    "expires_at",
    "pinned",
    "lock_mode",
    "lock_until",
    "lock_reason",
    "locked_by",
    "locked_at",
    "verified_by",
    "verified_at",
    "verified_sha256",
    "grounding",
    "source_class",
    "actor",
    "sensitivity",
    "qualifiers",
    "tags",
    "entities",
    "evidence_ids",
    "derived_from",
)


def _check(rows: Sequence[Mapping[str, Any]], cols: tuple[str, ...], table: str) -> None:
    """The authority never takes a partial row: a missing column would be filled
    with a DDL default and silently weaken what the row records."""
    wanted = set(cols)
    for i, row in enumerate(rows):
        if set(row) != wanted:
            raise ValueError(
                f"{table} row #{i} is not a full row: missing={sorted(wanted - set(row))} "
                f"unknown={sorted(set(row) - wanted)}"
            )


async def insert_commits(rows: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None) -> None:
    """Synchronous and committed: this insert is the publication boundary. The
    journal is not a projected table, so a column the live table lacks fails the
    insert instead of being dropped."""
    if not rows:
        return
    _check(rows, COMMIT_COLS, COMMITS)
    ch = ch or _default_client()
    # One token per batch: a retry of the same rows is deduplicated where the engine
    # keeps a window (replicated, Cloud); every reader also folds rows by commit_id.
    token = hashlib.sha256(",".join(sorted(str(r["commit_id"]) for r in rows)).encode()).hexdigest()
    await ch.insert(
        COMMITS,
        [dict(r) for r in rows],
        # The publisher's read-back is exact on the supported targets
        # (Cloud, one OSS node); on self-managed replication it is best-effort.
        # Claim sequences in Keeper (strict KeeperMap rows) if that is supported.
        settings={
            "async_insert": 0,
            "wait_for_async_insert": 1,
            "insert_deduplication_token": token,
        },
    )


async def insert_assertions(
    rows: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None
) -> None:
    if not rows:
        return
    _check(rows, ASSERTION_COLS, ASSERTIONS)
    ch = ch or _default_client()
    await ch.insert(ASSERTIONS, [dict(r) for r in rows])


async def commits_by_id(
    org: str, namespace: str, commit_ids: Sequence[str], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The committed rows of these ids, for reconciling an uncertain insert."""
    if not commit_ids:
        return []
    ch = ch or _default_client()
    return await ch.query_all(
        f"SELECT commit_id, commit_seq, toString(payload_sha256) AS payload_sha256 FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND commit_id IN {ids:Array(UUID)}",
        params={"org": org, "ns": namespace, "ids": list(commit_ids)},
    )


# The integrity scan looks at the newest sequences only: a break refuses every
# later write, so it is never buried deeper than the batch that made it.
_BREAK_WINDOW = 1000
_BROKEN_AT = f"""
    (SELECT min(commit_seq) FROM (
        SELECT commit_seq FROM {COMMITS}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND commit_seq > (
              SELECT max(commit_seq) FROM {COMMITS}
              WHERE org = {{org:String}} AND namespace = {{ns:String}}
          ) - {_BREAK_WINDOW}
        GROUP BY commit_seq HAVING uniqExact(payload_sha256) > 1
    ))"""


# The last sequence the publisher allocated, and the recording time it stamped,
# kept apart from the erasable commits.
_PUBLISHED = (
    f"(SELECT max(applied_seq) FROM {WATERMARKS} WHERE org = {{org:String}} "
    "AND namespace = {ns:String} AND projection = 'published')"
)
_PUBLISHED_AT = (
    f"(SELECT argMax(recorded_at, applied_seq) FROM {WATERMARKS} WHERE org = {{org:String}} "
    "AND namespace = {ns:String} AND projection = 'published')"
)


async def server_now(*, ch: _CHClient | None = None) -> datetime:
    """ClickHouse's clock: the one `inserted_at` and an erasure's `requested_at`
    are stamped with, so the publisher stamps `recorded_at` with it too."""
    ch = ch or _default_client()
    row = await ch.query_one("SELECT now64(6, 'UTC') AS t")
    t: datetime = (row or {})["t"]
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


async def landed_late(
    org: str, namespace: str, commit_ids: Sequence[str], *, ch: _CHClient | None = None
) -> bool:
    """Did any of these commits land outside `SETTLE_S` of its recording time?"""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT countIf(NOT ({_ON_TIME})) AS late FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND commit_id IN {ids:Array(UUID)}",
        params={"org": org, "ns": namespace, "ids": list(commit_ids)},
    )
    return int((row or {}).get("late") or 0) > 0


async def head(org: str, namespace: str, *, ch: _CHClient | None = None) -> tuple[int, Any, int]:
    """(highest sequence allocated, the latest recording time stamped, the lowest
    sequence holding two payloads or 0). An allocation whose commit was erased,
    expired or never landed still counts: neither its sequence nor its recording
    time is ever handed out again, so both keep rising together."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT greatest(max(commit_seq), {_PUBLISHED}) AS seq, "
        f"greatest(max(recorded_at), ifNull({_PUBLISHED_AT}, toDateTime64(0, 6, 'UTC'))) AS at, "
        f"{_BROKEN_AT} AS broken FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
    )
    row = row or {}
    return int(row.get("seq") or 0), row.get("at"), int(row.get("broken") or 0)


async def mark_published(
    org: str, namespace: str, seq: int, recorded_at: datetime, *, ch: _CHClient | None = None
) -> None:
    """Record an allocation (its last sequence and recording time) before its insert."""
    ch = ch or _default_client()
    await ch.insert(
        WATERMARKS,
        [
            {
                "org": org,
                "namespace": namespace,
                "projection": "published",
                "applied_seq": seq,
                "recorded_at": recorded_at,
            }
        ],
    )


async def other_commits_at(
    org: str,
    namespace: str,
    seqs: Sequence[int],
    commit_ids: Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> list[int]:
    """Sequences of `seqs` that also hold a commit other than `commit_ids`."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT commit_seq FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND commit_seq IN {seqs:Array(UInt64)} AND commit_id NOT IN {ids:Array(UUID)}",
        params={"org": org, "ns": namespace, "seqs": list(seqs), "ids": list(commit_ids)},
    )
    return [int(r["commit_seq"]) for r in rows]


async def withdraw_commits(
    org: str, namespace: str, commit_ids: Sequence[str], *, ch: _CHClient | None = None
) -> None:
    """Delete commits no caller was told of (a lost sequence race), synchronously."""
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND commit_id IN {ids:Array(UUID)}",
        params={"org": org, "ns": namespace, "ids": list(commit_ids)},
        settings={"lightweight_deletes_sync": 2},
    )


async def repair_namespace(org: str, namespace: str, *, ch: _CHClient | None = None) -> list[int]:
    """At each recent sequence holding more than one payload (a lost race whose
    withdrawal failed), keep the commit that landed first, which a caller may have
    been told of, and delete the others. Returns the sequences repaired.
    "First" is the server's inserted_at; the loser inserted after the
    winner's read-back, a round trip later, so replica clock skew past that
    misorders them. Claim sequences in Keeper if that ever shows up."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace}
    broken = await ch.query_all(
        f"SELECT commit_seq, toString(argMin(commit_id, (inserted_at, commit_id))) AS kept "
        f"FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        f"AND commit_seq > (SELECT max(commit_seq) FROM {COMMITS} "
        f"WHERE org = {{org:String}} AND namespace = {{ns:String}}) - {_BREAK_WINDOW} "
        "GROUP BY commit_seq HAVING uniqExact(payload_sha256) > 1",
        params=params,
    )
    for b in broken:
        await ch.execute(
            f"DELETE FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
            "AND commit_seq = {seq:UInt64} AND commit_id != {kept:UUID}",
            params={**params, "seq": b["commit_seq"], "kept": b["kept"]},
            settings={"lightweight_deletes_sync": 2},
        )
    return [int(b["commit_seq"]) for b in broken]


async def delete_orphan_vectors(*, ch: _CHClient | None = None) -> int:
    """Delete the vectors no assertion uses any more: retention expired their last
    one. A full anti-join per pass; bound it by namespace if it grows."""
    ch = ch or _default_client()
    orphan = (
        f"(org, namespace, embed_input_sha256) NOT IN "
        f"(SELECT org, namespace, embed_input_sha256 FROM {ASSERTIONS} "
        f"WHERE value != '{REDACTED_VALUE}')"  # a redacted assertion uses no vector
    )
    row = await ch.query_one(f"SELECT count() AS n FROM {VECTORS} WHERE {orphan}")
    n = int((row or {}).get("n") or 0)
    if n:
        await ch.execute(
            f"DELETE FROM {VECTORS} WHERE {orphan}", settings={"lightweight_deletes_sync": 2}
        )
    return n


async def namespaces_broken(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Namespaces with a sequence committed in the last day holding two payloads: a
    break a write would repair, found for namespaces nobody writes to."""
    ch = ch or _default_client()
    return await ch.query_all(
        f"SELECT DISTINCT org, namespace FROM {COMMITS} "
        "WHERE recorded_at > now64(6) - INTERVAL 1 DAY "
        "GROUP BY org, namespace, commit_seq HAVING uniqExact(payload_sha256) > 1 LIMIT 100"
    )


async def commits_for_idempotency_key(
    org: str,
    namespace: str,
    idempotency_key: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    erased: Erased = NOT_ERASED,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    """Per identity, the commits an earlier request under this client retry key
    made, in order (those in the journal: landed on time, not erased)."""
    if not keys:
        return {}
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "k": idempotency_key,
        "keys": [tuple(k) for k in keys],
    }
    rows = await ch.query_all(
        f"SELECT agent, user_id, session_id, memory_key, toString(memory_id) AS memory_id, "
        f"commit_id, commit_seq, recorded_at, row_version, "
        f"change_kind, retired_assertion_ids, assertions, request_sha256, request_slot "
        f"FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        f"AND idempotency_key = {{k:String}} AND {_IDENT} IN {_KEYS} AND {_ON_TIME}"
        + _not_erased("recorded_at", erased, params)
        + " ORDER BY commit_seq LIMIT 1 BY commit_id",
        params=params,
    )
    out: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        ident = (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))
        out.setdefault(ident, []).append(r)
    return out


async def served_for_keys(
    org: str,
    namespace: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    erased: Erased = NOT_ERASED,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The projection's currently served assertions of these (agent, user_id,
    session_id, memory_key) identities, every column."""
    if not keys:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "keys": [tuple(k) for k in keys]}
    return await ch.query_all(
        f"SELECT * FROM {ASSERTIONS} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        f"AND {_IDENT} IN {_KEYS} "
        "AND retired_seq IS NULL" + _not_erased("born_at", erased, params),
        params=params,
    )


async def commits_between(
    org: str,
    namespace: str,
    after_seq: int,
    upto_seq: int,
    *,
    memory_keys: Sequence[str] | None = None,
    erased: Erased = NOT_ERASED,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Journal rows with after_seq < commit_seq <= upto_seq, in order, one row per
    (seq, commit_id, payload) so an explicit retry duplicate is read once. Only
    the journal: commits that landed on time and no erasure has removed."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "lo": after_seq, "hi": upto_seq}
    key_clause = ""
    if memory_keys is not None:
        key_clause = "AND memory_key IN {keys:Array(String)}"
        params["keys"] = list(memory_keys)
    return await ch.query_all(
        f"SELECT * FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND commit_seq > {lo:UInt64} AND commit_seq <= {hi:UInt64} "
        f"{key_clause} AND {_ON_TIME}"
        + _not_erased("recorded_at", erased, params)
        + " ORDER BY commit_seq, commit_id LIMIT 1 BY commit_seq, commit_id, payload_sha256",
        params=params,
    )


async def cutoff(
    org: str, namespace: str, known_as_of: datetime | None, *, ch: _CHClient | None = None
) -> dict[str, Any]:
    """One journal read that fixes a read's cutoff: the highest sequence recorded at
    or before K (or the highest committed), the allocation head, the first commit,
    the lowest sequence holding two different payloads (an integrity break: the
    valid prefix ends below it), and what bounds the stable prefix: the newest
    settled commit and the sequences committed after it (`stable_prefix`)."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace}
    k_expr = "1"
    if known_as_of is not None:
        k_expr = "recorded_at <= {k:DateTime64(6, 'UTC')}"
        params["k"] = known_as_of
    row = await ch.query_one(
        f"""
        SELECT
            maxIf(commit_seq, {k_expr}) AS seq,
            countIf({k_expr}) AS known,
            greatest(max(commit_seq), {_PUBLISHED}) AS head,
            max(commit_seq) AS committed,
            min(recorded_at) AS first_at,
            count() AS n,
            maxIf(commit_seq, recorded_at <= now64(6) - INTERVAL {SETTLE_S} SECOND) AS settled,
            groupUniqArrayIf(commit_seq, recorded_at > now64(6) - INTERVAL {SETTLE_S} SECOND)
                AS recent,
            {_BROKEN_AT} AS broken_at
        FROM {COMMITS}
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {_ON_TIME}
        """,
        params=params,
    )
    return dict(row or {})


async def rows_before_journal(
    org: str, namespace: str, first_at: datetime | None, *, ch: _CHClient | None = None
) -> bool:
    """Did loom.memory hold a version well before the namespace's first commit
    (data that predates the journal, so history before it is unknown)? Read from
    the append-only history mirror, which merges never thin. A write draws its
    version before it commits, so "well before" is an hour. Cached per process: a
    namespace's journal start only moves when an erasure removes its first commits.
    A write slower than an hour reads as pre-journal."""
    key = (org, namespace, first_at)
    if key in _BEFORE_JOURNAL:
        return _BEFORE_JOURNAL[key]
    ch = ch or _default_client()
    before = "AND version < {v:UInt64}" if first_at is not None else ""
    v = int((first_at.timestamp() - 3600) * 1_000_000_000) if first_at is not None else 0
    row = await ch.query_one(
        "SELECT count() AS n FROM (SELECT 1 FROM loom.memory_versions "
        f"WHERE org = {{org:String}} AND namespace = {{ns:String}} {before} LIMIT 1)",
        params={"org": org, "ns": namespace, "v": v},
    )
    found = int((row or {}).get("n") or 0) > 0
    if first_at is not None:  # a namespace with no commits yet may still get its first
        _BEFORE_JOURNAL[key] = found
    return found


_BEFORE_JOURNAL: dict[tuple[str, str, datetime | None], bool] = {}


def stable_prefix(row: Mapping[str, Any], *, erased_head: int = 0) -> int:
    """The highest committed sequence below which no allocation is still in doubt.
    Sequences and recording times rise together and a commit counts only if it
    lands within SETTLE_S of its stamp, so a missing sequence below a settled
    commit can never land: only a gap above the newest settled commit is open.
    Sequences up to `erased_head` (the head a namespace erasure deleted below) are
    gone for good; every gap above it is still checked."""
    settled, committed = int(row.get("settled") or 0), int(row.get("committed") or 0)
    recent = {int(s) for s in row.get("recent") or ()}
    settled = max(settled, erased_head)
    for s in range(settled + 1, committed + 1):
        if s not in recent:
            return s - 1
    return committed


async def watermark(
    org: str, namespace: str, projection: str = "assertions", *, ch: _CHClient | None = None
) -> int:
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT max(applied_seq) AS seq FROM {WATERMARKS} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND projection = {p:String}",
        params={"org": org, "ns": namespace, "p": projection},
    )
    return int((row or {}).get("seq") or 0)


async def advance_watermark(
    org: str,
    namespace: str,
    applied_seq: int,
    projection: str = "assertions",
    *,
    ch: _CHClient | None = None,
) -> None:
    ch = ch or _default_client()
    await ch.insert(
        WATERMARKS,
        [
            {
                "org": org,
                "namespace": namespace,
                "projection": projection,
                "applied_seq": applied_seq,
            }
        ],
    )


async def namespaces_behind(
    *, limit: int = 50, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Namespaces whose assertion projection lags the published head, however long
    ago: the inline projection keeps up unless an insert failed."""
    return await unchecked_namespaces("assertions", limit=limit, ch=ch)


# Today's disclosure fence: a key whose latest assertion is 'high' (deleted or
# not), or whose current row is, is withheld from a non-key read at every K, its
# earlier revisions too. The current row covers a projection that lags the write.
# A commit adds kept segments and its new assertion at one sequence: a 'high' one
# among them wins the tie.
_HIGH_NOW = (
    f"SELECT agent, user_id, session_id, memory_key FROM {ASSERTIONS} FINAL "
    "WHERE org = {org:String} AND namespace = {ns:String} "
    "GROUP BY agent, user_id, session_id, memory_key "
    "HAVING max((born_seq, sensitivity = 'high')).2 = 1 "
    "UNION DISTINCT SELECT agent, user_id, session_id, memory_key FROM loom.memory FINAL "
    "WHERE org = {org:String} AND namespace = {ns:String} AND sensitivity = 'high'"
)


async def assertions_at(
    org: str,
    namespace: str,
    cutoff_seq: int,
    *,
    scope: ReadFilters,
    memory_keys: Sequence[str] | None = None,
    agent: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    include_high: bool = False,
    memory_type: str | None = None,
    tags: Sequence[str] = (),
    erased: Erased = NOT_ERASED,
    limit: int = 10_000,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Assertions served at cutoff C: born by C and not retired by C. `memory_type`
    and `tags` keep the keys with a served assertion that matches, before the
    limit; the caller filters assertions after the set is fixed, so a retired
    assertion never reappears because its replacement fails a filter."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "c": cutoff_seq, "lim": limit}
    served = (
        "org = {org:String} AND namespace = {ns:String} AND born_seq <= {c:UInt64} "
        "AND (retired_seq IS NULL OR retired_seq > {c:UInt64})"
    )
    where = [served]
    if memory_type is not None or tags:
        match = served
        if memory_type is not None:
            match += " AND memory_type = {mt:String}"
            params["mt"] = memory_type
        if tags:
            match += " AND hasAny(tags, {tg:Array(String)})"
            params["tg"] = list(tags)
        where.append(f"memory_key IN (SELECT memory_key FROM {ASSERTIONS} FINAL WHERE {match})")
    if memory_keys is not None:
        where.append("memory_key IN {keys:Array(String)}")
        params["keys"] = list(memory_keys)
    for col, val in (("agent", agent), ("user_id", user_id), ("session_id", session_id)):
        if val is not None and val != "*":
            where.append(f"{col} = {{{col}:String}}")
            params[col] = val
    if not include_high:
        where.append("sensitivity != 'high'")
        where.append(f"{_IDENT} NOT IN ({_HIGH_NOW})")
    sql = (
        f"SELECT * FROM {ASSERTIONS} FINAL WHERE "
        + " AND ".join(where)
        + _not_erased("born_at", erased, params)
        + scope_and(scope, params)
        + " ORDER BY memory_key, born_seq LIMIT {lim:UInt32}"
    )
    return await ch.query_all(sql, params=params)


async def key_history(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    upto_seq: int,
    scope: ReadFilters,
    agent: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    erased: Erased = NOT_ERASED,
    limit: int = 500,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The newest `limit` assertions the projection holds for one key born by
    `upto_seq`, retired or not, newest first."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "key": memory_key,
        "lim": limit,
        "c": upto_seq,
    }
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "memory_key = {key:String}",
        "born_seq <= {c:UInt64}",
    ]
    for col, val in (("agent", agent), ("user_id", user_id), ("session_id", session_id)):
        if val is not None and val != "*":
            where.append(f"{col} = {{{col}:String}}")
            params[col] = val
    sql = (
        f"SELECT * FROM {ASSERTIONS} FINAL WHERE "
        + " AND ".join(where)
        + _not_erased("born_at", erased, params)
        + scope_and(scope, params)
        + " ORDER BY born_seq DESC, assertion_id DESC LIMIT {lim:UInt32}"
    )
    return await ch.query_all(sql, params=params)


async def missing_vectors(
    model: str, *, limit: int = 200, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Searchable assertions born in the last week with no vector under `model` yet.
    An older one without a vector (after a model change) ranks lexically and the
    read says so (`vectors_pending`)."""
    ch = ch or _default_client()
    return await ch.query_all(
        f"""
        SELECT * FROM (
            SELECT org, namespace, agent, user_id, session_id, memory_key, memory_type, value,
                   toString(embed_input_sha256) AS embed_input_sha256, source_version
            FROM {ASSERTIONS} FINAL
            WHERE sensitivity != 'high' AND value != '{REDACTED_VALUE}'
              AND born_at > now64(6) - INTERVAL 7 DAY
              AND (org, namespace, agent, user_id, session_id, memory_key) NOT IN (
                  SELECT org, namespace, agent, user_id, session_id, memory_key
                  FROM {ASSERTIONS} FINAL
                  GROUP BY org, namespace, agent, user_id, session_id, memory_key
                  HAVING max((born_seq, sensitivity = 'high')).2 = 1)
              AND (org, namespace, embed_input_sha256) NOT IN (
                  SELECT org, namespace, embed_input_sha256 FROM {VECTORS}
                  WHERE embedding_model = {{model:String}})
            LIMIT 1 BY org, namespace, embed_input_sha256
        )
        -- A different draw each minute, and a share per namespace: rows that keep
        -- failing never hold the page.
        ORDER BY cityHash64(org, namespace, embed_input_sha256, toStartOfMinute(now()))
        LIMIT 50 BY org, namespace
        LIMIT {{lim:UInt32}}
        """,
        params={"model": model, "lim": limit},
    )


async def insert_vectors(rows: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None) -> None:
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(VECTORS, [dict(r) for r in rows])


async def current_vectors(
    org: str,
    namespace: str,
    model: str,
    keys_versions: Sequence[tuple[str, int]],
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """memory_embeddings rows still carrying the vector of (memory_key, version)."""
    if not keys_versions:
        return []
    ch = ch or _default_client()
    return await ch.query_all(
        "SELECT memory_key, version, embedding_dim, embedding FROM loom.memory_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND embedding_model = {model:String} "
        "AND embedding_dim > 0 AND (memory_key, version) IN {kv:Array(Tuple(String, UInt64))}",
        params={
            "org": org,
            "ns": namespace,
            "model": model,
            "kv": [tuple(x) for x in keys_versions],
        },
    )


async def rank_by_vector(
    org: str,
    namespace: str,
    model: str,
    query_vector: Sequence[float],
    embed_shas: Sequence[str],
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> dict[str, float]:
    """Exact cosine distance of each eligible embed text to the query; the
    eligible set is fixed before ranking, so no ANN limit can drop a match."""
    if not embed_shas:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT toString(embed_input_sha256) AS sha, "
        "cosineDistance(embedding, {q:Array(Float32)}) AS d "
        f"FROM {VECTORS} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND embedding_model = {model:String} "
        "AND embed_input_sha256 IN {shas:Array(String)} ORDER BY d LIMIT {lim:UInt32}",
        params={
            "org": org,
            "ns": namespace,
            "model": model,
            "q": list(query_vector),
            "shas": list(embed_shas),
            "lim": limit,
        },
    )
    return {str(r["sha"]): float(r["d"]) for r in rows}


async def memory_heads(
    org: str,
    namespace: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The loom.memory rows these identities currently serve, whole: what a new
    version row carries forward, and what a key written before the journal is
    seeded with."""
    if not keys:
        return []
    ch = ch or _default_client()
    return await ch.query_all(
        "SELECT * EXCEPT (value_json, content_sha256), "
        "toString(content_sha256) AS content_sha256 FROM loom.memory FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        f"AND {_IDENT} IN {_KEYS}",
        params={"org": org, "ns": namespace, "keys": [tuple(k) for k in keys]},
    )


async def foreign_publisher(
    org: str, namespace: str, publisher_id: str, since: datetime, *, ch: _CHClient | None = None
) -> bool:
    """Is another publisher writing the namespace now: a commit of its since `since`,
    within the last SETTLE_S and after this publisher's own last one? (A process
    that stopped while this one started ages out; a live sibling does not.)"""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT toUnixTimestamp64Micro(maxIf(recorded_at, publisher_id != {me:String})) AS theirs, "
        "toUnixTimestamp64Micro(maxIf(recorded_at, publisher_id = {me:String})) AS mine, "
        f"toUnixTimestamp64Micro(now64(6) - INTERVAL {SETTLE_S} SECOND) AS recent "
        f"FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND recorded_at >= {since:DateTime64(6, 'UTC')}",
        params={"org": org, "ns": namespace, "since": since, "me": publisher_id},
    )
    theirs, mine, recent = (int((row or {}).get(k) or 0) for k in ("theirs", "mine", "recent"))
    return theirs > max(mine, recent)


async def version_row(
    org: str,
    namespace: str,
    ident: tuple[str, str, str, str],
    version: int,
    *,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """The loom.memory row a commit wrote, as the history mirror keeps it: what a
    retry of a commit that carries no assertion (a restatement) answers and
    rewrites from."""
    ch = ch or _default_client()
    agent, uid, sid, key = ident
    return await ch.query_one(
        "SELECT * EXCEPT (value_json, content_sha256), toString(content_sha256) AS content_sha256 "
        "FROM loom.memory_versions FINAL "
        f"WHERE {_OF_IDENT} AND version = {{v:UInt64}} LIMIT 1",
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "uid": uid,
            "sid": sid,
            "key": key,
            "v": version,
        },
    )


async def restated_claims(
    org: str,
    namespace: str,
    heads: Sequence[Mapping[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str, str, str], str]:
    """Per identity, the assertion its current row shows when a restatement wrote
    that row (the commit at the row's version names it)."""
    pairs = [
        (
            str(h["agent"]),
            str(h["user_id"]),
            str(h["session_id"]),
            str(h["memory_key"]),
            int(h["version"]),
        )
        for h in heads
        if h.get("version")
    ]
    if not pairs:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT agent, user_id, session_id, memory_key, toString(restated_assertion_id) AS aid "
        f"FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND memory_key IN {mk:Array(String)} AND restated_assertion_id IS NOT NULL "
        "AND (agent, user_id, session_id, memory_key, row_version) IN "
        "{pairs:Array(Tuple(String, String, String, String, UInt64))}",
        params={"org": org, "ns": namespace, "mk": sorted({p[3] for p in pairs}), "pairs": pairs},
    )
    return {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"])): str(
            r["aid"]
        )
        for r in rows
    }


async def row_versions(
    org: str,
    namespace: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str, str, str], int]:
    """Each identity's highest loom.memory version, landed or not yet merged."""
    if not keys:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT agent, user_id, session_id, memory_key, max(version) AS v FROM loom.memory "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND memory_key IN {mk:Array(String)} "
        f"AND {_IDENT} IN {_KEYS} "
        "GROUP BY agent, user_id, session_id, memory_key",
        params={
            "org": org,
            "ns": namespace,
            "mk": sorted({k[3] for k in keys}),
            "keys": [tuple(k) for k in keys],
        },
    )
    return {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"])): int(
            r["v"]
        )
        for r in rows
    }


async def newest_row_versions(
    org: str,
    namespace: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str, str, str], int]:
    """Each identity's highest loom.memory version a commit recorded."""
    if not keys:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT agent, user_id, session_id, memory_key, max(row_version) AS v FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND memory_key IN {mk:Array(String)} "
        f"AND {_IDENT} IN {_KEYS} "
        "GROUP BY agent, user_id, session_id, memory_key",
        params={
            "org": org,
            "ns": namespace,
            "mk": sorted({k[3] for k in keys}),
            "keys": [tuple(k) for k in keys],
        },
    )
    return {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"])): int(
            r["v"]
        )
        for r in rows
    }


async def last_commits(
    org: str,
    namespace: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    ch: _CHClient | None = None,
) -> dict[tuple[str, str, str, str], str]:
    """Each identity's latest commit id: the parent of its next commit."""
    if not keys:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT agent, user_id, session_id, memory_key, argMax(commit_id, commit_seq) AS id "
        f"FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND memory_key IN {mk:Array(String)} "
        f"AND {_IDENT} IN {_KEYS} "
        "GROUP BY agent, user_id, session_id, memory_key",
        params={
            "org": org,
            "ns": namespace,
            "mk": sorted({k[3] for k in keys}),
            "keys": [tuple(k) for k in keys],
        },
    )
    return {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"])): str(
            r["id"]
        )
        for r in rows
    }


async def journal_keys(org: str, namespace: str, *, ch: _CHClient | None = None) -> list[str]:
    """Every memory key the namespace's journal has a commit for, in order."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT memory_key FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} ORDER BY memory_key",
        params={"org": org, "ns": namespace},
    )
    return [str(r["memory_key"]) for r in rows]


async def journal_only_identities(
    org: str,
    namespace: str,
    *,
    user_id: str | None = None,
    agent: str | None = None,
    session_id: str | None = None,
    memory_key: str | None = None,
    scope: ReadFilters | None = None,
    ch: _CHClient | None = None,
) -> list[tuple[tuple[str, str, str, str], str]]:
    """The (identity, memory_id) of every key of the scope the journal has a commit for
    and loom.memory serves no live row of (a plan an amendment made, not yet due):
    what `memory.clear` and `memory.delete` retract in the journal itself. The most
    recently written first, as a namespace-wide row lookup picks."""
    ch = ch or _default_client()
    where = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    if user_id is not None:
        where.append("user_id = {uid:String}")
        params["uid"] = user_id
    if agent is not None:
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if session_id is not None:
        where.append("session_id = {sid:String}")
        params["sid"] = session_id
    if memory_key is not None:
        where.append("memory_key = {mk:String}")
        params["mk"] = memory_key
    if scope is not None:  # the request's read scope (own_user, own_session)
        where += scope_where(scope, params)
    cond = " AND ".join(where)
    rows = await ch.query_all(
        "SELECT agent, user_id, session_id, memory_key, "
        "toString(argMax(memory_id, commit_seq)) AS memory_id "
        f"FROM {COMMITS} WHERE {cond} "
        f"AND {_IDENT} NOT IN (SELECT {_IDENT} FROM loom.memory FINAL "
        f"WHERE {cond} AND tombstoned_at IS NULL) "
        "GROUP BY agent, user_id, session_id, memory_key "
        "ORDER BY max(commit_seq) DESC",
        params=params,
    )
    return [
        (
            (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"])),
            str(r["memory_id"]),
        )
        for r in rows
    ]


async def seeded_after(
    org: str, namespace: str, keys: Sequence[str], at: datetime, *, ch: _CHClient | None = None
) -> set[tuple[str, str, str, str]]:
    """Identities among `keys` whose journal began after `at` with a seed: a key
    written before the journal existed, whose state at `at` the journal never had."""
    if not keys:
        return set()
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT DISTINCT agent, user_id, session_id, memory_key "
        f"FROM {COMMITS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND memory_key IN {mk:Array(String)} AND operation = 'memory.journal_seed' "
        "AND recorded_at > {at:DateTime64(6, 'UTC')}",
        params={"org": org, "ns": namespace, "mk": list(keys), "at": at},
    )
    return {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))
        for r in rows
    }


async def last_commit_at(
    org: str, namespace: str, ident: tuple[str, str, str, str], *, ch: _CHClient | None = None
) -> datetime | None:
    """When the identity's latest commit was recorded (None: it has none)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT max(recorded_at) AS at, count() AS n FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND memory_key = {mk:String} "
        f"AND {_IDENT} IN {_KEYS}",
        params={"org": org, "ns": namespace, "mk": ident[3], "keys": [tuple(ident)]},
    )
    if not rows or not int(rows[0]["n"] or 0):
        return None
    at: datetime = rows[0]["at"]
    return at if at.tzinfo else at.replace(tzinfo=UTC)


async def identity_of_memory_id(
    org: str, memory_id: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """Where an org-wide memory id lives in the journal, deleted or not, projected
    or not."""
    ch = ch or _default_client()
    return await ch.query_one(
        f"SELECT namespace, agent, user_id, session_id, memory_key FROM {COMMITS} "
        "WHERE org = {org:String} AND memory_id = toUUIDOrZero({mid:String}) "
        f"AND {_ON_TIME} ORDER BY recorded_at DESC LIMIT 1",
        params={"org": org, "mid": memory_id},
    )


async def assertions_by_id(
    org: str, namespace: str, ids: Sequence[str], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    if not ids:
        return []
    ch = ch or _default_client()
    return await ch.query_all(
        f"SELECT * FROM {ASSERTIONS} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND assertion_id IN {ids:Array(UUID)}",
        params={"org": org, "ns": namespace, "ids": list(ids)},
    )


async def shared_versions_at(
    org: str,
    namespace: str,
    known_ns: int,
    *,
    erased: Erased = NOT_ERASED,
    limit: int = 10_000,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Each shared key's newest version written at or before K, demotions included
    (the caller drops tombstoned ones). A version is its write time in ns: one
    written before an erasure of the namespace is never served."""
    ch = ch or _default_client()
    return await ch.query_all(
        "SELECT memory_key, value, memory_type, confidence, promoted_from, promotion_event, "
        "promoted_by, promoted_at, tombstoned_at, version FROM loom.shared_versions "
        "WHERE org = {org:String} AND namespace = {ns:String} AND version <= {k:UInt64} "
        "AND version >= {er:UInt64} "
        "ORDER BY version DESC LIMIT 1 BY memory_key LIMIT {lim:UInt32}",
        params={
            "org": org,
            "ns": namespace,
            "k": known_ns,
            "er": erased.namespace_us * 1000,
            "lim": limit,
        },
    )


_OF_IDENT = (
    "org = {org:String} AND namespace = {ns:String} AND agent = {agent:String} "
    "AND user_id = {uid:String} AND session_id = {sid:String} AND memory_key = {key:String}"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _key_commits(
    org: str, namespace: str, ident: tuple[str, str, str, str], ch: _CHClient
) -> list[dict[str, Any]]:
    """The identity's whole journal: the authority, whatever the projection holds."""
    agent, uid, sid, key = ident
    return await ch.query_all(
        f"SELECT commit_id, retired_assertion_ids, assertions FROM {COMMITS} WHERE {_OF_IDENT}",
        params={"org": org, "ns": namespace, "agent": agent, "uid": uid, "sid": sid, "key": key},
    )


def _added(commits: Sequence[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    for c in commits:
        yield from json.loads(c["assertions"])


async def assertion_value_sha(
    org: str,
    namespace: str,
    ident: tuple[str, str, str, str],
    assertion_id: str,
    *,
    ch: _CHClient | None = None,
) -> str:
    """The value hash of one of the identity's assertions ('' when unknown). Once the
    journal's copy is scrubbed, the hash the projection kept: a redaction that
    failed partway is retried by the same address."""
    ch = ch or _default_client()
    for a in _added(await _key_commits(org, namespace, ident, ch)):
        if str(a.get("assertion_id")) != assertion_id:
            continue
        if a.get("value") != REDACTED_VALUE:
            return _sha(str(a.get("value") or ""))
        row = await ch.query_one(
            f"SELECT toString(value_sha256) AS sha FROM {ASSERTIONS} FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND assertion_id = {aid:UUID}",
            params={"org": org, "ns": namespace, "aid": assertion_id},
        )
        return str(row["sha"]) if row else ""
    return ""


async def value_served(
    org: str,
    namespace: str,
    ident: tuple[str, str, str, str],
    value_sha256: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Does an assertion the journal has not retired still hold this value?"""
    ch = ch or _default_client()
    commits = await _key_commits(org, namespace, ident, ch)
    retired = {str(x) for c in commits for x in c["retired_assertion_ids"]}
    return any(
        str(a["assertion_id"]) not in retired and _sha(str(a.get("value") or "")) == value_sha256
        for a in _added(commits)
    )


async def served_contents(
    org: str, namespace: str, ident: tuple[str, str, str, str], *, ch: _CHClient | None = None
) -> set[str]:
    """The content hashes (as loom.memory keeps them) the identity's unretired
    assertions hold, read from the journal itself."""
    from server.memory.domain.temporal_model import Assertion

    ch = ch or _default_client()
    commits = await _key_commits(org, namespace, ident, ch)
    retired = {str(x) for c in commits for x in c["retired_assertion_ids"]}
    return {
        Assertion.from_json(a).content_sha256
        for a in _added(commits)
        if str(a["assertion_id"]) not in retired
    }


async def journal_values(
    org: str,
    namespace: str,
    idents: Sequence[tuple[str, str, str, str]],
    assertion_ids: Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, str]:
    """Each assertion's value as the journal's payloads hold it now: a redaction scrubs
    them in place, projected or not."""
    if not assertion_ids:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT JSONExtractString(a, 'assertion_id') AS id, JSONExtractString(a, 'value') AS v "
        f"FROM {COMMITS} ARRAY JOIN JSONExtractArrayRaw(assertions) AS a "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        f"AND memory_key IN {{mk:Array(String)}} AND {_IDENT} IN {_KEYS} "
        "AND JSONExtractString(a, 'assertion_id') IN {ids:Array(String)}",
        params={
            "org": org,
            "ns": namespace,
            "mk": sorted({k[3] for k in idents}),
            "keys": [tuple(k) for k in idents],
            "ids": sorted(set(assertion_ids)),
        },
    )
    return {str(r["id"]): str(r["v"]) for r in rows}


async def redact_value(
    org: str,
    namespace: str,
    ident: tuple[str, str, str, str],
    value_sha256: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Scrub every assertion of the identity that held this value, found in the
    journal's payloads (a set's, an amend's, a correction's segment; projected
    yet or not): in the payloads, in the projection, and the vectors no
    unredacted assertion still uses. Hashes stay: the audit is hash-only. The
    caller has checked none is still served. Returns the assertions scrubbed."""
    ch = ch or _default_client()
    hits: dict[str, str] = {}
    rewrites = []
    for c in await _key_commits(org, namespace, ident, ch):
        payload = json.loads(c["assertions"])
        hit = [a for a in payload if _sha(str(a.get("value") or "")) == value_sha256]
        for a in hit:
            hits[str(a["assertion_id"])] = str(a.get("embed_input_sha256") or "")
            a["value"], a["entities"] = REDACTED_VALUE, []
        if hit:
            rewrites.append((str(c["commit_id"]), json.dumps(payload)))
    # A retry after the payloads were scrubbed finds its assertions by the hash the
    # projection kept, and runs the (idempotent) scrubs below again.
    agent, uid, sid, key = ident
    for r in await ch.query_all(
        f"SELECT toString(assertion_id) AS id, toString(embed_input_sha256) AS e FROM {ASSERTIONS} "
        f"FINAL WHERE {_OF_IDENT} AND toString(value_sha256) = {{sha:String}}",
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "uid": uid,
            "sid": sid,
            "key": key,
            "sha": value_sha256,
        },
    ):
        hits.setdefault(str(r["id"]), str(r["e"]))
    if not hits:
        return 0
    params: dict[str, Any] = {"org": org, "ns": namespace}
    for cid, payload_json in rewrites:
        await ch.execute(
            "ALTER TABLE loom.memory_temporal_commits UPDATE assertions = {p:String} "
            "WHERE org = {org:String} AND namespace = {ns:String} AND commit_id = {cid:UUID} "
            "SETTINGS mutations_sync = 2",
            params={**params, "p": payload_json, "cid": cid},
        )
    await ch.execute(
        f"ALTER TABLE loom.memory_temporal_assertions UPDATE value = '{REDACTED_VALUE}', "
        "entities = [] WHERE org = {org:String} AND namespace = {ns:String} "
        "AND assertion_id IN {ids:Array(UUID)} SETTINGS mutations_sync = 2",
        params={**params, "ids": sorted(hits)},
    )
    # Another identity may embed the same text legitimately; its vector stays.
    await ch.execute(
        f"DELETE FROM {VECTORS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND embed_input_sha256 IN {shas:Array(String)} AND embed_input_sha256 NOT IN ("
        f"SELECT embed_input_sha256 FROM {ASSERTIONS} WHERE org = {{org:String}} "
        f"AND namespace = {{ns:String}} AND embed_input_sha256 IN {{shas:Array(String)}} "
        f"AND value != '{REDACTED_VALUE}')",
        params={**params, "shas": sorted(set(hits.values()))},
        settings={"lightweight_deletes_sync": 2},
    )
    return len(hits)


# What a current row must carry to agree with an assertion: every field the
# current projection serves, as `temporal_projection._state_of` lists them. This
# compares the raw value where that compares its canonical hash: a candidate
# filter may over-select (a re-formatted value), never miss.
_STATE = (
    "(value, toString(memory_type), toString(status), tags, entities, pinned, "
    "toString(lock_mode), toString(sensitivity), verified_by, verified_sha256, "
    "ifNull(toUnixTimestamp(verified_at), 0), "
    "ifNull(toUnixTimestamp({valid_from}), 0), ifNull(toUnixTimestamp(valid_to), 0), "
    "ifNull(toUnixTimestamp(expires_at), 0), round(confidence, 3), "
    "ifNull(toUnixTimestamp(lock_until), 0))"
)
# An inferred start is never projected (loom.memory has no basis column): compared as NULL.
_STATE_A = _STATE.format(valid_from="if(validity_basis = 'inferred', NULL, valid_from)")
_STATE_M = _STATE.format(valid_from="coalesce(valid_at, temporal_anchor)")
_IDENT_COLS = "agent, user_id, session_id, memory_key"


async def divergent_rows(
    org: str,
    namespace: str,
    keys: Sequence[tuple[str, str, str, str]],
    *,
    erased: Erased = NOT_ERASED,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Of these identities, those whose loom.memory row is live where the journal
    serves nothing, or does not carry the state of an assertion that applies now.
    A key with nothing applying now, or with snapshots or events, is always
    returned: only the reducer picks a closed fact, the latest snapshot or event.
    A superset of the real divergences; the caller decides exactly."""
    if not keys:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "keys": [tuple(k) for k in keys]}
    erased_sql = _not_erased("born_at", erased, params)
    return await ch.query_all(
        f"""
        WITH {_KEYS} AS ks
        SELECT {_IDENT_COLS} FROM (SELECT arrayJoin(ks) AS k, k.1 AS agent, k.2 AS user_id,
                                         k.3 AS session_id, k.4 AS memory_key) AS r
        LEFT JOIN (
            SELECT {_IDENT_COLS}, count() AS served,
                   groupArrayIf({_STATE_A}, status IN ('active', 'conflicted')
                       AND (valid_from IS NULL OR valid_from <= now64(6))
                       AND (valid_to IS NULL OR valid_to > now64(6)
                            OR temporal_kind IN ('event', 'snapshot'))) AS now_vals,
                   countIf(temporal_kind IN ('snapshot', 'event')) > 0 AS snapshots
            FROM {ASSERTIONS} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND retired_seq IS NULL
              AND {_IDENT} IN ks {erased_sql}
            GROUP BY {_IDENT_COLS}
        ) AS s USING ({_IDENT_COLS})
        LEFT JOIN (
            SELECT {_IDENT_COLS}, {_STATE_M} AS state,
                   tombstoned_at IS NULL AND redacted_at IS NULL
                       AND status IN ('active', 'conflicted', 'duplicate', 'proposed') AS live
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND {_IDENT} IN ks
        ) AS m USING ({_IDENT_COLS})
        WHERE s.snapshots
           OR if(s.served = 0, m.live,
                 empty(s.now_vals) OR NOT m.live OR NOT has(s.now_vals, m.state))
        """,
        params=params,
    )


async def unchecked_namespaces(
    cursor: str,
    *,
    limit: int = 50,
    only: tuple[str, str] | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Namespaces whose published head is past the `cursor` watermark (what a
    projection has applied, or the reconciler has checked), in random order: one
    that cannot advance yet (held by an erasure) never starves the others."""
    ch = ch or _default_client()
    scoped = "AND org = {o:String} AND namespace = {n:String}" if only else ""
    return await ch.query_all(
        f"""
        SELECT org, namespace, maxIf(applied_seq, projection = 'published') AS head,
               maxIf(applied_seq, projection = {{cur:String}}) AS applied,
               maxIf(updated_at, projection = 'published') AS published_at
        FROM {WATERMARKS} WHERE namespace != '' {scoped}
        GROUP BY org, namespace HAVING head > applied
        ORDER BY rand() LIMIT {{lim:UInt32}}
        """,
        params={
            "cur": cursor,
            "lim": limit,
            "o": only[0] if only else "",
            "n": only[1] if only else "",
        },
    )


async def commits_after(
    org: str, namespace: str, after_seq: int, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The next commits past a cursor, in order: sequence, recording time, identity."""
    ch = ch or _default_client()
    return await ch.query_all(
        f"SELECT commit_seq, recorded_at, {_IDENT_COLS} FROM {COMMITS} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND commit_seq > {lo:UInt64} "
        f"AND {_ON_TIME} "
        "ORDER BY commit_seq LIMIT {lim:UInt32}",
        params={"org": org, "ns": namespace, "lo": after_seq, "lim": limit},
    )


async def boundaries_after(
    after: datetime,
    upto: datetime,
    *,
    limit: int,
    org: str | None = None,
    namespace: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Served assertions whose valid-time start or end falls in (after, upto], the
    earliest first: a current row may have to change when one arrives. A page
    ends on a whole instant (WITH TIES), so a cursor at its last boundary misses
    none."""
    ch = ch or _default_client()
    a, u = "{a:DateTime64(6, 'UTC')}", "{u:DateTime64(6, 'UTC')}"
    params: dict[str, Any] = {"a": after, "u": upto, "lim": limit}
    scoped = ""
    if org is not None:
        scoped = "AND org = {org:String} AND namespace = {ns:String}"
        params |= {"org": org, "ns": namespace}
    return await ch.query_all(
        f"""
        SELECT org, namespace, {_IDENT_COLS},
               min(if(valid_from > {a} AND valid_from <= {u},
                      valid_from, valid_to)) AS boundary
        FROM {ASSERTIONS} FINAL
        WHERE retired_seq IS NULL {scoped}
          AND ((valid_from > {a} AND valid_from <= {u})
            OR (valid_to > {a} AND valid_to <= {u}))
        GROUP BY org, namespace, {_IDENT_COLS}
        ORDER BY boundary LIMIT {{lim:UInt32}} WITH TIES
        """,
        params=params,
    )


# A namespace the reconciler could not check holds its own place in the boundary
# stream (`boundary_pending`: its earliest unchecked boundary, epoch µs), so the
# global cursor moves on for everyone else.
PENDING = "boundary_pending"


async def pending_boundaries(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    ch = ch or _default_client()
    return await ch.query_all(
        f"SELECT org, namespace, min(applied_seq) AS since FROM {WATERMARKS} "
        "WHERE projection = {p:String} GROUP BY org, namespace",
        params={"p": PENDING},
    )


async def hold_boundary(
    org: str, namespace: str, since_us: int, *, ch: _CHClient | None = None
) -> None:
    """Keep the namespace's earliest unchecked boundary in its one pending row. One
    row only: the table keeps the highest value per key on a merge, so a second,
    later row would replace the earlier one."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT min(applied_seq) AS s, count() AS n FROM {WATERMARKS} "
        "WHERE org = {org:String} AND namespace = {ns:String} AND projection = {p:String}",
        params={"org": org, "ns": namespace, "p": PENDING},
    )
    if row and int(row.get("n") or 0) and int(row["s"]) <= since_us:
        return
    await ch.execute(
        f"DELETE FROM {WATERMARKS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND projection = {p:String} AND applied_seq > {s:UInt64}",
        params={"org": org, "ns": namespace, "p": PENDING, "s": since_us},
        settings={"lightweight_deletes_sync": 2},
    )
    await advance_watermark(org, namespace, since_us, PENDING, ch=ch)


async def release_boundary(org: str, namespace: str, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {WATERMARKS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND projection = {p:String}",
        params={"org": org, "ns": namespace, "p": PENDING},
        settings={"lightweight_deletes_sync": 2},
    )


async def delete_assertions(
    org: str, namespace: str, assertion_ids: Sequence[str], *, ch: _CHClient | None = None
) -> None:
    """Delete projection rows whose commit an erasure removed under a replay."""
    if not assertion_ids:
        return
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {ASSERTIONS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND assertion_id IN {ids:Array(UUID)}",
        params={"org": org, "ns": namespace, "ids": list(assertion_ids)},
        settings={"lightweight_deletes_sync": 2},
    )


async def drop_unbacked_vectors(
    org: str, namespace: str, shas: Sequence[str], *, ch: _CHClient | None = None
) -> None:
    """Delete these vectors where no assertion uses them any more (an erasure
    removed the assertion while the vector was being written)."""
    if not shas:
        return
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {VECTORS} WHERE org = {{org:String}} AND namespace = {{ns:String}} "
        "AND embed_input_sha256 IN {shas:Array(String)} AND embed_input_sha256 NOT IN ("
        f"SELECT embed_input_sha256 FROM {ASSERTIONS} WHERE org = {{org:String}} "
        "AND namespace = {ns:String} AND embed_input_sha256 IN {shas:Array(String)} "
        f"AND value != '{REDACTED_VALUE}')",
        params={"org": org, "ns": namespace, "shas": list(shas)},
        settings={"lightweight_deletes_sync": 2},
    )
