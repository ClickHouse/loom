"""The state gauge probes: the ClickHouse readings behind the observable gauges
`metrics_gauges.register_state_gauges` wires, each on its own clock.

The fast probe (per-replica counters) runs in every process; the whole-table
censuses run where the supervisor lease is.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any

from server.observability.otel_probe_client import _probe_client
from server.observability.state_inputs import state_inputs
from server.settings import settings
from server.workers import leader as _leader

log = logging.getLogger(__name__)


def _probe_state() -> dict[str, Any]:
    """One cheap ClickHouse round-trip for every state gauge.

    Deliberately a single query rather than one per gauge: the metric reader
    exports every 3s, and the caller wraps this in a 30s TTL cache, so the
    steady-state cost is two queries a minute regardless of gauge count. The
    whole-table readings (`_probe_invariants`) ride along from their own
    hourly cache.

    `loom.*` here, unlike the rest of this module, which writes to `default`.
    """
    client = _probe_client()
    row = client.query(
        """
        SELECT
            (SELECT count() FROM loom.memory_embeddings
             WHERE embedding_dim > 0 AND tombstoned_at IS NULL) AS vectors_total,
            -- Active patch parts on the vector table. One is enough to
            -- switch the fact leg's codes-first lazy read off (upstream
            -- ClickHouse #119885); the read-side gate follows the same
            -- count, so a non-zero line here is a writer that UPDATEs the
            -- table again or a rebuild not yet run.
            (SELECT countIf(startsWith(name, 'patch')) FROM system.parts
             WHERE database = {loom_db:String} AND table = 'memory_embeddings' AND active)
                AS memory_embeddings_patch_parts,
            -- One-hour windows the audit checkpoint chain has yet to seal. A stalled sealer
            -- reads as a rising line, a backfill as a falling one; 0 when nothing has ever
            -- been sealed. The sealer itself cannot report this when it is down.
            (SELECT if(count() = 0, 0,
                       greatest(dateDiff('hour', max(window_end), now()), 0))
             FROM loom.audit_checkpoints) AS audit_checkpoint_lag_windows,
            -- The HNSW index, as seen by usearch on THIS replica.
            -- `system.events` is per server, so the two counters are
            -- one replica's cumulative totals; a flat searches line beside a
            -- rising adds line means distance computations spent building a
            -- graph that few reads open.
            (SELECT sumIf(value, event = 'USearchSearchCount')
             FROM system.events) AS usearch_searches_total,
            (SELECT sumIf(value, event = 'USearchAddCount')
             FROM system.events) AS usearch_adds_total,
            -- Share of the last 30 minutes' vector reads on this replica that
            -- opened the index at all. Reads 0 while the planner pre-filters on
            -- the tenant key; would read near 1 on a ClickHouse that searches
            -- partially pruned parts. A vector read is classified by the
            -- columns it touched (`system.query_log.columns`), never by its
            -- text: a substring test over the statement text of every logged
            -- query in a 30-minute window reads ~65 KB per row, GBs per probe.
            (SELECT if(count() = 0, 0.,
                       countIf(ProfileEvents['USearchSearchCount'] > 0) / count())
             FROM system.query_log
             WHERE event_time > now() - INTERVAL 30 MINUTE
               AND type = 'QueryFinish'
               AND query_kind = 'Select'
               AND has(tables, concat({loom_db:String}, '.memory_embeddings'))
               AND has(columns, concat({loom_db:String}, '.memory_embeddings.embedding')))
               AS vector_reads_indexed_ratio
        """
    ).result_rows
    if not row:
        return {}
    (
        vectors_total,
        memory_embeddings_patch_parts,
        audit_checkpoint_lag_windows,
        usearch_searches_total,
        usearch_adds_total,
        vector_reads_indexed_ratio,
    ) = row[0]
    out = {
        "vectors_total": int(vectors_total),
        "memory_embeddings_patch_parts": int(memory_embeddings_patch_parts or 0),
        "audit_checkpoint_lag_windows": int(audit_checkpoint_lag_windows or 0),
        "usearch_searches_total": int(usearch_searches_total or 0),
        "usearch_adds_total": int(usearch_adds_total or 0),
        "vector_reads_indexed_ratio": float(vector_reads_indexed_ratio or 0.0),
    }
    out.update(_probe_invariants_cached())
    return out


# The invariant readings — a FINAL pass over memory, memory_embeddings and
# entity_vectors — move at the speed of the tables, not of the export
# interval. They are refreshed on their own clock (`otel_invariants_probe_s`,
# an hour) on the lease holder and merged into every fast reading.
_INVARIANTS_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _census_reads_here() -> bool:
    """A whole-table census is a reading of the deployment,
    not of the process; the process holding the supervisor lease takes it.
    Every other process (a sibling web worker, a pure web replica, an
    isolated bench server with the workers off) skips it and exports no
    point for those series. `otel_census_scope=process` restores one census
    per process."""
    scope = settings.otel_census_scope.strip().lower()
    if scope != "leader":
        return True
    lease = _leader.current()
    return bool(lease is not None and lease.held)


def _census_only(probe: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a census probe so it runs only where `_census_reads_here()`;
    elsewhere it returns None, which the gauge cache renders as "no
    observation" (never 0). The predicate rides along as `reads_here`, so
    the cache stops exporting the moment this process gives up the lease."""

    @functools.wraps(probe)
    def _run() -> Any:
        return probe() if _census_reads_here() else None

    setattr(_run, "reads_here", _census_reads_here)  # noqa: B010 — a function declares no field
    return _run


