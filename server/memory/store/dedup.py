"""Duplicate and supersession reads over loom.memory — clusters, pairs,
orphans, canonicals and successors — and the in-place duplicate-tag clear.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.core.repo_base import _CHClient, _default_client, query_all_sliced
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, scope_and
from server.memory.domain.search_types import RowKey
from server.memory.store.embeddings import mirror_status_to_embeddings
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.reads import get_by_id
from server.memory.store.rows import build_memory_version_row, next_version


def _s(row: Mapping[str, Any], name: str) -> str:
    return str(row.get(name) or "")


@dataclass(frozen=True, slots=True)
class OrphanDuplicate:
    """A live `duplicate` head whose canonical is gone."""

    memory_id: str
    memory_key: str
    agent: str
    user_id: str
    session_id: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> OrphanDuplicate:
        return cls(
            memory_id=_s(r, "memory_id"),
            memory_key=_s(r, "memory_key"),
            agent=_s(r, "agent"),
            user_id=_s(r, "user_id"),
            session_id=_s(r, "session_id"),
        )


@dataclass(frozen=True, slots=True)
class KeyedRow:
    """The live row of a key in a namespace, whichever identity wrote it."""

    memory_id: str
    memory_key: str
    agent: str
    user_id: str
    session_id: str
    status: str
    duplicate_of: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> KeyedRow:
        return cls(
            memory_id=_s(r, "memory_id"),
            memory_key=_s(r, "memory_key"),
            agent=_s(r, "agent"),
            user_id=_s(r, "user_id"),
            session_id=_s(r, "session_id"),
            status=_s(r, "status"),
            duplicate_of=_s(r, "duplicate_of"),
        )


@dataclass(frozen=True, slots=True)
class DuplicateCluster:
    """One canonical id and the duplicates naming it: each duplicate is
    (memory_id, memory_key, agent, created_at)."""

    canonical_id: str
    dup_count: int
    dups: tuple[tuple[Any, ...], ...]

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> DuplicateCluster:
        return cls(
            canonical_id=_s(r, "canonical_id"),
            dup_count=int(r.get("dup_count") or 0),
            dups=tuple(tuple(d) for d in (r.get("dups") or [])),
        )


@dataclass(frozen=True, slots=True)
class CanonicalRef:
    """The display fields of a live canonical row."""

    memory_key: str
    agent: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> CanonicalRef:
        return cls(
            memory_key=_s(r, "memory_key"),
            agent=_s(r, "agent"),
        )


class _RowKeyed:
    """A row read with its identity columns."""

    __slots__ = ()
    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str

    @property
    def row_key(self) -> RowKey:
        return RowKey(self.namespace, self.agent, self.user_id, self.session_id, self.memory_key)


@dataclass(frozen=True, slots=True)
class DuplicateStatus(_RowKeyed):
    """A pooled row's live status, and the canonical it names when it is a duplicate."""

    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    status: str
    canonical_id: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> DuplicateStatus:
        return cls(
            namespace=_s(r, "namespace"),
            agent=_s(r, "agent"),
            user_id=_s(r, "user_id"),
            session_id=_s(r, "session_id"),
            memory_key=_s(r, "memory_key"),
            status=_s(r, "row_status"),
            canonical_id=_s(r, "canonical_id"),
        )


@dataclass(frozen=True, slots=True)
class CanonicalRow(_RowKeyed):
    """The live active row behind a `duplicate_of` id."""

    namespace: str
    canonical_id: str
    memory_key: str
    memory_type: str
    value: Any
    tags: tuple[str, ...]
    version: int
    session_id: str
    user_id: str
    source_class: str
    created_at: str
    valid_at: str
    agent: str = ""
    sensitivity: str = ""

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> CanonicalRow:
        return cls(
            namespace=_s(r, "namespace"),
            canonical_id=_s(r, "canonical_id"),
            memory_key=_s(r, "memory_key"),
            memory_type=_s(r, "memory_type"),
            value=r.get("value"),
            tags=tuple(str(t) for t in (r.get("tags") or []) if t),
            version=int(r.get("version") or 0),
            session_id=_s(r, "session_id"),
            user_id=_s(r, "user_id"),
            source_class=_s(r, "source_class"),
            created_at=_s(r, "created_at"),
            valid_at=_s(r, "valid_at"),
            agent=_s(r, "agent"),
            sensitivity=_s(r, "sensitivity"),
        )


