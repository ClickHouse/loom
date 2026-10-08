"""The loom.memory_embeddings mirror: its live rows per key, the version
re-emits that change a state-of-row column on it (tombstone, status, any
override), and the status reconciliation against loom.memory.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.store.inserts import insert_embedding_rows
from server.memory.store.rows import (
    EMBEDDING_PERSISTED_COLS,
    build_embedding_row,
    next_version,
)

_log = logging.getLogger(__name__)


# A FINAL point read filters on its sort key in PREWHERE. memory_embeddings
# is sorted by (org, namespace, agent, user_id, session_id, scope,
# embedding_model, memory_key); with `scope` and `embedding_model`
# unconstrained the primary-key range is the identity prefix across every
# part, and under FINAL the WHERE is applied after the merge, so every
# selected column — the embedding above all — is read for every one of those
# granules to return one row (139.5 MiB per call). A PREWHERE on the sort-key columns is
# applied per part before the merge, so only the granules holding a matching
# row read their other columns: 9.9 MiB for the same call. Sort-key columns
# are identical across a row's versions, so filtering on them before FINAL
# cannot drop a newer version; `tombstoned_at` is not a key column and stays
# in WHERE, after the merge.
_EMB_KEY_PREWHERE = (
    "org = {org:String} AND namespace = {ns:String}"
    " AND agent = {a:String} AND user_id = {u:String}"
    " AND session_id = {s:String} AND memory_key = {k:String}"
)


async def live_embedding_rows_for_key(
    ch: _CHClient,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
) -> list[dict[str, Any]]:
    """Every live memory_embeddings row of one (identity, memory_key) — one per
    (scope, embedding_model) — with the key in PREWHERE."""
    return await ch.query_all(
        f"""
        SELECT * FROM loom.memory_embeddings FINAL
        PREWHERE {_EMB_KEY_PREWHERE}
        WHERE tombstoned_at IS NULL
        """,
        params={
            "org": org,
            "ns": namespace,
            "a": agent,
            "u": user_id,
            "s": session_id,
            "k": memory_key,
        },
    )


async def mirror_status_to_embeddings(
    memory_rows: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    dry_run: bool = False,
    batch: int = 500,
) -> int:
    """Re-emit each identity's live memory_embeddings rows with the memory
    row's `status` (and `invalid_at` / `superseded_by`-implied retirement),
    version bumped — the same re-emit `supersede_duplicates` does, for every
    path that flips status through `write_versions_by_ids`. Returns the number
    of embedding rows re-emitted (or, with `dry_run`, the number that would
    be); a row already carrying the status is left alone. The mirror is read
    by the FULL identity tuple (org, namespace, agent, user_id, session_id,
    memory_key): one memory_key can live under hundreds of identities, each
    with its own status, so a read by key would paint every identity's
    mirror row with one identity's status. One keyed read per `batch`
    identities and one insert. Never raises past a logged warning: the
    memory write already landed, the mirror is a projection."""
    ch = ch or _default_client()
    wanted: dict[EmbeddingKey, dict[str, Any]] = {}
    for m in memory_rows:
        key = (
            str(m.get("org") or ""),
            str(m.get("namespace") or ""),
            str(m.get("agent") or "-"),
            str(m.get("user_id") or "-"),
            str(m.get("session_id") or "-"),
            str(m.get("memory_key") or ""),
        )
        overrides: dict[str, Any] = {"status": m.get("status")}
        if m.get("invalid_at") is not None:
            overrides["invalid_at"] = m.get("invalid_at")
        wanted[key] = overrides
    keys = list(wanted)
    out: list[dict[str, Any]] = []
    step = max(1, int(batch))
    for start in range(0, len(keys), step):
        chunk = keys[start : start + step]
        try:
            rows = await _live_rows_for_keys(ch, chunk)
        except Exception as e:  # noqa: BLE001 — projection, never the write
            _log.warning("status mirror: embeddings read failed for %d keys: %s", len(chunk), e)
            continue
        for r in rows:
            want = wanted.get(_embedding_key(r))
            if want is None or str(r.get("status") or "") == str(want["status"]):
                continue  # not asked for, or already mirrored
            out.append(
                build_embedding_row(
                    **(
                        {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
                        | want
                        | {"version": next_version()}
                    )
                )
            )
    if dry_run:
        return len(out)
    if out:
        try:
            await insert_embedding_rows(out, ch=ch)
        except Exception as e:  # noqa: BLE001 — projection, never the write; reconcile-status repairs
            _log.warning("status mirror: embeddings re-emit failed (%d rows): %s", len(out), e)
            return 0
    return len(out)


def status_divergence_sql() -> str:
    """Identity rows (org, namespace, agent, user_id, session_id, memory_key)
    whose latest live memory status differs from the latest live embeddings
    status under the SAME identity. Used by `loom reconcile-status`. Per
    identity, not per key: one key under hundreds of identities carries a
    status per identity, and the newest version's status says nothing about
    the others.

    Liveness is judged on the newest version (HAVING), not per row (WHERE):
    filtering tombstones before argMax hands a deleted key its pre-delete
    status, so it reads as divergent forever — the re-emit writes nothing
    for it — and crowds live rows out of the LIMIT."""
    return """
        WITH m AS (
            SELECT org, namespace, agent, user_id, session_id, memory_key,
                   argMax(status, version) AS ms, argMax(invalid_at, version) AS invalid_at
            FROM loom.memory
            WHERE org = {org:String} AND namespace = {ns:String}
            GROUP BY org, namespace, agent, user_id, session_id, memory_key
            HAVING argMax(tombstoned_at IS NULL, version) = 1),
        e AS (
            SELECT org, namespace, agent, user_id, session_id, memory_key,
                   argMax(status, version) AS es
            FROM loom.memory_embeddings
            WHERE org = {org:String} AND namespace = {ns:String}
            GROUP BY org, namespace, agent, user_id, session_id, memory_key
            HAVING argMax(tombstoned_at IS NULL, version) = 1)
        SELECT m.org AS org, m.namespace AS namespace, m.memory_key AS memory_key,
               m.agent AS agent, m.user_id AS user_id, m.session_id AS session_id,
               m.ms AS status, m.invalid_at AS invalid_at, e.es AS embeddings_status
        FROM m INNER JOIN e USING (org, namespace, agent, user_id, session_id, memory_key)
        WHERE m.ms != e.es
        LIMIT {lim:UInt32}
    """


async def divergent_namespaces(
    org: str,
    *,
    since_days: int = 0,
    limit: int = 5000,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Namespaces of `org` with at least one identity row whose latest memory
    status differs from its mirror's, with the row count per namespace —
    the org-wide entry point of `loom reconcile-status --all-namespaces`.
    `since_days` bounds both scans by created_at so
    the sweep reads recent parts, not the whole table; 0 = everything."""
    ch = ch or _default_client()
    window = " AND created_at > now() - INTERVAL {days:UInt32} DAY" if since_days > 0 else ""
    sql = f"""
        WITH m AS (
            SELECT org, namespace, agent, user_id, session_id, memory_key,
                   argMax(status, version) AS ms
            FROM loom.memory
            WHERE org = {{org:String}}{window}
            GROUP BY org, namespace, agent, user_id, session_id, memory_key
            HAVING argMax(tombstoned_at IS NULL, version) = 1),
        e AS (
            SELECT org, namespace, agent, user_id, session_id, memory_key,
                   argMax(status, version) AS es
            FROM loom.memory_embeddings
            WHERE org = {{org:String}}{window}
            GROUP BY org, namespace, agent, user_id, session_id, memory_key
            HAVING argMax(tombstoned_at IS NULL, version) = 1)
        SELECT m.namespace AS namespace, count() AS divergent_rows
        FROM m INNER JOIN e USING (org, namespace, agent, user_id, session_id, memory_key)
        WHERE m.ms != e.es
        GROUP BY namespace
        ORDER BY divergent_rows DESC
        LIMIT {{lim:UInt32}}
    """
    params: dict[str, Any] = {"org": org, "lim": int(limit)}
    if since_days > 0:
        params["days"] = int(since_days)
    rows = await ch.query_all(sql, params=params)
    return [
        {"namespace": str(r["namespace"]), "divergent_rows": int(r["divergent_rows"])} for r in rows
    ]


