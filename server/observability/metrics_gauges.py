"""State gauges for Loom — the observable gauges `otel.py` registers once the
MeterProvider exists, and the in-process levels some of them read.

Levels, not events: OTel observable gauges invoke a callback at export time,
so they cost nothing on the request path. Any callback that touches ClickHouse
is cached and never produces inline (`_TTLCache`); the probes behind them are
`otel_probes`. The cardinality rules of `metrics.py` hold here too.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from server.observability.metrics import _clamp_ns
from server.settings import settings as _settings

log = logging.getLogger(__name__)

# Current failure streak per worker, written by the supervisor after every
# pass and read by the `loom.worker.consecutive_failures` gauge callback. A
# plain dict under a lock, not a CH probe: the supervisor already knows the
# number, and a gauge that needs a round-trip to report "the worker is down"
# would go dark exactly when CH is the thing that is down.
_worker_failure_streaks: dict[str, int] = {}
_worker_failure_lock = threading.Lock()
# Failures a pass noted and survived (`pass_context.note_failure`),
# per worker, for the most recent completed pass.
_worker_partial_failures: dict[str, int] = {}


class _TTLCache:
    """Single-value cache so gauge callbacks don't hammer — or wait on — ClickHouse.

    The metric reader exports every 3s. Gauge callbacks that query CH would
    otherwise issue ~20 queries/minute each, forever, to populate a number
    that changes slowly. Backlogs and row counts are fine to see at 30s
    resolution.

    The refresh is stale-while-revalidate in a background thread, never inline.
    The producer behind the state gauges is a multi-join FINAL query against CH
    Cloud that routinely takes longer than the reader's 2.5s export timeout, and
    the callback runs inside `collect()` — producing inline would mean a
    `MetricsTimeoutError` and a dropped export cycle (all instruments, not just
    the gauges) on every cache expiry.
    `get()` returns the last value immediately (None before the first refresh
    lands, and while a census producer's process does not hold the lease,
    `_census_only`; observers must render it as "no observation", not 0) and
    kicks off at most one refresh once the TTL has lapsed.
    """

    def __init__(self, ttl_s: float, producer: Callable[[], Any]) -> None:
        self._ttl = ttl_s
        self._producer = producer
        # A census producer's lease check (`otel_probes._census_only`).
        self._reads_here: Callable[[], bool] | None = getattr(producer, "reads_here", None)
        self._value: Any = None
        self._fetched_at = 0.0
        self._refreshing = False
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        failed = False
        try:
            v = self._producer()
        except Exception as e:  # noqa: BLE001 — never let telemetry break anything
            log.debug("gauge producer failed: %s", e)
            v, failed = None, True
        with self._lock:
            # A failed probe keeps the last reading; a None answer (off the lease) clears it.
            if not failed:
                self._value = v
            # Failures back off for one TTL; skipped censuses remain due.
            skipped = v is None and not failed
            self._fetched_at = 0.0 if skipped else time.monotonic()
            self._refreshing = False

    def get(self) -> Any:
        start = False
        with self._lock:
            # A census another process takes is not this one's to export,
            # whatever it read while it held the lease. In memory, no ClickHouse.
            # Back on the lease, the first read is never fetched and refreshes at once.
            if self._reads_here is not None and not self._reads_here():
                self._value, self._fetched_at = None, 0.0
                return None
            # "Never fetched" is always due: monotonic() counts from boot, so on
            # a host up for less than one TTL (a fresh CI runner, a container
            # started right after a reboot) `now - 0.0 >= ttl` is false and a
            # 15-minute cache would take no first refresh.
            never = self._fetched_at <= 0.0
            if not self._refreshing and (
                never or (time.monotonic() - self._fetched_at) >= self._ttl
            ):
                self._refreshing = start = True
            value = self._value
        if start:
            threading.Thread(target=self._refresh, name="loom-gauge-refresh", daemon=True).start()
        return value


# `code_probe` returns {key: [(value, {attr: val}), ...]} — one series per
# (org, namespace[, tier]) — so a stalled second repo cannot hide behind a
# deployment-wide max().
CodeSeries = dict[str, list[tuple[float, dict[str, str]]]]


def _register_agent_labels_gauge(meter: Any, probe: Callable[[], CodeSeries] | None) -> None:
    """Agent-sourced usage labels: a day's `used` / `acted` / `harm` per
    namespace and kind. No probe, no gauge. Cached 15 minutes."""
    if probe is None:
        return
    cache = _TTLCache(900.0, probe)

    def _cb(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        data = cache.get() or {}
        return [Observation(v, attributes=a) for v, a in data.get("agent_labels_24h", [])]

    meter.create_observable_gauge(
        name="loom.memory.agent_labels_24h",
        callbacks=[_cb],
        description="Usage labels an agent wrote in the last 24 h per namespace and kind "
        "(used = memory.outcome used / run.complete memories_used, acted, harm); "
        "memory.ask's own cited-key credit is excluded. The top "
        "LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES namespaces are named, the "
        "rest are summed under loom.namespace=other",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.memory.agent_labels_namespaces_at_pace",
        callbacks=[_cached_series(cache, "agent_labels_namespaces_at_pace")],
        description="Namespaces per org whose agent-written labels of the last 24 h are "
        "at or above a daily pace of 1,000 a month / 30; "
        "read with agent_labels_24h, which names only the top N",
        unit="1",
    )


# The storage housekeeping readings, one series per table
# (`loom.table`, a closed list in otel_probes.STORAGE_TABLES) or per refreshable view
# (`loom.view`). Each row names the threshold an operator DROP step waits on.
_STORAGE_GAUGES: tuple[tuple[str, str, str, str], ...] = (
    (
        "loom.ch.table.rows_per_mark",
        "storage_rows_per_mark",
        "sum(rows) / sum(marks) over the active parts of a table ("
        "index_granularity = 256): granularity lands on merge, "
        "so parts written at a coarser granularity read higher until they merge",
        "1",
    ),
    (
        "loom.ch.table.active_parts",
        "storage_active_parts",
        "Active parts of a housekept table",
        "1",
    ),
    (
        "loom.ch.table.wide_parts",
        "storage_wide_parts",
        "Active Wide parts of a table (parts from 128 MiB are written Wide on their next merge)",
        "1",
    ),
    (
        "loom.ch.table.patch_parts",
        "storage_patch_parts",
        "Active patch parts per table; the live vector table must read 0",
        "1",
    ),
    (
        "loom.ch.table.bytes_on_disk",
        "storage_bytes_on_disk",
        "Bytes on disk of the active parts of a housekept table — what a "
        "DROP TABLE of the dead twin gives back",
        "By",
    ),
    (
        "loom.ch.view_refresh.scheduled",
        "storage_view_refresh_scheduled",
        "1 while a refreshable view is Scheduled with no exception, else 0",
        "1",
    ),
    (
        "loom.ch.view_refresh.age_s",
        "storage_view_refresh_age_s",
        "Seconds since a refreshable view's last successful refresh; over 120 "
        "on a one-minute view is a stalled refresh",
        "s",
    ),
    (
        "loom.sli.rollup_lag_s",
        "sli_rollup_lag_s",
        "Seconds loom_telemetry.sli_minute's mark trailed now at the last storage "
        "probe (every five minutes, on the workers-lease holder): about 70 to 130 "
        "when current; more is a rollup catching up, whose newest minutes read empty",
        "s",
    ),
    (
        "loom.sli.rollup_missed_rows",
        "sli_rollup_missed_rows",
        "Ledger request rows of the last day the SLI rollup's mark passed before "
        "their insert committed, so loom_telemetry.sli_minute never counts them "
        "(storage probe, every five minutes); above 0 the SLIs undercount",
        "1",
    ),
)


def _register_storage_gauges(meter: Any, probe: Callable[[], CodeSeries] | None) -> None:
    """Storage housekeeping gauges. No probe, no gauges.
    Cached five minutes: system.parts metadata, moving on merges."""
    if probe is None:
        return
    cache = _TTLCache(300.0, probe)
    for name, key, description, unit in _STORAGE_GAUGES:
        meter.create_observable_gauge(
            name=name,
            callbacks=[_cached_series(cache, key)],
            description=description,
            unit=unit,
        )


def _register_agent_trust_gauges(meter: Any, probe: Callable[[], CodeSeries] | None) -> None:
    """How far each agent trust is towards its --max-agents, per `loom.trust`
    (operator-registered, so bounded). No probe, no gauges. Cached five minutes."""
    if probe is None:
        return
    cache = _TTLCache(300.0, probe)
    meter.create_observable_gauge(
        name="loom.auth.trust_agents",
        callbacks=[_cached_series(cache, "agent_trust_agents")],
        description="Live agents of an active workload trust (a released one does not count), "
        "as its --max-agents counts them, by loom.org and loom.trust",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.auth.trust_agents_used_ratio",
        callbacks=[_cached_series(cache, "agent_trust_used_ratio")],
        description="loom.auth.trust_agents over the trust's --max-agents; at 1 every "
        "new agent of the trust is refused",
        unit="1",
    )


def _register_code_lag_gauges(meter: Any, series: Callable[[str], Callable[[Any], Any]]) -> None:
    """Per-tier lag, in commits and in seconds, from `code_probe`'s series."""
    meter.create_observable_gauge(
        name="loom.code.index_lag_commits",
        callbacks=[series("code_index_lag_commits")],
        description="First-parent commits between the fast-tier watermark "
        "(git_head) and the fetched branch tip, per namespace. 0 = current. "
        "This, not index_age_seconds, is the staleness alert: a release "
        "branch with no new commits is current however old its last one.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.scip_lag_commits",
        callbacks=[series("code_scip_lag_commits")],
        description="First-parent commits between a compiler tier's watermark "
        "and the fast tier (git_head), per namespace and `tier` (clang, "
        "python, typescript, go; every tier with a watermark). 0 = converged.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.tier_lag_seconds",
        callbacks=[series("code_tier_lag_seconds")],
        description="Seconds since the oldest commit a tier has not reached "
        "landed (committer time), per namespace and `tier`: `fast` against "
        "the fetched branch tip, a compiler tier against git_head. 0 = caught "
        "up. A frozen watermark while git_head moves grows it without bound; "
        "this is the tier-lag alert.",
        unit="s",
    )


