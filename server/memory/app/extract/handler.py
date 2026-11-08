"""memory.set_from_messages + memory.bulk_set + entity-write helpers.

Public surface re-exported from server.memory.app.handlers. Shared helpers
are imported lazily inside function bodies to dodge a circular import.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from server.core.background import create_deferred_task
from server.core.ch import get_client
from server.core.datetimes import parse_iso_dt
from server.embedding import get_embedder
from server.memory.api.models_bulk import (
    REFUSED,
    MemoryBulkSetInput,
    MemoryExtractionRetryMineInput,
    MemoryExtractionRetryMineOutput,
    MemoryExtractionStatusInput,
    MemoryExtractionStatusMineInput,
    MemoryExtractionStatusMineOutput,
    MemoryExtractionStatusOutput,
    MemorySetFromMessagesInput,
    MemorySetFromMessagesOutput,
    _BulkSetEntry,
    _BulkSetResult,
    _ExtractionQueueRow,
    _RefusedFact,
    refused_facts,
)
from server.memory.app.agent_rules import request_rule_agent
from server.memory.app.bulk_set import (
    detect_updates_override,
    handle_memory_bulk_set,
    last_value_updates_linked,
)

# The projection writers are module globals here on purpose: the handlers in
# this module call them through these names, so tests monkeypatch them on
# THIS module.
from server.memory.app.chunk_projection import write_memory_chunks
from server.memory.app.projection_errors import (
    record_input_projection_failure,
)
from server.observability import degradations as _degradations
from server.observability import provider_health
from server.observability.otel import traced

log = logging.getLogger(__name__)


def _message_dict(m: Any) -> dict[str, Any]:
    """The wire message as the extractor and evidence writer see it. A per-message
    `timestamp` rides along as an ISO string with minute precision; absent timestamps leave
    the dict at {role, content}."""
    d: dict[str, Any] = {"role": m.role, "content": m.content}
    ts = getattr(m, "timestamp", None)
    if ts is not None:
        d["timestamp"] = ts.replace(tzinfo=None).isoformat(timespec="minutes")
    name = str(getattr(m, "name", "") or "").strip()
    if name:
        d["name"] = name
    return d


def _deferred_by_allowlist(input: MemorySetFromMessagesInput) -> bool:
    """An operator names the agents whose extraction runs off the request. Never when the
    caller set `record_schema` (it reads `written` back) and never when the caller said
    defer=false explicitly."""
    from server.settings import settings

    raw = str(settings.extraction_defer_agents).strip()
    if not raw:
        return False
    if input.record_schema:
        return False
    if "defer" in getattr(input, "model_fields_set", set()):
        return False  # the caller decided; the list is for callers who did not
    names = {n.strip() for n in raw.split(",") if n.strip()}
    if "*" in names:
        from server.auth.context import current_auth

        auth = current_auth()
        if auth is None or not getattr(auth, "is_service_account", False):
            return False
        return True
    return str(input.agent or "") in names


async def _enqueue_deferred(
    input: MemorySetFromMessagesInput, rule_agent: str
) -> MemorySetFromMessagesOutput:
    """defer=true: persist the request, return the queue handle.

    Nothing is validated here beyond the input model — the replay runs the
    full inline path, so a batch the extractor rejects is reported on the
    queue row (`failed` + `last_error`) rather than here. Fail-closed: a
    queue insert that fails is a 503, never a silent 200.
    """
    from server.identity.app.request_scope import request_scope
    from server.memory.app import extraction_queue

    # Resolved before the insert: a scope that does not resolve is refused as the
    # inline call would refuse it, not reported as a queue outage.
    scope = request_scope()
    try:
        with traced("extract.defer_enqueue", messages=len(input.messages)):
            queue_id = await extraction_queue.enqueue(input, scope=scope, rule_agent=rule_agent)
    except Exception as e:
        log.warning("memory_extraction_queue enqueue failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXTRACTION_QUEUE_UNAVAILABLE",
                "message": (
                    "deferred extraction could not be confirmed as queued; it may still run, "
                    "so resending can extract the messages twice"
                ),
            },
        ) from e
    return MemorySetFromMessagesOutput(deferred=True, queue_id=queue_id, deferred_by="flag")


async def handle_memory_extraction_status(
    input: MemoryExtractionStatusInput,
) -> MemoryExtractionStatusOutput:
    """memory.extraction_status — lifecycle of deferred batches in scope."""
    from server.identity.app.request_scope import request_scope
    from server.memory.app import extraction_queue

    rows = await extraction_queue.list_status(
        org=input.org,
        namespace=input.namespace,
        scope=request_scope(),
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        queue_id=input.queue_id or None,
        status=input.status or None,
        limit=input.limit,
    )
    pending, failed = _counts(rows)
    return MemoryExtractionStatusOutput(
        batches=[_ExtractionQueueRow.model_validate(r) for r in rows],
        pending=pending,
        failed=failed,
    )


def _counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """How many of `rows` still wait (pending, running or held) and how many failed."""
    from server.memory.app import extraction_queue as q

    waiting = (*q.WAITING, q.STATUS_HELD)
    return (
        sum(1 for r in rows if r.get("status") in waiting),
        sum(1 for r in rows if r.get("status") == q.STATUS_FAILED),
    )


def _own_enqueuer() -> str | None:
    """The caller's enqueuer, or None when a token names no principal."""
    from server.auth.context import current_auth
    from server.memory.app.provenance import writer_actor

    actor = writer_actor()
    if not actor and current_auth() is not None:
        return None
    return actor


