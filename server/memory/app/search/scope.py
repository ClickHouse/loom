"""The request's scope: the config-plane reads launched ahead of first use, and
the namespaces and canonical entity filter the legs search.

`_SearchRun` carries that state between the orchestrator's phases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from server.auth.fanout import Fanout, resolve_fanout
from server.core.background import create_deferred_task
from server.core.ch import get_client
from server.core.llm_free import reads_llm_free
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.query_plan import _enrichment_parser_gate_active
from server.memory.domain.graph_quality import MAX_ENTITY_CHARS

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
    fanout_task: asyncio.Task[Fanout] | None
    # Scope, filled before the embed (parser-gate prelaunch) or after it.
    namespaces: list[str] = field(default_factory=list)
    # Whether `namespaces` is a priority order (a scoped token's), and the
    # caller's namespaces the all_mine fan-out cut at its cap.
    order_authoritative: bool = False
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
            self.fanout_task,
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
    # A reranker is a model call: an LLM-free read runs only the one its request names.
    if not request_rerank_override and not reads_llm_free():
        _rerank_cfg_task = create_deferred_task(
            get_namespace_rerank_config(input.org, input.namespace)
        )
        _rerank_cfg_task.add_done_callback(_retain_task_result)
    _calibration_task = create_deferred_task(resolve_calibration(input.org, input.namespace))
    _calibration_task.add_done_callback(_retain_task_result)
    _fanout_task: asyncio.Task[Fanout] | None = None
    if input.namespace_scope == "all_mine":
        _fanout_task = create_deferred_task(resolve_fanout(input.org))
        _fanout_task.add_done_callback(_retain_task_result)
    return _SearchRun(
        rerank_override=request_rerank_override,
        rerank_cfg_task=_rerank_cfg_task,
        calibration_task=_calibration_task,
        fanout_task=_fanout_task,
    )


async def _settle_namespaces(
    input: MemorySearchInput, run: _SearchRun, actual_work: dict[str, Any]
) -> None:
    """The namespaces this search reads, onto `run`. "all_mine" reads the caller's fan-out
    (`server.auth.fanout`: its kinds, its cap) led by the addressed `input.namespace`:
    membership may only widen the search, never redirect it."""
    run.namespaces = [input.namespace]
    if run.fanout_task is not None:
        fanout = await run.fanout_task
        run.namespaces, run.namespaces_skipped = fanout.split(input.namespace)
        run.order_authoritative = fanout.ordered
    actual_work["namespace_count"] = len(run.namespaces)


def _entity_filter(input: MemorySearchInput, llm_query_entities: list[str]) -> list[str]:
    """The entity filter before canonicalisation: the caller's own wins over the
    entities the intent parser found. A name too long to be one is left out, so a failed
    canonical lookup never binds it."""

    def fits(names: list[str]) -> list[str]:
        return [n for n in names if len(n.strip()) <= MAX_ENTITY_CHARS]

    return fits(list(input.entity_filter or [])) or fits(list(llm_query_entities))


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
    _canonical_prelaunched = False
    if _enrichment_parser_gate_active(input):
        await _settle_namespaces(input, run, actual_work)
        effective_entity_filter = _entity_filter(input, llm_query_entities)
        if effective_entity_filter:
            from server.memory.app.entity_resolution import lookup_canonical_entities

            # Snapshot the inputs the task closes over so the parallel and
            # serial paths resolve the same canonical set.
            _canonical_entity_texts = list(effective_entity_filter)
            _canonical_namespaces = list(run.namespaces)
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
    effective_entity_filter = run.effective_entity_filter
    _canonical_prelaunched = run.canonical_prelaunched
    _canonical_task = run.canonical_task
    if not _canonical_prelaunched:
        # Default (serial) path: entity-filter build, namespace resolution,
        # and canonical lookup all run here, after the embed.
        await _settle_namespaces(input, run, actual_work)
        effective_entity_filter = _entity_filter(input, llm_query_entities)
        if effective_entity_filter:
            try:
                from server.memory.app.entity_resolution import (
                    lookup_canonical_entities,
                )

                effective_entity_filter = await lookup_canonical_entities(
                    get_client(),
                    org=input.org,
                    namespaces=run.namespaces,
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
    run.effective_entity_filter = effective_entity_filter
