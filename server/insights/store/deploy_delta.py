"""The deploy delta's reads: the ledger (`loom.agent_events`) and
`system.query_log` for a symmetric window around a colour swap. The row types,
thresholds and rendering are `server.insights.domain.deploy_delta`."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from server.insights.domain.analysis import ANALYST_USER
from server.insights.domain.deploy_delta import (
    DeployDelta,
    ErrorRow,
    OpRow,
    PlaneRow,
    ReadRow,
    ShapeRow,
    VitalsRow,
)

# The deployment's own rows in the ledger: the colour
# containers stamp `deploy:<role>:<colour>`; rows stamped with a build but no
# label are the same deployment's too. Isolated bench
# servers carry `iso…` labels and are excluded.
# …and never a bench: a paired replay run against the live colour is the
# operator measuring, not the deployment's callers, and it can inflate a
# swap's P95 several-fold. Bench tokens carry traffic_class='bench'.
DEPLOYMENT_FILTER = (
    "(instance_label LIKE 'deploy%' OR (instance_label = '' AND build_commit != ''))"
    " AND traffic_class != 'bench'"
)

_WINDOW = (
    "ts >= {swap:DateTime} - INTERVAL {window:UInt32} MINUTE"
    " AND ts < {swap:DateTime} + INTERVAL {window:UInt32} MINUTE"
)

OPS_SQL = f"""
SELECT operation,
       countIf(ts < {{swap:DateTime}}) AS n_before,
       countIf(ts >= {{swap:DateTime}}) AS n_after,
       countIf(success = 0 AND ts < {{swap:DateTime}}) AS err_before,
       countIf(success = 0 AND ts >= {{swap:DateTime}}) AS err_after,
       quantileIf(0.5)(latency_ms, ts < {{swap:DateTime}}) AS p50_before,
       quantileIf(0.5)(latency_ms, ts >= {{swap:DateTime}}) AS p50_after,
       quantileIf(0.95)(latency_ms, ts < {{swap:DateTime}}) AS p95_before,
       quantileIf(0.95)(latency_ms, ts >= {{swap:DateTime}}) AS p95_after,
       sumIf(upstream_cost_usd, ts < {{swap:DateTime}}) AS usd_before,
       sumIf(upstream_cost_usd, ts >= {{swap:DateTime}}) AS usd_after
FROM loom.agent_events
WHERE {_WINDOW} AND {DEPLOYMENT_FILTER}
GROUP BY operation
ORDER BY n_before + n_after DESC
"""

ERRORS_SQL = f"""
SELECT operation, error_code,
       countIf(ts < {{swap:DateTime}}) AS n_before,
       countIf(ts >= {{swap:DateTime}}) AS n_after
FROM loom.agent_events
WHERE {_WINDOW} AND {DEPLOYMENT_FILTER} AND success = 0 AND error_code != ''
GROUP BY operation, error_code
ORDER BY n_after DESC, n_before DESC
"""

BUILDS_SQL = f"""
SELECT if(ts < {{swap:DateTime}}, 'before', 'after') AS side,
       arraySort(groupUniqArray(build_commit)) AS builds,
       count() AS calls
FROM loom.agent_events
WHERE {_WINDOW} AND {DEPLOYMENT_FILTER}
GROUP BY side
"""

# The dashboard's own paint, per route, before and
# after the swap — the `ui.vitals` rows the dashboard shell posts once per page
# view (route, LCP of the first view, long tasks, DOM nodes, /v1 calls).
VITALS_SQL = f"""
SELECT args['route'] AS route,
       countIf(ts < {{swap:DateTime}}) AS n_before,
       countIf(ts >= {{swap:DateTime}}) AS n_after,
       quantileIf(0.5)(toFloat64OrZero(args['lcp_ms']),
                       ts < {{swap:DateTime}} AND args['lcp_ms'] != '') AS lcp_before,
       quantileIf(0.5)(toFloat64OrZero(args['lcp_ms']),
                       ts >= {{swap:DateTime}} AND args['lcp_ms'] != '') AS lcp_after,
       quantileIf(0.5)(toFloat64OrZero(args['long_task_ms']), ts < {{swap:DateTime}}) AS lt_before,
       quantileIf(0.5)(toFloat64OrZero(args['long_task_ms']), ts >= {{swap:DateTime}}) AS lt_after,
       quantileIf(0.5)(toFloat64OrZero(args['inp_ms']),
                       ts < {{swap:DateTime}} AND args['inp_ms'] != '') AS inp_before,
       quantileIf(0.5)(toFloat64OrZero(args['inp_ms']),
                       ts >= {{swap:DateTime}} AND args['inp_ms'] != '') AS inp_after,
       quantileIf(0.5)(toFloat64OrZero(args['api_calls']), ts < {{swap:DateTime}}) AS calls_before,
       quantileIf(0.5)(toFloat64OrZero(args['api_calls']), ts >= {{swap:DateTime}}) AS calls_after,
       quantileIf(0.5)(toFloat64OrZero(args['dom_nodes']), ts < {{swap:DateTime}}) AS nodes_before,
       quantileIf(0.5)(toFloat64OrZero(args['dom_nodes']), ts >= {{swap:DateTime}}) AS nodes_after