@dataclass(frozen=True, slots=True)
class SupersededSuccessor(_RowKeyed):
    """A superseded key's successor id, the successor's key when it is still
    live ('' otherwise), and when the row was retired."""

    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    superseded_by: str
    successor_key: str
    superseded_at: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> SupersededSuccessor:
        return cls(
            namespace=_s(r, "namespace"),
            agent=_s(r, "agent"),
            user_id=_s(r, "user_id"),
            session_id=_s(r, "session_id"),
            memory_key=_s(r, "memory_key"),
            superseded_by=_s(r, "superseded_by"),
            successor_key=_s(r, "successor_key"),
            superseded_at=_s(r, "superseded_at"),
        )


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    """A live duplicate row, the identity it was written under, and the
    canonical id it names."""

    memory_id: str
    memory_key: str
    agent: str
    user_id: str
    session_id: str
    duplicate_of: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> DuplicatePair:
        return cls(
            memory_id=_s(r, "memory_id"),
            memory_key=_s(r, "memory_key"),
            agent=_s(r, "agent"),
            user_id=_s(r, "user_id"),
            session_id=_s(r, "session_id"),
            duplicate_of=_s(r, "duplicate_of"),
        )


async def find_orphan_duplicates(
    org: str,
    namespace: str,
    *,
    batch_size: int = 500,
    ch: _CHClient | None = None,
) -> list[OrphanDuplicate]:
    """Active-head rows stuck at status='duplicate' whose canonical is gone.

    A row lands status='duplicate' + duplicate_of=<peer> from
    conflicts.check_on_commit when it's a same-fact/different-words match of a
    LIVE peer (see memory/app/conflicts.py). It's kept in recall but treated as
    non-canonical, deferring to that peer. When the peer later disappears —
    duplicate_of never got set, or the pointed-at memory is now tombstoned /
    superseded / otherwise not a live served head — the row is an ORPHAN
    duplicate: there is no canonical left to defer to, yet nothing flips it
    back. This scan finds those so the caller can re-activate them.

    "Orphan" predicate (evaluated on the argMax latest-version HEAD per identity
    key): live (tombstoned_at IS NULL) AND status='duplicate' AND
    (duplicate_of='' OR duplicate_of is NOT the memory_id of a live active or
    conflicted head in the same org+namespace). A duplicate whose duplicate_of
    still points at one is a REAL duplicate and is left alone: reads serve a
    conflicted canonical too.

    Aggregations are kept FLAT and alias-safe:
    the freshness input is exposed as `max_version`, NOT aliased `version` —
    aliasing it `version` would make CH read the argMax(x, version) calls as
    argMax(x, max(version)), the aggregate-inside-aggregate (ILLEGAL_AGGREGATION)
    trap. The HAVING clause references the aggregate ALIASES
    (post-aggregation), so `status`/`duplicate_of`/`live` there are the argMax
    results, not bare columns. The live-active id set is a self-contained
    subquery over the same table (its own argMax head-collapse, same alias
    discipline — no `max(version) AS version` shadow). No FINAL: argMax gives the
    latest state and re-activation is idempotent (a re-activated row is no longer
    status='duplicate', so it's never re-found).
    """
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT org, namespace, agent, user_id, session_id, memory_key,
               argMax(status, version)                AS status,
               argMax(duplicate_of, version)          AS duplicate_of,
               argMax(tombstoned_at IS NULL, version) AS live,
               argMax(created_at, version)            AS created_at,
               toString(argMax(memory_id, version))   AS memory_id,
               max(version)                           AS max_version
        FROM loom.memory
        WHERE org = {org:String} AND namespace = {ns:String}
        GROUP BY org, namespace, agent, user_id, session_id, memory_key
        HAVING live = 1
           AND status = 'duplicate'
           AND (
               duplicate_of = ''
               OR duplicate_of NOT IN (
                   SELECT toString(argMax(memory_id, version))
                   FROM loom.memory
                   WHERE org = {org:String} AND namespace = {ns:String}
                   GROUP BY org, namespace, agent, user_id, session_id, memory_key
                   HAVING argMax(tombstoned_at IS NULL, version) = 1
                      AND argMax(status, version) IN ('active', 'conflicted')
               )
           )
        ORDER BY org, namespace, agent, user_id, memory_key
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(batch_size)},
    )
    return [OrphanDuplicate.from_row(r) for r in rows]


