"""Usage fold-back worker.

Folds the SummingMergeTree usage totals (`loom.memory_usage_totals`) onto
the denormalized columns that ranking/promotion read:
`loom.memory_reinforcement.reinforcement_count` / `last_reinforced_at`
(search boost; a keyed side table the fact leg reads after
its fetch, so the vector table takes no lightweight UPDATE and keeps its
codes-first lazy read) and `loom.memory.evidence_count` / `recall_count` /
`last_recalled_at` (list score + auto-promote gate).

Every target is an *absolute* value recomputed from the full totals,
never an increment — so re-running the same totals is a fixed point and
crashes/restarts/overlapping passes converge instead of double-counting:

  - reinforcement_count := max(base, base + recall_total + evidence_total
    + used_weight * used_total + used_cited_weight * used_cited_total
    - harm_weight * harm_total), base being
    memory.reinforcement_count, the write-time value (never written here),
    which is what makes the absolute target derivable with no bookkeeping
    table. Evidence (positive memory.outcome) is folded in too so outcome
    drives the search-side reinforcement boost, not just the promote gate.
    `used` (the agent relied on the row — run.complete / memory.outcome
    outcome="used") is weighted by LOOM_MEMORY_USED_REINFORCEMENT_WEIGHT: a
    returned row is not a used row, and the boost should follow use, not
    exposure. `used_cited` (memory.ask's own credit for a key
    its answer cited) is weighted by
    LOOM_MEMORY_USED_CITED_REINFORCEMENT_WEIGHT (None = the `used` weight) and
    is read from the events table beside the identity row, since the totals
    view maps only `used`. `harm` (relied on, and the run ended badly)
    is weighted by LOOM_MEMORY_HARM_REINFORCEMENT_WEIGHT and subtracts; the
    floor at base means harm can cancel earned reinforcement but never
    pushes a row below its cosine rank.
  - last_reinforced_at, last_recalled_at := greatest(existing, live max).
  - recall_count := greatest(existing, recall_total).
  - evidence_count := greatest(existing, 1 + evidence_total) — the +1 is
    the schema DEFAULT write-time credit.

greatest() preserves bulk-import-seeded counts until live traffic passes
them.

Two shapes matter for correctness and cost:

  - The identity lookup selects `toString(memory_id) AS mid`, never
    `AS memory_id`. A same-named alias shadows the column in WHERE, so
    `memory_id IN {ids:Array(UUID)}` would compare a String alias to UUIDs
    and match nothing, with green tests. `tests/unit/test_sql_alias_shadow.py`
    fails the build on that pattern anywhere under server/.
  - Writes are batched: one UPDATE per chunk of targets for `loom.memory`
    carrying its per-row targets as parallel arrays through `transform()`,
    and one INSERT per chunk into `loom.memory_reinforcement` (not a
    lightweight UPDATE on `loom.memory_embeddings`, whose patch parts would
    switch the codec's lazy read off for every fact leg). A pass of 1,000
    memories is
    a handful of statements instead of 2,000; on ClickHouse Cloud each
    lightweight UPDATE is a patch part and the cost is per statement, not
    per row.

The watermark is process-local. On a cold start it is derived from the
destination tables (the earliest `bumped_at` whose totals exceed what
`loom.memory` already carries) instead of the epoch, so a redeploy does
not re-walk the whole history at the batch cap before reaching live
rows, and a pass keeps taking batches until it is caught up or its time
budget is spent.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from clickhouse_connect.driver.external import ExternalData

from server.core.ch_names import split_table
from server.core.datetimes import UNIX_EPOCH
from server.memory.store.usage import SERVED_EVENT
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Rows per UPDATE statement; keeps the transform() arrays and the IN list bounded.
UPDATE_CHUNK_ROWS = 500
# ClickHouse error codes that mean "lightweight UPDATE is not available here"
# (syntax / support disabled / not implemented). Only these earn the ALTER
# UPDATE fallback; a read-only replica (242), a Keeper session (999) or an
# unfinished load (341) is a transient the next pass retries — turning it into
# a heavy mutation queued in Keeper makes a Keeper incident worse.
_LIGHTWEIGHT_UNSUPPORTED_CODES = (48, 62, 344)

_UINT32_MAX = 2**32 - 1

_watermark: datetime = UNIX_EPOCH
_watermark_derived: bool = False


class _CHClient(Protocol):
    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None: ...

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        settings: dict[str, Any] | None = None,
        external_data: ExternalData | None = None,
    ) -> None: ...

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class UsageFoldConfig:
    max_memories_per_pass: int = field(
        default_factory=lambda: settings.memory_usage_fold_max_memories_per_pass
    )
    pass_time_budget_seconds: float = field(
        default_factory=lambda: settings.memory_usage_fold_pass_time_budget_s
    )
    update_chunk_rows: int = UPDATE_CHUNK_ROWS


@dataclass
class UsageFoldResult:
    candidates: int = 0
    folded: int = 0
    # Totals rows whose memory no longer exists (tombstoned/erased) —
    # nothing to fold onto; the watermark advances past them.
    skipped_no_memory: int = 0
    batches: int = 0
    statements: int = 0
    watermark: datetime = UNIX_EPOCH
    # Unfinished mutations seen on the destination table when the pass was
    # skipped for backpressure (0 = the pass ran).
    skipped_pending_mutations: int = 0
    # The `acted` derivation of this pass — (run, key) pairs
    # the join found, events emitted, the statement's wall clock.
    acted_candidates: int = 0
    acted_emitted: int = 0
    acted_ms: int = 0


def reset_watermark() -> None:
    """Reset the process-local watermarks (boot state). Test hook."""
    global _watermark, _watermark_derived, _acted_watermark, _acted_watermark_derived
    global _credits_warned
    _watermark = UNIX_EPOCH
    _watermark_derived = False
    _acted_watermark = UNIX_EPOCH
    _acted_watermark_derived = False
    _acted_recent.clear()
    _credits_warned = False


def current_watermark() -> datetime:
    return _watermark


def _utc(dt: datetime | None) -> datetime:
    """Normalize a CH-returned datetime to tz-aware UTC."""
    if dt is None:
        return UNIX_EPOCH
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _u32(n: int) -> int:
    return max(0, min(int(n), _UINT32_MAX))


def _lightweight_unsupported(e: BaseException) -> bool:
    """True only when the server says lightweight UPDATE itself is
    unavailable. Anything else — read-only replica, Keeper session, load
    still in progress, a genuine SQL error in our statement — propagates so the
    pass fails closed and retries next time from the same watermark."""
    msg = str(e)
    return any(f"Code: {code}." in msg for code in _LIGHTWEIGHT_UNSUPPORTED_CODES)


async def _apply_update(
    ch: _CHClient, table: str, set_clause: str, where: str, params: dict[str, Any]
) -> None:
    """Lightweight UPDATE; ALTER UPDATE only where lightweight is unsupported."""
    try:
        await ch.execute(f"UPDATE {table} SET {set_clause} WHERE {where}", params=params)
    except Exception as e:
        if not _lightweight_unsupported(e):
            raise
        await ch.execute(
            f"ALTER TABLE {table} UPDATE {set_clause} WHERE {where} SETTINGS mutations_sync = 0",
            params=params,
        )


async def _pending_mutations(ch: _CHClient, table: str) -> int:
    """Unfinished mutations on `table` at the connected replica; 0 when the
    probe itself fails (never block folding on an observability read)."""
    db, name = split_table(table)
    try:
        row = await ch.query_one(
            "SELECT count() AS n FROM system.mutations "
            "WHERE database = {db:String} AND table = {t:String} AND NOT is_done",
            params={"db": db, "t": name},
        )
    except Exception:  # noqa: BLE001 — a failed probe must not stop the fold
        return 0
    return int((row or {}).get("n") or 0)


def _used_weight() -> int:
    """How many recall-equivalents one `used` event is worth (settings)."""
    return max(0, int(settings.memory_used_reinforcement_weight))


def _used_cited_weight() -> int:
    """How many recall-equivalents one `used_cited` event (memory.ask's own
    credit for a key its answer cited) is worth. None (the
    default) = the `used` weight, so the split changes nothing by default; an
    explicit 0 records the self-credit without letting it rank."""
    raw = settings.memory_used_cited_reinforcement_weight
    return _used_weight() if raw is None else max(0, int(raw))


def _harm_weight() -> int:
    """How many recall-equivalents one `harm` event takes away (settings).
    None (the default) = the `used` weight, so the loop is symmetric; an
    explicit 0 counts harm without charging it."""
    raw = settings.memory_harm_reinforcement_weight
    return _used_weight() if raw is None else max(0, int(raw))


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass
class _Target:
    """One memory's absolute fold targets, resolved against its identity row."""

    memory_id: str
    org: str
    namespace: str
    agent: str
    user_id: str
    memory_key: str
    evidence: int  # loom.memory.evidence_count floor (1 + evidence_total)
    recall: int  # loom.memory.recall_count floor
    last_recalled: datetime  # UNIX_EPOCH when never recalled
    reinforcement: int | None  # None: no reinforcing activity, embeddings untouched
    last_activity: datetime
    # False when loom.memory already carries every counter above, so its UPDATE is skipped.
    # A bump from an event the totals do not count (`served`, `acted`, ...) re-folds the same
    # absolute values, and a lightweight UPDATE that changes nothing still costs a patch part.
    changes_memory: bool = True