FROM loom.agent_events
WHERE {_WINDOW} AND {DEPLOYMENT_FILTER} AND operation = 'ui.vitals' AND success = 1
GROUP BY route
ORDER BY n_before + n_after DESC
"""


# What a search RETURNED, by plane, before → after. A swap can leave every
# latency and error count flat and still change what callers see (e.g. a chunk
# floor that starts filling a fact-only namespace's windows with chunk ids).
# The plane is read from the key shape the legs emit:
# `chunk:` (loom.memory_chunks), `summary:` / `entity_summary:` (digests),
# `observation:`, `record:` and every other key is a fact row. Read from
# loom.search_trace_log (one row per memory.search, every returned key), per
# namespace, so a floor tuned for one workload is seen where it lands.
PLANES_SQL = f"""
SELECT namespace,
       countIf(ts < {{swap:DateTime}}) AS n_before,
       countIf(ts >= {{swap:DateTime}}) AS n_after,
       sumIf(length(returned_keys), ts < {{swap:DateTime}}) AS keys_before,
       sumIf(length(returned_keys), ts >= {{swap:DateTime}}) AS keys_after,
       sumIf(arrayCount(k -> startsWith(k, 'chunk:'), returned_keys),
             ts < {{swap:DateTime}}) AS chunk_before,
       sumIf(arrayCount(k -> startsWith(k, 'chunk:'), returned_keys),
             ts >= {{swap:DateTime}}) AS chunk_after,
       sumIf(arrayCount(k -> startsWith(k, 'summary:') OR startsWith(k, 'entity_summary:'),
                        returned_keys), ts < {{swap:DateTime}}) AS summary_before,
       sumIf(arrayCount(k -> startsWith(k, 'summary:') OR startsWith(k, 'entity_summary:'),
                        returned_keys), ts >= {{swap:DateTime}}) AS summary_after,
       sumIf(arrayCount(k -> startsWith(k, 'observation:') OR startsWith(k, 'record:'),
                        returned_keys), ts < {{swap:DateTime}}) AS other_before,
       sumIf(arrayCount(k -> startsWith(k, 'observation:') OR startsWith(k, 'record:'),
                        returned_keys), ts >= {{swap:DateTime}}) AS other_after
FROM loom.search_trace_log
WHERE {_WINDOW}
GROUP BY namespace
ORDER BY n_before + n_after DESC
"""

# Whether a read ANSWERED. `out_found` (single-row reads) and
# `out_found_n` / `out_missing_n` (bulk reads) are the ledger's read-outcome
# columns (`event_args._read_outcome_columns`); a miss is a 200 with success=1,
# so the error table never sees it. Per operation, before → after.
READS_SQL = f"""
SELECT operation,
       countIf(ts < {{swap:DateTime}}) AS n_before,
       countIf(ts >= {{swap:DateTime}}) AS n_after,
       sumIf(if(args['out_found'] = 'false', 1, toUInt64OrZero(args['out_missing_n'])),
             ts < {{swap:DateTime}}) AS miss_before,
       sumIf(if(args['out_found'] = 'false', 1, toUInt64OrZero(args['out_missing_n'])),
             ts >= {{swap:DateTime}}) AS miss_after,
       sumIf(if(args['out_found'] != '', 1, toUInt64OrZero(args['out_found_n'])
                                           + toUInt64OrZero(args['out_missing_n'])),
             ts < {{swap:DateTime}}) AS asked_before,
       sumIf(if(args['out_found'] != '', 1, toUInt64OrZero(args['out_found_n'])
                                           + toUInt64OrZero(args['out_missing_n'])),
             ts >= {{swap:DateTime}}) AS asked_after
