"""Version history of loom.memory, read from the append-only mirror
loom.memory_versions: one key's history, rollback diffs and historical rows.
The in-place redaction of a version lives in `redaction`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and
from server.memory.store.rows import MEMORY_PERSISTED_COLS, Identity, MemoryRow, _hydrate
from server.memory.store.sql import _SEQUENTIAL, _is_full_row, read_your_writes_enabled


@dataclass(frozen=True, slots=True)
class HistoryVersion:
    """One version of a key as `list_history` reads it; `value` is the stored
    text ('' when the value read found none)."""

    memory_id: str
    version: int
    value: Any
    confidence: Any
    source_trace: Any
    pinned: Any
    created_at: Any
    updated_at: Any
    expires_at: Any
    tombstoned_at: Any
    redacted_at: Any
    redaction_reason: Any
    actor: str
    source_class: str
    agent: str
    status: str
    conflict_with: tuple[str, ...]
    tags: tuple[str, ...]
    sensitivity: str
    verified_at: Any
    verified_by: str
    verified_sha256: str
    lock_mode: str
    lock_reason: str
    lock_until: Any

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> HistoryVersion:
        return cls(
            memory_id=str(r.get("memory_id") or ""),
            version=int(r.get("version") or 0),
            value=r.get("value", ""),
            confidence=r.get("confidence"),
            source_trace=r.get("source_trace"),
            pinned=r.get("pinned"),
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
            expires_at=r.get("expires_at"),
            tombstoned_at=r.get("tombstoned_at"),
            redacted_at=r.get("redacted_at"),
            redaction_reason=r.get("redaction_reason"),
            actor=str(r.get("actor") or ""),
            source_class=str(r.get("source_class") or ""),
            agent=str(r.get("agent") or ""),
            status=str(r.get("status") or "active"),
            conflict_with=tuple(str(c) for c in r.get("conflict_with") or ()),
            tags=tuple(r.get("tags") or ()),
            sensitivity=str(r.get("sensitivity") or ""),
            verified_at=r.get("verified_at"),
            verified_by=str(r.get("verified_by") or ""),
            verified_sha256=str(r.get("verified_sha256") or ""),
            lock_mode=str(r.get("lock_mode") or ""),
            lock_reason=str(r.get("lock_reason") or ""),
            lock_until=r.get("lock_until"),
        )


@dataclass(frozen=True, slots=True)
class HistoryTarget:
    """The identity and state of a memory_id's latest version."""

    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    version: int
    tombstoned_at: Any

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> HistoryTarget:
        return cls(
            namespace=str(r.get("namespace") or ""),
            agent=str(r.get("agent") or ""),
            user_id=str(r.get("user_id") or ""),
            session_id=str(r.get("session_id") or ""),
            memory_key=str(r.get("memory_key") or ""),
            version=int(r.get("version") or 0),
            tombstoned_at=r.get("tombstoned_at"),
        )


# Every persisted column but the version stamp and `entities`; `content_sha256`
# stands in for the value, which the version list does not read.
_SAME_STATE_COLS = tuple(
    c for c in MEMORY_PERSISTED_COLS if c not in ("version", "entities", "value", "value_json")
)
_STATE_HASH = f"cityHash64(tuple({', '.join(_SAME_STATE_COLS)}))"