async def handle_memory_extraction_status_mine(
    input: MemoryExtractionStatusMineInput,
) -> MemoryExtractionStatusMineOutput:
    """Report the caller's batches; foreign and missing ids both read as unknown."""
    from server.identity.app.request_scope import request_scope
    from server.memory.app import extraction_queue

    asked = list(dict.fromkeys(input.queue_ids))
    enqueuer = _own_enqueuer()
    if enqueuer is None:
        return MemoryExtractionStatusMineOutput(unknown=asked)
    rows = await extraction_queue.list_own_status(
        org=input.org,
        namespace=input.namespace,
        queue_ids=asked,
        enqueued_by=enqueuer,
        scope=request_scope(),
    )
    found = {str(r.get("queue_id") or "") for r in rows}
    pending, failed = _counts(rows)
    return MemoryExtractionStatusMineOutput(
        batches=[_ExtractionQueueRow.model_validate(r) for r in rows],
        unknown=[qid for qid in asked if qid not in found],
        pending=pending,
        failed=failed,
    )


async def handle_memory_extraction_retry_mine(
    input: MemoryExtractionRetryMineInput,
) -> MemoryExtractionRetryMineOutput:
    """Requeue the caller's failed batches for one more extraction attempt."""
    from server.identity.app.request_scope import request_scope
    from server.memory.app import extraction_queue
    from server.settings import settings

    asked = list(dict.fromkeys(input.queue_ids))
    enqueuer = _own_enqueuer()
    if enqueuer is None:
        return MemoryExtractionRetryMineOutput(unknown=asked)
    outcome = await extraction_queue.requeue_own_failed(
        org=input.org,
        namespace=input.namespace,
        queue_ids=asked,
        enqueued_by=enqueuer,
        scope=request_scope(),
        max_attempts=settings.extraction_queue_max_attempts,
    )
    known = {*outcome.requeued, *outcome.not_failed, *outcome.model_calls_off, *outcome.expired}
    return MemoryExtractionRetryMineOutput(
        requeued=outcome.requeued,
        not_failed=outcome.not_failed,
        model_calls_off=outcome.model_calls_off,
        expired=outcome.expired,
        unknown=[qid for qid in asked if qid not in known],
    )


async def _source_evidence_ids(
    input: MemorySetFromMessagesInput, msgs: list[dict[str, Any]], observation_date: str | None
) -> list[str]:
    """The transcript's evidence rows, fail-open: a failed write is recorded as a projection
    failure and links nothing."""
    try:
        with traced("extract.evidence_write", messages=len(msgs)):
            return await _write_source_evidence(input, msgs, observation_date=observation_date)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("memory_evidence write failed (non-fatal): %s", e)
        await record_input_projection_failure(
            input,
            projection="memory_evidence",
            error=e,
            payload={"message_count": len(msgs)},
        )
        return []