FROM loom.agent_events
WHERE {_WINDOW} AND {DEPLOYMENT_FILTER} AND success = 1
  AND (args['out_found'] != '' OR args['out_missing_n'] != '')
GROUP BY operation
ORDER BY n_before + n_after DESC
"""

# `query` is read only for the shapes the aggregate kept (a second statement
# filtered by hash), never for every row of the window: the column holds the
# whole statement text, 65 KB when it carries a vector literal.
_QL_WINDOW = (
    "type = 'QueryFinish' AND is_initial_query AND query_kind IN ('Select', 'Insert')"
    " AND event_time >= {swap:DateTime} - INTERVAL {window:UInt32} MINUTE"
    " AND event_time < {swap:DateTime} + INTERVAL {window:UInt32} MINUTE"
    " AND http_user_agent LIKE {ua:String}"
    # The analyst user's statements are callers' SQL and the probe, not shapes.
    " AND user != {analyst:String}"
)


_CLUSTER_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _query_log_source(cluster: str) -> str:
    """system.query_log of every replica of `cluster`, or of this one for ''.
    The name is spliced into a string literal, so it must be a plain name."""
    if not cluster:
        return "system.query_log"
    if not _CLUSTER_NAME.fullmatch(cluster):
        raise ValueError(f"not a cluster name: {cluster!r}")
    return f"clusterAllReplicas('{cluster}', system.query_log)"


def shapes_sql(cluster: str) -> str:
    src = _query_log_source(cluster)
    return f"""
SELECT normalized_query_hash AS shape,
       countIf(event_time < {{swap:DateTime}}) AS n_before,
       countIf(event_time >= {{swap:DateTime}}) AS n_after,
       sumIf(read_bytes, event_time < {{swap:DateTime}}) AS bytes_before,
       sumIf(read_bytes, event_time >= {{swap:DateTime}}) AS bytes_after,
       sumIf(query_duration_ms, event_time < {{swap:DateTime}}) AS ms_before,
       sumIf(query_duration_ms, event_time >= {{swap:DateTime}}) AS ms_after
FROM {src}
WHERE {_QL_WINDOW}
GROUP BY shape
HAVING n_before + n_after >= {{min_runs:UInt32}}
ORDER BY ms_after + bytes_after / 1000000 DESC
LIMIT {{top:UInt32}}
"""


def samples_sql(cluster: str) -> str:
    src = _query_log_source(cluster)
    return f"""
SELECT normalized_query_hash AS shape,
       substring(replaceRegexpAll(query, '\\\\s+', ' '), 1, 140) AS sample,
       log_comment
