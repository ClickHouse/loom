"""The loom.memory_embeddings mirror: its live rows per key, the version
re-emits that change a state-of-row column on it (tombstone, status, any
override), and the status reconciliation against loom.memory.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from server.core import pg
from server.core.ch_writes import committed_insert_settings
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client, query_all_sliced
from server.memory.domain.temporal_sql import SERVED_STATUSES
from server.memory.store.inserts import insert_embedding_rows
from server.memory.store.rows import (
    EMBEDDING_PERSISTED_COLS,
    IdentityKey,
    KeyInNamespace,
    build_embedding_row,
    identity_key,
    key_in_namespace,
    next_version,
    reemit_version,
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
EMB_KEY_PREWHERE = (
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
        PREWHERE {EMB_KEY_PREWHERE}
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
    strict: bool = False,
    judged: bool = False,
    sequential: bool = False,
) -> int:
    """Re-emit each identity's live vector rows with its memory row's `status` (and
    `invalid_at`, and `tombstoned_at` for a tombstone), at the memory row's `version`, or
    at read + 1 without one; a vector at or above that version (a newer write's) and one
    already mirrored are left alone. Read by the full identity: a key repeats across
    identities, each with its own status. Returns the rows re-emitted (with `dry_run`,
    the rows it would). Never raises, unless `strict`. `sequential` reads the vector
    rows sequentially consistent, so a vector row written just before is found.

    `judged` (a verdict reached after the write): only the vectors of the memory row's
    `content_sha256` are copied, at a version drawn before a sequential read, so a
    metadata copy stays below it and a newer revision's vector stays above it; a vector
    a retire hid meanwhile stays hidden."""
    ch = ch or _default_client()
    drawn = next_version() if judged else 0
    wanted: dict[IdentityKey, dict[str, Any]] = {}
    shas: dict[IdentityKey, str] = {}
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
        # A served head's None is its value too: a vector served again loses the end a
        # retirement stamped on it.
        if m.get("invalid_at") is not None or (
            "invalid_at" in m and m.get("status") in SERVED_STATUSES
        ):
            overrides["invalid_at"] = m.get("invalid_at")
        if m.get("tombstoned_at") is not None:
            overrides["tombstoned_at"] = m.get("tombstoned_at")
        if judged:
            overrides["version"] = drawn
            shas[key] = str(m.get("content_sha256") or "")
        elif m.get("version"):
            overrides["version"] = int(m["version"])
        wanted[key] = overrides
    keys = list(wanted)
    out: list[dict[str, Any]] = []
    step = max(1, int(batch))
    for start in range(0, len(keys), step):
        chunk = keys[start : start + step]
        try:
            rows = await _live_rows_for_keys(ch, chunk, sequential=judged or sequential)
        except Exception as e:  # a mirror, never the write; strict re-raises
            if strict:
                raise
            _log.warning("status mirror: embeddings read failed for %d keys: %s", len(chunk), e)
            continue
        for r in rows:
            want = wanted.get(identity_key(r))
            if judged and str(r.get("content_sha256") or "") != shas.get(identity_key(r)):
                continue  # another revision's vector
            if judged and str(r.get("status") or "") not in SERVED_STATUSES:
                continue  # a retire (a refines supersede) won meanwhile
            if want is None or (
                str(r.get("status") or "") == str(want["status"])
                and "tombstoned_at" not in want
                and ("invalid_at" not in want or r.get("invalid_at") == want["invalid_at"])
            ):
                continue  # not asked for, or already mirrored
            row = _copy_row(r, want)
            if row is not None:
                out.append(row)
    if dry_run:
        return len(out)
    if out:
        try:
            await insert_embedding_rows(out, ch=ch)
            await retire_copies_under_unembedded_heads(out, ch=ch)
        except Exception as e:  # a mirror, never the write; strict re-raises
            if strict:
                raise
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
                   argMax(status, version) AS ms, max(version) AS mv,
                   argMax(tuple(invalid_at), version).1 AS invalid_at
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
               m.ms AS status, m.invalid_at AS invalid_at, e.es AS embeddings_status,
               m.mv AS memory_version
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
    a dry run — what it WOULD write), and `held_back`. Under the Postgres authority a row
    is held back when its head is newer than loom.memory's (a reject's vectors land
    before its publish) or gone (an erasure not yet in ClickHouse): mirroring the older
    status would undo the authority's change. Held-back rows count against `limit`, so a
    namespace holding `limit` of them mirrors nothing until they publish or their
    erasure reaches ClickHouse."""
    ch = ch or _default_client()
    version = next_version()  # before the scan: a row written after it is newer, and kept
    scanned = await ch.query_all(
        status_divergence_sql(), params={"org": org, "ns": namespace, "lim": int(limit)}
    )
    rows = await _at_authority_head(scanned)
    counts = {"held_back": len(scanned) - len(rows)}
    for r in rows:
        counts[str(r["status"])] = counts.get(str(r["status"]), 0) + 1
    counts["divergent_rows"] = len(rows)
    if not rows:
        counts["mirrored"] = 0
        return counts
    # A copy to a status not served by default is a retire, stamped with the pre-scan
    # version so a late metadata copy can't bring it back. One that serves the row
    # again takes read + 1, so a memory.delete racing the pass keeps its retire.
    counts["mirrored"] = await mirror_status_to_embeddings(
        [
            dict(r) | ({} if str(r["status"]) in SERVED_STATUSES else {"version": version})
            for r in rows
        ],
        ch=ch,
        dry_run=dry_run,
    )
    return counts


async def _at_authority_head(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The divergent rows whose authority head exists and is no newer than loom.memory's
    row (all of them on the ClickHouse authority)."""
    from server.memory.store import pg_memory

    if not rows or not pg.enabled():
        return rows
    heads = await pg_memory.head_versions([identity_key(r) for r in rows])
    return [
        r
        for r in rows
        if (head := heads.get(identity_key(r))) is not None and head <= int(r["memory_version"])
    ]


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
    column carried forward, the vectors included, the read row's version + 1
    (`reemit_version`: a write that lands after the read keeps winning; a
    `version` in `overrides` replaces it). This is the ONLY way a state-of-row
    column (expires_at, confidence, tags, tombstoned_at, status) changes on the
    vector table: a lightweight UPDATE leaves a patch part, and one patch part
    switches the codes-first lazy read off for every fact leg. The read is one
    keyed FINAL statement; the write one committed insert. Returns the rows
    re-emitted (0 when the key has no live embedding row)."""
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
    return await reemit_rows(rows, overrides, ch=ch)


async def reemit_embedding_rows_for_keys(
    items: list[tuple[IdentityKey, dict[str, Any]]],
    *,
    ch: _CHClient | None = None,
    batch: int = 500,
    of_content: Mapping[IdentityKey, str] | None = None,
) -> int:
    """`reemit_embedding_rows_for_key` for many keys at once: one keyed FINAL
    read and one committed insert per `batch` keys, instead of one of each
    per key (one-row parts from a sweep of ~1,000 keys a pass would dominate
    memory_embeddings' part count). Each key keeps its own overrides (a
    `version` among them replaces the read row's + 1, and only rows older than it are
    re-emitted); a key listed twice takes the last. A key in `of_content` re-emits only
    the rows of that content hash, so a copy never carries another version's text; the
    caller names only a real hash (`is_content_hash`), since a placeholder is shared by
    every version written before hashes. Returns the rows re-emitted."""
    ch = ch or _default_client()
    by_key: dict[IdentityKey, dict[str, Any]] = {k: dict(o) for k, o in items}
    keys = list(by_key)
    written = 0
    for start in range(0, len(keys), max(1, int(batch))):
        rows = await _live_rows_for_keys(ch, keys[start : start + max(1, int(batch))])
        out = []
        for r in rows:
            key = identity_key(r)
            want = (of_content or {}).get(key)
            if want is not None and str(r.get("content_sha256") or "") != want:
                continue
            overrides = by_key.get(key)
            row = _copy_row(r, overrides) if overrides is not None else None
            if row is not None:
                out.append(row)
        if out:
            await insert_embedding_rows(out, ch=ch)
            await retire_copies_under_unembedded_heads(out, ch=ch)
            written += len(out)
    return written


async def live_rows_for_keys(
    keys: list[IdentityKey], *, ch: _CHClient | None = None, sequential: bool = False
) -> list[dict[str, Any]]:
    """Every live memory_embeddings row of the given identity keys, `_HEAD_READ_BATCH`
    keys per statement."""
    ch = ch or _default_client()
    out: list[dict[str, Any]] = []
    for start in range(0, len(keys), _HEAD_READ_BATCH):
        out += await _live_rows_for_keys(
            ch, keys[start : start + _HEAD_READ_BATCH], sequential=sequential
        )
    return out


async def head_sensitivities(
    keys: list[IdentityKey], *, ch: _CHClient | None = None, sequential: bool = False
) -> dict[IdentityKey, str]:
    """The live head's sensitivity per identity key (absent when there is none),
    `_HEAD_READ_BATCH` keys per statement."""
    ch = ch or _default_client()
    heads: dict[IdentityKey, str] = {}
    for start in range(0, len(keys), _HEAD_READ_BATCH):
        heads |= await _head_sensitivity(ch, keys[start : start + _HEAD_READ_BATCH], sequential)
    return heads


async def _live_rows_for_keys(
    ch: _CHClient,
    keys: list[IdentityKey],
    *,
    sequential: bool = False,
    columns: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Every live memory_embeddings row of the given identity keys (only its `columns` when
    named), in one keyed FINAL read (the key tuple in PREWHERE, see `EMB_KEY_PREWHERE`)."""
    from server.memory.store.sql import _SEQUENTIAL

    if not keys:
        return []
    cols = list(zip(*keys, strict=True))
    return await ch.query_all(
        f"""
        SELECT {", ".join(columns) or "*"} FROM loom.memory_embeddings FINAL
        PREWHERE (org, namespace, agent, user_id, session_id, memory_key) IN arrayZip(
            {{o:Array(String)}}, {{n:Array(String)}}, {{a:Array(String)}},
            {{u:Array(String)}}, {{s:Array(String)}}, {{k:Array(String)}})
        WHERE tombstoned_at IS NULL
        {_SEQUENTIAL if sequential else ""}
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


def _copy_row(r: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any] | None:
    """A new version of read row `r` with `overrides`: at read + 1, or at the `version`
    the overrides pin (a retire's, or the expiry sweep's demote). None when the row is
    at or above that pin: it is a newer write's, and an older retire must not touch
    it."""
    pinned = overrides.get("version")
    if pinned is not None and int(r.get("version") or 0) >= int(pinned):
        return None
    return _reemitted(r, overrides)


def _reemitted(r: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Read row `r` re-emitted with `overrides`, at read + 1 unless they pin a version."""
    return build_embedding_row(
        **(
            {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
            | {"version": reemit_version(r)}
            | dict(overrides)
        )
    )


async def reemit_rows(
    rows: list[dict[str, Any]], overrides: dict[str, Any], *, ch: _CHClient
) -> int:
    """Re-emit read rows as new versions with `overrides` (see `_copy_row`); returns
    the rows written."""
    out = [row for r in rows if (row := _copy_row(r, overrides)) is not None]
    if out:
        await insert_embedding_rows(out, ch=ch)
        await retire_copies_under_unembedded_heads(out, ch=ch)
    return len(out)


# Identity keys per head read: past ~2,000 ClickHouse refuses the request's form field.
_HEAD_READ_BATCH = 500


async def retire_copies_under_unembedded_heads(
    written: list[dict[str, Any]], *, ch: _CHClient | None = None
) -> int:
    """After a copy of live vector rows (a touch, a verify, a tag, a status mirror),
    re-read the keys' heads; a key whose head is 'high' or deleted has each copied
    row tombstoned two above itself. A copy that read its head before a 'high' write
    landed would otherwise carry the vector up past that write's retire, and copies
    chain: each one checks after its own insert, so the last to land retires above
    itself. Tombstones, dim-0 rows and shared-scope rows (no head of their own) are
    not checked. A missing head counts only on a sequentially consistent read (a
    lagging replica can miss a fresh key). Never raises: the copy already landed, and
    a failed check is logged (the orphan reconcile is the backstop for a deleted
    head). Returns the rows tombstoned."""
    from server.memory.store.sql import read_your_writes_enabled

    copies = [
        r
        for r in written
        if r.get("tombstoned_at") is None
        and str(r.get("scope") or "") != "shared"
        and int(r.get("embedding_dim") or 0) > 0
    ]
    if not copies:
        return 0
    ch = ch or _default_client()
    sequential = read_your_writes_enabled()
    try:
        keys = sorted({identity_key(r) for r in copies})
        heads = await head_sensitivities(keys, ch=ch, sequential=sequential)
        stale = {k for k in keys if heads.get(k) == "high" or (sequential and k not in heads)}
        if not stale:
            return 0
        now = naive_utc_now()
        tombstones = [
            build_embedding_row(
                **(
                    {c: v for c, v in r.items() if c in EMBEDDING_PERSISTED_COLS}
                    # two above: a read + 1 copy of this copy stays below it
                    | {"tombstoned_at": now, "version": int(r["version"]) + 2}
                )
            )
            for r in copies
            if identity_key(r) in stale
        ]
        await insert_embedding_rows(tombstones, ch=ch)
        return len(tombstones)
    except Exception as e:  # noqa: BLE001 — the copy landed; a missed check is logged, not raised
        _log.warning("vector copy: head re-check failed for %d rows: %s", len(copies), e)
        return 0


async def _head_sensitivity(
    ch: _CHClient, keys: list[IdentityKey], sequential: bool
) -> dict[IdentityKey, str]:
    """The live head's sensitivity per identity key (absent when there is none): the
    narrow keyed read the post-copy check needs."""
    from server.memory.store.sql import _SEQUENTIAL

    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, sensitivity
        FROM loom.memory FINAL
        PREWHERE (org, namespace, agent, user_id, session_id, memory_key)
                 IN {{keys:Array(Tuple(String, String, String, String, String, String))}}
        WHERE tombstoned_at IS NULL
        {_SEQUENTIAL if sequential else ""}
        """,
        params={"keys": [tuple(k) for k in keys]},
    )
    return {identity_key(r): str(r.get("sensitivity") or "") for r in rows}


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
    return await reemit_rows(rows, overrides, ch=ch)


async def tombstone_embeddings_for_key(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    ts: datetime,
    version: int,
    ch: _CHClient | None = None,
) -> int:
    """Mirror a loom.memory tombstone onto every live memory_embeddings row
    of one key: same identity, every persisted column carried, tombstoned_at
    set. Search reads the embeddings table, so a memory tombstoned only in
    loom.memory keeps surfacing. `version` is the loom.memory row that retires
    the key (the delete's tombstone, the sweep's relabel): a late metadata copy
    of an older read writes read + 1, below it, so it cannot bring the vector
    back; rows at or above it (a newer write's) are kept. Returns rows re-emitted."""
    key = (org, namespace, agent, user_id, session_id, memory_key)
    return await reemit_embedding_rows_for_keys(
        [(key, {"tombstoned_at": ts, "version": int(version)})], ch=ch
    )


async def tombstone_vectors_of(previous: Any, *, version: int, ch: _CHClient | None = None) -> int:
    """A new version that is not embedded ('high', or a non-searchable type) retires
    the vectors of the version it replaces: no search leg may keep serving the old
    vector or excerpt. `previous` is that version's loom.memory row (a key that was
    never embedded has nothing to drop); `version` is the new row's."""
    from server.memory.domain.memory_types import is_embedded

    if previous is None or not is_embedded(
        previous.memory_type, getattr(previous, "sensitivity", "")
    ):
        return 0
    key = (
        previous.org,
        previous.namespace,
        previous.agent,
        previous.user_id,
        previous.session_id,
        previous.memory_key,
    )
    return await retire_vectors_for_keys([(key, version)], ch=ch)


async def retire_vectors_for_keys(
    items: list[tuple[IdentityKey, int]], *, ch: _CHClient | None = None, bare: bool = False
) -> int:
    """Tombstone the live vectors each key had before the given version, the one of
    the unembedded memory row that replaces them: its memory_embeddings rows below
    that version and its entity_vectors rows created no later than that second. A
    concurrent embedded write that lands a newer version keeps its memory_embeddings
    rows, and its entity_vectors rows only when created in a later second. Residual:
    created_at is whole seconds, so a newer write's entity vectors from the same
    second are retired too; that write loses only its entity boost. `bare`: each
    memory_embeddings tombstone holds no text or vector (`_bare_tombstones`) rather than
    the row's own, so one that lands after a redaction's purge leaves nothing to scrub.
    Returns the memory_embeddings rows retired."""
    from server.core.datetimes import naive_utc_now

    if not items:
        return 0
    ch = ch or _default_client()
    now = naive_utc_now()
    if bare:
        n = await _bare_tombstones(items, ch=ch)
    else:
        n = await reemit_embedding_rows_for_keys(
            [(key, {"tombstoned_at": now, "version": int(v)}) for key, v in items], ch=ch
        )
    # entity_vectors has no version, only a created_at in whole seconds (see the residual above).
    cutoffs = {key: version_second(v) for key, v in items}
    await tombstone_entity_vectors_for_keys(list(cutoffs), ts=now, ch=ch, created_by=cutoffs)
    return n


# What a bare tombstone keeps of the row it retires: the sort key, and `created_at`, the
# partition key, so the two merge.
_BARE_TOMBSTONE_KEPT = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "scope",
    "embedding_model",
    "memory_key",
    "created_at",
)


def _bare_tombstone(r: dict[str, Any], version: int, now: datetime) -> dict[str, Any]:
    return build_embedding_row(
        **{c: r[c] for c in _BARE_TOMBSTONE_KEPT}, tombstoned_at=now, version=version
    )


async def _bare_tombstones(items: list[tuple[IdentityKey, int]], *, ch: _CHClient) -> int:
    """A tombstone with no text or vector for each live memory_embeddings row below its
    key's version, at that version, from a read of only the columns a tombstone keeps.
    Returns the rows retired."""
    now = naive_utc_now()
    pins = {key: int(v) for key, v in items}
    keys = list(pins)
    n = 0
    for start in range(0, len(keys), _HEAD_READ_BATCH):
        rows = await _live_rows_for_keys(
            ch,
            keys[start : start + _HEAD_READ_BATCH],
            columns=(*_BARE_TOMBSTONE_KEPT, "version"),
        )
        out = [
            _bare_tombstone(r, pins[identity_key(r)], now)
            for r in rows
            if int(r["version"]) < pins[identity_key(r)]
        ]
        if out:
            await insert_embedding_rows(out, ch=ch)
            n += len(out)
    return n


async def reject_embedding_rows(
    items: list[tuple[IdentityKey, int, str | None]], *, ch: _CHClient | None = None
) -> int:
    """Each key's live memory_embeddings rows below its reject version, each key read once:
    those of the given content hash copied at that version as 'rejected', every other one
    (older text, or any row when the hash is None) retired with no text, as a redaction
    may be purging it. A later 'high' write's fence or tombstone shares a copy's sort key
    at a higher version, so it wins whatever order they land in. Run it under
    `sequential_reads()`, so a row that landed on another replica is read too. Returns the
    rows written."""
    ch = ch or _default_client()
    n = await _bare_tombstones([(key, v) for key, v, sha in items if sha is None], ch=ch)
    pins = {key: (int(v), sha) for key, v, sha in items if sha is not None}
    keys = list(pins)
    now = naive_utc_now()
    for start in range(0, len(keys), _HEAD_READ_BATCH):
        rows = await _live_rows_for_keys(ch, keys[start : start + _HEAD_READ_BATCH])
        out = []
        for r in rows:
            version, sha = pins[identity_key(r)]
            if int(r["version"]) >= version:
                continue  # a newer write's
            if str(r.get("content_sha256") or "") != sha:
                out.append(_bare_tombstone(r, version, now))
            else:
                out.append(_reemitted(r, {"status": "rejected", "version": version}))
        if out:
            await insert_embedding_rows(out, ch=ch)
            n += len(out)
    return n


async def fence_vectors(
    items: list[tuple[IdentityKey, int]], *, model: str, ch: _CHClient | None = None
) -> int:
    """Write a tombstoned, vector-less row for each key at the given version, the one
    of the 'high' memory row that keeps the key unembedded. An older write's vector
    still in flight carries its own (lower) memory version, so it lands under this row
    and stays hidden, even for a key that had no vector to retire. `model` is the served
    embedder's: it is in the sort key, and only rows of that model are written. Returns
    the rows written."""
    if not items:
        return 0
    ch = ch or _default_client()
    from server.core.datetimes import naive_utc_now

    now = naive_utc_now()
    rows = [
        build_embedding_row(
            org=key[0],
            namespace=key[1],
            agent=key[2],
            user_id=key[3],
            session_id=key[4],
            memory_key=key[5],
            scope="private",
            embedding_model=model,
            embedding_dim=0,
            created_at=now,
            tombstoned_at=now,
            version=int(version),
        )
        for key, version in items
    ]
    await insert_embedding_rows(rows, ch=ch)
    return len(rows)


def version_second(version: int) -> datetime:
    """The naive-UTC second a next_version() stamp (epoch ns) was drawn in."""
    return datetime.fromtimestamp(int(version) // 1_000_000_000, UTC).replace(tzinfo=None)


def _naive_utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


async def tombstone_entity_vectors_for_keys(
    keys: list[IdentityKey],
    *,
    ts: datetime,
    ch: _CHClient | None = None,
    created_by: dict[IdentityKey, datetime] | None = None,
) -> int:
    """Re-emit the live loom.entity_vectors rows of each key with `tombstoned_at`
    (the table is RMT on created_at, so the copy keeps it and wins as the later
    insert). That table has no session column: a key's rows are matched on
    (agent, user_id, memory_key). `created_by` keeps a key's rows created after its
    cutoff (a newer write's). One read per namespace. Returns rows re-emitted."""
    from server.memory.store.sweeps import entity_vectors_live_sql

    ch = ch or _default_client()
    by_ns: dict[tuple[str, str], dict[tuple[str, str, str], datetime | None]] = {}
    for full in keys:
        org, ns, agent, user_id, _session, key = full
        wanted = by_ns.setdefault((org, ns), {})
        cutoff = created_by.get(full) if created_by else None
        prior = wanted.get((agent, user_id, key))
        if prior is not None and cutoff is not None:
            cutoff = max(prior, cutoff)  # two sessions share these rows: retire the most
        wanted[(agent, user_id, key)] = cutoff
    written = 0
    for (org, ns), wanted in by_ns.items():
        live = await ch.query_all(
            entity_vectors_live_sql(),
            params={"org": org, "ns": ns, "keys": sorted({k for _, _, k in wanted})},
        )
        rows = []
        for r in live:
            ident = (str(r.get("agent")), str(r.get("user_id")), str(r.get("memory_key")))
            if ident not in wanted:
                continue
            cutoff, created = wanted[ident], r.get("created_at")
            if (
                cutoff is not None
                and isinstance(created, datetime)
                and _naive_utc(created) > cutoff
            ):
                continue
            rows.append({**r, "tombstoned_at": ts})
        if rows:
            await ch.insert(
                "loom.entity_vectors",
                rows,
                settings=committed_insert_settings(),
            )
            written += len(rows)
    return written


async def unsupersede_embeddings(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    status: str = "active",
    ch: _CHClient | None = None,
) -> int:
    """Inverse of the supersede re-emit for one key: every live
    memory_embeddings row goes back to `status` (the restored memory row's:
    `active`, `conflicted` or `duplicate`) with the retirement
    stamp cleared, so recall sees it again. A row already so is left as it is: an Undo
    finished again writes nothing. Returns rows re-emitted."""
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
                | {"status": status, "invalid_at": None, "version": reemit_version(r)}
            )
        )
        for r in rows
        if r.get("status") != status or r.get("invalid_at") is not None
    ]
    if out:
        await insert_embedding_rows(out, ch=ch)
        await retire_copies_under_unembedded_heads(out, ch=ch)
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
    version: int,
    ch: _CHClient | None = None,
) -> int:
    """Re-emit mirror rows with `status` at `version` — an insert, never an
    in-place ALTER (those diverge under FINAL). `duplicate` hides a row from
    search, so this is a retire: `version` is the memory row's that caused it
    (or one drawn before the caller's scan), and rows at or above it are kept.
    Every other persisted column is carried forward. Returns the rows written."""
    overrides = {"status": status, "version": int(version)}
    out = [row for r in rows if (row := _copy_row(r, overrides)) is not None]
    if out:
        await insert_embedding_rows(out, ch=ch)
        await retire_copies_under_unembedded_heads(out, ch=ch)
    return len(out)


async def live_vectors(
    org: str,
    namespace: str,
    idents: list[KeyInNamespace],
    *,
    model: str,
    ch: _CHClient | None = None,
) -> dict[KeyInNamespace, list[float]]:
    """The live vector of `model` of each (agent, user_id, session_id, memory_key), read
    with sequential consistency: what a semantic admission measures a gap's rows by. A key
    keeps a live row per model; another model's is in another space."""
    from server.memory.store.sql import _SEQUENTIAL

    if not idents:
        return {}
    ch = ch or _default_client()
    # Sort-key columns, in PREWHERE as EMB_KEY_PREWHERE's are.
    rows: list[Any] = await query_all_sliced(
        ch,
        f"""
        SELECT agent, user_id, session_id, memory_key, embedding
        FROM loom.memory_embeddings FINAL
        PREWHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND embedding_model = {{model:String}}
          AND (agent, user_id, session_id, memory_key)
              IN {{ids:Array(Tuple(String, String, String, String))}}
        WHERE tombstoned_at IS NULL {_SEQUENTIAL}
        """,
        {"org": org, "ns": namespace, "ids": idents, "model": model},
        "ids",
    )
    return {key_in_namespace(r): list(r["embedding"]) for r in rows}