async def _set_record_from_messages(
    input: MemorySetFromMessagesInput,
    msgs: list[dict[str, Any]],
    observation_date: str | None,
    evidence_task: asyncio.Task[list[str]],
    record_schema: dict[str, str | list[str]],
    *,
    operator_policy: str,
) -> MemorySetFromMessagesOutput:
    """Schema mode: ONE typed record matching the caller's schema, written
    through memory.bulk_set with its evidence links and inline record
    projection."""
    from server.memory.app.extract import ExtractionError, extract_typed_record_iterative

    belief_source_links = 0
    try:
        with traced("extract.typed_record", messages=len(msgs)):
            record, llm = await extract_typed_record_iterative(
                msgs,
                record_schema,
                max_tokens=input.max_tokens,
                observation_date=observation_date,
                operator_policy=operator_policy,
            )
    except ExtractionError as e:
        raise HTTPException(
            status_code=502,
            detail={"code": "EXTRACTION_FAILED", "message": str(e)},
        ) from e
    stubbed_fields = list(getattr(record, "stubbed_fields", None) or [])
    if stubbed_fields:
        from server.memory.app.drops import record_drop

        for field_name in stubbed_fields:
            record_drop(
                input.org,
                input.namespace,
                "records.field",
                "stubbed",
                item=f"{input.memory_key}.{field_name}",
            )

    bulk_input = MemoryBulkSetInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        entries=[
            _BulkSetEntry(
                memory_key=input.memory_key or "",  # checked above, before the evidence write
                value=record.record,
                memory_type="semantic",
                source_trace="extract:messages+schema",
                tags=list(input.extra_tags or []),
                entities=list(record.entities or []),
                relations=list(record.relations or []),
                # Valid-time defaults to the conversation date so
                # point-extracted records stay chronologically groupable.
                valid_at=input.valid_at or parse_iso_dt(observation_date),
            )
        ],
    )
    bulk_out = await handle_memory_bulk_set(bulk_input)
    # A refused record (blacklisted, pinned or locked key) gets no evidence, typed
    # record or count: the message did not produce the stored row. A restatement of
    # the stored value keeps its evidence.
    written = [r for r in bulk_out.written if r.conflict_verdict not in REFUSED]
    evidence_ids = await evidence_task
    try:
        belief_source_links = await _link_written_memories_to_evidence(
            input,
            [r.memory_key for r in written],
            evidence_ids,
        )
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("memory_belief_sources link failed (non-fatal): %s", e)
        await record_input_projection_failure(
            input,
            projection="memory_belief_sources",
            error=e,
            memory_keys=[r.memory_key for r in written],
            payload={"evidence_ids": evidence_ids},
        )
    # The typed record follows the row: none for an unchanged entry (version 0).
    if any(r.version > 0 for r in written):
        try:
            projected = await _write_inline_record_projection(
                input,
                record.record,
                record_schema,
                evidence_ids=evidence_ids,
            )
            if not projected:
                # A scope skip, not a provider failure: keep it off /v1/health.
                with provider_health.unrecorded():
                    _degradations.note(
                        "records", "projection_skipped", "read scope below the namespace"
                    )
        except Exception as e:  # noqa: BLE001 — a projection; the write stands
            log.warning("memory_records inline projection failed (non-fatal): %s", e)
            await record_input_projection_failure(
                input,
                projection="memory_records",
                error=e,
                memory_keys=[r.memory_key for r in written],
                payload={
                    "record_schema_fields": sorted(str(k) for k in (record_schema or {}).keys())
                },
            )
    _account_build(input.namespace, llm.tokens_in, llm.tokens_out, len(written), len(msgs))
    return MemorySetFromMessagesOutput(
        written=[r.model_copy() for r in written],
        refused=refused_facts(bulk_out.written),
        tokens_in=llm.tokens_in,
        tokens_out=llm.tokens_out,
        llm_provider=llm.provider,
        llm_model=llm.model,
        evidence_ids=evidence_ids,
        belief_source_links=belief_source_links,
        stubbed_fields=stubbed_fields,
        value_updates_linked=last_value_updates_linked.get(),
        degraded=_degradations.current(),
        under_operator_policy=bool(operator_policy),
    )


