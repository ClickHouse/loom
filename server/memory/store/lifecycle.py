"""Status and tombstone writes over loom.memory that carry the change onto
the embeddings mirror: version rows by id, supersession and its inverse,
consolidated dependents of a deleted source, scope clears and by-id
tombstone cascades.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.errors import PreconditionFailedError
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.temporal_capture import declare_fields, left_out_reasons, row_key
from server.memory.store.embeddings import (
    mirror_status_to_embeddings,
    reemit_embedding_rows_for_keys,
)
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.reads import get_by_id
from server.memory.store.redaction import delete_by_key
from server.memory.store.rows import (
    MemoryRow,
    build_memory_version_row,
    next_version,
)

_log = logging.getLogger(__name__)


async def write_versions_by_ids(
    org: str,
    memory_ids: list[str],
    update: Callable[[MemoryRow], dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
) -> int:
    """Write new RMT versions for live rows addressed by memory_id.

    `update(existing)` returns the column overrides for that row. The helper
    always assigns a fresh `version` unless the callback explicitly supplies
    one, and it carries every persisted column forward via
    build_memory_version_row. This is the preferred replacement for
    in-place status flips on `loom.memory`. A row `skip(existing)` answers
    True for is left as it is.
    """

    if not memory_ids:
        return 0
    ch = ch or _default_client()
    heads: list[MemoryRow] = []
    for mid in dict.fromkeys(str(m) for m in memory_ids if m):
        existing = await get_by_id(mid, org=org, ch=ch, scope=WHOLE_NAMESPACE)
        if existing is not None:
            heads.append(existing)
    written, _ = await write_versions_of_rows(heads, update, ch=ch, skip=skip)
    return written


async def write_versions_of_rows(
    heads: list[MemoryRow],
    update: Callable[[MemoryRow], dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
) -> tuple[int, int]:
    """`write_versions_by_ids` for heads already read. Returns (version rows the
    journal kept, embedding rows mirrored); a row it left out (a retry the key
    has moved past) is neither counted nor mirrored."""
    ch = ch or _default_client()
    kept = await rewrite_heads(heads, update, ch=ch, skip=skip)
    # A status change on loom.memory is mirrored onto the row memory.search actually
    # reads: the pool filters and penalises on memory_embeddings.status, so without
    # the mirror it would keep admitting superseded and unpenalised conflicted rows.
    status_changes = [r for r in kept if r.get("status") and r.get("status") != "active"]
    mirrored = await mirror_status_to_embeddings(status_changes, ch=ch) if status_changes else 0
    return len(kept), mirrored


async def rewrite_heads(
    heads: Sequence[MemoryRow],
    update: Callable[[MemoryRow], dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
    reasons: dict[str, str] | None = None,
) -> list[Any]:
    """A new version of each head with `update(head)` over it; returns the rows the
    journal kept. The columns `update` sets are declared: the journal builds each
    row from the key as it stands then, with only those set, so a change never
    carries a stale lock, pin, expiry or value over a newer one. `reasons`
    receives, per memory_key, why a row was left out."""
    rows: list[tuple[dict[str, Any], frozenset[str]]] = []
    for h in heads:
        if skip is not None and skip(h):
            continue
        overrides = update(h)
        rows.append((build_memory_version_row(h, **_with_version(overrides)), frozenset(overrides)))
    return await insert_over_heads(rows, ch=ch, reasons=reasons)


def not_written(memory_key: str, op: str, reasons: dict[str, str]) -> PreconditionFailedError:
    """The refusal for a metadata write the journal left out, naming why."""
    why = reasons.get(memory_key) or "left out"
    return PreconditionFailedError(f"{memory_key!r}: {op} not written ({why}); re-read and retry")


async def insert_over_heads(
    rows: Sequence[tuple[dict[str, Any], frozenset[str]]],
    *,
    ch: _CHClient | None = None,
    reasons: dict[str, str] | None = None,
) -> list[Any]:
    """Insert metadata version rows, each with the columns it sets; returns the rows
    the journal kept (`rewrite_heads`)."""
    kept: list[Any] = []

    async def _keep(kept_rows: Any) -> None:
        kept.extend(kept_rows)

    if not rows:
        return kept
    declared = {row_key(r): fields - _STAMPS for r, fields in rows}
    with declare_fields(declared), left_out_reasons() as why:
        await insert_memory_versions(
            [r for r, _ in rows], ch=ch or _default_client(), alongside=[_keep]
        )
    if reasons is not None:
        landed = {id(r) for r in kept}
        reasons.update(
            {str(r["memory_key"]): why.get(row_key(r), "") for r, _ in rows if id(r) not in landed}
        )
    return kept


# A version row's own stamps, never "set" by a metadata write.
_STAMPS = frozenset({"version", "updated_at"})


def _with_version(overrides: dict[str, Any]) -> dict[str, Any]:
    return {"version": next_version(), **overrides}


def _version_of(head: MemoryRow) -> int:
    """The version a head was read at (0 for a row built without one)."""
    return int(getattr(head, "version", 0) or 0)


async def supersede_duplicates(
    org: str,
    victims: list[dict[str, Any]],
    *,
    ch: _CHClient | None = None,
    skip: Callable[[MemoryRow], bool] | None = None,
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
    live in both tables and not counted. Returns how many were retired."""
    if not victims:
        return 0
    ch = ch or _default_client()

    # 1) loom.memory — status + superseded_by, carrying every column forward.
    retired_at = naive_utc_now()
    version = next_version()  # the supersede's, which the vector retire carries
    by_id = {str(v["memory_id"]): str(v.get("superseded_by") or "") for v in victims}
    skipped: set[str] = set()

    def _skip(row: MemoryRow) -> bool:
        if skip is not None and skip(row):
            skipped.add(str(row.memory_id))
            return True
        return False

    await write_versions_by_ids(
        org,
        list(by_id),
        lambda row: {
            "status": "superseded",
            "superseded_by": by_id.get(str(row.memory_id), ""),
            # record-axis retirement stamp: knowledge_time replay before this
            # instant still sees the row, after it does not
            "invalid_at": retired_at,
            "version": version,
        },
        ch=ch,
        skip=_skip,
    )
    victims = [v for v in victims if str(v["memory_id"]) not in skipped]
    # 2) loom.memory_embeddings — the write recall actually reads.
    #    `write_versions_by_ids` mirrored the flip there at the supersede's version.
    return len(victims)


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


