"""The request's scope: the config-plane reads launched ahead of first use, and
the namespaces and canonical entity filter the legs search.

`_SearchRun` carries that state between the orchestrator's phases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from server.core.background import create_deferred_task
from server.core.ch import get_client
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.query_plan import _enrichment_parser_gate_active

log = logging.getLogger(__package__)


def _retain_task_result(task: asyncio.Task[Any]) -> None:
    """Done-callback that marks an abandoned task's outcome as observed.

    Pre-launched config reads are normally awaited on the main path; if
    an exception unwinds the handler first, the orphaned task must not
    log "exception was never retrieved" at GC time.
    """
    if not task.cancelled():
        task.exception()


@dataclass(slots=True)
class _SearchRun:
    """Request-scoped state handle_memory_search's phases hand each other
    that SearchContext does not carry: the config-plane reads launched ahead
    of first use and the namespace / entity scope they resolve. Built once
    per call by `_launch_scope_reads`; recursive calls build their own."""

    rerank_override: str
    rerank_cfg_task: asyncio.Task[Any] | None
    calibration_task: asyncio.Task[Any]
    my_namespaces_task: asyncio.Task[Any] | None
    order_authoritative: bool
    # Scope, filled before the embed (parser-gate prelaunch) or after it.
    namespaces: list[str] = field(default_factory=list)
    # The caller's namespaces the all_mine fan-out cut at its cap.
    namespaces_skipped: list[str] = field(default_factory=list)
    effective_entity_filter: list[str] = field(default_factory=list)
    canonical_task: asyncio.Task[list[str]] | None = None
    canonical_prelaunched: bool = False
    # Set once the retrieval stages ran; read by the fallback, verdict and
    # output phases (every return path reports the same values).
    ns_calibration: Any = None
    top_k_ceiling: int = 0
    retrieval_cost_estimate: dict[str, Any] = field(default_factory=dict)
    graph_seeds: int = 0
    graph_neighbors: int = 0
    triples_matched: int = 0
    # The side legs the orchestrator launched after the scope reads.
    side_tasks: list[asyncio.Task[Any] | None] = field(default_factory=list)

    def cancel_pending(self) -> None:
        """Cancel every task this pass launched that is still running. The normal path has
        awaited them all; an exception, or a `wait_for` timeout cancelling a re-entrant pass,
        would otherwise leave their ClickHouse reads running after the search returned."""
        for task in (
            self.rerank_cfg_task,
            self.calibration_task,
            self.my_namespaces_task,
            self.canonical_task,
            *self.side_tasks,
        ):
            if task is not None and not task.done():
                task.cancel()


def _launch_scope_reads(input: MemorySearchInput) -> _SearchRun:
    """Phase 4: launch the config-plane reads (rerank config, calibration,
    fan-out namespaces) as tasks so they overlap the plan/LLM/embed phases,
    and note whether the token's namespace order is authoritative."""
    from server.memory.app.embedder_calibration import resolve_calibration
    from server.memory.app.rerank import get_namespace_rerank_config

    # Config-plane reads depend only on request identity, so they run
    # concurrently with the plan/LLM/embed stages and are awaited at first use.
    request_rerank_override = (input.rerank or "").strip()
    _rerank_cfg_task: asyncio.Task[Any] | None = None
    if not request_rerank_override:
        _rerank_cfg_task = create_deferred_task(
            get_namespace_rerank_config(input.org, input.namespace)
        )
        _rerank_cfg_task.add_done_callback(_retain_task_result)
    _calibration_task = create_deferred_task(resolve_calibration(input.org, input.namespace))
    _calibration_task.add_done_callback(_retain_task_result)
    _my_namespaces_task: asyncio.Task[Any] | None = None
    # Scoped token (carries `ns_roles`): its `namespaces` claim is an ordered
    # priority list, so the search fans out in that exact order as strict tiers
    # and the addressed namespace is NOT force-pinned to the front.
    from server.auth.context import current_auth as _current_auth

    _auth = _current_auth()
    _order_authoritative = bool(_auth is not None and _auth.has_ns_roles())
    if input.namespace_scope == "all_mine":
        from server.auth.context import resolve_fanout

        _my_namespaces_task = create_deferred_task(resolve_fanout(input.org))
        _my_namespaces_task.add_done_callback(_retain_task_result)
    return _SearchRun(
        rerank_override=request_rerank_override,
        rerank_cfg_task=_rerank_cfg_task,
        calibration_task=_calibration_task,
        my_namespaces_task=_my_namespaces_task,
        order_authoritative=_order_authoritative,
    )


