"""Worker registry — single source of truth for background loops.

Each worker declares itself once here (name, enabling setting, interval,
pass callable); the supervisor iterates the registry to boot, monitor,
and drain.

To add a worker: implement `async def run_pass()` in the owning domain's
`server/<domain>/workers/<name>.py`, then add a `_Worker(...)` row to
`_WORKERS` (a pass that needs settings reads them at pass time). Each row's
defaults are in docs/reference/workers.md, generated from this table.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from server.settings import settings
from server.workers.first_pass import OwnedTable, Scope, due_hook


@dataclass(frozen=True)
class WorkerSpec:
    """Declarative description of a background worker.

    `pass_callable` is invoked once per interval; it should be idempotent
    and reasonably bounded (the supervisor wraps it with timeout +
    crash recovery, but a single pass that takes hours blocks both the
    next pass and graceful shutdown).
    """

    name: str
    interval_seconds: float
    # The supervisor awaits this and discards the return, so a worker's
    # run_pass can hand back its result dataclass directly (no None wrapper).
    pass_callable: Callable[[], Awaitable[object]]
    # The pass deadline. None → `default_max_pass_seconds()`:
    # four intervals, at least DEADLINE_FLOOR_SECONDS (a 5 s worker's pass
    # may wait on a provider retry loop), at most DEADLINE_CAP_SECONDS. A
    # pass past its deadline is cancelled and counted as a failure.
    max_pass_seconds: float | None = None
    # Asked once, when the supervisor starts this worker (the
    # lease is held). True → the first pass runs now instead of after the
    # boot stagger — a weekly worker whose output table is empty or older
    # than its interval must not wait crc32(name) % interval (up to days) on
    # a deployment whose processes live a few hours. None → the stagger. Fail-quiet: an error or a
    # slow answer (FIRST_PASS_DUE_TIMEOUT_SECONDS) reads as "not due".
    first_pass_due: Callable[[], Awaitable[bool]] | None = None
    # The generic form of `first_pass_due`. The tables this worker's pass
    # fills and the (org, namespace) pairs it fills them for; with both set
    # and no `first_pass_due` of its own, the first pass is due when any
    # owned table holds no row for a namespace in scope (one statement per
    # table, `server.workers.first_pass`). A worker with a finer notion of
    # "owed" (code_graph_stats: stale stamps) keeps its own hook.
    owned_tables: tuple[OwnedTable, ...] = ()
    due_scope: Scope | None = None
    # The sleep after a clean pass that found nothing
    # (`pass_context.mark_idle()`); None → `interval_seconds`. A worker whose
    # work arrives in bursts asks often while there is some and seldom while
    # there is none, instead of scanning its table at one cadence for both.
    idle_interval_seconds: float | None = None

    def deadline_seconds(self) -> float:
        if self.max_pass_seconds is not None and self.max_pass_seconds > 0:
            return float(self.max_pass_seconds)
        return default_max_pass_seconds(self.interval_seconds)

    def first_pass_hook(self) -> Callable[[], Awaitable[bool]] | None:
        """The check the supervisor asks at start: the worker's own
        `first_pass_due`, else the empty-table rule derived from
        `owned_tables` + `due_scope`, else none (the stagger)."""
        if self.first_pass_due is not None:
            return self.first_pass_due
        if self.owned_tables and self.due_scope is not None:
            return due_hook(self.name, self.owned_tables, self.due_scope)
        return None


DEADLINE_FLOOR_SECONDS = 60.0
DEADLINE_CAP_SECONDS = 900.0
FIRST_PASS_DUE_TIMEOUT_SECONDS = 30.0


def default_max_pass_seconds(interval_seconds: float) -> float:
    """min(4 x interval, 900), floored at 60 s."""
    four = 4.0 * float(interval_seconds)
    return float(min(DEADLINE_CAP_SECONDS, max(DEADLINE_FLOOR_SECONDS, four)))


@dataclass(frozen=True)
class _Worker:
    """One row of the registry table: how `_enabled_workers` builds a
    worker's `WorkerSpec` from settings. Setting names are strings so the
    table reads settings when the specs are built, not at import."""

    name: str
    # Imported only when the worker is enabled: boot stays fast and a worker's
    # optional dependencies stay optional.
    module: str
    # The settings flag that switches it on; None → always on.
    enabled: str | None
    # The settings field holding the cadence (or, with `interval_in_module`,
    # the module attribute holding it).
    interval: str
    interval_in_module: bool = False
    pass_attr: str = "run_pass"
    # A second settings flag the worker needs beyond its own (the ledger
    # it reads, the plane it serves); off → the worker is off.
    also_requires: str | None = None
    # WorkerSpec.idle_interval_seconds from this settings field.
    idle_interval: str | None = None
    # WorkerSpec.max_pass_seconds: pinned here, or float() of a settings field.
    max_pass_seconds: float | None = None
    max_pass_setting: str | None = None
    # WorkerSpec.first_pass_due from this module attribute.
    first_pass_due: str | None = None
    # WorkerSpec.owned_tables / due_scope from the module's OWNED_TABLES +
    # due_scope.
    owned_tables: bool = False


def _spec_for(row: _Worker) -> WorkerSpec:
    """Import an enabled worker and build its spec — the `WorkerSpec(...)`
    each row would otherwise spell out, field for field."""
    mod = importlib.import_module(row.module)
    pass_callable = getattr(mod, row.pass_attr)
    interval = _interval_of(row, mod)
    max_pass = row.max_pass_seconds
    if row.max_pass_setting is not None:
        max_pass = float(getattr(settings, row.max_pass_setting))
    return WorkerSpec(
        row.name,
        interval,
        pass_callable,
        max_pass_seconds=max_pass,
        first_pass_due=getattr(mod, row.first_pass_due) if row.first_pass_due else None,
        owned_tables=mod.OWNED_TABLES if row.owned_tables else (),
        due_scope=mod.due_scope if row.owned_tables else None,
        idle_interval_seconds=(
            getattr(settings, row.idle_interval) if row.idle_interval is not None else None
        ),
    )


# Every background worker, in the order the supervisor boots them. Whether a
# row runs by default is its setting's default (docs/reference/workers.md).
_WORKERS: tuple[_Worker, ...] = (
    _Worker(
        "embedding_worker",
        "server.memory.workers.embedding",
        "embedding_worker_enabled",
        "embedding_worker_interval_seconds",
        idle_interval="embedding_worker_idle_interval_seconds",
    ),
    _Worker(
        "audit_verifier",
        "server.insights.workers.audit_verifier",
        "audit_verifier_enabled",
        "audit_verifier_interval_seconds",
    ),
    _Worker(
        "recalibration",
        "server.memory.workers.recalibration",
        "recalibration_enabled",
        "recalibration_interval_seconds",
    ),
    _Worker(
        "erasure_recovery",
        "server.erasure.workers.recovery",
        "erasure_recovery_enabled",
        "erasure_recovery_interval_seconds",
    ),
    _Worker(
        "erasure_reclaim",
        "server.erasure.workers.reclaim",
        "erasure_reclaim_enabled",
        "erasure_reclaim_interval_seconds",
    ),
    _Worker(
        "retention_reconcile",
        "server.memory.workers.retention_reconcile",
        "retention_reconcile_enabled",
        "retention_reconcile_interval_seconds",
    ),
    _Worker(
        "namespace_gc",
        "server.identity.workers.namespace_gc",
        "namespace_gc_worker_enabled",
        "namespace_gc_interval_seconds",
    ),
    _Worker(
        "projection_repair",
        "server.memory.workers.projection_repair",
        "projection_repair_enabled",
        "projection_repair_interval_seconds",
    ),
    # cadence is the module's INTERVAL_SECONDS, not a setting
    _Worker(
        "usage_fold",
        "server.memory.workers.usage_fold",
        "usage_fold_enabled",
        "INTERVAL_SECONDS",
        interval_in_module=True,
    ),
    _Worker(
        "orphan_vector_reconcile",
        "server.memory.workers.orphan_vector_reconcile",
        "orphan_vector_reconcile_enabled",
        "orphan_vector_reconcile_interval_seconds",
    ),
    _Worker(
        "sensitivity_sweep",
        "server.memory.workers.sensitivity_sweep",
        "sensitivity_sweep_enabled",
        "sensitivity_sweep_interval_seconds",
    ),
    _Worker(
        "conversation_titler",
        "server.knowledge.workers.conversation_titler",
        "conversation_titler_enabled",
        "conversation_titler_interval_seconds",
    ),
    _Worker(
        "profile_consolidation",
        "server.memory.workers.profile_consolidation",
        "profile_consolidation_enabled",
        "profile_consolidation_interval_seconds",
    ),
    _Worker(
        "reflection_distill",
        "server.memory.workers.reflection_distill",
        "reflection_distill_enabled",
        "reflection_distill_interval_seconds",
    ),
    _Worker(
        "entity_extractor",
        "server.memory.workers.entity_extractor",
        "entity_extract_enabled",
        "entity_extract_interval_seconds",
        idle_interval="entity_extract_idle_interval_seconds",
    ),
    _Worker(
        "memory_expiry_sweep",
        "server.memory.workers.expiry_sweep",
        "memory_expiry_sweep_enabled",
        "memory_expiry_sweep_interval_seconds",
    ),
    _Worker(
        "observe_extract",
        "server.memory.workers.observe_extract",
        "observe_extract_enabled",
        "observe_extract_interval_seconds",
        # P99 113 s measured; the default would be 900 anyway
        # at a 300 s interval, pinned so an interval change cannot lower it.
        max_pass_seconds=900.0,
    ),
    _Worker(
        "extraction_queue",
        "server.memory.workers.extraction_queue",
        "extraction_queue_enabled",
        "extraction_queue_interval_seconds",
    ),
    _Worker(
        "code_ingest",
        "server.code.workers.ingest",
        "code_ingest_enabled",
        "code_ingest_interval_seconds",
        # 900 s by default (p99 pass ~114 s at a 120 s interval); a setting.
        max_pass_setting="code_ingest_max_pass_seconds",
    ),
    _Worker(
        "code_scip_apply",
        "server.code.workers.scip_apply",
        "code_scip_apply_enabled",
        "code_scip_apply_interval_seconds",
    ),
    # The per-merge compiler tiers: scip-clang, scip-typescript, scip-python
    # and scip-go.
    _Worker(
        "code_scip_delta",
        "server.code.workers.scip_delta",
        "code_scip_delta_enabled",
        "code_scip_delta_interval_seconds",
    ),
    _Worker(
        "code_scip_ts_delta",
        "server.code.workers.scip_ts_delta",
        "code_scip_ts_delta_enabled",
        "code_scip_ts_delta_interval_seconds",
    ),
    _Worker(
        "code_scip_py_delta",
        "server.code.workers.scip_py_delta",
        "code_scip_py_delta_enabled",
        "code_scip_py_delta_interval_seconds",
    ),
    _Worker(
        "code_scip_go_delta",
        "server.code.workers.scip_go_delta",
        "code_scip_go_delta_enabled",
        "code_scip_go_delta_interval_seconds",
        # A first pass over a Go monorepo builds every module (~40 min for
        # 234), far past the 900 s the interval implies: the pass runs under
        # the worker's own timeout.
        max_pass_setting="code_scip_go_delta_timeout_seconds",
    ),
    _Worker(
        "code_pr_history",
        "server.code.workers.pr_history",
        "code_pr_history_enabled",
        "code_pr_history_interval_seconds",
    ),
    _Worker(
        "code_embed_recovery",
        "server.code.workers.embed_recovery",
        "code_embed_recovery_enabled",
        "code_embed_recovery_interval_seconds",
        idle_interval="code_embed_recovery_idle_interval_seconds",
    ),
    _Worker(
        "entity_communities",
        "server.memory.workers.entity_communities",
        "entity_communities_enabled",
        "entity_communities_interval_seconds",
    ),
    _Worker(
        "code_graph_stats",
        "server.code.workers.graph_stats",
        "code_graph_stats_enabled",
        "code_graph_stats_interval_seconds",
        first_pass_due="first_pass_due",  # Empty table → now
        # An hour for ten namespaces' unit-risk rows; the
        # 900 s cap stays for every other worker
        max_pass_setting="code_graph_stats_max_pass_seconds",
    ),
    _Worker(
        "tracker_test_daily",
        "server.tracker.workers.test_daily",
        "tracker_test_daily_enabled",
        "tracker_test_daily_interval_seconds",
        # Six days × two paced remote statements per pass;
        # A namespace with no rolled day runs at boot
        max_pass_seconds=900.0,
        owned_tables=True,
    ),
    _Worker(
        "code_summary",
        "server.code.workers.summary",
        "code_summary_enabled",
        "code_summary_interval_seconds",
    ),
    # jwt_revocation_refresh: always-on, no enable flag.
    _Worker(
        "jwt_revocation_refresh",
        "server.auth.revocation",
        None,
        "jwt_revocation_cache_ttl_seconds",
        pass_attr="refresh",
    ),
    _Worker(
        "task_lease_sweep",
        "server.tasks.workers.lease_sweep",
        "task_lease_sweep_enabled",
        "task_lease_sweep_interval_seconds",
        idle_interval="task_lease_sweep_idle_interval_seconds",
    ),
    _Worker(
        "mcp_session_run_sweep",
        "server.tasks.workers.session_run_sweep",
        None,
        "INTERVAL_SECONDS",
        interval_in_module=True,
        # twice IDLE_CLOSE_BUDGET_S, so closes begun just before it ends can finish
        max_pass_seconds=240.0,
    ),
    _Worker(
        "task_webhook_deliver",
        "server.tasks.workers.webhook_deliver",
        "task_webhook_deliver_enabled",
        "task_webhook_deliver_interval_seconds",
        # up to 200 inboxes × a 10 s ack window is far past four intervals
        max_pass_seconds=120.0,
        idle_interval="task_webhook_deliver_idle_interval_seconds",
    ),
    _Worker(
        "tracker_links",
        "server.tracker.workers.links",
        "tracker_links_enabled",
        "tracker_links_interval_seconds",
        # one bounded tracker read per issue of the window (~2,300 for 90
        # days), sequential: minutes, not the four-interval default
        max_pass_seconds=3600.0,
        # A namespace with no candidate row runs at boot, not after
        # crc32(name) % 86400 seconds
        owned_tables=True,
    ),
    _Worker(
        "tracker_embed",
        "server.tracker.workers.embed",
        "tracker_embed_enabled",
        "tracker_embed_interval_seconds",
        # the first fill is ~38k rows in batches through the embedder
        # (minutes); the cursor advances per page, so a cut pass resumes
        max_pass_seconds=900.0,
        owned_tables=True,
    ),
    _Worker(
        "code_backport_gaps",
        "server.code.workers.backport_gaps",
        "code_backport_gaps_enabled",
        "code_backport_gaps_interval_seconds",
        # The report reads the PR ledger: no ledger, no pass.
        also_requires="code_pr_history_enabled",
        # ≤ 300 composed reads a night, paced one per second, budgeted inside
        # the pass: the setting is the ceiling, not the plan
        max_pass_setting="code_backport_gaps_max_pass_seconds",
    ),
    _Worker(
        "code_coverage_notify",
        "server.code.workers.coverage_notify",
        "code_coverage_notify_enabled",
        "code_coverage_notify_interval_seconds",
        also_requires="code_intel_enabled",
        idle_interval="code_coverage_notify_idle_interval_seconds",
    ),
    _Worker(
        "code_coverage_import",
        "server.code.workers.coverage_import",
        "code_coverage_import_enabled",
        "code_coverage_import_interval_seconds",
        also_requires="code_intel_enabled",
        # 64 slices of a nightly run, two source statements each
        max_pass_setting="code_coverage_import_max_pass_seconds",
    ),
)


def _is_enabled(row: _Worker) -> bool:
    if row.enabled is not None and not getattr(settings, row.enabled):
        return False
    return row.also_requires is None or bool(getattr(settings, row.also_requires))


def _interval_of(row: _Worker, mod: Any = None) -> Any:
    """The row's cadence: a settings field, or a module attribute."""
    if row.interval_in_module:
        return getattr(mod or importlib.import_module(row.module), row.interval)
    return getattr(settings, row.interval)


def enabled_workers() -> list[WorkerSpec]:
    """The specs of the enabled rows, in table order. Imports happen here,
    not at module import, so a worker that touches an external service is
    imported only when it is on. Re-reads settings on each call."""
    return [_spec_for(row) for row in _WORKERS if _is_enabled(row)]


def worker_meta() -> dict[str, dict[str, float | bool]]:
    """Enable state and cadence of EVERY worker, disabled ones included —
    what `/v1/admin/workers.status` renders beside the supervised set."""
    return {
        row.name: {"enabled": _is_enabled(row), "interval_seconds": _interval_of(row)}
        for row in _WORKERS
    }
