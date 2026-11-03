"""Status and tombstone writes over loom.memory that carry the change onto
the embeddings mirror: version rows by id, supersession and its inverse,
consolidated dependents of a deleted source, scope clears and by-id
tombstone cascades.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now, naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.webhook_events import memory_change
from server.memory.store.embeddings import (
    mirror_status_to_embeddings,
    reemit_embedding_rows_for_keys,
)
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.reads import get_by_id
from server.memory.store.records import tombstone_decision_records
from server.memory.store.redaction import delete_by_key
from server.memory.store.rows import (
    MemoryRow,
    build_memory_version_row,
    next_version,
)
from server.observability import degradations

_log = logging.getLogger(__name__)

# Rows `write_versions_by_ids` reads concurrently.
_READ_AT_ONCE = 16  # not a setting: the fan-out of one call


async def write_versions_by_ids(
    org: str,
    memory_ids: list[str],
    update: Callable[[MemoryRow], dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
    strong: bool = False,
    on_written: Callable[[list[dict[str, Any]]], None] | None = None,
    on_unconfirmed: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    judged: bool = False,
) -> int:
    """Write a new version of each live row in `memory_ids`: `update(existing)`'s
    overrides on every persisted column carried forward, a fresh `version` unless it
    sets one. `skip` leaves a row as it is; `strong` reads read-your-writes.
    `on_written` / `on_unconfirmed` hear what the re-read confirmed or could not (see
    Lifecycle in memory-architecture.md). `judged`: the rows land a verdict reached
    after the write (the conflict resolver's) on the write it judged
    (`conflicts.judged_write`), and the status mirror copies only that write's vector
    rows (`mirror_status_to_embeddings(judged=True)`).
    Returns the rows written, or confirmed."""

    if not memory_ids:
        return 0
    ch = ch or _default_client()
    # Each row once, in the order given, at most `_READ_AT_ONCE` reads in flight.
    ids = list(dict.fromkeys(mid for mid in map(str, memory_ids) if mid))
    gate = asyncio.Semaphore(_READ_AT_ONCE)

    async def read(mid: str) -> MemoryRow | None:
        async with gate:
            return await get_by_id(mid, org=org, ch=ch, scope=WHOLE_NAMESPACE, strong=strong)

    reads = [asyncio.ensure_future(read(mid)) for mid in ids]
    try:
        found = await asyncio.gather(*reads)
    except BaseException:
        # Stop the other reads, letting any already cancelled finish their cleanup.
        for r in reads:
            if not r.cancelling():
                r.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
        raise
    rows: list[dict[str, Any]] = []
    for existing in found:
        if existing is None or (skip is not None and skip(existing)):
            continue
        overrides = dict(update(existing))
        overrides.setdefault("version", next_version())
        rows.append(build_memory_version_row(existing, **overrides))

    if not rows:
        return 0
    await insert_memory_versions(rows, ch=ch)
    confirmed = rows
    if on_written is not None:
        heads = await _head_versions(org, rows, ch=ch)
        confirmed = [r for r in rows if heads.get(_key(r)) == int(r["version"])]
        on_written(confirmed)
        # Not seen at the head yet (no key, or an older version): it may or may not land.
        unconfirmed = [r for r in rows if heads.get(_key(r), -1) < int(r["version"])]
        if unconfirmed and on_unconfirmed is not None:
            await on_unconfirmed(unconfirmed)
        # Mirrored: what was confirmed, or, with no one to reconcile the rest, all but
        # the overtaken.
        rows = (
            confirmed
            if on_unconfirmed is not None
            else [r for r in rows if heads.get(_key(r), -1) <= int(r["version"])]
        )
    # A status change on loom.memory is mirrored onto the row
    # memory.search actually reads: the pool filters and penalises on
    # memory_embeddings.status, so without the mirror it would keep
    # admitting superseded and unpenalised conflicted rows.
    status_changes = [r for r in rows if r.get("status") and r.get("status") != "active"]
    if status_changes:
        await mirror_status_to_embeddings(status_changes, ch=ch, judged=judged)
    return len(confirmed)


_IDENTITY_KEY = ("namespace", "agent", "user_id", "session_id", "memory_key")


async def _head_versions(
    org: str, rows: list[dict[str, Any]], *, ch: _CHClient
) -> dict[tuple[str, ...], int]:
    """The head version at each of `rows`' keys under FINAL, read up to twice;
    empty when both reads fail, so nothing is confirmed."""
    keys = sorted({_key(r) for r in rows})
    for attempt in (1, 2):
        try:
            heads = await ch.query_all(
                """
                SELECT namespace, agent, user_id, session_id, memory_key, version
                FROM loom.memory FINAL
                WHERE org = {org:String}
                  AND (namespace, agent, user_id, session_id, memory_key)
                      IN {keys:Array(Tuple(String, String, String, String, String))}
                SETTINGS select_sequential_consistency = 1
                """,
                params={"org": org, "keys": keys},
            )
            return {_key(h): int(h["version"]) for h in heads}
        except Exception as e:  # noqa: BLE001 — the mirror must still run for what was written
            _log.warning("write_versions_by_ids: head re-read %d failed: %s", attempt, e)
            if attempt == 1:
                await asyncio.sleep(0.05)
    return {}


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[c]) for c in _IDENTITY_KEY)


async def supersede_duplicates(
    org: str,
    victims: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
    strong: bool = False,
    on_written: Callable[[list[dict[str, Any]]], None] | None = None,
    on_unconfirmed: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    judged: bool = False,
) -> int:
    """Mark near-duplicate memories status='superseded' in BOTH loom.memory
    and loom.memory_embeddings via version re-emit (never an in-place ALTER,
    which diverges under FINAL).

    `memory.search` reads loom.memory_embeddings and keeps only status IN
    ('active','duplicate','conflicted'), so the EMBEDDINGS write is what
    removes the row from recall; the loom.memory write keeps the audit trail
    and the `superseded_by` back-pointer consistent (mirror of
    conflicts_repo.tombstone_ids).

    `victims`: dicts carrying `memory_id`, the embedding identity
    (`agent`, `user_id`, `session_id`, `memory_key`) and `superseded_by`
    (the kept row's memory_id). Empty short-circuits. A victim whose head
    row `skip` answers True for (the caller's policy, e.g. a lock) is left
    live in both tables and not counted. `strong` reads the rows read-your-writes
    (`write_versions_by_ids`). `on_written` hears the rows whose retirement the
    re-read confirmed, `on_unconfirmed` those it could not tell about
    (`write_versions_by_ids`). `judged` (a verdict reached after the write): each
    row is superseded at its read version + 1, so a write drawn after that read stays
    newer. Returns how many were written; with `on_written`, how many were confirmed."""
    if not victims:
        return 0
    ch = ch or _default_client()

    # 1) loom.memory — status + superseded_by, carrying every column forward.
    retired_at = naive_utc_now()
    version = next_version()  # the supersede's, which the vector retire carries
    by_id = {str(v["memory_id"]): str(v.get("superseded_by") or "") for v in victims}
    # `write_versions_by_ids` also mirrors the flip onto loom.memory_embeddings, the
    # table recall reads, at the supersede's version.
    return await write_versions_by_ids(
        org,
        list(by_id),
        lambda row: {
            "status": "superseded",
            "superseded_by": by_id.get(str(row.memory_id), ""),
            # record-axis retirement stamp: knowledge_time replay before this
            # instant still sees the row, after it does not
            "invalid_at": retired_at,
            "version": int(row.version) + 1 if judged else version,
        },
        ch=ch,
        skip=skip,
        strong=strong,
        on_written=on_written,
        on_unconfirmed=on_unconfirmed,
        judged=judged,
    )


async def reject_by_identity(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    memory_key: str,
    reason: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Mark the live rows of `memory_key` in every session of the identity
    `rejected`, by version re-emit on loom.memory and its vector rows. Never a
    lightweight UPDATE: on loom.memory_embeddings it leaves a patch part, and
    the fact leg's codes-first read stays off while one exists
    (`server.core.vectors.patch_parts`). Returns the rows written."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND agent = {agent:String} AND user_id = {uid:String}
          AND memory_key = {key:String} AND tombstoned_at IS NULL
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "agent": agent, "uid": user_id, "key": memory_key},
    )
    return await write_versions_by_ids(
        org,
        [str(r["memory_id"]) for r in rows],
        lambda row: {"status": "rejected", "rejection_reason": reason},
        ch=ch,
        strong=True,
    )


async def consolidated_dependents_of(
    org: str, namespace: str, source_key: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Live rows synthesised FROM `source_key`: consolidated topic memories
    (and merged canonical rows) tag every source as `src:<key>`. Returns
    identity + tags per dependent."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS mid, namespace, agent, user_id, session_id,
               memory_key, tags
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND has(tags, 'consolidated') AND has(tags, {srctag:String})
          AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "srctag": f"src:{source_key}"},
    )
    return [dict(r) for r in rows]


async def live_source_count(
    org: str, namespace: str, keys: list[str], *, ch: _CHClient | None = None
) -> int:
    if not keys:
        return 0
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT uniqExact(memory_key) AS n FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
        """,
        params={"org": org, "ns": namespace, "keys": keys},
    )
    return int((row or {}).get("n") or 0)


async def _reemit_embedding_rows(
    org: str, victims: list[dict[str, Any]], delta: dict[str, Any], *, ch: _CHClient
) -> None:
    """Re-emit each victim's embedding rows with `delta`, at their version + 1 or at
    the `version` in `delta` (a retire's; rows at or above it are kept). Recall reads
    this table."""
    items = [
        (
            (
                org,
                str(v["namespace"]),
                str(v["agent"]),
                str(v["user_id"]),
                str(v["session_id"]),
                str(v["memory_key"]),
            ),
            dict(delta),
        )
        for v in victims
    ]
    await reemit_embedding_rows_for_keys(items, ch=ch)


async def downgrade_consolidated_dependents(
    org: str, namespace: str, source_key: str, *, ch: _CHClient | None = None
) -> tuple[int, int]:
    """A source memory was deleted. Every live summary built from it either
    goes with it (none of its sources survive) or is downgraded to
    `conflicted` with a `stale-source:<key>` tag so the review queue sees
    that it now says something its evidence no longer supports. Returns
    (tombstoned, downgraded). Best effort; the caller never fails on it."""
    ch = ch or _default_client()
    deps = await consolidated_dependents_of(org, namespace, source_key, ch=ch)
    if not deps:
        return 0, 0
    now = naive_utc_now()
    to_tomb: list[dict[str, Any]] = []
    to_down: list[dict[str, Any]] = []
    for d in deps:
        srcs = [str(t)[4:] for t in (d.get("tags") or []) if str(t).startswith("src:")]
        live = await live_source_count(org, namespace, srcs, ch=ch)
        (to_tomb if live == 0 else to_down).append(d)
    if to_tomb:
        version = next_version()  # the tombstones', which the vector retire carries
        await write_versions_by_ids(
            org,
            [str(d["mid"]) for d in to_tomb],
            lambda row: {"tombstoned_at": now, "invalid_at": now, "version": version},
            ch=ch,
        )
        await _reemit_embedding_rows(
            org,
            to_tomb,
            {"tombstoned_at": now, "invalid_at": now, "version": version},
            ch=ch,
        )
    if to_down:
        stale_tag = f"stale-source:{source_key}"
        await write_versions_by_ids(
            org,
            [str(d["mid"]) for d in to_down],
            lambda row: {
                "status": "conflicted",
                "tags": [*[t for t in (row.tags or []) if t != stale_tag], stale_tag],
            },
            ch=ch,
        )
        for d in to_down:
            d_tags = [t for t in (d.get("tags") or []) if t != stale_tag] + [stale_tag]
            await _reemit_embedding_rows(
                org,
                [d],
                {"status": "conflicted", "tags": d_tags},
                ch=ch,
            )
    return len(to_tomb), len(to_down)


# Tombstoned rows one page of `bulk_tombstone_scope`'s callback carries.
# not a setting: an in-process buffer, the rows one page holds and one outbox insert takes
CLEAR_PAGE_ROWS = 1000


async def bulk_tombstone_scope(
    *,
    org: str,
    namespace: str,
    user_id: str | None = None,
    agent: str | None = None,
    on_tombstoned: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """`memory.clear`: bulk-tombstone every live memory row matching
    (org, namespace, user_id?, agent?), plus the loom.memory_embeddings rows.

    The handler requires a concrete `user_id` or `agent` other than `'-'`
    (`"*"` arrives as unset); the repo runs the query as given (a scope-less
    call would clear the whole namespace — that's `erasure.request_namespace`'s job).

    `on_tombstoned`, when given, is called after the memory mutation and before
    the embeddings one, with each page of the memory rows it tombstoned
    (identity tuple, memory_key, memory_id and version), for the handler's
    webhook events; without it no row is read. A failed read-back is noted as
    a degradation, never raised.

    Returns {table_name: rows_tombstoned}.
    """
    ch = ch or _default_client()

    scope = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    if user_id is not None:
        scope.append("user_id = {uid:String}")
        params["uid"] = user_id
    if agent is not None:
        scope.append("agent = {agent:String}")
        params["agent"] = agent
    scope_sql = " AND ".join([*scope, "tombstoned_at IS NULL"])
    # Locked rows survive a clear. memory_embeddings has no lock columns, so it excludes
    # them by full identity: a lock under one identity must not keep another's vectors live.
    # The skipped rows are counted under `locked_skipped`.
    from server.memory.domain.lock import locked_predicate, unlocked_predicate

    # One timestamp for the whole clear, so the rows it tombstoned can be read back; and a
    # bound one, as a mutation on a replicated table refuses now().
    params["at"] = naive_utc_now_seconds()
    locked = locked_predicate(now="{at:DateTime}")
    locked_row = await ch.query_one(
        f"SELECT count() AS n FROM loom.memory FINAL WHERE {scope_sql} AND {locked}",
        params=params,
    )
    locked_n = int((locked_row or {}).get("n", 0) or 0)
    counts = {
        "loom.memory": await _tombstone_where(
            "loom.memory",
            f"{scope_sql} AND {unlocked_predicate(now='{at:DateTime}')}",
            params,
            ch=ch,
        )
    }
    # Announced before the embeddings step, so a failure there cannot lose
    # the events: a retry finds no live memory rows and announces nothing.
    if on_tombstoned is not None and counts["loom.memory"]:
        try:
            await _page_tombstoned(" AND ".join(scope), params, on_tombstoned, ch=ch)
        except Exception as e:  # noqa: BLE001 — the rows are tombstoned; the clear must not fail
            degradations.note("memory.webhook", "not_announced", e)
    parts = (
        await _unlocked_parts(scope_sql, locked, params, ch=ch) if locked_n else [(scope_sql, {})]
    )
    counts["loom.memory_embeddings"] = 0
    for where_sql, extra in parts:
        counts["loom.memory_embeddings"] += await _tombstone_where(
            "loom.memory_embeddings", where_sql, {**params, **extra}, ch=ch
        )
    counts["locked_skipped"] = locked_n
    return counts


async def _tombstone_where(
    table: str, where_sql: str, params: dict[str, Any], *, ch: _CHClient
) -> int:
    """Tombstone the live rows of `table` that match, and return how many there were."""
    row = await ch.query_one(
        f"SELECT count() AS n FROM {table} FINAL WHERE {where_sql}", params=params
    )
    n = int((row or {}).get("n", 0) or 0)
    if n:
        await ch.execute(
            f"""
            ALTER TABLE {table}
            UPDATE tombstoned_at = {{at:DateTime}}
            WHERE {where_sql}
            SETTINGS mutations_sync = 2
            """,
            params=params,
        )
    return n


# ~21 bytes per rendered hash: a bucket's list stays well inside one 128 KiB field
# and small in Keeper.
_LOCKED_PER_MUTATION = 2_500  # not a setting: a bound of the ClickHouse form field
_IDENTITY_HASH = "cityHash64(agent, user_id, session_id, memory_key)"


async def _unlocked_parts(
    scope_sql: str, locked: str, params: dict[str, Any], *, ch: _CHClient
) -> list[tuple[str, dict[str, Any]]]:
    """Predicates, with their params, that together cover the embedding rows whose identity
    no locked memory row in scope shares. A mutation takes no subquery, so the locked
    identities are read first and named by hash; past `_LOCKED_PER_MUTATION` the scope is
    split into hash buckets, each naming only its own locked identities."""
    rows = await ch.query_all(
        f"SELECT DISTINCT {_IDENTITY_HASH} AS h FROM loom.memory FINAL"
        f" WHERE {scope_sql} AND {locked}",
        params=params,
    )
    hashes = sorted({int(r["h"]) for r in rows})
    buckets = max(1, math.ceil(len(hashes) / _LOCKED_PER_MUTATION))
    unlocked = f"{scope_sql} AND {_IDENTITY_HASH} NOT IN {{locked:Array(UInt64)}}"
    parts = []
    for b in range(buckets):
        where = unlocked
        extra: dict[str, Any] = {"locked": [h for h in hashes if h % buckets == b]}
        if buckets > 1:
            where += f" AND {_IDENTITY_HASH} % {{buckets:UInt32}} = {{bucket:UInt32}}"
            extra |= {"buckets": buckets, "bucket": b}
        parts.append((where, extra))
    return parts


async def _page_tombstoned(
    scope_sql: str,
    params: dict[str, Any],
    on_page: Callable[[list[dict[str, Any]]], Awaitable[None]],
    *,
    ch: Any,  # a CHClient: `raw_stream` is not on `_CHClient`
) -> None:
    """Hand `on_page` the memory rows in scope tombstoned at `params["at"]`, a page
    at a time in sort-key order. One streamed read, so the scope is scanned once
    however many pages it fills. A row a concurrent delete tombstoned in the same
    second comes too, at the version that delete announced, so its event repeats
    that one's id."""
    sql = f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, memory_id, version
        FROM loom.memory FINAL
        WHERE {scope_sql} AND tombstoned_at = {{at:DateTime}}
        ORDER BY agent, user_id, session_id, memory_key
        FORMAT JSONEachRow
    """
    page: list[dict[str, Any]] = []
    partial = b""  # a row split across two chunks
    async with ch.raw_stream(sql, params) as chunks:
        async for chunk in chunks:
            *lines, partial = (partial + chunk).split(b"\n")
            for line in lines:
                if not line:
                    continue
                row = json.loads(line)
                row["version"] = int(row["version"])  # JSONEachRow quotes a UInt64
                page.append(row)
                if len(page) == CLEAR_PAGE_ROWS:
                    await on_page(page)
                    page = []
    if page:
        await on_page(page)


async def tombstone_cascade_by_ids(
    org: str,
    memory_ids: list[str],
    *,
    when: datetime,
    update: Callable[[MemoryRow], dict[str, Any]] | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Tombstone rows by memory_id AND mirror the tombstone onto every tier
    search reads.

    A tombstone that reaches only `loom.memory` is not a tombstone: search
    reads `loom.memory_embeddings` on its own predicates and never joins the
    memory table, so a row killed here alone keeps surfacing as an ANN
    candidate. This helper is the one funnel for by-id tombstones:

      1. a new RMT version per row with status='tombstoned', tombstoned_at
         set and expires_at cleared (every persisted column carried by
         build_memory_version_row; never an ALTER UPDATE, which mutates
         every version in place and skips the version discipline);
      2. the status mirror `write_versions_by_ids` runs, at that version, so
         the embeddings table collapses onto a tombstoned version too.

    `update(existing)` may add overrides (an audit note on source_trace).
    The embeddings mirror is best-effort: the memory tombstone is
    durable and the orphan_vector_reconcile worker heals a missed mirror.
    Returns each tombstoned row's identity tuple, memory_key, memory_id and
    tombstone version (`memory_change`), for the caller's webhook events."""
    ch = ch or _default_client()
    rows: list[MemoryRow] = []
    seen: set[str] = set()
    for memory_id in memory_ids:
        mid = str(memory_id)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        existing = await get_by_id(mid, org=org, ch=ch, scope=WHOLE_NAMESPACE)
        if existing is not None:
            rows.append(existing)
    if not rows:
        return []

    # Drawn after the rows are read, so a write that landed before the read sorts below it.
    version = next_version()  # the tombstones', which the vector retire carries
    tombstoned: list[dict[str, Any]] = []

    def _tombstone(existing: MemoryRow) -> dict[str, Any]:
        tombstoned.append(memory_change(existing, version))
        overrides: dict[str, Any] = {
            "status": "tombstoned",
            "tombstoned_at": when,
            "expires_at": None,
            "version": version,
        }
        if update is not None:
            overrides.update(update(existing))
        return overrides

    # `write_versions_by_ids` mirrors the tombstone onto memory_embeddings at its version.
    await write_versions_by_ids(
        org,
        [r.memory_id for r in rows],
        _tombstone,
        ch=ch,
    )
    # A decision's record goes with its writer's row, as on memory.delete: one statement
    # per namespace.
    decision_keys: dict[str, set[str]] = {}
    for r in rows:
        if r.memory_type == "decision":
            decision_keys.setdefault(r.namespace, set()).add(r.memory_key)
    for namespace, keys in decision_keys.items():
        try:
            await tombstone_decision_records(org, namespace, sorted(keys), version=version, ch=ch)
        except Exception as e:  # noqa: BLE001 — the row tombstones stand
            _log.warning(
                "decision records not retired in %s/%s for %s: %s", org, namespace, sorted(keys), e
            )
    return tombstoned


async def tombstone_by_id(
    org: str,
    memory_id: str,
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Tombstone one row by memory_id (UUID), cascading to the embeddings
    tier (see tombstone_cascade_by_ids), and return what that returns.

    Used by `memory.dedup_resolve` when the decision is to kill one
    side of the duplicate pair, keyed on the surrogate UUID rather
    than the identity tuple."""
    return await tombstone_cascade_by_ids(
        org,
        [memory_id],
        when=naive_utc_now(),
        ch=ch,
    )


async def delete_procedure_runs_by_key(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> int:
    """Hard-delete one procedure's run ledger. Called when the procedure row
    itself is deleted so its history does not outlive it."""
    return await delete_by_key(
        "loom.procedure_runs",
        "org = {org:String} AND namespace = {ns:String} AND memory_key = {key:String}",
        {"org": org, "ns": namespace, "key": memory_key},
        ch=ch,
    )