def _probe_invariants_cached() -> dict[str, Any]:
    from server.settings import settings

    if not _census_reads_here():
        return {}
    ttl = float(settings.otel_invariants_probe_s)
    now_m = time.monotonic()
    never = _INVARIANTS_CACHE["value"] is None
    if ttl <= 0 or never or now_m - float(_INVARIANTS_CACHE["at"]) >= ttl:
        _INVARIANTS_CACHE["value"] = _probe_invariants()
        _INVARIANTS_CACHE["at"] = now_m
    return dict(_INVARIANTS_CACHE["value"] or {})


# The whole-table censuses read each FINAL once. Parallel FINAL splits
# intersecting part ranges into layers and reads the granules at every layer
# boundary again: 1.3-1.5x the table's rows on unmerged parts, measured. Off, a
# FINAL reads each row once; the result is the same, the census is only
# less parallel.
_FINAL_SINGLE_PASS = "split_intersecting_parts_ranges_into_layers_final = 0"


def _invariants_sql() -> str:
    """The census statement: every invariant from ONE FINAL pass over each of
    memory, memory_embeddings and entity_vectors.

    The rows are grouped once by the full tenancy identity (org, namespace,
    agent, user_id, session_id, memory_key), then by the entity-vector identity
    (no session_id), then by namespace, and each invariant is a sum at the level
    its join lives at — one FINAL scan per table instead of one per count. The
    per-invariant statements the CLIs run (`integrity-repair`,
    `erasure-re-verify`, the namespace census) are their own, and an
    integration test holds this statement equal to them.
    """
    live_chunk_sql = state_inputs().live_chunk_sql
    return f"""
        SELECT
            -- `system.mutations` is materialised from the shared metadata store on Cloud
            -- (~2 s even for a count of zero rows), too slow for every process's 30 s fast
            -- probe. A pending mutation is a level that moves at ALTER speed; it reads
            -- here, on the census clock, from the lease holder.
            (SELECT count() FROM system.mutations
             WHERE database = {{loom_db:String}} AND NOT is_done) AS mutations_pending,
            -- Memories that cannot be found by search, for ANY reason: live memory
            -- rows minus live vectors. Not `embedding_dim = 0` alone, which is blind
            -- to a memory with no embedding row at all.
            sum(s.live_rows) - sum(s.e_vec) AS embed_pending,
            -- INVARIANTS, not levels: statements that must hold for ALL rows, so
            -- each catches the next instance of its class. 0 is the only right
            -- reading for every one below.
            --
            -- A live vector whose sorted entities differ from its live memory's.
            sum(s.divergent) AS divergent_entities,
            -- A live vector whose memory is gone: a search hit that resolves to nothing.
            sum(s.orphan_vec) AS orphan_vectors,
            -- Erasure residue: an entity vector with no live memory under its
            -- (org, namespace, agent, user_id, memory_key); a namespace with graph
            -- rows and neither live memory nor live chunks.
            sum(s.orphan_ev) AS orphan_entity_vectors,
            countIf(g.has_graph = 1 AND s.live_rows = 0 AND c.has_chunks = 0)
                AS orphan_graph_namespaces,
            -- Registry census: live memory in a namespace with no loom.namespaces row.
            countIf(s.live_rows > 0 AND r.registered = 0) AS live_ns_unregistered,
            sumIf(s.live_rows, r.registered = 0) AS live_rows_unregistered,
            -- Row-level invariants: a live vector row with `embedding_dim = 0` and a
            -- vector (never returned); a tombstoned memory whose status is not
            -- `tombstoned`; an active memory past its own `valid_to`; a live `high`
            -- memory with a live embedding row (a high value one similarity query away).
            sum(s.dim0_vec) AS dim0_with_vector,
            sum(s.drift) AS tombstone_status_drift,
            sum(s.past) AS active_past_valid_to,
            sum(s.high_emb) AS high_with_embedding
        FROM (
            SELECT org, namespace,
                   sum(live5) AS live_rows, sum(e_vec5) AS e_vec, sum(div5) AS divergent,
                   sum(orphan_vec5) AS orphan_vec, sum(orphan_ev5) AS orphan_ev,
                   sum(dim0_vec5) AS dim0_vec, sum(drift5) AS drift, sum(past5) AS past,
                   sum(high_emb5) AS high_emb
            FROM (
                -- The entity-vector identity: the anti join against live memory.
                SELECT org, namespace, agent, user_id, memory_key,
                       sum(m_live) AS live5, sum(e_vec) AS e_vec5, sum(div) AS div5,
                       sum(orphan_vec) AS orphan_vec5,
                       if(live5 = 0, sum(ev_rows), 0) AS orphan_ev5,
                       sum(dim0_vec) AS dim0_vec5, sum(drift) AS drift5, sum(past) AS past5,
                       sum(high_emb) AS high_emb5
                FROM (
                    -- The full tenancy identity: the memory <-> vector joins. The same
                    -- memory_key under two agents, users or sessions is two memories.
                    SELECT org, namespace, agent, user_id, session_id, memory_key,
                           sum(is_m AND live) AS m_live,
                           sum(is_m AND NOT live AND st != 'tombstoned') AS drift,
                           sum(is_m AND live AND st = 'active' AND valid_to IS NOT NULL
                               AND assumeNotNull(valid_to) < now()) AS past,
                           sum(is_e AND dim > 0) AS e_vec,
                           sum(is_e AND dim = 0 AND vlen > 0) AS dim0_vec,
                           sum(is_v) AS ev_rows,
                           if(m_live > 0,
                              arrayCount(x -> x != anyIf(ents, is_m), groupArrayIf(ents, is_e)),
                              0) AS div,
                           if(m_live = 0, e_vec, 0) AS orphan_vec,
                           if(m_live > 0 AND maxIf(high, is_m) = 1, countIf(is_e), 0) AS high_emb
                    FROM (
                        -- Every memory row after FINAL (the drift count reads the
                        -- tombstoned ones), live vector rows, live entity vectors.
                        -- `entities` is an unordered set: both sides arraySort()ed.
                        -- `embedding.size0` is length(embedding) without reading the
                        -- vectors; FINAL skips that rewrite on its own.
                        SELECT org, namespace, agent, user_id, session_id, memory_key,
                               1 AS is_m, 0 AS is_e, 0 AS is_v,
                               tombstoned_at IS NULL AS live, toString(status) AS st,
                               valid_to, sensitivity = 'high' AS high,
                               arraySort(entities) AS ents, 0 AS dim, 0 AS vlen
                        FROM loom.memory FINAL
                        UNION ALL
                        SELECT org, namespace, agent, user_id, session_id, memory_key,
                               0, 1, 0, 1, '', NULL, 0, arraySort(entities),
                               embedding_dim, embedding.size0
                        FROM loom.memory_embeddings FINAL
                        WHERE tombstoned_at IS NULL
                        UNION ALL
                        SELECT org, namespace, agent, user_id, '', memory_key,
                               0, 0, 1, 1, '', NULL, 0, [], 0, 0
                        FROM loom.entity_vectors FINAL
                        WHERE tombstoned_at IS NULL
                    )
                    GROUP BY org, namespace, agent, user_id, session_id, memory_key
                )
                GROUP BY org, namespace, agent, user_id, memory_key
            )
            GROUP BY org, namespace
        ) AS s
        FULL OUTER JOIN (
            SELECT org, namespace, 1 AS has_graph FROM loom.entity_edges GROUP BY org, namespace
        ) AS g USING (org, namespace)
        LEFT JOIN (
            SELECT org, namespace, 1 AS registered FROM loom.namespaces GROUP BY org, namespace
        ) AS r USING (org, namespace)
        LEFT JOIN (
            SELECT org, namespace, 1 AS has_chunks FROM loom.chunks
            WHERE {live_chunk_sql}
            GROUP BY org, namespace
        ) AS c USING (org, namespace)
        SETTINGS join_use_nulls = 0, {_FINAL_SINGLE_PASS}
    """