def _targets(rows: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> list[_Target]:
    out: list[_Target] = []
    used_w = _used_weight()
    used_cited_w = _used_cited_weight()
    harm_w = _harm_weight()
    for row in rows:
        ident = by_id.get(row["memory_id"])
        if ident is None:
            continue
        recall_total = int(row.get("recall_total") or 0)
        evidence_total = int(row.get("evidence_total") or 0)
        used_total = int(row.get("used_total") or 0)
        # memory.ask's cited-key credit rides the identity row
        # (the totals view maps only `used` into used_count; `used_cited` is
        # summed from the events beside the identity read, one statement).
        used_cited_total = int(ident.get("used_cited_total") or 0)
        harm_total = int(row.get("harm_total") or 0)
        last_recalled = _utc(row.get("last_recalled"))
        # Harm counts as activity so the floor below is applied, not skipped.
        reinforcing = (
            recall_total > 0
            or evidence_total > 0
            or used_total > 0
            or used_cited_total > 0
            or harm_total > 0
        )
        base_rc = int(ident.get("reinforcement_count") or 1)
        evidence = _u32(1 + evidence_total)
        recall = _u32(recall_total)
        stored_lra = ident.get("last_recalled_at")
        changes_memory = (
            evidence > int(ident.get("evidence_count") or 0)
            or recall > int(ident.get("recall_count") or 0)
            or (
                last_recalled > UNIX_EPOCH
                and (stored_lra is None or last_recalled > _utc(stored_lra))
            )
        )
        out.append(
            _Target(
                memory_id=str(row["memory_id"]),
                org=str(ident["org"]),
                namespace=str(ident["namespace"]),
                agent=str(ident["agent"]),
                user_id=str(ident["user_id"]),
                memory_key=str(ident["memory_key"]),
                evidence=evidence,
                recall=recall,
                last_recalled=last_recalled,
                # Harm subtracts, floored at the write-time base: a memory the
                # agent relied on in failed runs loses the reinforcement it
                # earned, never its cosine rank, and is never deleted here.
                reinforcement=(
                    _u32(
                        max(
                            base_rc,
                            base_rc
                            + recall_total
                            + evidence_total
                            + used_w * used_total
                            + used_cited_w * used_cited_total
                            - harm_w * harm_total,
                        )
                    )
                    if reinforcing
                    else None
                ),
                # The rank's decay anchor: the last event other than `served`, read beside
                # the identity row (`bumped` moves on `served` too).
                last_activity=max(last_recalled, _utc(ident.get("last_counted_at"))),
                changes_memory=changes_memory,
            )
        )
    return out


async def _fold_memory_rows(ch: _CHClient, targets: list[_Target], chunk: int) -> int:
    """loom.memory: ONE UPDATE per chunk of targets across every (org,
    namespace) — the WHERE carries the identity pairs, transform() picks each
    row's own value by memory_id, so a pass touching 60 namespaces issues one
    mutation, not 60."""
    statements = 0
    for part in _chunks(targets, chunk):
        ids = [t.memory_id for t in part]
        # last_recalled_at is Nullable: never-recalled rows must stay NULL,
        # so the sentinel epoch means "leave the column alone".
        lras = [t.last_recalled for t in part]
        set_clause = (
            "evidence_count = greatest(evidence_count, "
            "transform(toString(memory_id), {ids:Array(String)}, {evs:Array(UInt32)}, "
            "toUInt32(0))), "
            "recall_count = greatest(recall_count, "
            "transform(toString(memory_id), {ids:Array(String)}, {rcls:Array(UInt32)}, "
            "toUInt32(0))), "
            "last_recalled_at = multiIf("
            "transform(toString(memory_id), {ids:Array(String)}, {lras:Array(DateTime)}, "
            "toDateTime(0)) = toDateTime(0), last_recalled_at, "
            "last_recalled_at IS NULL, "
            "transform(toString(memory_id), {ids:Array(String)}, {lras:Array(DateTime)}, "
            "toDateTime(0)), "
            "greatest(last_recalled_at, "
            "transform(toString(memory_id), {ids:Array(String)}, {lras:Array(DateTime)}, "
            "toDateTime(0))))"
        )
        await _apply_update(
            ch,
            "loom.memory",
            set_clause,
            "(org, namespace) IN {pairs:Array(Tuple(String, String))} "
            "AND toString(memory_id) IN {ids:Array(String)} AND tombstoned_at IS NULL",
            {
                "pairs": sorted({(t.org, t.namespace) for t in part}),
                "ids": ids,
                "evs": [t.evidence for t in part],
                "rcls": [t.recall for t in part],
                "lras": lras,
            },
        )
        statements += 1
    return statements


async def _fold_reinforcement_rows(ch: _CHClient, targets: list[_Target], chunk: int) -> int:
    """loom.memory_reinforcement: one INSERT per chunk of the
    targets that carry reinforcing activity, one row per fold identity (org,
    namespace, agent, user_id, memory_key; every session's row of the key
    reads the same value). The row carries the ABSOLUTE target, so a re-fold collapses to
    itself under the table's ReplacingMergeTree(version). Nothing here
    touches loom.memory_embeddings: a lightweight UPDATE there leaves a
    patch part that switches the fact leg's codes-first read off."""
    from server.memory.app.reinforcement import build_reinforcement_row, insert_reinforcement_rows

    live = [t for t in targets if t.reinforcement is not None]
    if not live:
        return 0  # no reinforcing activity of any kind — nothing to fold
    statements = 0
    for part in _chunks(live, chunk):
        # One key may carry several memory_ids (versions); all targets for a
        # key are absolute so any is correct, and the newest version wins.
        rows = [
            build_reinforcement_row(
                org=t.org,
                namespace=t.namespace,
                agent=t.agent,
                user_id=t.user_id,
                memory_key=t.memory_key,
                reinforcement_count=int(t.reinforcement or 1),
                last_reinforced_at=t.last_activity.replace(tzinfo=None)
                if t.last_activity.tzinfo is not None
                else t.last_activity,
            )
            for t in part
        ]
        await insert_reinforcement_rows(rows, ch=ch)
        statements += 1
    return statements


async def _derive_cold_start_watermark(ch: _CHClient, hwm: datetime) -> datetime:
    """Where to start after a restart: just before the earliest totals row
    whose recall/evidence exceed what `loom.memory` already carries. Targets
    are absolute, so the join is exact for the two counters that live on
    `loom.memory`; `used`-only activity has no destination counter there
    and re-folds when its row is next bumped. No unfolded rows → the high
    water mark (nothing to do)."""
    row = await ch.query_one(
        """
        SELECT min(t.bumped) AS first_unfolded
        FROM (
            SELECT memory_id,
                   sum(recall_count)   AS recall_total,
                   sum(evidence_count) AS evidence_total,
                   max(bumped_at)      AS bumped
            FROM loom.memory_usage_totals
            GROUP BY memory_id
        ) AS t
        INNER JOIN (
            SELECT memory_id, recall_count, evidence_count
            FROM loom.memory FINAL
            WHERE tombstoned_at IS NULL
        ) AS m ON m.memory_id = t.memory_id
        WHERE t.recall_total > m.recall_count
           OR (1 + t.evidence_total) > m.evidence_count
        """
    )
    first = _utc((row or {}).get("first_unfolded"))
    if first <= UNIX_EPOCH:
        return hwm
    return first - timedelta(seconds=1)


# The identity read of a batch: memory_id → fold identity + write-time base,
# with memory.ask's `used_cited` credit summed from the events
# table beside it. The totals view (memory_usage_totals_mv) maps only `used`
# into used_count, so the self-credit is read here,
# keyed on the same ids in one statement. `AS mid`, never
# `AS memory_id`.
IDENTITY_STATEMENT = """
        SELECT toString(m.memory_id) AS mid, m.org AS org, m.namespace AS namespace,
               m.agent AS agent, m.user_id AS user_id, m.memory_key AS memory_key,
               m.reinforcement_count AS reinforcement_count,
               m.evidence_count AS evidence_count, m.recall_count AS recall_count,
               m.last_recalled_at AS last_recalled_at,
               coalesce(c.used_cited_total, 0) AS used_cited_total,
               c.last_counted_at AS last_counted_at
        FROM (
            SELECT memory_id, org, namespace, agent, user_id, memory_key, reinforcement_count,
                   evidence_count, recall_count, last_recalled_at
            FROM loom.memory FINAL
            WHERE org IN {orgs:Array(String)}
              AND namespace IN {nss:Array(String)}
              AND memory_id IN {ids:Array(UUID)}
              AND tombstoned_at IS NULL
        ) AS m
        LEFT JOIN (
            SELECT org, namespace, memory_id,
                   sumIf(event_count, event_type = 'used_cited') AS used_cited_total,
                   max(ts) AS last_counted_at
            FROM loom.memory_usage_events
            WHERE org IN {orgs:Array(String)}
              AND namespace IN {nss:Array(String)}
              AND memory_id IN {ids:Array(UUID)}
              AND event_type != {served:String}
            GROUP BY org, namespace, memory_id
        ) AS c ON c.org = m.org AND c.namespace = m.namespace AND c.memory_id = m.memory_id
"""


async def _fold_batch(ch: _CHClient, cfg: UsageFoldConfig, result: UsageFoldResult) -> bool:
    """One batch past the watermark. Returns True when a full batch was
    taken (there may be more), False when the backlog is drained."""
    global _watermark
    # Backpressure: while the destination already carries a
    # queue of unfinished mutations, folding more only lengthens it. Skip the
    # pass; the watermark stays, so the next pass folds the same rows.
    pending = await _pending_mutations(ch, "loom.memory")
    if pending > settings.memory_usage_fold_max_pending_mutations:
        result.skipped_pending_mutations = pending
        log.warning(
            "usage_fold: %d unfinished mutations on loom.memory "
            "(cap %d); skipping this pass, watermark held at %s",
            pending,
            settings.memory_usage_fold_max_pending_mutations,
            _watermark.isoformat(),
        )
        return False
    # Absolute totals per memory. The HAVING filter must come AFTER
    # the GROUP BY (a row-level bumped_at predicate would drop the
    # older unmerged SummingMergeTree part-rows of a freshly-bumped
    # memory and undercount its total).
    rows = await ch.query_all(
        """
        SELECT org, namespace, toString(memory_id) AS memory_id,
               sum(recall_count)     AS recall_total,
               sum(evidence_count)   AS evidence_total,
               sum(used_count)       AS used_total,
               sum(harm_count)       AS harm_total,
               max(last_recalled_at) AS last_recalled,
               max(bumped_at)        AS bumped
        FROM loom.memory_usage_totals
        GROUP BY org, namespace, memory_id
        HAVING bumped > {wm:DateTime}
        ORDER BY bumped ASC
        LIMIT {lim:UInt32}
        """,
        params={"wm": _watermark, "lim": int(cfg.max_memories_per_pass)},
    )
    result.candidates += len(rows)
    if not rows:
        return False
    result.batches += 1

    # Resolve memory_id → identity tuple + write-time reinforcement base
    # via loom.memory FINAL (loom.memory_reinforcement is keyed by the
    # identity tuple, not memory_id). `AS mid`, never `AS memory_id`:
    # a same-named alias shadows the column in WHERE and the IN matches
    # nothing.
    idents = await ch.query_all(
        IDENTITY_STATEMENT,
        params={
            "orgs": sorted({r["org"] for r in rows}),
            "nss": sorted({r["namespace"] for r in rows}),
            "ids": [r["memory_id"] for r in rows],
            "served": SERVED_EVENT,
        },
    )
    by_id = {str(r["mid"]): r for r in idents}
    targets = _targets(rows, by_id)
    result.skipped_no_memory += len(rows) - len(targets)

    if targets:
        result.statements += await _fold_memory_rows(
            ch, [t for t in targets if t.changes_memory], cfg.update_chunk_rows
        )
        result.statements += await _fold_reinforcement_rows(ch, targets, cfg.update_chunk_rows)
        result.folded += len(targets)

    # Advance the watermark only after every fold in the batch landed — a
    # partial batch re-folds from the old watermark, which is safe because
    # targets are absolute.
    max_bumped = max(_utc(r["bumped"]) for r in rows)
    full = len(rows) >= int(cfg.max_memories_per_pass)
    if full:
        # Full batch: bumped_at is second-resolution, so boundary ties may
        # exist past the LIMIT. Step back one second to re-fold the boundary
        # next batch (idempotent) — unless that would stall the watermark.
        new_wm = max_bumped - timedelta(seconds=1)
        _watermark = new_wm if new_wm > _watermark else max_bumped
    else:
        _watermark = max_bumped
    return full


# ---------------------------------------------------------------- acted
#
# `recall` says a row was returned; `used` says the agent
# relied on it, and only memory.ask's citations and run.complete write it.
# `acted` is the server's own observation of reliance: a key a run was shown
# that the same run then rewrote (a new version, or a tombstone), named as
# the canonical of a `duplicate` row it wrote (a restatement under a run
# inserts, so the pointer exists), or superseded through a write. One
# statement per pass joins memory_exposures.returned_keys (the read side,
# keyed by run) to memory_versions(run_id) (the write side); the events go
# through record_usage_for_keys, the key→id funnel, one
# call per (org, agent). event_type is LowCardinality(String), so no DDL;
# the totals view ignores this kind, so ranking is untouched.
#
# The watermark is on the write's created_at, process-local like the fold's;
# on a cold start it is the last `acted` event's time (else a day back). The
# statement re-reads a lag window each pass, so an insert that became visible
# late is still credited. The pairs credited are committed to
# loom.usage_acted_credits before their events, so a new leader re-reading the
# window credits none of them twice (the in-process set covers a schema
# without that table).

# How far back a run's exposures are read for the writes of one pass: a
# session can be shown a key hours before it acts on it.
ACTED_EXPOSURE_LOOKBACK_SECONDS = 24 * 3600
# Writes are re-read this far behind the pass clock (async inserts, the
# events-stream flush) before the watermark moves past them.
ACTED_WRITE_LAG_SECONDS = 120

_acted_watermark: datetime = UNIX_EPOCH
_acted_watermark_derived: bool = False
# (org, namespace, run_id, key) credited inside the current lag window.
_acted_recent: set[tuple[str, str, str, str]] = set()
_credits_warned = False

ACTED_CREDITS_TABLE = "loom.usage_acted_credits"
ACTED_CREDITS_SQL = """
SELECT toString(pair_sha256) AS pair_sha256
FROM loom.usage_acted_credits
WHERE first_write > {since:DateTime}
"""

ACTED_STATEMENT = """
WITH
expk AS (
    SELECT org, namespace, run_id, k AS memory_key, min(ts) AS first_seen
    FROM loom.memory_exposures
    ARRAY JOIN returned_keys AS k
    WHERE ts > {since_exp:DateTime} AND run_id NOT IN ('-', '')
    GROUP BY org, namespace, run_id, k
),
w AS (
    SELECT org, namespace, run_id, agent, memory_key, toString(memory_id) AS mid,
           duplicate_of, created_at, tombstoned_at IS NOT NULL AS deleted
    FROM loom.memory_versions
    WHERE created_at > {since:DateTime} AND run_id NOT IN ('-', '')
      AND (org, namespace, run_id) IN (SELECT org, namespace, run_id FROM expk)
),
canon AS (
    SELECT org, namespace, toString(memory_id) AS mid, superseded_by, memory_key
    FROM loom.memory_versions
    WHERE (org, namespace) IN (SELECT org, namespace FROM w)
      AND (toString(memory_id) IN (SELECT duplicate_of FROM w WHERE duplicate_of != '')
           OR superseded_by IN (SELECT mid FROM w))
),
hits AS (
    SELECT w.org AS org, w.namespace AS namespace, w.run_id AS run_id,
           w.memory_key AS acted_key, if(w.deleted, 'deleted', 'revised') AS how,
           w.agent AS agent, w.created_at AS at
    FROM w
    INNER JOIN expk ON expk.org = w.org AND expk.namespace = w.namespace
                   AND expk.run_id = w.run_id AND expk.memory_key = w.memory_key
    WHERE w.created_at > expk.first_seen
    UNION ALL
    SELECT w.org, w.namespace, w.run_id, c.memory_key, 'duplicate_of', w.agent, w.created_at
    FROM w
    INNER JOIN canon AS c ON c.org = w.org AND c.namespace = w.namespace
                         AND c.mid = w.duplicate_of
    INNER JOIN expk ON expk.org = w.org AND expk.namespace = w.namespace
                   AND expk.run_id = w.run_id AND expk.memory_key = c.memory_key
    WHERE w.duplicate_of != '' AND w.created_at > expk.first_seen
    UNION ALL
    SELECT w.org, w.namespace, w.run_id, c.memory_key, 'superseded', w.agent, w.created_at
    FROM w
    INNER JOIN canon AS c ON c.org = w.org AND c.namespace = w.namespace
                         AND c.superseded_by = w.mid
    INNER JOIN expk ON expk.org = w.org AND expk.namespace = w.namespace
                   AND expk.run_id = w.run_id AND expk.memory_key = c.memory_key
    WHERE c.memory_key != w.memory_key AND w.created_at > expk.first_seen
)
SELECT h.org AS org, h.namespace AS namespace, h.run_id AS run_id,
       h.acted_key AS acted_key, any(h.how) AS how, any(h.agent) AS agent,
       min(h.at) AS first_write
FROM hits AS h
GROUP BY org, namespace, run_id, acted_key
ORDER BY first_write ASC
LIMIT {lim:UInt32}
"""


def current_acted_watermark() -> datetime:
    return _acted_watermark


async def _derive_acted_watermark(ch: _CHClient, now: datetime) -> datetime:
    """Cold start: just behind the last `acted` event ever emitted; a day back
    when none exists (nothing before this record was threaded)."""
    row = await ch.query_one(
        "SELECT max(ts) AS last FROM loom.memory_usage_events WHERE event_type = 'acted'"
    )
    last = _utc((row or {}).get("last"))
    if last <= UNIX_EPOCH:
        return now - timedelta(days=1)
    return last - timedelta(seconds=ACTED_WRITE_LAG_SECONDS)


def _warn_credits_once(what: str, e: BaseException) -> None:
    global _credits_warned
    if not _credits_warned:
        _credits_warned = True
        log.warning(
            "usage fold: %s %s failed (%s); `acted` dedup is process-local until it works",
            what,
            ACTED_CREDITS_TABLE,
            e,
        )


def _pair_sha256(pair: tuple[str, str, str, str]) -> str:
    """A credit mark names its (org, namespace, run, key) by hash only, so the
    table holds no memory content for the erasure saga to chase."""
    return hashlib.sha256("\x00".join(pair).encode()).hexdigest()


async def _credited_pairs(ch: _CHClient, since: datetime) -> set[str]:
    """The pair hashes some leader already credited for writes past `since`. Read
    sequentially consistent: a new leader may reach a replica that has not seen
    the marks its predecessor committed."""
    from server.core.ch import sequential_reads

    try:
        with sequential_reads():
            rows = await ch.query_all(ACTED_CREDITS_SQL, params={"since": since})
    except Exception as e:  # noqa: BLE001 — a schema without the table falls back to memory
        _warn_credits_once("read of", e)
        return set()
    return {str(r["pair_sha256"]) for r in rows}


async def _record_credits(ch: _CHClient, marks: list[dict[str, Any]]) -> None:
    """Commit the credit marks before the (buffered) events: a leader that
    dies in between loses a credit rather than giving it twice."""
    try:
        await ch.insert(
            ACTED_CREDITS_TABLE,
            marks,
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
    except Exception as e:  # noqa: BLE001 — the credit still goes out, deduped in-process
        _warn_credits_once("insert into", e)


async def acted_pass(
    ch: _CHClient,
    result: UsageFoldResult | None = None,
    *,
    now: datetime | None = None,
) -> UsageFoldResult:
    """Derive and emit this pass's `acted` events. One join
    statement, one key→id resolution per (org, agent), buffered inserts."""
    global _acted_watermark, _acted_watermark_derived, _acted_recent
    result = result or UsageFoldResult()
    now = now or datetime.now(UTC)
    started = time.monotonic()
    if not _acted_watermark_derived and _acted_watermark <= UNIX_EPOCH:
        _acted_watermark = await _derive_acted_watermark(ch, now)
        _acted_watermark_derived = True
        log.info("usage fold: acted watermark derived as %s", _acted_watermark.isoformat())
    since = _acted_watermark
    rows = await ch.query_all(
        ACTED_STATEMENT,
        params={
            "since": since,
            "since_exp": since - timedelta(seconds=ACTED_EXPOSURE_LOOKBACK_SECONDS),
            "lim": settings.memory_usage_fold_acted_max_rows_per_pass,
        },
    )
    result.acted_candidates += len(rows)
    credited = await _credited_pairs(ch, since) if rows else set()
    seen_now: set[tuple[str, str, str, str]] = set()
    by_org_agent: dict[tuple[str, str], dict[str, list[str]]] = {}
    marks: list[dict[str, Any]] = []
    for r in rows:
        pair = (str(r["org"]), str(r["namespace"]), str(r["run_id"]), str(r["acted_key"]))
        seen_now.add(pair)
        digest = _pair_sha256(pair)
        if pair in _acted_recent or digest in credited:
            continue
        credited.add(digest)
        marks.append(
            {
                "pair_sha256": digest,
                "first_write": _utc(r.get("first_write")).replace(tzinfo=None),
            }
        )
        bucket = by_org_agent.setdefault((pair[0], str(r.get("agent") or "")), {})
        keys = bucket.setdefault(pair[1], [])
        if pair[3] not in keys:
            keys.append(pair[3])
    if marks:
        await _record_credits(ch, marks)
    if by_org_agent:
        from server.memory.store.usage import record_usage_for_keys

        for (org, agent), ns_keys in by_org_agent.items():
            result.acted_emitted += await record_usage_for_keys(
                org, ns_keys, event_type="acted", agent=agent, ch=ch
            )
    full = len(rows) >= settings.memory_usage_fold_acted_max_rows_per_pass
    if full:
        # A full page: hold the watermark at the last write credited so the
        # next pass continues from there (the remembered pairs stop the
        # overlap from double-crediting).
        last_write = max((_utc(r.get("first_write")) for r in rows), default=since)
        new_wm = last_write - timedelta(seconds=ACTED_WRITE_LAG_SECONDS)
        _acted_watermark = new_wm if new_wm > since else since
    else:
        _acted_watermark = max(since, now - timedelta(seconds=ACTED_WRITE_LAG_SECONDS))
    _acted_recent = seen_now
    result.acted_ms = int((time.monotonic() - started) * 1000)
    return result


async def run_pass(
    cfg: UsageFoldConfig | None = None,
    ch: _CHClient | None = None,
) -> UsageFoldResult:
    """One fold pass: probe for new usage, derive the watermark on a cold
    start, then fold batches until caught up or the time budget is spent."""
    global _watermark, _watermark_derived
    cfg = cfg or UsageFoldConfig()
    result = UsageFoldResult(watermark=_watermark)
    started = time.monotonic()

    with record_pass("usage_fold") as rec:

        def _done(outcome: dict[str, Any]) -> UsageFoldResult:
            result.watermark = _watermark
            rec.outcome = {
                **outcome,
                "acted_candidates": result.acted_candidates,
                "acted_emitted": result.acted_emitted,
                "acted_ms": result.acted_ms,
            }
            return result

        if ch is None:
            from server.core.ch import get_client

            ch = get_client()

        # The `acted` derivation rides the same pass. It reads
        # its own tables and fails open — a broken join must not stop the
        # fold, and vice versa; a failed one makes the pass partial.
        try:
            await acted_pass(ch, result)
        except Exception as e:  # noqa: BLE001 — telemetry never stops the fold
            log.warning("usage fold: acted pass failed (non-fatal): %s", e)
            pass_context.note_failure(e, "acted")

        # Cheap no-op probe: skip the GROUP BY entirely when nothing new
        # has landed since the last pass.
        probe = await ch.query_one("SELECT max(bumped_at) AS hwm FROM loom.memory_usage_totals")
        hwm = _utc((probe or {}).get("hwm"))
        if hwm <= UNIX_EPOCH:
            return _done({"candidates": 0, "folded": 0, "skipped_no_memory": 0})

        if not _watermark_derived and _watermark <= UNIX_EPOCH:
            _watermark = await _derive_cold_start_watermark(ch, hwm)
            _watermark_derived = True
            log.info("usage fold: cold-start watermark derived as %s", _watermark.isoformat())

        if hwm <= _watermark:
            return _done({"candidates": 0, "folded": 0, "skipped_no_memory": 0})

        while True:
            more = await _fold_batch(ch, cfg, result)
            if not more:
                break
            if time.monotonic() - started >= float(cfg.pass_time_budget_seconds):
                log.info("usage fold: time budget spent, resuming next pass")
                break

        log.info(
            "usage fold pass: candidates=%d folded=%d skipped_no_memory=%d "
            "batches=%d statements=%d watermark=%s",
            result.candidates,
            result.folded,
            result.skipped_no_memory,
            result.batches,
            result.statements,
            _watermark.isoformat(),
        )
        return _done(
            {
                "candidates": result.candidates,
                "folded": result.folded,
                "skipped_no_memory": result.skipped_no_memory,
                "batches": result.batches,
                "statements": result.statements,
                "watermark": _watermark.isoformat(),
            }
        )


__all__ = [
    "ACTED_STATEMENT",
    "IDENTITY_STATEMENT",
    "UsageFoldConfig",
    "UsageFoldResult",
    "acted_pass",
    "current_acted_watermark",
    "current_watermark",
    "reset_watermark",
    "run_pass",
]
