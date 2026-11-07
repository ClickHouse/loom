"""Repository for conflict-related reads/writes on loom.memory and
loom.conflict_allowlist.

Conflict semantics: when memory.set writes a row that's
semantically close to an existing row in the same scope, both get
`status='conflicted'` and link to each other via `conflict_with`.
Operators resolve in the dashboard's Review queue; the three decisions are
`revise` (target wins), `contract` (target loses), `allow`
(both legit; remember the pair via loom.conflict_allowlist).

This repo carries the SQL for:
  - listing conflicted rows for the operator surface
  - fetching one target row + its conflict_with set
  - writing conflict-resolution state via memory_repo RMT version rows
  - allowlist CRUD (list / exists / insert / soft-delete)
  - the namespace row and allowlist reads memory.set's conflict check makes
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and, scope_where
from server.memory.domain.delete_marks import carried_trace


async def list_conflicted(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    before_version: int | None = None,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Operator-surface listing: every conflicted row in the namespace,
    newest first. `before_version` cursor for pagination."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "status = 'conflicted'",
        "tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "limit": int(limit),
    }
    if before_version is not None:
        where.append("version < {bv:UInt64}")
        params["bv"] = before_version
    where.extend(scope_where(scope, params))
    # Locate, then fetch. `status = 'conflicted'` is not in the sort key, so
    # under FINAL a statement that selects `value` reads every value in the
    # namespace to keep the few conflicted rows (31.4 MB for 8 rows of 1,490).
    # The key columns locate the page under FINAL; the values come by
    # (memory_id, version) pairs without FINAL, the bulk-fetch shape, reading
    # only the page's rows' granules.
    located = await ch.query_all(
        f"""
        SELECT memory_id, memory_key, memory_type, version,
               pinned, created_at, conflict_with, sensitivity
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY version DESC
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
    if not located:
        return []
    if scope.narrowed:
        # A peer outside the scope is not named, not even by its id.
        visible = await _ids_in_scope(
            ch, org, namespace, [str(p) for r in located for p in r["conflict_with"]], scope
        )
        located = [
            {**r, "conflict_with": [p for p in r["conflict_with"] if str(p) in visible]}
            for r in located
        ]
    # A high row is listed for review, but its value is read by key only.
    pairs = [
        (str(r["memory_id"]), int(r["version"])) for r in located if r["sensitivity"] != "high"
    ]
    values: list[dict[str, Any]] = []
    if pairs:
        values = await ch.query_all(
            """
            SELECT memory_id, version, value
            FROM loom.memory
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
            LIMIT 1 BY memory_id, version
            """,
            params={"org": org, "ns": namespace, "pairs": pairs},
        )
    by_pair = {(str(v["memory_id"]), int(v["version"])): v["value"] for v in values}
    out: list[dict[str, Any]] = []
    for r in located:
        row = dict(r)
        row["value"] = by_pair.get((str(r["memory_id"]), int(r["version"])))
        out.append(row)
    return out


async def _ids_in_scope(
    ch: _CHClient, org: str, namespace: str, memory_ids: list[str], scope: ReadFilters
) -> set[str]:
    """The ids among `memory_ids` whose rows lie inside `scope`."""
    if not memory_ids:
        return set()
    params: dict[str, Any] = {"org": org, "ns": namespace, "ids": sorted(set(memory_ids))}
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT toString(memory_id) AS mid
        FROM loom.memory
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND memory_id IN {{ids:Array(UUID)}}{scope_and(scope, params)}
        """,
        params=params,
    )
    return {str(r["mid"]) for r in rows}


async def count_conflicted(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> int:
    """The badge's number: live conflicted rows in the namespace, under FINAL
    like the listing so a resolved pair is not counted from a stale version —
    but without the value column, which reads 33 MB per tick on a namespace
    of large values."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace}
    rows = await ch.query_all(
        f"""
        SELECT count() AS n
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND status = 'conflicted' AND tombstoned_at IS NULL{scope_and(scope, params)}
        """,
        params=params,
    )
    return int((rows[0] if rows else {}).get("n") or 0)


async def conflicted_keys(
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    limit: int = 5000,
    ch: _CHClient | None = None,
) -> list[str]:
    """The conflicted memory keys of the namespace: one FINAL read of the key
    columns for the cards' conflict chips. The row listing reads the value
    column too (49.5 MB for 2.3 kB of answer on a namespace of large values)."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "limit": int(limit)}
    rows = await ch.query_all(
        f"""
        SELECT memory_key
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND status = 'conflicted' AND tombstoned_at IS NULL{scope_and(scope, params)}
        ORDER BY version DESC
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
    return [str(r["memory_key"]) for r in rows]


@dataclass(frozen=True, slots=True)
class ConflictTarget:
    """A live row's id, the ids it conflicts with, and its status."""

    memory_id: str
    conflict_with: tuple[str, ...]
    status: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> ConflictTarget:
        return cls(
            memory_id=str(r.get("memory_id") or ""),
            conflict_with=tuple(str(p) for p in (r.get("conflict_with") or [])),
            status=str(r.get("status") or ""),
        )