async def _scope_inputs(
    input: MemorySearchInput,
    run: _SearchRun,
    llm_query_entities: list[str],
    actual_work: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """(entity filter, namespaces searched), before canonicalisation. The caller's explicit
    `entity_filter` wins over the entities the intent parser found. "all_mine" fans out across
    every namespace the caller belongs to, but the addressed `input.namespace` is ALWAYS in the
    set: membership may only widen the search, never redirect it."""
    from server.auth.context import with_addressed

    entity_filter = list(input.entity_filter or []) or list(llm_query_entities)
    if run.my_namespaces_task is not None:
        fanout = await run.my_namespaces_task
        namespaces = with_addressed(input.namespace, fanout.namespaces)
        run.namespaces_skipped = [ns for ns in fanout.skipped if ns not in namespaces]
    else:
        namespaces = [input.namespace]
    actual_work["namespace_count"] = len(namespaces)
    return entity_filter, namespaces


async def _prelaunch_canonical_lookup(
    input: MemorySearchInput,
    run: _SearchRun,
    llm_query_entities: list[str],
    actual_work: dict[str, Any],
) -> None:
    """Phase 9 (parser gate only): resolve the namespace + entity scope before
    the embed and launch the canonical-entity lookup so it overlaps the embed
    RTT. Writes the scope and the task into `run`; gate off leaves `run`
    untouched and the scope is resolved serially after the embed."""
    # Latency: the canonical-entity lookup inputs (entity texts + resolved namespaces) are
    # known here, BEFORE the embed RTT. Under the active gate we launch the lookup as a task so
    # it overlaps the embed; the awaited result is identical to the serial path. Gate off →
    # serial.
    _canonical_task: asyncio.Task[list[str]] | None = None
    effective_entity_filter: list[str] = []
    namespaces: list[str] = []
    _canonical_prelaunched = False
    if _enrichment_parser_gate_active(input):
        effective_entity_filter, namespaces = await _scope_inputs(
            input, run, llm_query_entities, actual_work
        )
        if effective_entity_filter:
            from server.memory.app.entity_resolution import lookup_canonical_entities

            # Snapshot the inputs the task closes over so the parallel and
            # serial paths resolve the same canonical set.
            _canonical_entity_texts = list(effective_entity_filter)
            _canonical_namespaces = list(namespaces)
            _canonical_task = create_deferred_task(
                lookup_canonical_entities(
                    get_client(),
                    org=input.org,
                    namespaces=_canonical_namespaces,
                    entity_texts=_canonical_entity_texts,
                )
            )
            _canonical_task.add_done_callback(_retain_task_result)
        _canonical_prelaunched = True
    run.canonical_task = _canonical_task
    run.effective_entity_filter = effective_entity_filter
    run.namespaces = namespaces
    run.canonical_prelaunched = _canonical_prelaunched


async def _resolve_entity_scope(
    input: MemorySearchInput,
    run: _SearchRun,
    llm_query_entities: list[str],
    actual_work: dict[str, Any],
) -> None:
    """Phase 11: settle the namespaces searched and the canonical entity
    filter — serially here, or by awaiting the lookup phase 9 prelaunched.
    Writes the result into `run`."""
    namespaces = run.namespaces
    effective_entity_filter = run.effective_entity_filter
    _canonical_prelaunched = run.canonical_prelaunched
    _canonical_task = run.canonical_task
    if not _canonical_prelaunched:
        # Default (serial) path: entity-filter build, namespace resolution,
        # and canonical lookup all run here, after the embed.
        effective_entity_filter, namespaces = await _scope_inputs(
            input, run, llm_query_entities, actual_work
        )
        if effective_entity_filter:
            try:
                from server.memory.app.entity_resolution import (
                    lookup_canonical_entities,
                )

                effective_entity_filter = await lookup_canonical_entities(
                    get_client(),
                    org=input.org,
                    namespaces=namespaces,
                    entity_texts=effective_entity_filter,
                )
            except Exception as e:  # noqa: BLE001 — canonical entity lookup is optional; raw filter kept
                log.warning("entity canonical lookup failed (non-fatal): %s", e)
    elif _canonical_task is not None:
        # Parallel path: the lookup was launched before the embed and
        # overlapped its RTT. Await it now; same fail-open as the serial path.
        try:
            effective_entity_filter = await _canonical_task
        except Exception as e:  # noqa: BLE001 — canonical entity lookup is optional; raw filter kept
            log.warning("entity canonical lookup failed (non-fatal): %s", e)
    run.namespaces = namespaces
    run.effective_entity_filter = effective_entity_filter