async def live_unlocked_identities(
    *,
    org: str,
    namespace: str,
    user_id: str | None = None,
    agent: str | None = None,
    ch: _CHClient | None = None,
) -> list[tuple[tuple[str, str, str, str], str]]:
    """The (identity, memory_id) of every live, unlocked row `memory.clear` clears."""
    from server.memory.domain.lock import unlocked_predicate

    ch = ch or _default_client()
    where = ["org = {org:String}", "namespace = {ns:String}", "tombstoned_at IS NULL"]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    if user_id is not None:
        where.append("user_id = {uid:String}")
        params["uid"] = user_id
    if agent is not None:
        where.append("agent = {agent:String}")
        params["agent"] = agent
    rows = await ch.query_all(
        "SELECT agent, user_id, session_id, memory_key, toString(memory_id) AS memory_id "
        f"FROM loom.memory FINAL WHERE {' AND '.join(where)} AND {unlocked_predicate()}",
        params=params,
    )
    return [
        (
            (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"])),
            str(r["memory_id"]),
        )
        for r in rows
    ]


async def locked_in_scope(
    *,
    org: str,
    namespace: str,
    user_id: str | None = None,
    agent: str | None = None,
    ch: _CHClient | None = None,
) -> int:
    """Live rows in (org, namespace, user_id?, agent?) under a lock in force: what
    a `memory.clear` leaves behind."""
    from server.memory.domain.lock import locked_predicate

    ch = ch or _default_client()
    where = ["org = {org:String}", "namespace = {ns:String}", "tombstoned_at IS NULL"]
    params: dict[str, Any] = {"org": org, "ns": namespace}
    if user_id is not None:
        where.append("user_id = {uid:String}")
        params["uid"] = user_id
    if agent is not None:
        where.append("agent = {agent:String}")
        params["agent"] = agent
    row = await ch.query_one(
        f"SELECT count() AS n FROM loom.memory FINAL WHERE {' AND '.join(where)} "
        f"AND {locked_predicate()}",
        params=params,
    )
    return int((row or {}).get("n") or 0)


async def tombstone_cascade_by_ids(
    org: str,
    memory_ids: list[str],
    *,
    when: datetime,
    update: Callable[[MemoryRow], dict[str, Any]] | None = None,
    ch: _CHClient | None = None,
) -> int:
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
    Returns the number of memory rows tombstoned."""
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
        return 0

    version = next_version()  # the tombstones', which the vector retire carries

    def _tombstone(existing: MemoryRow) -> dict[str, Any]:
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
    return await write_versions_by_ids(
        org,
        [r.memory_id for r in rows],
        _tombstone,
        ch=ch,
    )


async def tombstone_by_id(
    org: str,
    memory_id: str,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Tombstone one row by memory_id (UUID), cascading to the embeddings
    tier (see tombstone_cascade_by_ids).

    Used by `memory.dedup_resolve` when the decision is to kill one
    side of the duplicate pair, keyed on the surrogate UUID rather
    than the identity tuple."""
    await tombstone_cascade_by_ids(
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