def _pipeline_steps(input: MemorySetFromMessagesInput) -> list[Any]:
    """The caller's `pipeline_steps`, else the default pipeline (its extract
    step judged when the request asks)."""
    from server.memory.app.pipeline import DEFAULT_PIPELINE
    from server.memory.app.pipeline_state import PipelineStep

    if input.pipeline_steps:
        steps = [
            PipelineStep(step_id=str(s.get("step_id", "")), config=dict(s))
            for s in input.pipeline_steps
            if isinstance(s, dict) and s.get("step_id")
        ]
    else:
        if input.judge_extraction:
            steps = [
                PipelineStep(
                    step_id=step.step_id,
                    config={
                        **dict(step.config or {}),
                        **({"judge": True} if step.step_id == "loom.llm_extract" else {}),
                    },
                )
                for step in DEFAULT_PIPELINE
            ]
        else:
            steps = DEFAULT_PIPELINE

    return steps


async def _ontology_warnings(input: MemorySetFromMessagesInput, state: Any) -> list[str]:
    """Ontology warnings: extracted entities + relation verbs not in the
    namespace's accepted ontology each yield one soft warning. Cold (no
    ontology) namespaces return empty."""
    ontology_warnings: list[str] = []
    try:
        from server.identity.app.namespace_ontology import load_namespace_ontology

        ont = await load_namespace_ontology(input.org, input.namespace)
        if ont.get("entity_type") or ont.get("relation_verb"):
            accepted_entities = {n.lower() for n in ont.get("entity_type", [])}
            accepted_verbs = {n.lower() for n in ont.get("relation_verb", [])}
            seen_entity_warnings: set[str] = set()
            seen_verb_warnings: set[str] = set()
            for fact in state.extracted_facts or []:
                for ent in fact.get("entities") or []:
                    e_lc = str(ent).strip().lower()
                    if (
                        e_lc
                        and accepted_entities
                        and e_lc not in accepted_entities
                        and e_lc not in seen_entity_warnings
                    ):
                        seen_entity_warnings.add(e_lc)
                        ontology_warnings.append(f"entity_type:{e_lc} not in accepted ontology")
                for triple in fact.get("relations") or []:
                    if not isinstance(triple, (list, tuple)) or len(triple) < 3:
                        continue
                    v_lc = str(triple[1]).strip().lower()
                    if (
                        v_lc
                        and accepted_verbs
                        and v_lc not in accepted_verbs
                        and v_lc not in seen_verb_warnings
                    ):
                        seen_verb_warnings.add(v_lc)
                        ontology_warnings.append(f"relation_verb:{v_lc} not in accepted ontology")
    except Exception as e:  # noqa: BLE001 — warnings are advisory
        log.warning("ontology-warning check failed (non-fatal): %s", e)
    return ontology_warnings