async def list_history(
    identity: Identity,
    memory_key: str,
    *,
    scope: ReadFilters,
    limit: int = 100,
    ch: _CHClient | None = None,
) -> list[HistoryVersion]:
    """Version history of one memory_key, newest-first, including
    tombstoned + redacted rows; entity-only re-emits are left out.

    Reads loom.memory_versions — the append-only mirror a materialized view
    fills on every INSERT into loom.memory — UNIONed with the
    live table's still-unmerged rows, deduplicated on (memory_id, version).
    loom.memory alone is ReplacingMergeTree(version): a background merge
    keeps only the head, so it cannot serve as the history."""
    ch = ch or _default_client()
    # Agent "*" reads the key's history namespace-wide (every writer's rows,
    # like get_by_key's wildcard) — a key another client wrote
    # has a history too; each row names its writer.
    if identity.agent == "*":
        ident_where = """org = {org:String} AND namespace = {ns:String}
          AND memory_key = {k:String}"""
    else:
        ident_where = """org = {org:String} AND namespace = {ns:String}
          AND agent = {a:String} AND user_id = {u:String}
          AND session_id = {s:String}
          AND memory_key = {k:String}"""
    # The version list is read without the value column (the union scans the
    # identity's whole history), then the values
    # of the listed versions alone — 13.6 MB → under 1 MB for eight versions.
    cols = """memory_id, version, confidence, source_trace, pinned,
               created_at, updated_at, expires_at, tombstoned_at, redacted_at, redaction_reason,
               actor, source_class, agent, status, conflict_with, tags, sensitivity,
               verified_at, verified_by, verified_sha256, lock_mode, lock_reason, lock_until"""
    params = {
        "org": identity.org,
        "ns": identity.namespace,
        "a": identity.agent,
        "u": identity.user_id,
        "s": identity.session_id,
        "k": memory_key,
        "limit": limit,
    }
    # A wildcard-agent history spans writers, never users outside the read scope.
    ident_where += scope_and(scope, params)
    # A version that reads like the one before it, `entities` aside, is the entity
    # tagger's re-emit: a caller's write stamps `updated_at`, a re-emit carries it
    # forward. It is left out before the limit, so a run of them cannot hide an
    # older edit. Every other persisted column counts (_SAME_STATE_COLS).
    rows = await ch.query_all(
        f"""
        SELECT {cols}
        FROM (
            SELECT *,
                   lagInFrame(_state) OVER w AS _prev_state,
                   row_number() OVER w AS _n
            FROM (
                SELECT * FROM (
                    SELECT {cols}, content_sha256, {_STATE_HASH} AS _state
                    FROM loom.memory_versions WHERE {ident_where}
                    UNION ALL
                    SELECT {cols}, content_sha256, {_STATE_HASH} AS _state
                    FROM loom.memory WHERE {ident_where}
                )
                LIMIT 1 BY memory_id, version
            )
            WINDOW w AS (PARTITION BY memory_id ORDER BY version
                         ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
        )
        WHERE NOT (_n > 1 AND content_sha256 != '' AND _state = _prev_state)
        ORDER BY version DESC
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
    if not rows or all(_is_full_row(r) for r in rows):
        return [HistoryVersion.from_row(r) for r in rows]
    values = await ch.query_all(
        f"""
        SELECT memory_id, version, value
        FROM (
            SELECT memory_id, version, value FROM loom.memory_versions
            WHERE {ident_where} AND version IN {{vs:Array(UInt64)}}
            UNION ALL
            SELECT memory_id, version, value FROM loom.memory
            WHERE {ident_where} AND version IN {{vs:Array(UInt64)}}
        )
        LIMIT 1 BY memory_id, version
        """,
        params={**params, "vs": [int(r["version"]) for r in rows]},
    )
    # Keyed on (memory_id, version): a namespace-wide read spans
    # several writers' rows of the key.
    by_row = {(str(v["memory_id"]), int(v["version"])): v["value"] for v in values}
    return [
        HistoryVersion.from_row(
            {**r, "value": by_row.get((str(r["memory_id"]), int(r["version"])), "")}
        )
        for r in rows
    ]


async def find_history_target(
    org: str,
    memory_id: str,
    *,
    ch: _CHClient | None = None,
) -> HistoryTarget | None:
    """Lookup the latest-version row (NOT FINAL) for a memory_id. Used
    by redact, which needs to know the identity tuple + tombstone state
    before mutating in place."""
    ch = ch or _default_client()
    cols = "namespace, agent, user_id, session_id, memory_key, version, tombstoned_at"
    r = await ch.query_one(
        f"""
        SELECT {cols}
        FROM (
            SELECT {cols} FROM loom.memory_versions
            WHERE org = {{org:String}} AND memory_id = {{mid:UUID}}
            UNION ALL
            SELECT {cols} FROM loom.memory
            WHERE org = {{org:String}} AND memory_id = {{mid:UUID}}
        )
        ORDER BY version DESC
        LIMIT 1
        """,
        params={"org": org, "mid": memory_id},
    )
    return None if r is None else HistoryTarget.from_row(r)


async def version_exists(
    org: str,
    memory_id: str,
    version: int,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether the mirror holds (memory_id, version): memory.redact checks the
    named version before it scrubs anything built from the memory."""
    ch = ch or _default_client()
    r = await ch.query_one(
        """
        SELECT 1 AS found FROM loom.memory_versions
        WHERE org = {org:String} AND memory_id = {mid:UUID} AND version = {v:UInt64}
        LIMIT 1
        """,
        params={"org": org, "mid": memory_id, "v": version},
    )
    return r is not None


async def rollback_change_rows(
    org: str,
    namespace: str,
    version_at: int,
    *,
    memory_keys: list[str] | None = None,
    limit: int = 5000,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Keys whose head now differs from their head as of `version_at`.

    FULL OUTER JOIN of the live heads (loom.memory FINAL) with the highest
    version ≤ version_at per key (loom.memory_versions). A side with no row
    reads as version 0 (join_use_nulls=0). Returns up to `limit + 1` rows so
    the caller can tell truncation from an exact fit. Rows where both
    versions agree are excluded in SQL."""
    ch = ch or _default_client()
    key_filter = " AND memory_key IN {keys:Array(String)}" if memory_keys else ""
    params: dict[str, Any] = {"org": org, "ns": namespace, "v": version_at, "lim": limit + 1}
    if memory_keys:
        params["keys"] = list(memory_keys)
    return await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key,
               version_now, tombstoned_now, version_at
        FROM (
            SELECT agent, user_id, session_id, memory_key,
                   version AS version_now,
                   toUInt8(tombstoned_at IS NOT NULL) AS tombstoned_now
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}{key_filter}
        ) AS now
        FULL OUTER JOIN (
            SELECT agent, user_id, session_id, memory_key,
                   max(version) AS version_at
            FROM loom.memory_versions
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND version <= {{v:UInt64}}{key_filter}
            GROUP BY agent, user_id, session_id, memory_key
        ) AS at USING (agent, user_id, session_id, memory_key)
        WHERE version_now != version_at
        ORDER BY memory_key, agent, user_id, session_id
        LIMIT {{lim:UInt32}}
        SETTINGS join_use_nulls = 0
        """,
        params=params,
    )


async def fetch_version_rows(
    org: str,
    namespace: str,
    pairs: list[tuple[str, int]],
    *,
    sequential: bool = False,
    ch: _CHClient | None = None,
) -> list[MemoryRow]:
    """The exact historical rows for (memory_key, version) pairs, hydrated
    so build_memory_version_row can re-emit them as new heads. `sequential` reads with
    sequential consistency, for a row another read just saw committed."""
    if not pairs:
        return []
    ch = ch or _default_client()
    settings = _SEQUENTIAL if sequential and read_your_writes_enabled() else ""
    rows = await ch.query_all(
        f"""
        SELECT * FROM loom.memory_versions FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND (memory_key, version) IN {{pairs:Array(Tuple(String, UInt64))}}
        {settings}
        """,
        params={"org": org, "ns": namespace, "pairs": [(k, int(v)) for k, v in pairs]},
    )
    return [_hydrate(r) for r in rows]


async def earliest_version_in_namespace(
    org: str,
    namespace: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Oldest version stamp the history holds for a namespace; 0 when none.
    A rollback earlier than this would tombstone every key, so the handler
    refuses it."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT min(version) AS v FROM loom.memory_versions
        WHERE org = {org:String} AND namespace = {ns:String}
        """,
        params={"org": org, "ns": namespace},
    )
    return int((row or {}).get("v") or 0)