def _probe_invariants() -> dict[str, Any]:
    """The slow-clock half of the state probe: the mutation level, one
    backlog and the invariants, in one statement (`_invariants_sql`)."""
    client = _probe_client()
    row = client.query(_invariants_sql()).result_rows
    if not row:
        return {}
    (
        mutations_pending,
        embed_pending,
        divergent_entities,
        orphan_vectors,
        orphan_entity_vectors,
        orphan_graph_namespaces,
        live_ns_unregistered,
        live_rows_unregistered,
        dim0_with_vector,
        tombstone_status_drift,
        active_past_valid_to,
        high_with_embedding,
    ) = row[0]
    return {
        "mutations_pending": int(mutations_pending or 0),
        "embed_pending": int(embed_pending),
        "divergent_entities": int(divergent_entities or 0),
        "orphan_vectors": int(orphan_vectors or 0),
        "orphan_entity_vectors": int(orphan_entity_vectors or 0),
        "orphan_graph_namespaces": int(orphan_graph_namespaces or 0),
        "live_ns_unregistered": int(live_ns_unregistered or 0),
        "live_rows_unregistered": int(live_rows_unregistered or 0),
        "dim0_with_vector": int(dim0_with_vector or 0),
        "tombstone_status_drift": int(tombstone_status_drift or 0),
        "active_past_valid_to": int(active_past_valid_to or 0),
        "high_with_embedding": int(high_with_embedding or 0),
    }


# code_index_state keys the freshness gauges read, and the series each feeds.
_CODE_STATE_GAUGES: dict[str, str] = {
    "git_head": "code_index_age_seconds",
    "scip_commit": "code_scip_age_seconds",
    "scip_commit_py": "code_scip_py_age_seconds",
    "pr_cursor": "code_pr_cursor_age_seconds",
}


_CODE_STATE_SQL = """
        SELECT org, namespace, concat('state:', state_key) AS k,
               toFloat64(toUnixTimestamp(updated_at)) AS v, state_value AS s
        FROM loom.code_index_state FINAL
        WHERE state_key IN ('git_head', 'scip_commit', 'scip_commit_py', 'scip_commit_ts',
                            'scip_commit_go', 'pr_cursor', 'reconcile')
"""