async def _project_extraction(
    input: MemorySetFromMessagesInput,
    msgs: list[dict[str, Any]],
    state: Any,
    evidence_ids: list[str],
    summary_draft_task: asyncio.Task[Any] | None,
) -> int:
    """The projections of a pipeline write — evidence links, verbatim chunks,
    the session summary — run together; returns the belief-source links
    made."""
    evidence_ids_by_memory_key = _evidence_ids_by_memory_key(
        msgs,
        state.extracted_facts,
        evidence_ids,
    )
    # Cited indices first, then the lexical fallback, then the whole window. The extractor
    # omits `source_message_indices` for most facts (0 of 27 on one session), and a
    # whole-window link under a (0,0) span makes a fact's evidence ~100x its own length. The
    # scope split is counted in the drop ledger so the fallback rate stays visible.
    evidence_refs_by_memory_key, evidence_scope_by_memory_key = _scoped_evidence_refs(
        msgs,
        state.extracted_facts,
        evidence_ids,
    )
    written_keys = {str(w["memory_key"]) for w in state.written}
    scope_counts: dict[str, int] = {}
    for key, scope in evidence_scope_by_memory_key.items():
        if key in written_keys and scope != SCOPE_CITED:
            scope_counts[scope] = scope_counts.get(scope, 0) + 1
    if scope_counts:
        from server.memory.app.drops import record_drop

        for scope, n in scope_counts.items():
            record_drop(input.org, input.namespace, "evidence.link", f"{scope}_fallback", n=n)
    written_memory_keys = [str(w["memory_key"]) for w in state.written]

    async def _project_evidence_links() -> int:
        try:
            with traced("extract.evidence_link", written=len(state.written)):
                return int(
                    await _link_written_memories_to_evidence(
                        input,
                        written_memory_keys,
                        evidence_ids,
                        evidence_ids_by_memory_key=evidence_ids_by_memory_key,
                        evidence_refs_by_memory_key=evidence_refs_by_memory_key,
                        evidence_scope_by_memory_key=evidence_scope_by_memory_key,
                    )
                    or 0
                )
        except Exception as e:  # noqa: BLE001 — a projection; the write stands
            log.warning("memory_belief_sources link failed (non-fatal): %s", e)
            await record_input_projection_failure(
                input,
                projection="memory_belief_sources",
                error=e,
                memory_keys=written_memory_keys,
                payload={"evidence_ids": evidence_ids},
            )
            return 0

    # Verbatim chunk layer — raw text alongside extracted facts. Awaited
    # before the response: chunks are read on the default bm25_hybrid path,
    # so backgrounding risks a query landing before they embed (read-your-
    # write gap). Fail-open.
    async def _project_chunks() -> None:
        try:
            with traced("extract.chunks_write", messages=len(msgs)):
                await write_memory_chunks(
                    input,
                    msgs,
                    parent_memory_keys=written_memory_keys,
                    evidence_ids=evidence_ids,
                )
        except Exception as e:  # noqa: BLE001 — a projection; the write stands
            log.warning("memory_chunks write failed (non-fatal): %s", e)
            await record_input_projection_failure(
                input,
                projection="memory_chunks",
                error=e,
                memory_keys=written_memory_keys,
                payload={"message_count": len(msgs)},
            )

    # Session-summary plane: one cheap LLM call per session emits a dense
    # dated digest, embedded with the SAME model/dim as the fact plane so
    # cosine is comparable. Awaited before the response so an immediate
    # post-ingest read sees it. Fail-open — the facts already landed.
    async def _project_session_summary() -> None:
        draft: Any = None
        if summary_draft_task is not None:
            try:
                draft = await summary_draft_task
            except Exception as e:  # noqa: BLE001 — a projection; the write stands
                log.warning("session digest draft failed (non-fatal): %s", e)
                await record_input_projection_failure(
                    input,
                    projection="session_summaries",
                    error=e,
                    memory_keys=written_memory_keys,
                    payload={"message_count": len(msgs), "stage": "draft"},
                )
                return
            if draft is None:
                # The model returned nothing (or the embedder did): the same
                # skip the inline path takes, without a second model call.
                return
        try:
            with traced("extract.session_summary", messages=len(msgs)):
                await _write_session_summary(input, msgs, state, draft=draft)
        except Exception as e:  # noqa: BLE001 — a projection; the write stands
            log.warning("session_summaries write failed (non-fatal): %s", e)
            await record_input_projection_failure(
                input,
                projection="session_summaries",
                error=e,
                memory_keys=written_memory_keys,
                payload={"message_count": len(msgs)},
            )

    # The three projections read the pipeline result and write three different tables; none
    # reads another's output, so they run together (each handles its own failure, so one
    # cannot cancel the rest; run back to back they took ~2.1 s of an ~8.4 s p50 ingest).
    links, _, _ = await asyncio.gather(
        _project_evidence_links(),
        _project_chunks(),
        _project_session_summary(),
    )
    return int(links)