async def lookup_target(
    org: str,
    namespace: str,
    memory_id: str,
    *,
    ch: _CHClient | None = None,
) -> ConflictTarget | None:
    """Fetch one target row's id + conflict_with set + status, in `namespace` (a
    namespace restored from another's snapshot keeps its memory ids).

    Used by resolve as the precheck: if None, the target is already
    tombstoned and we return resolved=False.
    """
    ch = ch or _default_client()
    r = await ch.query_one(
        """
        SELECT memory_id, conflict_with, status
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id = {mid:UUID}
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "mid": memory_id},
    )
    return None if r is None else ConflictTarget.from_row(r)


async def tombstone_ids(
    org: str,
    memory_ids: list[str],
    audit_note: str,
    when: datetime,
    *,
    namespace: str,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Tombstone N rows of `namespace` by memory_id. `audit_note` is appended to
    source_trace so the resolution-audit trail is queryable.
    Empty list short-circuits (an empty IN would mutate every row).

    Returns each tombstoned row's identity tuple, memory_key, memory_id and
    tombstone version, for the handler's webhook events."""
    if not memory_ids:
        return []
    import server.memory.store as memory_repo

    def _note(row: memory_repo.MemoryRow) -> dict[str, Any]:
        trace = carried_trace(row.source_trace)  # the delete_version marker is not carried
        return {"source_trace": f"{trace} | {audit_note}" if trace else audit_note}

    # The tombstone must reach memory_embeddings too, or the losing side of a
    # conflict keeps surfacing in search.
    return await memory_repo.tombstone_cascade_by_ids(
        org,
        memory_ids,
        when=when,
        update=_note,
        ch=ch,
        namespace=namespace,
    )


async def mark_active_by_ids(
    org: str,
    memory_ids: list[str],
    *,
    namespace: str,
    ch: _CHClient | None = None,
) -> None:
    """Flip status='active' for the listed rows of `namespace`. Empty short-circuits."""
    if not memory_ids:
        return
    import server.memory.store as memory_repo

    await memory_repo.write_versions_by_ids(
        org,
        memory_ids,
        lambda _row: {"status": "active"},
        ch=ch,
        namespace=namespace,
    )


async def lookup_keys_for_ids(
    org: str,
    memory_ids: list[str],
    *,
    namespace: str,
    ch: _CHClient | None = None,
) -> dict[str, str]:
    """Resolve memory_id → memory_key for the listed rows of `namespace`. Used by
    the resolve(allow) path to canonicalize allowlist pairs."""
    if not memory_ids:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT memory_id, memory_key FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_id IN {ids:Array(UUID)}
          AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "ids": memory_ids},
    )
    return {str(r["memory_id"]): r["memory_key"] for r in rows}


async def allowlist_list(
    org: str,
    namespace: str,
    *,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Active (non-tombstoned) allowlist entries for one namespace."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT memory_key_a, memory_key_b, allowed_at, allowed_by, reason
        FROM loom.conflict_allowlist FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND tombstoned_at IS NULL
        ORDER BY allowed_at DESC
        LIMIT {lim:UInt32}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )


async def allowlist_insert(
    rows: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Bulk insert allowlist rows. Caller canonicalizes the (key_a,
    key_b) pair to (min, max). Empty short-circuits."""
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(
        "loom.conflict_allowlist",
        rows,
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def allowlist_exists(
    org: str,
    namespace: str,
    key_a: str,
    key_b: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """True iff a live allowlist row exists for (key_a, key_b). Caller
    passes canonical (min, max) order; this function trusts that."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT 1 FROM loom.conflict_allowlist FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key_a = {a:String} AND memory_key_b = {b:String}
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "a": key_a, "b": key_b},
    )
    return row is not None


async def allowlist_remove(
    org: str,
    namespace: str,
    key_a: str,
    key_b: str,
    audit_note: str,
    when: datetime,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Soft-delete: set tombstoned_at + append audit_note. Caller
    passes canonical (min, max) pair."""
    ch = ch or _default_client()
    await ch.execute(
        """
        ALTER TABLE loom.conflict_allowlist
        UPDATE tombstoned_at = {ts:DateTime},
               reason = concat(reason, ' | ', {note:String})
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key_a = {a:String} AND memory_key_b = {b:String}
        SETTINGS mutations_sync=2
        """,
        params={
            "org": org,
            "ns": namespace,
            "a": key_a,
            "b": key_b,
            "ts": when,
            "note": audit_note,
        },
    )


async def namespace_write_row(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> dict[str, Any]:
    """The namespace row's three write-time conflict columns ({} when the
    namespace has no row)."""
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT conflict_threshold, duplicate_threshold, default_conflict_policy "
        "FROM loom.namespaces FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} LIMIT 1",
        params={"org": org, "ns": namespace},
    )
    return dict(row or {})


async def allowlisted_peers(
    org: str, namespace: str, memory_key: str, *, ch: _CHClient | None = None
) -> set[str]:
    """The keys a live allowlist pair joins to `memory_key`."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT memory_key_a, memory_key_b
        FROM loom.conflict_allowlist FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND tombstoned_at IS NULL
          AND (memory_key_a = {k:String} OR memory_key_b = {k:String})
        """,
        params={"org": org, "ns": namespace, "k": memory_key},
    )
    return {
        r["memory_key_b"] if r["memory_key_a"] == memory_key else r["memory_key_a"] for r in rows
    }