_CODE_COUNTS_SQL = f"""
        SELECT org, namespace, 'files_errored' AS k,
               toFloat64(countIf(index_error != '')) AS v, '' AS s
        FROM loom.code_files FINAL
        WHERE tombstoned_at IS NULL
        GROUP BY org, namespace
        UNION ALL
        -- countIf over ALL live chunks, not a filtered count(): a namespace
        -- with no stubs must read 0, not vanish from the series.
        SELECT org, namespace, 'embed_backlog' AS k,
               toFloat64(countIf(embedding_dim = 0 AND embed_failed_at IS NULL)) AS v, '' AS s
        FROM loom.code_chunks FINAL
        WHERE tombstoned_at IS NULL
        GROUP BY org, namespace
        UNION ALL
        SELECT org, namespace, concat('symbols:', toString(tier)) AS k,
               toFloat64(count()) AS v, '' AS s
        FROM loom.code_symbols FINAL
        WHERE tombstoned_at IS NULL
        GROUP BY org, namespace, tier
        UNION ALL
        -- Parser-tier edge quality. `edges_unqualified`: call edges whose
        -- target is a bare name (no `::` or `.` qualifier) — the shape a
        -- repo-wide name match binds when the callee is really an unrelated
        -- same-named function (~9% of parser call edges on a large C++
        -- repo). `edges_low_confidence`: the 0.6 grade itself. Both
        -- are countIf over every parser call edge so a clean namespace
        -- reads 0, and both ride ONE FINAL scan of the parser call edges:
        -- the two counts as columns of one aggregate, unfolded into the
        -- (k, v) rows the series transform reads (one scan instead of two
        -- cuts the probe's read by ~60%, same values).
        SELECT org, namespace, kv.1 AS k, kv.2 AS v, '' AS s
        FROM (
            SELECT org, namespace,
                   toFloat64(countIf(to_qualified_name NOT LIKE '%::%'
                                     AND to_qualified_name NOT LIKE '%.%')) AS unqualified,
                   toFloat64(countIf(confidence < 0.7)) AS low_confidence
            FROM loom.code_edges FINAL
            WHERE tombstoned_at IS NULL AND tier = 'treesitter' AND edge_kind = 'calls'
            GROUP BY org, namespace
        )
        ARRAY JOIN [tuple('edges_unqualified', unqualified),
                    tuple('edges_low_confidence', low_confidence)] AS kv
        SETTINGS {_FINAL_SINGLE_PASS}
"""

_CODE_COUNTS_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}


# Retention census: what the store holds that nobody asks
# for, per org, on the slow clock. One FINAL pass over loom.memory with the
# narrow columns the census needs plus `value` for the length. The stale
# predicate is the age from when the fact entered the store (mentioned_at, else created_at),
# never from valid_at — kept as a count of old, unpinned, non-durable rows
# nobody recalled. Nothing retires them. Hourly (`otel_retention_census_s`)
# on the lease holder: the windows are 7 and 45 days.
_RETENTION_SQL = f"""
        SELECT org,
               count() AS active,
               countIf(created_at < now() - INTERVAL 7 DAY) AS older_7d,
               countIf(recall_count = 0 AND created_at < now() - INTERVAL 7 DAY)
                   AS never_recalled_7d,
               -- `value.size` is length(value) read from the size stream; FINAL skips
               -- that rewrite and would read every value's bytes.
               countIf(value.size < 25) AS low_information,
               countIf(sensitivity != '' AND expires_at IS NULL) AS sensitive_no_expiry,
               countIf(pinned = 0
                       AND memory_type NOT IN ('preference', 'procedural')
                       AND coalesce(mentioned_at, created_at)
                           < now() - INTERVAL {{stale:UInt32}} DAY
                       AND (last_recalled_at IS NULL
                            OR last_recalled_at < now() - INTERVAL {{stale:UInt32}} DAY))
                   AS stale_candidates
        FROM loom.memory FINAL
        WHERE tombstoned_at IS NULL AND status = 'active'
        GROUP BY org
        SETTINGS {_FINAL_SINGLE_PASS}
"""

RETENTION_STALE_DAYS = 45  # the stale window of the retention census


def _retention_series_from_rows(rows: list[Any]) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Pure transform of `_RETENTION_SQL` rows into per-org gauge series."""
    out: dict[str, list[tuple[float, dict[str, str]]]] = {}
    for row in rows:
        org, active, older_7d, never_7d, low_info, sens_no_exp, stale = row[:7]
        attrs = {"loom.org": str(org)}
        older = float(older_7d or 0)
        ratio = (float(never_7d or 0) / older) if older > 0 else 0.0
        out.setdefault("retention_active_rows", []).append((float(active or 0), attrs))
        out.setdefault("retention_never_recalled_ratio", []).append((ratio, attrs))
        out.setdefault("retention_low_information_rows", []).append((float(low_info or 0), attrs))
        out.setdefault("retention_sensitive_without_expiry", []).append(
            (float(sens_no_exp or 0), attrs)
        )
        out.setdefault("retention_stale_candidates", []).append((float(stale or 0), attrs))
    return out


# The labels an AGENT wrote in the last day, per namespace and kind — `used`
# (memory.outcome used / run.complete memories_used), `acted` and `harm`.
# memory.ask's own `used_cited` credit, `recall`, `evidence` (extraction writes
# it too) and `contradicted` are server-sourced and left out.
_AGENT_LABELS_SQL = """
        SELECT org, namespace, event_type AS kind, sum(event_count) AS labels
        FROM loom.memory_usage_events
        WHERE ts > now() - INTERVAL 24 HOUR
          AND event_type IN {kinds:Array(String)}
        GROUP BY org, namespace, kind