async def reconcile_embedding_status(
    org: str,
    namespace: str,
    *,
    limit: int = 5000,
    dry_run: bool = True,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Find identity rows whose embeddings status diverged from the memory
    table and re-emit those embeddings rows with the memory status. Returns
    counts by memory status found, `divergent_rows` (identity rows, the unit
    the mirror is written in) and `mirrored` rows written (the same count on
    a dry run — what it WOULD write)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        status_divergence_sql(), params={"org": org, "ns": namespace, "lim": int(limit)}
    )
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["status"])] = counts.get(str(r["status"]), 0) + 1
    counts["divergent_rows"] = len(rows)
    if not rows:
        counts["mirrored"] = 0
        return counts
    counts["mirrored"] = await mirror_status_to_embeddings(
        [dict(r) for r in rows], ch=ch, dry_run=dry_run
    )
    return counts


async def reemit_embedding_rows_for_key(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
    **overrides: Any,
) -> int:
    """Re-emit every live memory_embeddings row of one (identity, memory_key)
    as a new version with `overrides` applied — the RMT rule: every persisted
    column carried forward, the vectors included, a fresh `version`. This is
    the ONLY way a state-of-row column (expires_at, confidence, tags,
    tombstoned_at, status) changes on the vector table: a lightweight UPDATE
    leaves a patch part, and one patch part switches the codes-first lazy
    read off for every fact leg. The read is one keyed FINAL statement; the
    write one committed insert. Returns the
    rows re-emitted (0 when the key has no live embedding row)."""
    ch = ch or _default_client()
    rows = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        memory_key=memory_key,
    )
    return await _reemit_rows(rows, overrides, ch=ch)