async def handle_memory_set_from_messages(
    input: MemorySetFromMessagesInput,
    *,
    rule_agent: str | None = None,
) -> MemorySetFromMessagesOutput:
    """The op: settle the request's learnings switch and rule agent, then extract.

    Only the deployment setting turns assistant-turn learnings on; a request can
    only turn them off, so its `agent_learnings=true` while the setting is off is
    dropped and named in `ignored_args`. The setting is read once here, so the
    extraction and `ignored_args` agree even when it changes mid-call; a deferred
    replay reads it again. `rule_agent` is the agent a filed rule is for: None
    resolves it from the request's token, a deferred replay passes the one stored
    when it was queued ('' files nothing)."""
    from server.settings import settings as _settings

    setting_on = _settings.memory_extract_agent_learnings
    learnings_on = setting_on and input.agent_learnings is not False
    ignored = bool(input.agent_learnings) and not setting_on
    if ignored:
        # Dropped, so a deferred replay does not carry the ask either.
        input = input.model_copy(update={"agent_learnings": None})
    if rule_agent is None:
        rule_agent = request_rule_agent(input.agent)
    out = await _set_from_messages(input, rule_agent, learnings_on)
    if ignored:
        out.ignored_args = ["agent_learnings"]
    return out


async def _set_from_messages(
    input: MemorySetFromMessagesInput,
    rule_agent: str,
    learnings_on: bool,
) -> MemorySetFromMessagesOutput:
    """Extract memorable facts from raw messages and bulk-write them (or queue them).

    Extractor errors surface as 502 — silently writing zero memories is
    worse than telling the caller the extractor is broken.
    """
    detect_updates_override.set(input.detect_updates)
    last_value_updates_linked.set(0)
    from server.memory.app.extract import ExtractionError

    if input.defer:
        return await _enqueue_deferred(input, rule_agent)
    if _deferred_by_allowlist(input):
        out = await _enqueue_deferred(input, rule_agent)
        out.deferred_by = "allowlist"
        return out
    from server.memory.app import extraction_policy

    # The operator's policy, never the request's. Read before anything is
    # written: a policy this process cannot read refuses the request, no rows left.
    operator_policy = await extraction_policy.effective(input.org, input.namespace)
    msgs = [_message_dict(m) for m in input.messages]
    last_msgs = [_message_dict(m) for m in (input.last_messages or [])]
    from server.memory.domain.observation_date import resolve_observation_date
    from server.settings import settings as _od_settings

    observation_date, observation_date_source = resolve_observation_date(
        input.observation_date,
        default=str(_od_settings.memory_extract_observation_date_default),
    )
    evidence_ids: list[str] = []
    belief_source_links = 0
    # The ingest names its stages on the response.
    from server.memory.app.search.clock import StageClock

    _ing_work: dict[str, Any] = {}
    _ing_clock = StageClock(_ing_work)

    # Refused before the evidence write starts: a rejected request leaves no rows.
    if input.record_schema is not None and not input.memory_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SCHEMA_REQUIRES_KEY",
                "message": "memory_key is required when record_schema is set",
            },
        )
    # The pipeline never reads the evidence ids; the write runs beside it and is awaited
    # where the ids are first used.
    evidence_task = create_deferred_task(_source_evidence_ids(input, msgs, observation_date))

    # Schema mode: extract ONE typed record matching the caller's schema.
    if input.record_schema is not None:
        return await _set_record_from_messages(
            input,
            msgs,
            observation_date,
            evidence_task,
            input.record_schema,
            operator_policy=operator_policy,
        )

    # Pipeline DAG: each step mutates a shared WorkflowState. Default is
    # fetch_existing → llm_extract → hash_dedup → bulk_write; callers can
    # override `pipeline_steps` to reorder or skip steps.
    from server.memory.app.pipeline import run_pipeline
    from server.memory.app.pipeline_state import WorkflowState

    steps = _pipeline_steps(input)

    state = WorkflowState(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        messages=msgs,
        last_messages=last_msgs,
        observation_date=observation_date,
        extraction_instructions=input.extraction_instructions,
        operator_policy=operator_policy,
        max_tokens=input.max_tokens,
        extra_tags=list(input.extra_tags or []),
        valid_at=input.valid_at,
        rule_agent=rule_agent,
        agent_learnings=learnings_on,
        **_extract_request_pins(input),
    )
    # The session digest is a function of the transcript, not of the extraction result, so
    # its model call and embedding run WHILE the extractor runs; the row is written once the
    # memory keys exist. A session that extracts nothing discards the draft (one cheap call,
    # ~0.4% of sessions).
    summary_draft_task: asyncio.Task[Any] | None = None
    if msgs:

        async def _draft_digest() -> Any:
            with traced("extract.session_summary_draft", messages=len(msgs)):
                return await _draft_session_summary(
                    input, msgs, observation_date, operator_policy=operator_policy
                )

        summary_draft_task = create_deferred_task(_draft_digest())
        summary_draft_task.add_done_callback(_observe_task_result)
    try:
        with traced("extract.pipeline", steps=len(steps)):
            state = await run_pipeline(steps, state)
    except BaseException as e:
        # Any failure or cancel of the extraction also stops the digest draft's LLM call.
        if summary_draft_task is not None:
            summary_draft_task.cancel()
        if not isinstance(e, ExtractionError):
            raise
        raise HTTPException(
            status_code=502,
            detail={"code": "EXTRACTION_FAILED", "message": str(e)},
        ) from e
    _ing_clock.mark("pipeline")

    if state.skipped_dedup:
        log.info(
            "set_from_messages: hash-deduped %d facts (already in namespace)",
            state.skipped_dedup,
        )
    evidence_ids = await evidence_task
    _ing_clock.mark("evidence_write")  # only what the pipeline did not cover
    belief_source_links = await _project_extraction(
        input, msgs, state, evidence_ids, summary_draft_task
    )
    _ing_clock.mark("projections")

    ontology_warnings = await _ontology_warnings(input, state)
    _ing_clock.mark("ontology")
    _ing_clock.finish()

    _account_build(
        input.namespace,
        state.tokens_in,
        state.tokens_out,
        len(state.written),
        len(msgs),
    )
    return MemorySetFromMessagesOutput(
        written=[_BulkSetResult.model_validate(w) for w in state.written],
        refused=[_RefusedFact.model_validate(r) for r in state.refused],
        tokens_in=state.tokens_in,
        tokens_out=state.tokens_out,
        llm_provider=state.llm_provider,
        llm_model=state.llm_model,
        segments=int(getattr(state, "segments", 1) or 1),
        foresight=int(getattr(state, "foresight", 0) or 0),
        forgotten=int(getattr(state, "forgotten", 0) or 0),
        forgotten_keys=list(getattr(state, "forgotten_keys", []) or []),
        forget_requests=int(getattr(state, "forget_requests", 0) or 0),
        about_other=int(getattr(state, "about_other", 0) or 0),
        premise_facts=int(getattr(state, "premise_facts", 0) or 0),
        learning_candidates=state.learning_candidates,
        segmenter=str(getattr(state, "segmenter", "") or "whole"),
        cached_tokens_in=int(getattr(state, "cached_tokens_in", 0) or 0),
        ontology_warnings=ontology_warnings,
        evidence_ids=evidence_ids,
        belief_source_links=belief_source_links,
        pipeline_errors=list(state.errors),
        value_updates_linked=last_value_updates_linked.get(),
        stage_ms={**dict(state.stage_ms), **dict(_ing_work.get("stage_ms", {}))},
        observation_date_source=observation_date_source,
        under_operator_policy=bool(operator_policy),
        **_extract_cache_fields(state),
    )