"""


# The namespace label is capped. A 24 h window can hold ~1,000 namespaces /
# series — benches mint a namespace per conversation — and a per-namespace gauge exported once a
# minute per process would write ~1.5M gauge rows a day for figures nobody
# panels. The top N namespaces by label total keep their name; the rest are
# summed under `loom.namespace = "other"` per org and kind, so the fleet total
# is exact and the per-namespace pace (1,000 in a month in ONE namespace) is
# still read from a second series that needs no namespace label at all:
# `agent_labels_namespaces_at_pace` counts, per org, the namespaces whose day
# is at or above that daily pace (1,000 a month / 30), so a tenant
# out-ranked by twenty busier bench namespaces is still counted.
# LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES, default 20, 0 = no cap.
OTHER_NAMESPACE = "other"
WORTH_TRIGGER_LABELS_PER_MONTH = 1000
WORTH_TRIGGER_LABELS_PER_DAY = WORTH_TRIGGER_LABELS_PER_MONTH / 30.0


def _agent_labels_series_from_rows(
    rows: list[Any],
    *,
    max_namespaces: int = 0,
) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Pure transform of `_AGENT_LABELS_SQL` rows into one gauge series.

    Rows whose kind is not an agent label (memory.ask's `used_cited`,
    `recall`, `evidence`, `contradicted`) are dropped here as well as in the
    statement's parameter: the self-credit never reaches the series even if a
    caller hands this transform a wider read.
    """
    kinds_kept = state_inputs().agent_label_kinds
    per_ns: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        org, namespace, kind, labels = row[:4]
        if str(kind) not in kinds_kept:
            continue
        bucket = per_ns.setdefault((str(org), str(namespace)), {})
        bucket[str(kind)] = bucket.get(str(kind), 0.0) + float(labels or 0)
    named, folded = _cap_namespaces(per_ns, max_namespaces)
    out: list[tuple[float, dict[str, str]]] = []
    for (org, namespace), kinds in named:
        for kind, labels in kinds.items():
            out.append((labels, {"loom.org": org, "loom.namespace": namespace, "loom.kind": kind}))
    for (org, kind), labels in sorted(folded.items()):
        out.append(
            (labels, {"loom.org": org, "loom.namespace": OTHER_NAMESPACE, "loom.kind": kind})
        )
    at_pace: dict[str, float] = {}
    for (org, _namespace), kinds in per_ns.items():
        at_pace.setdefault(org, 0.0)
        if sum(kinds.values()) >= WORTH_TRIGGER_LABELS_PER_DAY:
            at_pace[org] += 1.0
    return {
        "agent_labels_24h": out,
        "agent_labels_namespaces_at_pace": [
            (n, {"loom.org": org}) for org, n in sorted(at_pace.items())
        ],
    }