async def find_row_by_key_namespace(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> KeyedRow | None:
    """Find the active row for `memory_key` under (org, ns) without
    requiring the full identity tuple.

    Used by `memory.dedup_resolve` — the operator passes
    `canonical_key` + `duplicate_key`; the agent / user_id / session_id
    of those rows is whatever wrote them. Returns the row dict with
    memory_id, agent, user_id, session_id, status, duplicate_of.
    """
    ch = ch or _default_client()
    r = await ch.query_one(
        """
        SELECT memory_id, memory_key, agent, user_id, session_id, status, duplicate_of, created_at
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key = {key:String}
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "key": memory_key,
        },
    )
    return None if r is None else KeyedRow.from_row(r)


async def find_duplicate_clusters(
    org: str,
    namespace: str,
    *,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[DuplicateCluster]:
    """List clusters of status='duplicate' rows grouped by canonical id.

    Each cluster is one canonical row plus the duplicates pointing at it.
    Ordered by duplicate count desc; bounded by `limit` clusters and 50
    duplicates per cluster in the groupArray.
    """
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT
            duplicate_of AS canonical_id,
            count() AS dup_count,
            groupArray(50)((memory_id, memory_key, agent, created_at)) AS dups
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND status = 'duplicate'
          AND duplicate_of != ''
          AND tombstoned_at IS NULL
        GROUP BY duplicate_of
        ORDER BY dup_count DESC
        LIMIT {lim:UInt32}
        """,
        params={
            "org": org,
            "ns": namespace,
            "lim": int(limit),
        },
    )
    return [DuplicateCluster.from_row(r) for r in rows]


async def get_canonical_row(
    org: str,
    memory_id: str,
    *,
    ch: _CHClient | None = None,
) -> CanonicalRef | None:
    """Point-fetch a canonical row by memory_id (UUID).

    Used by `memory.dedup_list` to enrich clusters with the canonical
    row's memory_key + agent for display. Returns None if the row was
    tombstoned or doesn't exist.
    """
    ch = ch or _default_client()
    r = await ch.query_one(
        """
        SELECT memory_id, memory_key, agent, value, created_at
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND memory_id = {mid:UUID}
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "mid": memory_id},
    )
    return None if r is None else CanonicalRef.from_row(r)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _uuid_strings(ids: list[str]) -> list[str]:
    return sorted({str(i) for i in ids if i and _UUID_RE.match(str(i))})


async def status_by_rows(
    org: str,
    rows: list[RowKey],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[DuplicateStatus]:
    """The live status, and the canonical a duplicate names, of each stored row
    (namespace, agent, user_id, session_id, memory_key) a search pooled.

    `loom.memory` is the truth for a row's status: older duplicates carry
    `active` on their `memory_embeddings` mirror, so the leg cannot tell
    from its own row. The pooled rows only, a slice of them per statement.

    Never with sequential consistency, even inside a read-your-writes scope
    (one opens for a namespace this process wrote moments ago): under it the
    statement fetches fresh parts from object storage on every call (p50 72 /
    p90 429 ms against 13 ms without). A row whose duplicate verdict landed a
    moment ago is served once more at worst; its own mirror row already
    carries the status.

    Only rows inside `scope` are read, so another user's row never names a
    canonical for this read.
    """
    wanted = sorted({r for r in rows if r.memory_key})
    if not wanted:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "rows": wanted}
    in_scope = scope_and(scope, params)
    found = await query_all_sliced(
        ch,
        f"""
        SELECT namespace, agent, user_id, session_id, memory_key,
               toString(status) AS row_status,
               duplicate_of AS canonical_id
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND (namespace, agent, user_id, session_id, memory_key)
              IN {{rows:Array(Tuple(String, String, String, String, String))}}
          AND tombstoned_at IS NULL{in_scope}
        SETTINGS select_sequential_consistency = 0
        """,
        params,
        "rows",
    )
    return [DuplicateStatus.from_row(r) for r in found]


async def canonical_rows_by_ids(
    org: str,
    namespaces: list[str],
    memory_ids: list[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[CanonicalRow]:
    """The live ACTIVE rows behind a set of `duplicate_of` ids, one per id:
    the canonical that answers for a hidden duplicate.
    Returns nothing for an id whose row is gone or no longer active, so the
    caller drops the duplicate without a substitute. Malformed ids are
    skipped rather than bound. A canonical outside `scope` is not returned:
    to this read it is gone."""
    ids = _uuid_strings(memory_ids)
    nss = sorted({str(n) for n in namespaces if n})
    if not ids or not nss:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "nss": nss, "ids": ids}
    in_scope = scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT namespace, toString(memory_id) AS canonical_id, memory_key, memory_type,
               value, tags, version, agent, session_id, user_id, source_class, sensitivity,
               toString(created_at) AS created_at,
               toString(coalesce(valid_at, toDateTime(0))) AS valid_at
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace IN {{nss:Array(String)}}
          AND memory_id IN {{ids:Array(UUID)}}
          AND tombstoned_at IS NULL
          AND status = 'active'{in_scope}
        ORDER BY namespace, memory_id, version DESC
        LIMIT 1 BY namespace, memory_id
        SETTINGS select_sequential_consistency = 0
        """,
        params=params,
    )
    return [CanonicalRow.from_row(r) for r in rows]


async def live_ids_in_scope(
    org: str,
    namespaces: list[str],
    memory_ids: list[str],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> set[str]:
    """The ids among `memory_ids` whose row is live (not tombstoned) inside `scope`,
    whatever its status: a successor that was itself superseded or conflicted still
    names a row this read may see. Malformed ids are skipped rather than bound."""
    ids = _uuid_strings(memory_ids)
    nss = sorted({str(n) for n in namespaces if n})
    if not ids or not nss:
        return set()
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "nss": nss, "ids": ids}
    in_scope = scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT toString(memory_id) AS mid
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace IN {{nss:Array(String)}}
          AND memory_id IN {{ids:Array(UUID)}}
          AND tombstoned_at IS NULL{in_scope}
        SETTINGS select_sequential_consistency = 0
        """,
        params=params,
    )
    return {str(r["mid"]) for r in rows}


async def superseded_successors(
    org: str,
    rows: list[RowKey],
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[SupersededSuccessor]:
    """For each superseded row (namespace, agent, user_id, session_id, memory_key) a
    search pooled: its `superseded_by` id, the successor's memory_key when that row is
    still live, and the retirement instant (`invalid_at`, else the row's own `updated_at`).
    The pooled rows only, a slice of them per statement. Both the superseded row and its
    successor are read inside `scope`: a successor outside it is unnamed ('')."""
    wanted = sorted({r for r in rows if r.memory_key})
    if not wanted:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "nss": sorted({r.namespace for r in wanted}),
        "rows": wanted,
    }
    in_scope = scope_and(scope, params)
    found = await query_all_sliced(
        ch,
        f"""
        SELECT m.namespace AS namespace, m.agent AS agent, m.user_id AS user_id,
               m.session_id AS session_id, m.memory_key AS memory_key,
               m.superseded_by AS superseded_by, s.memory_key AS successor_key,
               toString(coalesce(m.invalid_at, m.updated_at, m.created_at)) AS superseded_at
        FROM (
            SELECT namespace, agent, user_id, session_id, memory_key, superseded_by,
                   invalid_at, updated_at, created_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}}
              AND (namespace, agent, user_id, session_id, memory_key)
                  IN {{rows:Array(Tuple(String, String, String, String, String))}}
              AND status = 'superseded'
              AND tombstoned_at IS NULL{in_scope}
        ) AS m
        LEFT JOIN (
            SELECT namespace, toString(memory_id) AS mid, memory_key
            FROM loom.memory FINAL
            WHERE org = {{org:String}}
              AND namespace IN {{nss:Array(String)}}
              AND tombstoned_at IS NULL{in_scope}
        ) AS s ON s.namespace = m.namespace AND s.mid = m.superseded_by
        SETTINGS select_sequential_consistency = 0
        """,
        params,
        "rows",
    )
    return [SupersededSuccessor.from_row(r) for r in found]


async def find_duplicate_pairs(
    org: str,
    namespace: str,
    *,
    key_prefix: str = "",
    limit: int = 500,
    ch: _CHClient | None = None,
) -> list[DuplicatePair]:
    """Live `status='duplicate'` rows of a namespace that name a canonical
    (`duplicate_of != ''`), with the identity the row was written under —
    the worklist of `memory.dedup_collapse`. Ordered by key
    so a bounded pass is deterministic; `key_prefix` narrows it."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
        "status = 'duplicate'",
        "duplicate_of != ''",
    ]
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    if key_prefix:
        where.append("startsWith(memory_key, {prefix:String})")
        params["prefix"] = key_prefix
    rows = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS memory_id, memory_key, agent, user_id, session_id,
               duplicate_of
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY memory_key ASC, version DESC
        LIMIT 1 BY memory_id
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [DuplicatePair.from_row(r) for r in rows]


async def clear_duplicate_tag(
    org: str,
    memory_id: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Flip a `status='duplicate'` row back to 'active' and clear
    `duplicate_of`. Used by `memory.dedup_resolve` decisions
    `keep_duplicate` (the dup wins; it becomes the canonical) and
    `keep_both` (both stay live as peers).

    A new version carrying every column (so `memory_versions`, history and
    rollback record the flip), and the embeddings mirror re-emitted with the
    new status, which is what memory.search reads. False when no live row
    has that id."""
    ch = ch or _default_client()
    existing = await get_by_id(memory_id, org=org, ch=ch, fresh=True, scope=WHOLE_NAMESPACE)
    if existing is None:
        return False
    row = build_memory_version_row(
        existing, status="active", duplicate_of="", version=next_version()
    )
    await insert_memory_versions([row], ch=ch)
    await mirror_status_to_embeddings([row], ch=ch)
    return True