# (org, namespace, agent, user_id, session_id, memory_key)
EmbeddingKey = tuple[str, str, str, str, str, str]


async def reemit_embedding_rows_for_keys(
    items: list[tuple[EmbeddingKey, dict[str, Any]]],
    *,
    ch: _CHClient | None = None,
    batch: int = 500,
) -> int:
    """`reemit_embedding_rows_for_key` for many keys at once: one keyed FINAL
    read and one committed insert per `batch` keys, instead of one of each
    per key (one-row parts from a sweep of ~1,000 keys a pass would dominate
    memory_embeddings' part count). Each key keeps its own overrides; a key
    listed twice takes the last. Returns the rows re-emitted."""
    ch = ch or _default_client()
    by_key: dict[EmbeddingKey, dict[str, Any]] = {k: dict(o) for k, o in items}
    keys = list(by_key)
    written = 0
    for start in range(0, len(keys), max(1, int(batch))):
        rows = await _live_rows_for_keys(ch, keys[start : start + max(1, int(batch))])
        out = []
        for r in rows:
            overrides = by_key.get(_embedding_key(r))
            if overrides is None:
                continue
            out.append(
                build_embedding_row(
                    **(
                        {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
                        | overrides
                        | {"version": next_version()}
                    )
                )
            )
        if out:
            await insert_embedding_rows(out, ch=ch)
            written += len(out)
    return written


async def _live_rows_for_keys(ch: _CHClient, keys: list[EmbeddingKey]) -> list[dict[str, Any]]:
    """Every live memory_embeddings row of the given identity keys, in one
    keyed FINAL read (the key tuple in PREWHERE, see `_EMB_KEY_PREWHERE`)."""
    if not keys:
        return []
    cols = list(zip(*keys, strict=True))
    return await ch.query_all(
        """
        SELECT * FROM loom.memory_embeddings FINAL
        PREWHERE (org, namespace, agent, user_id, session_id, memory_key) IN arrayZip(
            {o:Array(String)}, {n:Array(String)}, {a:Array(String)},
            {u:Array(String)}, {s:Array(String)}, {k:Array(String)})
        WHERE tombstoned_at IS NULL
        """,
        params={
            "o": list(cols[0]),
            "n": list(cols[1]),
            "a": list(cols[2]),
            "u": list(cols[3]),
            "s": list(cols[4]),
            "k": list(cols[5]),
        },
    )


def _embedding_key(r: dict[str, Any]) -> EmbeddingKey:
    return (
        str(r["org"]),
        str(r["namespace"]),
        str(r["agent"]),
        str(r["user_id"]),
        str(r["session_id"]),
        str(r["memory_key"]),
    )


async def _reemit_rows(
    rows: list[dict[str, Any]], overrides: dict[str, Any], *, ch: _CHClient
) -> int:
    out = [
        build_embedding_row(
            **(
                {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
                | dict(overrides)
                | {"version": next_version()}
            )
        )
        for r in rows
    ]
    if out:
        await insert_embedding_rows(out, ch=ch)
    return len(out)


_EMB_SHARED_PREWHERE = (
    "org = {org:String} AND namespace = {ns:String}"
    " AND scope = 'shared' AND memory_key = {k:String}"
)


async def live_shared_embedding_rows_for_key(
    ch: _CHClient, *, org: str, namespace: str, memory_key: str
) -> list[dict[str, Any]]:
    """Every live scope='shared' memory_embeddings row of one key (the
    promote mirror carries agent='_shared'; the key is namespace-wide)."""
    return await ch.query_all(
        f"""
        SELECT * FROM loom.memory_embeddings FINAL
        PREWHERE {_EMB_SHARED_PREWHERE}
        WHERE tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "k": memory_key},
    )


async def reemit_shared_embedding_rows(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
    **overrides: Any,
) -> int:
    """`reemit_embedding_rows_for_key` for the scope='shared' rows of a key
    (memory.demote's mirror, shared_repo)."""
    ch = ch or _default_client()
    rows = await live_shared_embedding_rows_for_key(
        ch, org=org, namespace=namespace, memory_key=memory_key
    )
    return await _reemit_rows(rows, overrides, ch=ch)


async def tombstone_embeddings_for_key(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    ts: datetime,
    ch: _CHClient | None = None,
) -> int:
    """Mirror a loom.memory tombstone onto every live memory_embeddings row
    of one key: same identity, every persisted column carried, tombstoned_at
    set, fresh version. Search reads the embeddings table, so a memory
    tombstoned only in loom.memory keeps surfacing. Returns rows re-emitted."""
    return await reemit_embedding_rows_for_key(
        org, namespace, agent, user_id, session_id, memory_key, ch=ch, tombstoned_at=ts
    )


async def unsupersede_embeddings(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Inverse of the supersede re-emit for one key: every live
    memory_embeddings row goes back to status='active' with the retirement
    stamp cleared, so recall sees it again. Returns rows re-emitted."""
    ch = ch or _default_client()
    rows = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        memory_key=memory_key,
    )
    out = [
        build_embedding_row(
            **(
                {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
                | {"status": "active", "invalid_at": None, "version": next_version()}
            )
        )
        for r in rows
    ]
    if out:
        await insert_embedding_rows(out, ch=ch)
    return len(out)


async def embedding_mirror_for_keys(
    org: str,
    namespace: str,
    memory_keys: list[str],
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Every live `memory_embeddings` row under the keys, with the identity
    tuple and the status the MIRROR carries — what the search leg's own
    predicate sees. `SELECT *` so a row can be re-emitted
    with only its status changed (the `supersede_duplicates` pattern)."""
    keys = sorted({str(k) for k in memory_keys if k})
    if not keys:
        return []
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT * FROM loom.memory_embeddings FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "keys": keys},
    )


async def restamp_embedding_rows(
    rows: list[dict[str, Any]],
    *,
    status: str,
    ch: _CHClient | None = None,
) -> int:
    """Re-emit mirror rows with `status` and a bumped version — an insert,
    never an in-place ALTER (those diverge under FINAL). Every other
    persisted column is carried forward. Returns the rows written."""
    if not rows:
        return 0
    out = [
        build_embedding_row(
            **(
                {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
                | {"status": status, "version": next_version()}
            )
        )
        for r in rows
    ]
    await insert_embedding_rows(out, ch=ch)
    return len(out)