def _cap_namespaces(
    per_ns: dict[tuple[str, str], dict[str, float]], max_namespaces: int
) -> tuple[list[tuple[tuple[str, str], dict[str, float]]], dict[tuple[str, str], float]]:
    """Top `max_namespaces` (org, namespace) pairs by label total keep their
    rows; the remainder is summed per (org, kind). 0 or less = no cap.
    Ties break on the name so two probes of the same rows agree."""
    ordered = sorted(per_ns.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    if max_namespaces <= 0 or len(ordered) <= max_namespaces:
        return ordered, {}
    folded: dict[tuple[str, str], float] = {}
    for (org, _namespace), kinds in ordered[max_namespaces:]:
        for kind, labels in kinds.items():
            folded[(org, kind)] = folded.get((org, kind), 0.0) + labels
    return ordered[:max_namespaces], folded


def _probe_agent_labels() -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Agent-sourced usage labels per namespace over 24 h; cached on the slow clock."""
    client = _probe_client()
    rows = client.query(
        _AGENT_LABELS_SQL, parameters={"kinds": list(state_inputs().agent_label_kinds)}
    ).result_rows
    cap = int(settings.otel_agent_labels_max_namespaces)
    return _agent_labels_series_from_rows(list(rows), max_namespaces=cap)


# The storage housekeeping readings the operator's DROP steps
# gate on, as gauges. One statement
# over `system.parts` for a closed list of tables — active parts, Wide parts,
# patch parts, rows per mark, bytes on disk — and one over
# `system.view_refreshes` for the refreshable roll-ups. Both read metadata
# only (no loom.* scan); the figures move on merges, so they refresh every
# five minutes. The table list is closed so `loom.table` stays bounded.
STORAGE_TABLES: tuple[str, ...] = (
    "memory_embeddings",
    "memory_evidence",
    "code_term_freq",
    "memory_term_freq",
    "session_summaries",
)

_STORAGE_PARTS_SQL = """
        SELECT table,
               count() AS active_parts,
               countIf(part_type = 'Wide') AS wide_parts,
               countIf(startsWith(name, 'patch')) AS patch_parts,
               sum(rows) AS row_total,
               sum(marks) AS mark_total,
               sum(bytes_on_disk) AS disk_bytes
        FROM system.parts
        WHERE database = {loom_db:String} AND active AND table IN {tables:Array(String)}
        GROUP BY table
"""

# A view outside the schema is named with its alias (loom_telemetry.sli_minute_refresh)
# whatever LOOM_CH_TELEMETRY_DATABASE calls it, as the docs and dashboards do.
_VIEW_REFRESH_SQL = """
        SELECT multiIf(database = {loom_db:String}, view,
                       database = {telemetry_db:String}, concat('loom_telemetry.', view),
                       concat(database, '.', view)) AS view,
               status, last_success_time, exception
        FROM system.view_refreshes
        WHERE database IN ({loom_db:String}, {telemetry_db:String})
"""


# How far the SLI rollup's mark trails now, from its sentinel rows (a range
# read on the sort key). A refresh that runs but lags reads as healthy to the
# refresh gauges while burn-rate windows read nothing; this one shows it.
_SLI_LAG_SQL = """
        SELECT dateDiff('millisecond', max(ingested_hwm), now64(3)) / 1000
        FROM loom_telemetry.sli_minute
        WHERE slo_class = '' AND minute >= toStartOfMinute(now() - toIntervalDay(31))
        HAVING count() > 0
"""

# Ledger request rows of the last day that ClickHouse took before the rollup's
# mark but the rollup did not count, minute by minute: a ledger insert whose
# part committed after the mark had passed its rows (the view waits 70 s for
# that). `mark` is read before `rolled`, so a refresh that commits between the
# two only adds to `rolled`: never a false miss, though that probe can hide a
# real one in those minutes until the next. A ledger row erasure removed makes
# a minute read short, never long.
_SLI_MISSED_SQL = """
        WITH (SELECT max(ingested_hwm) FROM loom_telemetry.sli_minute
              WHERE slo_class = '' AND minute >= toStartOfMinute(now() - toIntervalDay(31))
             ) AS mark,
             toStartOfMinute(now() - toIntervalDay(1)) AS since
        SELECT sum(greatest(toInt64(ledger.n) - toInt64(rolled.n), 0))
        FROM (
            SELECT toStartOfMinute(ts) AS minute, count() AS n
            FROM loom.agent_events
            WHERE kind = 'request' AND slo_class != '' AND ts >= since
              AND ingested_at > mark - toIntervalDay(1) AND ingested_at <= mark
            GROUP BY minute
        ) AS ledger
        LEFT JOIN (
            SELECT minute, sum(requests) AS n
            FROM loom_telemetry.sli_minute
            WHERE slo_class != '' AND minute >= since
            GROUP BY minute
        ) AS rolled USING (minute)
"""


def _storage_series_from_rows(
    parts_rows: list[Any], refresh_rows: list[Any], *, now: Any
) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Pure transform of the two storage statements into gauge series."""
    out: dict[str, list[tuple[float, dict[str, str]]]] = {}
    for row in parts_rows:
        table, parts, wide, patch, row_total, mark_total, disk_bytes = row[:7]
        attrs = {"loom.table": str(table)}
        rpm = float(row_total or 0) / float(mark_total) if mark_total else 0.0
        out.setdefault("storage_active_parts", []).append((float(parts or 0), attrs))
        out.setdefault("storage_wide_parts", []).append((float(wide or 0), attrs))
        out.setdefault("storage_patch_parts", []).append((float(patch or 0), attrs))
        out.setdefault("storage_rows_per_mark", []).append((rpm, attrs))
        out.setdefault("storage_bytes_on_disk", []).append((float(disk_bytes or 0), attrs))
    for row in refresh_rows:
        view, status, last_success, exception = row[:4]
        attrs = {"loom.view": str(view)}
        healthy = str(status) == "Scheduled" and not exception
        out.setdefault("storage_view_refresh_scheduled", []).append(
            (1.0 if healthy else 0.0, attrs)
        )
        if last_success is not None:
            ts = last_success if last_success.tzinfo else last_success.replace(tzinfo=UTC)
            age = max(0.0, (now - ts).total_seconds())
            out.setdefault("storage_view_refresh_age_s", []).append((age, attrs))
    return out


def _probe_storage() -> dict[str, list[tuple[float, dict[str, str]]]]:
    """The storage housekeeping gauges; cached five minutes.
    A refused `system.view_refreshes` (a login without the grant, an engine
    without refreshable views) leaves the parts readings in place."""
    import datetime as _dt

    client = _probe_client()
    parts = list(
        client.query(_STORAGE_PARTS_SQL, parameters={"tables": list(STORAGE_TABLES)}).result_rows
    )
    try:
        refreshes = list(client.query(_VIEW_REFRESH_SQL).result_rows)
    except Exception as e:  # noqa: BLE001 — half a reading beats none
        log.debug("view_refreshes probe failed: %s", e)
        refreshes = []
    series = _storage_series_from_rows(parts, refreshes, now=_dt.datetime.now(_dt.UTC))
    try:
        lag = list(client.query(_SLI_LAG_SQL).result_rows)
    except Exception as e:  # noqa: BLE001 — a login without SELECT on loom_telemetry
        log.debug("sli rollup lag probe failed: %s", e)
        lag = []
    if lag:
        series["sli_rollup_lag_s"] = [(float(lag[0][0]), {})]
        try:
            missed = client.query(_SLI_MISSED_SQL).result_rows
        except Exception as e:  # noqa: BLE001 — a login without SELECT on loom.agent_events
            log.debug("sli rollup missed-rows probe failed: %s", e)
        else:
            series["sli_rollup_missed_rows"] = [(float(missed[0][0] or 0), {})]
    return series


# How many agents each agent trust has against its --max-agents, counted as
# `server.auth.agents._made` counts them for the cap: every live namespace in
# the trust's org carrying its mark (`agents.tag`); a released one does not
# count. One row per trust and per marked namespace, so the census is small.
_AGENT_TRUSTS_SQL = f"""
        SELECT t.org AS org, t.name AS trust, t.max_agents AS max_agents,
               countIf(n.namespace != '') AS made
        FROM (
            SELECT org, name, max_agents, concat('machine:', name) AS mark
            FROM loom.machine_identities FINAL
            WHERE active = 1 AND agent_claim != ''
        ) AS t
        LEFT JOIN (
            SELECT org, namespace, mark
            FROM loom.namespaces FINAL
            ARRAY JOIN tags AS mark
            WHERE startsWith(mark, 'machine:') AND active = 1
        ) AS n ON n.org = t.org AND n.mark = t.mark
        GROUP BY t.org, t.name, t.max_agents
        SETTINGS {_FINAL_SINGLE_PASS}
"""


def _agent_trust_series_from_rows(rows: list[Any]) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Pure transform of `_AGENT_TRUSTS_SQL` rows: agents made, and the share
    of --max-agents they use, per trust."""
    out: dict[str, list[tuple[float, dict[str, str]]]] = {
        "agent_trust_agents": [],
        "agent_trust_used_ratio": [],
    }
    for org, trust, max_agents, made in rows:
        attrs = {"loom.org": str(org), "loom.trust": str(trust)}
        out["agent_trust_agents"].append((float(made), attrs))
        out["agent_trust_used_ratio"].append((float(made) / int(max_agents), attrs))
    return out


def _probe_agent_trusts() -> dict[str, list[tuple[float, dict[str, str]]]]:
    """The agent trusts' headroom; cached five minutes."""
    rows = _probe_client().query(_AGENT_TRUSTS_SQL).result_rows
    return _agent_trust_series_from_rows(list(rows))


def _state_gauge_probes() -> dict[str, Any]:
    """The probe callables `register_state_gauges` wires, named once."""
    # The fast probe (per-replica counters, 30 s) runs in
    # every process; the whole-table censuses run where the lease is.
    return {
        "ch_probe": _probe_state,
        "code_probe": _census_only(_probe_code_state),
        "retention_probe": _census_only(_probe_retention),
        "agent_labels_probe": _census_only(_probe_agent_labels),
        "storage_probe": _census_only(_probe_storage),
        "agent_trusts_probe": _census_only(_probe_agent_trusts),
    }


def _probe_retention() -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Per-org retention census; the caller caches it on the slow clock."""
    client = _probe_client()
    rows = client.query(_RETENTION_SQL, parameters={"stale": RETENTION_STALE_DAYS}).result_rows
    return _retention_series_from_rows(list(rows))


def _probe_code_state() -> dict[str, list[tuple[float, dict[str, str]]]]:
    """One round-trip for every per-namespace code-plane gauge.

    Per namespace, so a stalled second repo is not hidden behind a healthy
    first, with the compiler tiers, the PR cursor, indexing errors, the embed
    backlog and symbol counts beside the watermarks. One UNION ALL keeps it
    a single query; its own 60s cache in `register_state_gauges` keeps the
    watermark read to one a minute, and the FINAL counts ride their own
    hourly clock (below).

    Returns ``{series_key: [(value, {"loom.org": .., "loom.namespace": ..}), ...]}``.
    """
    client = _probe_client()
    rows = list(client.query(_CODE_STATE_SQL).result_rows)
    # The FINAL counts over the four code tables read every live code row (GBs a call on
    # a large deployment) for figures that move once an index run; the watermark rows
    # above cost nothing. So the counts refresh on their own, slower clock:
    # LOOM_OTEL_CODE_COUNTS_CACHE_S, default an hour, 0 = every call.
    ttl = float(settings.otel_code_counts_cache_s)
    now_m = time.monotonic()
    never = float(_CODE_COUNTS_CACHE["at"]) <= 0.0  # monotonic() may be < ttl after boot
    if ttl <= 0 or never or now_m - float(_CODE_COUNTS_CACHE["at"]) >= ttl:
        _CODE_COUNTS_CACHE["rows"] = list(client.query(_CODE_COUNTS_SQL).result_rows)
        _CODE_COUNTS_CACHE["at"] = now_m
    rows.extend(_CODE_COUNTS_CACHE["rows"])
    import datetime as _dt

    series = _code_series_from_rows(rows, now=_dt.datetime.now(_dt.UTC))
    series.update(_code_lag_series(rows))
    series.update(_grep_wrong_abstain_series(client, now_m))
    return series


# The code.grep wrong-abstain gate — the ledger's literal
# abstains of the trailing 24 h recounted against the live chunks, one scan
# per namespace that has any (hundreds of MB of `content` on a large repo). Its
# own clock: LOOM_OTEL_CODE_GREP_REPLAY_CACHE_S, default an hour, 0 = off.
_GREP_REPLAY_CACHE: dict[str, Any] = {"at": 0.0, "series": {}}


def _grep_wrong_abstain_series(
    client: Any, now_m: float
) -> dict[str, list[tuple[float, dict[str, str]]]]:
    ttl = float(settings.otel_code_grep_replay_cache_s)
    if ttl <= 0:
        return {}
    never = float(_GREP_REPLAY_CACHE["at"]) <= 0.0
    if never or now_m - float(_GREP_REPLAY_CACHE["at"]) >= ttl:
        from server.observability import grep_replay_probe

        _GREP_REPLAY_CACHE["series"] = grep_replay_probe.probe(client, hours=24)
        _GREP_REPLAY_CACHE["at"] = now_m
    return dict(_GREP_REPLAY_CACHE["series"])


# A git object id. The watermarks come from `loom.code_index_state`, and a
# compiler-tier watermark is the `commit_sha` a namespace writer sent to
# code.scip_ingest: a value starting with `-` would reach `git log` as an
# option (`--output=<path>` writes a file), so anything else is refused.
_OBJECT_ID = re.compile(r"[0-9a-f]{7,64}")


def _git_lag(git_dir: str, since: str, until: str) -> tuple[int, float]:
    """First-parent commits in since..until and the committer time (unix s) of the
    oldest of them, via the bare clone; (0, 0.0) when caught up, (-1, 0.0) if unknown
    (including a watermark that is not an object id). Synchronous on purpose: the
    probe runs off the export thread."""
    import subprocess

    if since == until:
        return 0, 0.0
    if not (_OBJECT_ID.fullmatch(since) and _OBJECT_ID.fullmatch(until)):
        return -1, 0.0
    try:
        out = subprocess.run(
            ["git", "-C", git_dir, "log", "--first-parent", "--format=%ct", f"{since}..{until}"],
            capture_output=True,
            text=True,
            timeout=20,  # not a setting: past it the probe reports unknown, not a slow mirror
            check=True,
        ).stdout.split()
        # newest first: the last line is the first commit the watermark has not reached
        return len(out), float(out[-1]) if out else 0.0
    except Exception:  # noqa: BLE001 — unknown, never a crash in the probe
        return -1, 0.0


def _git_tip(git_dir: str, branch: str) -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "-C", git_dir, "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=20,  # not a setting: past it the probe reports unknown, not a slow mirror
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — an unreadable repo exports no lag point
        return ""


def _code_lag_series(
    rows: Any, *, tip_of: Any = _git_tip, lag_of: Any = _git_lag, now: float | None = None
) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Per-tier lag between a watermark and what it trails, per configured repo.

    The age gauges measure time since a watermark last advanced, which for a
    release branch that receives a backport a week apart is the age of the
    last commit, not staleness. Lag is the honest signal, in two units:

    - commits: `index_lag_commits` = git_head..tip, `scip_lag_commits{tier}` =
      <tier watermark>..git_head for every compiler tier with a watermark;
    - seconds: `tier_lag_seconds{tier}` = now minus the committer time of the
      oldest commit the tier has not reached (tier `fast` against the tip, a
      compiler tier against git_head); 0 when caught up. A watermark that stops
      while git_head moves grows this without bound, whatever the worker's last
      status says.

    -1 commits (unknown) is not observed, nor is a repo whose mirror is not on this
    host (still cloning, or LOOM_GIT_HISTORY=none)."""
    import time as _time

    if settings.git_history == "none":
        return {}
    try:
        inputs = state_inputs()
        repos = inputs.code_repos()
    except Exception:  # noqa: BLE001 — an unreadable repo config exports no lag points
        return {}
    now_s = _time.time() if now is None else now
    state: dict[tuple[str, str], dict[str, str]] = {}
    for org, namespace, key, _value, text in rows:
        key = str(key)
        if key.startswith("state:"):
            state.setdefault((str(org), str(namespace)), {})[key[len("state:") :]] = str(text or "")
    out: dict[str, list[tuple[float, dict[str, str]]]] = {}

    def _emit(
        git_dir: str, series: str, since: str, until: str, base: dict[str, str], tier: str
    ) -> None:
        n, oldest = lag_of(git_dir, since, until)
        if n < 0:
            return
        # the fast tier's commit series carries no tier attribute
        out.setdefault(series, []).append(
            (float(n), base if tier == "fast" else {**base, "tier": tier})
        )
        secs = max(0.0, now_s - oldest) if n > 0 and oldest > 0 else 0.0
        out.setdefault("code_tier_lag_seconds", []).append((secs, {**base, "tier": tier}))

    for cfg in repos:
        st = state.get((cfg.org, cfg.namespace))
        if not st or not st.get("git_head"):
            continue
        base = {"loom.org": cfg.org, "loom.namespace": cfg.namespace}
        head = st["git_head"]
        tip = tip_of(cfg.git_dir, cfg.git_branch)
        if tip:
            _emit(cfg.git_dir, "code_index_lag_commits", head, tip, base, "fast")
        for t in inputs.compiler_tiers:
            if st.get(t.watermark_key):
                wm = st[t.watermark_key]
                _emit(cfg.git_dir, "code_scip_lag_commits", wm, head, base, t.producer)
    return out


def _code_series_from_rows(rows: Any, *, now: Any) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Pure transform of `_probe_code_state`'s rows into gauge series.

    Ages are `now - updated_at` of the watermark row, except `pr_cursor`,
    whose VALUE is the datetime that matters (the `updated_at` of the newest
    merged PR the sync has seen — how far behind GitHub the history is); the
    row's own `updated_at` is the fallback when the value does not parse.
    """
    import datetime as _dt

    out: dict[str, list[tuple[float, dict[str, str]]]] = {}
    for org, namespace, key, value, text in rows:
        attrs = {"loom.org": str(org), "loom.namespace": str(namespace)}
        key = str(key)
        if key.startswith("state:"):
            state_key = key[len("state:") :]
            if state_key == "reconcile":
                # value column = stale path count written by the ingest worker's
                # content reconciliation (code/app/index/reconcile.py)
                try:
                    out.setdefault("code_stale_files", []).append((float(text or 0), attrs))
                except (TypeError, ValueError):
                    pass
                continue
            series = _CODE_STATE_GAUGES.get(state_key)
            if series is None:
                continue
            ts = _dt.datetime.fromtimestamp(float(value), tz=_dt.UTC)
            if state_key == "pr_cursor" and text:
                try:
                    cursor = _dt.datetime.fromisoformat(str(text))
                    if cursor.tzinfo is None:
                        cursor = cursor.replace(tzinfo=_dt.UTC)
                    ts = cursor
                except ValueError:
                    pass
            out.setdefault(series, []).append((max(0.0, (now - ts).total_seconds()), attrs))
        elif key == "files_errored":
            out.setdefault("code_files_errored", []).append((float(value), attrs))
        elif key == "embed_backlog":
            out.setdefault("code_embed_backlog", []).append((float(value), attrs))
        elif key == "edges_unqualified":
            out.setdefault("code_edges_unqualified", []).append((float(value), attrs))
        elif key == "edges_low_confidence":
            out.setdefault("code_edges_low_confidence", []).append((float(value), attrs))
        elif key.startswith("symbols:"):
            tier = key[len("symbols:") :]
            tier = tier if tier in ("treesitter", "scip") else "other"
            out.setdefault("code_symbols", []).append((float(value), {**attrs, "tier": tier}))
    return out