def _cached_series(cache: _TTLCache | None, key: str) -> Callable[[Any], Any]:
    """A gauge callback reading one attributed series out of a TTL cache."""

    def _cb(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        data = (cache.get() if cache is not None else None) or {}
        return [Observation(v, attributes=a) for v, a in data.get(key, [])]

    return _cb


def _level_reading(cache: _TTLCache, key: str) -> Callable[[Any], Any]:
    """A gauge callback reading one unattributed level out of the state probe's cache."""

    def _cb(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        data = cache.get() or {}
        v = data.get(key)
        return [Observation(v)] if v is not None else []

    return _cb


def _register_backlog_gauges(meter: Any, _reading: Callable[[str], Callable[[Any], Any]]) -> None:
    """Embedding backlog and vector count, from the state probe."""
    meter.create_observable_gauge(
        name="loom.embed.pending",
        callbacks=[_reading("embed_pending")],
        description="Rows awaiting embedding (embedding_dim=0) — worker backlog",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.vectors.total",
        callbacks=[_reading("vectors_total")],
        description="Embedded vectors present, for capacity planning against "
        "the brute-force/index threshold",
        unit="1",
    )


def _register_process_gauges(meter: Any) -> None:
    """File descriptors and the ClickHouse slot gate of this process; no store is touched."""

    # Process descriptors: fd exhaustion otherwise first shows as every
    # embedding pass failing. Each process
    # reports its own count against its soft limit, so a leak reads as a
    # rising line hours before EMFILE. No store is touched.
    def _fd_cb(which: str) -> Callable[[Any], Any]:
        def _cb(_options: Any) -> Any:
            from opentelemetry.metrics import Observation

            open_fds, limit = process_fd_reading()
            value = open_fds if which == "open" else limit
            if value < 0:
                return []
            return [Observation(value, attributes={"loom.pid": str(os.getpid())})]

        return _cb

    meter.create_observable_gauge(
        name="loom.process.open_fds",
        callbacks=[_fd_cb("open")],
        description="Open file descriptors in this server process; a rising "
        "line is a leak, EMFILE follows when it meets loom.process.fd_limit",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.process.fd_limit",
        callbacks=[_fd_cb("limit")],
        description="Soft file-descriptor limit of this server process",
        unit="1",
    )

    # The disk under the data directory (var/: the code mirrors and checkouts).
    # A repository repack can fill it, and then every write on the host fails;
    # free bytes against total is the alarm's input.
    def _disk_cb(which: str) -> Callable[[Any], Any]:
        def _cb(_options: Any) -> Any:
            from opentelemetry.metrics import Observation

            free, total = data_disk_reading()
            value = free if which == "free" else total
            if value < 0:
                return []
            return [Observation(value, attributes={"loom.disk.path": "var"})]

        return _cb

    meter.create_observable_gauge(
        name="loom.host.disk.free_bytes",
        callbacks=[_disk_cb("free")],
        description="Bytes free to the server on the disk that holds var/; "
        "at zero every write on the host fails",
        unit="By",
    )
    meter.create_observable_gauge(
        name="loom.host.disk.total_bytes",
        callbacks=[_disk_cb("total")],
        description="Size of the disk that holds var/",
        unit="By",
    )

    # The ClickHouse slot gate of this process — one
    # semaphore the size of the connection pool. `waiters` above zero for
    # more than a moment is the process running out of connections;
    # `overloaded_total` counts the requests refused with 503 OVERLOADED
    # after waiting `ch_pool_wait_ms`; `timeouts_total` the statements
    # cut at their class deadline. Cumulative counters read as gauges so a
    # panel can `delta()` them; no store is touched.
    def _pool_cb(key: str) -> Callable[[Any], Any]:
        def _cb(_options: Any) -> Any:
            from opentelemetry.metrics import Observation

            from server.core.ch import pool_reading

            return [
                Observation(
                    int(pool_reading().get(key, 0)),
                    attributes={"loom.pid": str(os.getpid())},
                )
            ]

        return _cb

    for key, desc in (
        ("in_use", "Pooled ClickHouse connections in use by this process"),
        ("waiters", "Statements waiting for a ClickHouse pool slot in this process"),
        ("overloaded_total", "Statements refused with 503 OVERLOADED (cumulative)"),
        ("timeouts_total", "Statements cut at their class deadline (cumulative)"),
    ):
        meter.create_observable_gauge(
            name=f"loom.ch.pool.{key}",
            callbacks=[_pool_cb(key)],
            description=desc,
            unit="1",
        )


def _register_store_gauges(meter: Any, _reading: Callable[[str], Callable[[Any], Any]]) -> None:
    """Mutations, patch parts, the audit checkpoint chain and the vector index."""
    meter.create_observable_gauge(
        name="loom.ch.mutations_pending",
        callbacks=[_reading("mutations_pending")],
        description="Unfinished ClickHouse mutations — a stuck ALTER shows here",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.memory_embeddings.patch_parts",
        callbacks=[_reading("memory_embeddings_patch_parts")],
        description="Active patch parts on loom.memory_embeddings: "
        "one switches the fact leg's codes-first read off; the read-side gate "
        "follows this count, so non-zero means a writer UPDATEs the table "
        "or a rebuild has not run",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.audit.checkpoint_lag_windows",
        callbacks=[_reading("audit_checkpoint_lag_windows")],
        description="One-hour windows the audit checkpoint chain has yet to seal; "
        "rising = sealer stalled, falling = backfill",
        unit="1",
    )
    # The HNSW index's two ledgers: what it costs (adds)
    # against what it serves (searches), plus the share of recent vector
    # reads that opened it. Per replica; see _probe_state.
    meter.create_observable_gauge(
        name="loom.vector_index.searches_total",
        callbacks=[_reading("usearch_searches_total")],
        description="Cumulative usearch searches on the probed replica; flat while "
        "tenant-filtered reads pre-filter past the index",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.vector_index.adds_total",
        callbacks=[_reading("usearch_adds_total")],
        description="Cumulative vectors added to usearch graphs on the probed "
        "replica — the build cost the index charges on inserts and merges",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.vector_index.reads_indexed_ratio",
        callbacks=[_reading("vector_reads_indexed_ratio")],
        description="Share of the last 30 min of memory_embeddings vector reads "
        "that opened the HNSW index (0 = brute force served them all)",
        unit="1",
    )


def _register_integrity_gauges(meter: Any, _reading: Callable[[str], Callable[[Any], Any]]) -> None:
    """Invariants of the stored data, from the state probe."""
    # --- invariants ------------------------------------------------------
    #
    # These answer "is the data self-consistent", not "how much work is
    # queued". Both should read 0 forever; a non-zero value is a bug that has
    # already happened, not a backlog that will drain.
    meter.create_observable_gauge(
        name="loom.integrity.divergent_entities",
        callbacks=[_reading("divergent_entities")],
        description="Memories whose entities differ between loom.memory and "
        "loom.memory_embeddings — one copy is wrong and which reader you are "
        "decides what you see. Should be 0.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.integrity.orphan_vectors",
        callbacks=[_reading("orphan_vectors")],
        description="Embedded vectors whose memory row is gone — search can "
        "return a hit that resolves to nothing. Should be 0.",
        unit="1",
    )
    # Erasure residue: what a saga that stopped before
    # its census leaves readable. Both read 0 only when every erased
    # namespace's graph and entity vectors left with its memories.
    meter.create_observable_gauge(
        name="loom.integrity.orphan_entity_vectors",
        callbacks=[_reading("orphan_entity_vectors")],
        description="Entity vectors whose memory row is gone — the entity "
        "boost can still rank an erased key from its entity text. Should be 0.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.integrity.orphan_graph_namespaces",
        callbacks=[_reading("orphan_graph_namespaces")],
        description="Namespaces holding entity edges with no live memory and "
        "no live knowledge chunk — an erasure that stopped before its graph "
        "step. Should be 0.",
        unit="1",
    )
    # Registry census: live memory with no owner row.
    # Both read 0 only when every namespace holding live rows has a
    # loom.namespaces row (owner, kind, retention policy).
    meter.create_observable_gauge(
        name="loom.integrity.live_ns_unregistered",
        callbacks=[_reading("live_ns_unregistered")],
        description="Namespaces holding live memory rows with no loom.namespaces "
        "row — no owner, kind or retention policy. Should be 0.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.integrity.live_rows_unregistered",
        callbacks=[_reading("live_rows_unregistered")],
        description="Live memory rows in namespaces with no loom.namespaces row. Should be 0.",
        unit="1",
    )

    # Row-level invariants. `loom integrity-repair` reads
    # the same statements and re-emits the dim-0 rows.
    meter.create_observable_gauge(
        name="loom.integrity.dim0_with_vector",
        callbacks=[_reading("dim0_with_vector")],
        description="Embedding rows holding a vector with embedding_dim = 0 — "
        "stored, embedded, and excluded by every search leg's dim predicate. "
        "Should be 0; `loom integrity-repair --apply` re-emits them.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.integrity.tombstone_status_drift",
        callbacks=[_reading("tombstone_status_drift")],
        description="Memory rows with tombstoned_at set and status != tombstoned "
        "— dead to readers, alive to anything grouping by status. Should be 0.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.integrity.active_past_valid_to",
        callbacks=[_reading("active_past_valid_to")],
        description="Active memory rows whose own valid_to has passed and nothing "
        "retired them. Should be 0.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.integrity.high_with_embedding",
        callbacks=[_reading("high_with_embedding")],
        description="Live high-sensitivity memory rows that still have a live "
        "embedding row — a high value one similarity query away. "
        "Should be 0.",
        unit="1",
    )


def _register_retention_gauges(meter: Any, rseries: Callable[[str], Callable[[Any], Any]]) -> None:
    """The retention census, one series per org."""
    # Retention census, per org: what the store holds that
    # nobody asks for. Panels read these before anyone proposes forgetting.
    meter.create_observable_gauge(
        name="loom.retention.active_rows",
        callbacks=[rseries("retention_active_rows")],
        description="Live, active memory rows per org",
    )
    meter.create_observable_gauge(
        name="loom.retention.never_recalled_ratio",
        callbacks=[rseries("retention_never_recalled_ratio")],
        description="Share of active rows older than 7 days that no read has ever "
        "recalled (recall_count = 0), per org",
    )
    meter.create_observable_gauge(
        name="loom.retention.low_information_rows",
        callbacks=[rseries("retention_low_information_rows")],
        description="Active rows whose value is under 25 characters, per org — "
        "general content the extractor should have refused",
    )
    meter.create_observable_gauge(
        name="loom.retention.sensitive_without_expiry",
        callbacks=[rseries("retention_sensitive_without_expiry")],
        description="Active rows with a sensitivity tier and no expires_at, per org "
        "(the sensitivity retention window sets one at write when enabled)",
    )
    meter.create_observable_gauge(
        name="loom.retention.stale_candidates",
        callbacks=[rseries("retention_stale_candidates")],
        description="Active, unpinned, non-durable rows older than 45 days with no "
        "recall in 45 days, per org. A count only: Loom "
        "retires no memory by age",
    )


def _register_code_plane_gauges(meter: Any, series: Callable[[str], Callable[[Any], Any]]) -> None:
    """The code plane, one series per (loom.org, loom.namespace)."""
    #
    # Per namespace, so a stalled second repo is not hidden behind a healthy
    # first; a panel that wants the deployment-wide figure takes max() over
    # the series.
    meter.create_observable_gauge(
        name="loom.code.index_age_seconds",
        callbacks=[series("code_index_age_seconds")],
        description="Seconds since the code plane's fast-tier watermark "
        "(git_head) last advanced, per namespace. The rollout SLO reads "
        "this: ingest lag < 5 min p99. No observation = not backfilled.",
        unit="s",
    )
    meter.create_observable_gauge(
        name="loom.code.scip_age_seconds",
        callbacks=[series("code_scip_age_seconds")],
        description="Seconds since the C++ compiler tier's watermark "
        "(scip_commit) last advanced, per namespace — how long "
        "compiler-exact answers have trailed the fast tier.",
        unit="s",
    )
    meter.create_observable_gauge(
        name="loom.code.stale_files",
        callbacks=[series("code_stale_files")],
        description="Indexed paths whose live chunks predate a change to the "
        "file at the branch head (content reconciliation, hourly). Watermarks "
        "count commits walked; this counts rows that are actually current. "
        "0 = every indexed file reflects its head content.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.scip_py_age_seconds",
        callbacks=[series("code_scip_py_age_seconds")],
        description="Seconds since the Python compiler tier's watermark "
        "(scip_commit_py) last advanced, per namespace.",
        unit="s",
    )
    meter.create_observable_gauge(
        name="loom.code.pr_cursor_age_seconds",
        callbacks=[series("code_pr_cursor_age_seconds")],
        description="Seconds between now and the PR-history cursor "
        "(pr_cursor = updated_at of the newest merged PR synced), per "
        "namespace: how far behind GitHub the PR history is. Falls back "
        "to the cursor row's updated_at when the value is not a datetime.",
        unit="s",
    )

    # A configuration error is a state: it is logged once per process and
    # this gauge carries whether it still holds.
    def _repos_config_errors_cb(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        from server.observability.state_inputs import state_inputs

        n = state_inputs().repos_config_errors()
        return [Observation(n)] if n is not None else []

    meter.create_observable_gauge(
        name="loom.code.repos_config_errors",
        callbacks=[_repos_config_errors_cb],
        description="Conditions in LOOM_CODE_REPOS_JSON this process could not "
        "use at its last configured_repos() call: invalid JSON (the scalar "
        "single-repo fallback is in force), a nameless entry, a duplicate "
        "namespace. 0 = the configuration parsed clean; no observation = "
        "not evaluated yet in this process. Each condition is logged once.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.files_errored",
        callbacks=[series("code_files_errored")],
        description="Live code_files rows with a non-empty index_error, "
        "per namespace — what code.index_errors lists.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.embed_backlog",
        callbacks=[series("code_embed_backlog")],
        description="Live code_chunks stubs (embedding_dim=0, not "
        "poisoned) awaiting the code_embed_recovery worker, per namespace.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.edges_unqualified",
        callbacks=[series("code_edges_unqualified")],
        description="Parser-tier call edges whose target is a bare, "
        "unqualified name, per namespace — the shape a repo-wide name "
        "match takes when it binds `parse`/`check` to an unrelated "
        "same-named function. Compare against loom.code.symbols{tier="
        "treesitter}; a rising share means the name table grew ambiguous.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.edges_low_confidence",
        callbacks=[series("code_edges_low_confidence")],
        description="Parser-tier call edges at the 0.6 grade (unique "
        "repo-wide name match, no same-file or enclosing-class evidence), "
        "per namespace. These are what code.callers min_confidence=0.7 "
        "drops and what a graph-weighted ranking must not trust blindly.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.symbols",
        callbacks=[series("code_symbols")],
        description="Live code_symbols rows per namespace and tier "
        "(treesitter|scip) — a drop is a bad apply or tombstone sweep.",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.code.grep.wrong_abstain_24h",
        callbacks=[series("code_grep_wrong_abstain_24h")],
        description="code.grep calls of the trailing 24 h that abstained on a "
        "literal the namespace's live chunks contain, per org/namespace/"
        "consumer: the ledger's abstains recounted against "
        "the corpus in one scan. 0 = every abstain was a true no-match.",
        unit="1",
    )


def _register_worker_gauges(meter: Any) -> None:
    """Supervised worker failures and the Langfuse breaker; no store is touched."""

    def _streaks(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        with _worker_failure_lock:
            items = list(_worker_failure_streaks.items())
        return [Observation(n, attributes={"worker": w}) for w, n in items]

    meter.create_observable_gauge(
        name="loom.worker.consecutive_failures",
        callbacks=[_streaks],
        description="Current run of consecutive failed passes per "
        "supervised worker (0 = healthy). The supervisor logs DEGRADED "
        "at 3; this is the alertable form of that line.",
        unit="1",
    )

    def _partials(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        with _worker_failure_lock:
            items = list(_worker_partial_failures.items())
        return [Observation(n, attributes={"worker": w}) for w, n in items]

    meter.create_observable_gauge(
        name="loom.worker.partial_failures",
        callbacks=[_partials],
        description="Failures the most recent completed pass noted and "
        "survived (pass_context.note_failure), per supervised worker. "
        "0 = the last pass was clean.",
        unit="1",
    )

    # Breaker state needs no CH round-trip, so it gets its own callback.
    def _breaker(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        try:
            from server.observability.langfuse_scores import circuit_status

            state = circuit_status().get("state", "CLOSED")
        except Exception:  # noqa: BLE001 — breaker read must not fail export
            return []
        # 0 CLOSED (healthy) / 1 HALF_OPEN (probing) / 2 OPEN (dropping)
        code = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}.get(state, 0)
        return [Observation(code)]

    meter.create_observable_gauge(
        name="loom.langfuse.breaker",
        callbacks=[_breaker],
        description="Langfuse score-POST breaker: 0=closed 1=half-open 2=open",
        unit="1",
    )


def health_problem_series(problems: list[dict[str, Any]]) -> list[tuple[int, dict[str, str]]]:
    """One point per open `/v1/health` problem. `provider_failing:<name>`
    splits into its code and a `provider` label, so every label stays bounded."""
    out: list[tuple[int, dict[str, str]]] = []
    for p in problems:
        code, _, provider = str(p.get("code", "")).partition(":")
        attrs = {"code": code, "severity": str(p.get("severity", ""))}
        if provider:
            attrs["provider"] = provider[:32]
        out.append((1, attrs))
    return out


def code_tier_series(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[int, dict[str, str]]], list[tuple[int, dict[str, str]]]]:
    """(failing, lag) points, one per entry and tier the `code_tier_failing` problem
    reports: its failed passes in a row, labelled with the error kind and the tier's
    severity as the problem grades it, and the commits it trails the fetched tip by
    (no point when unknown). Labelled raw, as the other code-plane gauges are: the
    entries come from the config."""
    failing: list[tuple[int, dict[str, str]]] = []
    lag: list[tuple[int, dict[str, str]]] = []
    for r in rows:
        attrs = {"loom.org": r["org"], "loom.namespace": r["namespace"], "tier": r["tier"]}
        failing.append(
            (r["failed_passes"], {**attrs, "kind": r["kind"], "severity": r["severity"]})
        )
        if (behind := r["lag_commits"]) >= 0:
            lag.append((behind, attrs))
    return failing, lag


def _register_code_tier_gauges(meter: Any) -> None:
    """The failing code-index tiers this process last read; in-process, no store read."""

    def _points(series: str) -> Callable[[Any], Any]:
        def _cb(_options: Any) -> Any:
            from opentelemetry.metrics import Observation

            try:
                from server.observability.state_inputs import state_inputs

                failing, lag = code_tier_series(state_inputs().failing_code_tiers())
                points = {"failing": failing, "lag": lag}[series]
            except Exception:  # noqa: BLE001 — a gauge reading must not fail export
                return []
            return [Observation(v, attributes=a) for v, a in points]

        return _cb

    meter.create_observable_gauge(
        name="loom.code.tier_failing",
        callbacks=[_points("failing")],
        description=(
            "Failed passes in a row of a code-index tier the `code_tier_failing` health "
            "problem reports, per namespace, `tier`, error `kind` and the tier's "
            "`severity` as the problem grades it (warn, error); a tier reports from the "
            "problem's warn threshold until its next successful pass"
        ),
        # an annotation unit: an OTLP-to-Prometheus export names it loom_code_tier_failing
        unit="{pass}",
    )
    meter.create_observable_gauge(
        name="loom.code.tier_lag_commits",
        callbacks=[_points("lag")],
        description=(
            "Commits a code-index tier the `code_tier_failing` problem reports trails the "
            "fetched branch tip by, as the entry's last published status counted it (a lower "
            "bound), per namespace and `tier`; reported while its lag is known"
        ),
        unit="{commit}",
    )


def _register_health_gauges(meter: Any) -> None:
    """This process's health problems and its lost ledger rows; in-process only."""

    def _problems(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        try:
            from server.health import health_problems

            series = health_problem_series(health_problems())
        except Exception:  # noqa: BLE001 — a health read must not fail export
            return []
        return [Observation(v, attributes=a) for v, a in series]

    # A fake provider fallback, a revocation list that never loaded or a
    # drifted configuration is a problem on one process; this makes each one
    # alertable without polling every replica's /v1/health.
    meter.create_observable_gauge(
        name="loom.health.problems",
        callbacks=[_problems],
        description=(
            "Open /v1/health problems on this process, by code and severity: 1 while "
            "open; a resolved problem stops reporting, so alert on a window of at "
            "least the gauge heartbeat plus one export interval (two intervals "
            "without a heartbeat)"
        ),
        unit="1",
    )

    def _dropped(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        from server.observability.events import get_emitter

        stats = get_emitter().stats()
        return [
            Observation(stats["dropped_queue_full"], attributes={"reason": "queue_full"}),
            Observation(stats["dropped_flush_failed"], attributes={"reason": "flush_failed"}),
            Observation(stats["dropped_batch_repeat"], attributes={"reason": "batch_repeat"}),
        ]

    # Rows lost here are missing from loom.agent_events and from every SLI
    # rolled up from it, so the SLIs read better than the service was.
    meter.create_observable_counter(
        name="loom.ledger.dropped",
        callbacks=[_dropped],
        description="Ledger rows this process lost since it started, by reason",
        unit="1",
    )

    def _batch_repeats(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        from server.observability.events import get_emitter

        return [Observation(get_emitter().stats()["batch_repeats"])]

    # A caller sets this by sending a batch of its own refused calls, so it is
    # no loss and stays out of loom.ledger.dropped.
    meter.create_observable_counter(
        name="loom.ledger.batch_repeats",
        callbacks=[_batch_repeats],
        description="A caller's refusal rows an MCP batch did not write again, since start",
        unit="1",
    )


def register_state_gauges(
    meter: Any,
    *,
    ch_probe: Callable[[], dict[str, Any]],
    code_probe: Callable[[], CodeSeries] | None = None,
    retention_probe: Callable[[], CodeSeries] | None = None,
    agent_labels_probe: Callable[[], CodeSeries] | None = None,
    storage_probe: Callable[[], CodeSeries] | None = None,
    agent_trusts_probe: Callable[[], CodeSeries] | None = None,
) -> None:
    """Register observable gauges for system state.

    Backlogs and breaker state are levels, and they cannot be derived from
    event counters.

    `ch_probe` returns a dict of level readings; it is wrapped in a 30s TTL
    cache, refreshed off the export thread, so the 3s export interval turns
    into neither CH load nor export timeouts.

    `code_probe` feeds the per-namespace code-plane gauges. It FINAL-scans
    the code tables (millions of symbol rows on a ClickHouse-sized repo), so
    it gets its own cache at a slower cadence — one query a minute.
    """
    try:
        cache = _TTLCache(30.0, ch_probe)
        reading = partial(_level_reading, cache)

        code_cache = _TTLCache(60.0, code_probe) if code_probe is not None else None
        # Retention census: one FINAL pass over loom.memory, on its own clock.
        ttl_r = float(_settings.otel_retention_census_s)
        retention_cache = _TTLCache(ttl_r, retention_probe) if retention_probe is not None else None

        _rseries = partial(_cached_series, retention_cache)
        _series = partial(_cached_series, code_cache)

        _register_agent_labels_gauge(meter, agent_labels_probe)
        _register_storage_gauges(meter, storage_probe)
        _register_agent_trust_gauges(meter, agent_trusts_probe)
        _register_code_lag_gauges(meter, _series)
        _register_backlog_gauges(meter, reading)
        _register_process_gauges(meter)
        _register_store_gauges(meter, reading)
        _register_integrity_gauges(meter, reading)
        _register_retention_gauges(meter, _rseries)
        _register_code_plane_gauges(meter, _series)
        _register_worker_gauges(meter)
        _register_health_gauges(meter)
        _register_code_tier_gauges(meter)
        log.info("loom state gauges registered")
    except Exception as e:  # noqa: BLE001 — gauge wiring must not stop startup
        log.warning("state gauge registration failed: %s", e)


def process_fd_reading() -> tuple[int, int]:
    """(open descriptors, soft limit) for this process; -1 for what cannot be
    read (a non-Linux host has no /proc/self/fd). Pure enough to test: it
    reads the kernel, never the store."""
    open_fds = -1
    limit = -1
    try:
        open_fds = len(os.listdir("/proc/self/fd"))
    except Exception:  # noqa: BLE001 — absent /proc is not an error worth raising
        pass
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = int(soft) if soft != resource.RLIM_INFINITY else -1
    except Exception:  # noqa: BLE001 — a gauge reading never raises
        pass
    return open_fds, limit


def data_disk_reading(path: str = "var") -> tuple[int, int]:
    """(bytes free to an unprivileged writer, total bytes) of the disk holding
    `path` (the working directory's when `path` is absent); -1 for what cannot
    be read. Reads the kernel, never the store."""
    target = path if os.path.isdir(path) else "."
    try:
        st = os.statvfs(target)
    except Exception:  # noqa: BLE001 — a gauge reading never raises
        return -1, -1
    return int(st.f_bavail * st.f_frsize), int(st.f_blocks * st.f_frsize)


def set_worker_consecutive_failures(name: str, count: int) -> None:
    """Publish a worker's current failure streak for the gauge. Never raises."""
    try:
        with _worker_failure_lock:
            _worker_failure_streaks[_clamp_ns(name)] = max(0, int(count))
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def worker_consecutive_failures() -> dict[str, int]:
    """Snapshot of the streaks (tests, admin views)."""
    with _worker_failure_lock:
        return dict(_worker_failure_streaks)


def set_worker_partial_failures(name: str, count: int) -> None:
    """Publish how many leg failures a worker's latest pass noted. Never raises."""
    try:
        with _worker_failure_lock:
            _worker_partial_failures[_clamp_ns(name)] = max(0, int(count))
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def worker_partial_failures() -> dict[str, int]:
    """Snapshot of the partial-failure gauge (tests, admin views)."""
    with _worker_failure_lock:
        return dict(_worker_partial_failures)