FROM {src}
WHERE {_QL_WINDOW} AND normalized_query_hash IN {{shapes:Array(UInt64)}}
ORDER BY event_time DESC
LIMIT 1 BY shape
"""


def _f(v: Any) -> float:
    return float(v or 0.0)


def _i(v: Any) -> int:
    return int(v or 0)


async def collect(
    ch: Any,
    *,
    swap_at: datetime,
    window_min: int = 60,
    ch_os_user: str = "loom",
    cluster: str = "default",
    top: int = 25,
    min_runs: int = 20,
) -> DeployDelta:
    """Read both records for `window_min` minutes on each side of `swap_at`.

    `ch_os_user` selects the deployment's own ClickHouse sessions in
    `system.query_log` by the client's `os_user` (the colour containers run as
    `loom`; isolated servers on the host run as the operator). `cluster` names
    the cluster for `clusterAllReplicas`; '' reads the connected replica only.
    A query_log read that fails (no cluster grant, a self-hosted server
    without the function) leaves the ledger half intact and says so.
    """
    swap = swap_at.astimezone(UTC).replace(tzinfo=None)
    params: dict[str, Any] = {"swap": swap, "window": int(window_min)}
    out = DeployDelta(swap_at=swap_at, window_min=int(window_min))
    for row in await ch.query_all(BUILDS_SQL, params=params):
        builds = [str(b) for b in (row.get("builds") or []) if b]
        if row.get("side") == "before":
            out.builds_before, out.calls_before = builds, _i(row.get("calls"))
        else:
            out.builds_after, out.calls_after = builds, _i(row.get("calls"))
    out.ops = [
        OpRow(
            operation=str(r["operation"]),
            n_before=_i(r["n_before"]),
            n_after=_i(r["n_after"]),
            err_before=_i(r["err_before"]),
            err_after=_i(r["err_after"]),
            p50_before=_f(r["p50_before"]),
            p50_after=_f(r["p50_after"]),
            p95_before=_f(r["p95_before"]),
            p95_after=_f(r["p95_after"]),
            usd_before=_f(r["usd_before"]),
            usd_after=_f(r["usd_after"]),
        )
        for r in await ch.query_all(OPS_SQL, params=params)
    ]
    out.errors = [
        ErrorRow(str(r["operation"]), str(r["error_code"]), _i(r["n_before"]), _i(r["n_after"]))
        for r in await ch.query_all(ERRORS_SQL, params=params)
    ]
    try:
        out.vitals = [
            VitalsRow(
                route=str(r["route"] or "?"),
                n_before=_i(r["n_before"]),
                n_after=_i(r["n_after"]),
                lcp_before=_f(r["lcp_before"]),
                lcp_after=_f(r["lcp_after"]),
                lt_before=_f(r["lt_before"]),
                lt_after=_f(r["lt_after"]),
                inp_before=_f(r.get("inp_before", 0.0)),
                inp_after=_f(r.get("inp_after", 0.0)),
                calls_before=_f(r["calls_before"]),
                calls_after=_f(r["calls_after"]),
                nodes_before=_f(r["nodes_before"]),
                nodes_after=_f(r["nodes_after"]),
            )
            for r in await ch.query_all(VITALS_SQL, params=params)
        ]
    except Exception:  # noqa: BLE001 — a ledger behind the ui.vitals op has no rows, not an error
        out.vitals = []
    try:
        out.planes = [
            PlaneRow(
                namespace=str(r["namespace"] or "?"),
                n_before=_i(r["n_before"]),
                n_after=_i(r["n_after"]),
                keys_before=_i(r["keys_before"]),
                keys_after=_i(r["keys_after"]),
                chunk_before=_i(r["chunk_before"]),
                chunk_after=_i(r["chunk_after"]),
                summary_before=_i(r["summary_before"]),
                summary_after=_i(r["summary_after"]),
                other_before=_i(r["other_before"]),
                other_after=_i(r["other_after"]),
            )
            for r in await ch.query_all(PLANES_SQL, params=params)
        ]
    except Exception:  # noqa: BLE001 — a deployment with the trace log off has no rows, not an error
        out.planes = []
    try:
        out.reads = [
            ReadRow(
                operation=str(r["operation"]),
                n_before=_i(r["n_before"]),
                n_after=_i(r["n_after"]),
                asked_before=_i(r["asked_before"]),
                asked_after=_i(r["asked_after"]),
                miss_before=_i(r["miss_before"]),
                miss_after=_i(r["miss_after"]),
            )
            for r in await ch.query_all(READS_SQL, params=params)
        ]
    except Exception:  # noqa: BLE001 — each section stands on its own
        out.reads = []
    ql = dict(
        params,
        ua=f"%os_user:{ch_os_user}%",
        analyst=ANALYST_USER,
        top=int(top),
        min_runs=int(min_runs),
    )
    try:
        rows = await ch.query_all(shapes_sql(cluster), params=ql)
        out.shapes = [
            ShapeRow(
                shape=_i(r["shape"]),
                n_before=_i(r["n_before"]),
                n_after=_i(r["n_after"]),
                bytes_before=_i(r["bytes_before"]),
                bytes_after=_i(r["bytes_after"]),
                ms_before=_i(r["ms_before"]),
                ms_after=_i(r["ms_after"]),
            )
            for r in rows
        ]
        if out.shapes:
            ids = [s.shape for s in out.shapes]
            texts = await ch.query_all(samples_sql(cluster), params=dict(ql, shapes=ids))
            by_id = {_i(t["shape"]): t for t in texts}
            for s in out.shapes:
                t = by_id.get(s.shape)
                if t:
                    s.sample, s.log_comment = (
                        str(t.get("sample") or ""),
                        str(t.get("log_comment") or ""),
                    )
    except Exception as e:  # noqa: BLE001 — the ledger half stands on its own
        out.shapes_error = f"{type(e).__name__}: {e}"
    return out


# --- flags -------------------------------------------------------------------
# Coarse on purpose: a quiet swap prints none. Each flag names the row and the
# two readings so the reader can decide whether it is the build or the hour.