def _observe_task_result(task: asyncio.Task[Any]) -> None:
    """Done-callback that marks a background draft's outcome as observed, so a
    draft whose request unwound early never logs "exception was never
    retrieved" at GC time. The projection stage re-awaits the task itself."""
    if not task.cancelled():
        task.exception()


# Helper clusters live in sibling modules but are re-imported here so that:
#   1. handle_memory_set_from_messages (which lives in THIS module) keeps
#      resolving them as module globals — tests monkeypatch them on THIS
#      module, and that patch point must stay live;
#   2. every name other modules / tests import from
#      server.memory.app.extract.handler (and the re-export through
#      server.memory.app.handlers) keeps resolving.
# The sibling modules never import this module at module scope, so importing
# them at the bottom here does not cycle.
from server.memory.app.extract.evidence import (  # noqa: E402  (re-export)
    SCOPE_CITED,
    _best_support_span,
    _evidence_ids_by_memory_key,
    _evidence_refs_by_memory_key,
    _lexical_support_scores,
    _link_written_memories_to_evidence,
    _scoped_evidence_refs,
    _write_source_evidence,
)
from server.memory.app.inline_records import (  # noqa: E402  (re-export)
    _write_inline_record_projection,
)
from server.memory.app.session_summary import (  # noqa: E402, F401  (re-export)
    _SESSION_SUMMARY_MAX_INPUT_CHARS,
    _SESSION_SUMMARY_MAX_KEY_FACTS,
    _SESSION_SUMMARY_PROMPT_VERSION,
    _SESSION_SUMMARY_SYSTEM,
    _build_session_summary_user_prompt,
    _draft_session_summary,
    _top_key_facts,
    _write_session_summary,
)

__all__ = [
    "handle_memory_bulk_set",
    "handle_memory_set_from_messages",
    "_best_support_span",
    "_evidence_ids_by_memory_key",
    "_evidence_refs_by_memory_key",
    "_lexical_support_scores",
    "_scoped_evidence_refs",
    "_link_written_memories_to_evidence",
    "_write_source_evidence",
    "_write_inline_record_projection",
    "_build_session_summary_user_prompt",
    "_top_key_facts",
    "_draft_session_summary",
    "_write_session_summary",
    "_SESSION_SUMMARY_MAX_INPUT_CHARS",
    "_SESSION_SUMMARY_MAX_KEY_FACTS",
    # Patch points: the session-summary helpers read these through this module.
    "get_client",
    "get_embedder",
]


def _extract_request_pins(input: Any) -> dict[str, Any]:
    """The per-call A/B pins the pipeline state carries: the coverage sweep and the
    extractor's response cache; None = the namespace's or the deployment's setting."""
    return {
        "coverage_sweep": getattr(input, "coverage_sweep", None),
        "llm_cache": getattr(input, "llm_cache", None),
        # The keys the session's tools served ride into the workflow state so the echo filter
        # can judge against them.
        "recalled_keys": list(getattr(input, "recalled_keys", None) or []),
        # What the session's Loom calls carried, clipped.
        "session_values": [
            str(v)[:8000] for v in (getattr(input, "session_values", None) or []) if v
        ][:100],
    }


def _extract_cache_fields(state: Any) -> dict[str, Any]:
    """The rendered prompts' cache keys (no text) and the response cache's hit / miss counts,
    for the wire and the ledger."""
    return {
        "extract_prompt_keys": list(getattr(state, "extract_prompt_keys", []) or []),
        "llm_cache_hits": int(getattr(state, "llm_cache_hits", 0) or 0),
        "llm_cache_misses": int(getattr(state, "llm_cache_misses", 0) or 0),
    }


def _account_build(
    namespace: str, tokens_in: int, tokens_out: int, written: int, messages: int
) -> None:
    """Build-side cost accounting; telemetry never fails a write."""
    try:
        from server.observability.metrics import record_extraction_build

        record_extraction_build(
            namespace=namespace,
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            written=int(written),
            messages=int(messages),
        )
    except Exception:  # noqa: BLE001 — metrics never fail a write
        pass
    # The marginal cost of construction: the request's provider dollars (the cost
    # accumulator's tally; a response-cache hit reports nothing there) per row written,
    # labelled by the request's traffic class (the namespace's registered kind).
    try:
        from server.observability.cost_accumulator import current as _acc_current
        from server.observability.metrics import record_extract_cost_per_row
        from server.observability.traffic_class import current_traffic_class

        acc = _acc_current()
        if acc is not None:
            record_extract_cost_per_row(
                namespace_kind=current_traffic_class() or "unclassified",
                cost_usd=float(getattr(acc, "cost_usd", 0.0) or 0.0),
                rows=int(written),
            )
    except Exception:  # noqa: BLE001 — metrics never fail a write
        pass
