"""Sequential step pipeline for memory.set_from_messages.

The default (`fetch_existing → llm_extract → hash_dedup → bulk_write`)
reproduces the no-custom-steps behavior; per-request `pipeline_steps`
reorder or skip steps. Each step is addressed by a registered step ID
(`loom.<name>` for the built-ins) and reads/writes the shared `WorkflowState`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.core.datetimes import parse_iso_dt
from server.core.errors import caller_text
from server.core.hashing import hash_text
from server.memory.api.models_bulk import REFUSED, MemoryBulkSetInput, _BulkSetEntry, refused_facts
from server.memory.api.models_crud import MemoryDeleteInput
from server.memory.app.assistant_learnings import file_assistant_rules
from server.memory.app.pipeline_state import PipelineStep, StepCallable, WorkflowState
from server.memory.domain.extracted_facts import (
    _disambiguate_key,
    _merge_extractions,
    _source_role_tags,
    foresight_end,
    judge_verdict,
    judged_confidence,
    keep_latest_per_key,
    keep_latest_relation_keyed,
)
from server.memory.domain.forget_requests import (
    MAX_FORGET_REQUESTS,
    forget_requests_from_messages,
    forget_targets,
    is_forget_request,
    replacement_facts,
)
from server.memory.domain.ownership import ownership_of, user_names_in
from server.memory.domain.premise_facts import premise_facts_from_messages
from server.memory.domain.recall_echoes import key_tokens, recall_echoes
from server.settings import settings

log = logging.getLogger(__name__)


# Step callables by step ID.
_REGISTRY: dict[str, StepCallable] = {}


def register_step(step_id: str, callable_: StepCallable) -> None:
    """Register a callable under a step_id. Replaces existing registration."""
    _REGISTRY[step_id] = callable_


def registered_step_ids() -> list[str]:
    return sorted(_REGISTRY.keys())


# Default pipeline. Operators can override per call via `pipeline_steps`.
DEFAULT_PIPELINE: list[PipelineStep] = [
    PipelineStep(step_id="loom.fetch_namespace_categories"),
    PipelineStep(step_id="loom.fetch_existing_memories"),
    PipelineStep(step_id="loom.llm_extract"),
    PipelineStep(step_id="loom.drop_recall_echoes"),
    PipelineStep(step_id="loom.drop_session_echoes"),
    PipelineStep(step_id="loom.drop_activity_narration"),
    PipelineStep(step_id="loom.drop_non_durable"),
    PipelineStep(step_id="loom.verify_durable"),
    PipelineStep(step_id="loom.hash_dedup"),
    PipelineStep(step_id="loom.bulk_write"),
]


async def run_pipeline(
    steps: list[PipelineStep],
    state: WorkflowState,
) -> WorkflowState:
    """Execute steps in order against the shared state.

    Each step is responsible for its own error handling — fetch steps
    log, record the failure in `state.errors` and skip (preserving
    state). `loom.llm_extract` is the one
    fail-CLOSED step: its `ExtractionError` aborts the pipeline and the
    handler surfaces it as 502. `state.errors` is returned to the caller
    as `pipeline_errors` so fail-open skips are visible, not silent.
    """
    from server.observability.otel import traced

    # One span per step, name fixed and step_id carried as an attribute: the
    # step ids come from the request, so putting the id in the span NAME would
    # let a caller mint unbounded span names.
    for step in steps:
        _t0 = time.perf_counter()
        try:
            with traced("pipeline.step", step=step.step_id):
                callable_ = _REGISTRY.get(step.step_id)
                if callable_ is None:
                    log.warning(
                        "pipeline: unknown step_id %r; skipping. registered: %s",
                        step.step_id,
                        ",".join(registered_step_ids()),
                    )
                    state.errors.append(f"unknown_step:{step.step_id}")
                    continue
                await callable_(state, step.config)
        finally:
            # The step's wall time rides the state so the
            # response can say which step was slow (a span needs a trace lookup).
            state.stage_ms[f"step.{step.step_id}"] = round((time.perf_counter() - _t0) * 1000.0, 1)
    return state


# ============================================================
# Built-in steps
# ============================================================


# Registered categories the extraction prompt offers as tags for new rows, by name.
OFFERED_CATEGORIES = 32  # not a setting: the prompt's size


async def _step_fetch_namespace_categories(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Populate state.namespace_categories from loom.categories.

    Pulled once per ingest call; the LLM extractor uses this list to
    anchor emitted tags to the registered taxonomy. Empty list = no
    injection in the extraction prompt = free-form tags.
    Fail-open: any CH error leaves the list empty.
    """
    del config
    from server.core.ch import get_client

    try:
        rows = await get_client().query_all(
            """
            SELECT name, description, substring(summary, 1, 200) AS summary
            FROM loom.categories FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            ORDER BY name
            LIMIT {lim:UInt32}
            """,
            params={"org": state.org, "ns": state.namespace, "lim": OFFERED_CATEGORIES},
        )
        state.namespace_categories = [
            {
                "name": str(r["name"]),
                "description": str(r["description"] or ""),
                "summary": str(r["summary"] or ""),
            }
            for r in rows
        ]
    except Exception as e:  # noqa: BLE001 — categories only steer the prompt
        log.warning("fetch_namespace_categories step failed: %s", e)
        state.namespace_categories = []
    return state


async def _step_fetch_existing_memories(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Populate state.existing_memories via semantic ANN against the
    namespace. Limit configurable; default 50.
    """
    del config
    import server.memory.store as memory_repo
    from server.memory.app.handlers import _fetch_relevant_existing_memories

    try:
        identity = memory_repo.Identity(
            org=state.org,
            namespace=state.namespace,
            agent=state.agent,
            user_id=state.user_id,
            session_id=state.session_id,
        )
        state.existing_memories = await _fetch_relevant_existing_memories(
            identity,
            state.messages,
            limit=50,
        )
    except Exception as e:  # noqa: BLE001 — a failed existing-memory read must not fail extraction
        log.warning("fetch_existing_memories step failed: %s", e)
        state.existing_memories = []
        # `memory.set_from_messages` returns these as `pipeline_errors`: the kind.
        state.errors.append(f"fetch_existing_failed:{caller_text(e)}")
    return state


async def _step_llm_extract(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Run the LLM extractor and populate state.extracted_facts.

    Fail-closed: an `ExtractionError` propagates out of the pipeline to
    the handler's 502 path. Catching it here and returning a state with
    zero facts would make `memory.set_from_messages` answer HTTP 200 with
    `written=[]` — the caller's transcript silently lost, which is the
    exact outcome the extractor's fail-closed contract exists to prevent
    (see memory/app/extract/).

    Config options:
        judge: bool   — override the judge gate (default False on
                        messages path; see handle_memory_set_from_messages)
    """
    from server.memory.app.extract import (
        _use_compact_prompt,
        extract_from_messages,
    )

    judge_enabled = bool(config.get("judge", False))
    segments = await _plan_segments(state)
    state.segments = len(segments)
    # The coverage sweep is the namespace's choice; one lookup
    # per ingest, shared by every segment.
    from server.memory.app.coverage_sweep_gate import resolve as _resolve_sweep

    # Per-call override first, then the namespace
    # switch, then the density gates inside the extractor.
    sweep_override = (
        state.coverage_sweep
        if state.coverage_sweep is not None
        else await _resolve_sweep(state.org, state.namespace)
    )
    # In a one-shot coding session (a lookup, a test run) the
    # statement-dense reply is a report of tool results; the density/uncovered
    # auto-gates (tuned on multi-session conversations) fire on it and re-read that
    # report. A working session keeps the sweep. An explicit namespace/per-call switch still wins.
    min_turns = int(settings.memory_extract_coding_sweep_min_user_turns)
    coding_off = (
        sweep_override is None
        and _is_coding_capture(state)
        and _user_turns(state.messages) < min_turns
    )
    sweep_override = False if coding_off else sweep_override

    async def _one(seg_messages: list[dict[str, Any]], last: list[dict[str, Any]] | None) -> Any:
        return await extract_from_messages(
            seg_messages,
            max_tokens=state.max_tokens,
            observation_date=state.observation_date,
            existing_memories=state.existing_memories or None,
            last_messages=last or None,
            custom_instructions=state.extraction_instructions,
            operator_policy=state.operator_policy,
            judge=judge_enabled,
            namespace_categories=state.namespace_categories or None,
            coverage_sweep=sweep_override,
            speaker_attribution=not _is_coding_capture(state),
            coverage_sweep_source=(
                "per_call"
                if state.coverage_sweep is not None
                else ("coding" if coding_off else "namespace" if sweep_override is not None else "")
            ),
            org=state.org,
            namespace=state.namespace,
            llm_cache=state.llm_cache,
            # The WHOLE input decides, so a short
            # segment of a long transcript keeps the full prompt.
            compact_prompt=_use_compact_prompt(state.messages),
            agent_learnings=_agent_learnings_on(state),
        )

    if len(segments) == 1:
        parts = [(0, await _one(state.messages, state.last_messages or None))]
    else:
        # One extraction per segment, concurrently (bounded). Each
        # segment after the first sees the tail of the previous one as
        # `last_messages` so a reference across the boundary still resolves.
        import asyncio

        sem = asyncio.Semaphore(4)

        async def _run(k: int, seg: Any) -> tuple[int, Any]:
            msgs = state.messages[seg.start : seg.end]
            last = (
                state.messages[max(0, seg.start - 2) : seg.start]
                if k > 0
                else (state.last_messages or None)
            )
            async with sem:
                return seg.start, await _one(msgs, last)

        parts = list(await asyncio.gather(*[_run(k, s) for k, s in enumerate(segments)]))
    extraction = _merge_extractions(parts)
    state.extract_prompt_keys = [
        str(getattr(ex, "prompt_key", "") or "")
        for _, ex in sorted(parts, key=lambda p: p[0])
        if getattr(ex, "prompt_key", "")
    ]
    state.llm_cache_hits = sum(int(getattr(ex, "llm_cache_hits", 0) or 0) for _, ex in parts)
    state.llm_cache_misses = sum(int(getattr(ex, "llm_cache_misses", 0) or 0) for _, ex in parts)
    state.tokens_in = extraction.llm_result.tokens_in
    state.tokens_out = extraction.llm_result.tokens_out
    state.cached_tokens_in = int(getattr(extraction.llm_result, "cached_input_tokens", 0) or 0)
    state.llm_provider = extraction.llm_result.provider
    state.llm_model = extraction.llm_result.model
    state.dropped_parse = int(getattr(extraction, "dropped_parse", 0) or 0)
    state.dropped_judge_wrong = int(getattr(extraction, "dropped_judge_wrong", 0) or 0)
    from server.memory.app.drops import record_drop

    if state.dropped_parse:
        record_drop(
            state.org,
            state.namespace,
            "extract.parse",
            "discarded",
            n=state.dropped_parse,
        )
    if state.dropped_judge_wrong:
        record_drop(
            state.org,
            state.namespace,
            "extract.judge",
            "wrong",
            n=state.dropped_judge_wrong,
        )
    # The response did not fit the output budget: "retried" cost a second
    # call, "salvaged" lost the tail of the payload. One row per call so
    # the rate per thousand extractions is readable from loom.memory_drops.
    truncation = str(getattr(extraction, "truncation", "") or "")
    if truncation in ("retried", "salvaged"):
        record_drop(state.org, state.namespace, "extract.truncate", truncation)
    state.extracted_facts = [
        {
            "key": f.memory_key,
            "value": f.value,
            "tags": list(f.tags),
            "entities": list(f.entities or []),
            "linked_keys": list(f.linked_keys or []),
            "source_message_indices": list(f.source_message_indices or []),
            "relations": [list(r) for r in (f.relations or [])],
            "key_from_relation": bool(getattr(f, "key_from_relation", False)),
            # Per-occurrence derived operands carried to loom.derived_facts.
            "derived": [dict(d) for d in (getattr(f, "derived", None) or [])],
            "temporal_anchor": f.temporal_anchor,
            "temporal_confidence": float(getattr(f, "temporal_confidence", 0.0) or 0.0),
            "valid_at": f.valid_at,
            "valid_to": f.valid_to,
            "foresight": getattr(f, "foresight", None),
            "about": str(getattr(f, "about", "") or "user"),
            "forget": bool(getattr(f, "forget", False)),
            "files": list(getattr(f, "files", None) or []),
            "concepts": list(getattr(f, "concepts", None) or []),
            "question_topic": str(getattr(f, "question_topic", "") or ""),
            "judge_verdict": f.judge_verdict,
            "prompt_version": extraction.prompt_version,
            "evidence": str(getattr(f, "evidence", "") or ""),
            "grounding": str(getattr(f, "grounding", "") or ""),
        }
        for f in extraction.facts
    ]
    return state


async def _fetch_for_forget_requests(
    state: WorkflowState, requests: list[dict[str, Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    """The existing rows nearest each forget request's own clause: one ANN read per
    request, fail-open, so the target is a candidate even when the window's batch-level
    fetch missed it."""
    if not requests:
        return []
    import server.memory.store as memory_repo
    from server.memory.app.handlers import _fetch_relevant_existing_memories

    identity = memory_repo.Identity(
        org=state.org,
        namespace=state.namespace,
        agent=state.agent,
        user_id=state.user_id,
        session_id=state.session_id,
    )
    seen = {str(r.get("memory_key") or "") for r in state.existing_memories}
    out: list[dict[str, Any]] = []
    for req in requests:
        clause = str(req.get("value") or "").strip()
        if not clause:
            continue
        try:
            rows = await _fetch_relevant_existing_memories(
                identity, [{"role": "user", "content": clause}], limit=limit
            )
        except Exception as e:  # noqa: BLE001 — a missed candidate, never a failed write
            log.warning("forget request: candidate fetch failed (open): %s", e)
            continue
        for r in rows or []:
            k = str(r.get("memory_key") or "")
            if k and k not in seen:
                seen.add(k)
                out.append(r)
    return out


async def _retire_forgotten(state: WorkflowState, keys: set[str]) -> int:
    """Tombstone the live row of every key the user asked to forget, under
    this identity's org/namespace and any agent/session (the row may have
    been written in an earlier session). Fails open per key; returns how many
    were retired."""
    from server.memory.app.handlers import handle_memory_delete

    n = 0
    for key in sorted(keys):
        try:
            out = await handle_memory_delete(
                MemoryDeleteInput(
                    org=state.org,
                    namespace=state.namespace,
                    agent="*",
                    user_id=state.user_id or "-",
                    session_id="-",
                    memory_key=key,
                )
            )
            n += 1 if getattr(out, "tombstoned", False) else 0
        except Exception as e:  # noqa: BLE001 — a forget that fails is logged, never fatal
            log.warning("forget request: retiring %r failed (open): %s", key, e)
    return n


async def _plan_segments(state: WorkflowState) -> list[Any]:
    """The segments this extraction reads: fixed size windows for a long or
    heavy session, else one segment covering everything.

    Size windows: one extraction call over a long session
    emits roughly the same number of facts whatever the length (220
    messages → 36 facts, every location line dropped), so past
    `memory_extract_size_segment_messages` the session is read in fixed
    windows of `memory_extract_size_segment_turns` turns."""
    from server.memory.domain.topic_segment import Segment, size_segments
    from server.settings import settings

    cap = int(settings.memory_extract_size_segment_messages)
    # A character budget triggers the same windows for a
    # short-but-heavy session (tool results carried whole).
    char_cap = int(settings.memory_extract_size_segment_chars)
    total_chars = (
        sum(len(str(m.get("content", "") or "")) for m in state.messages) if char_cap else 0
    )
    if (cap and len(state.messages) > cap) or (char_cap and total_chars > char_cap):
        state.segmenter = "size"
        return size_segments(
            state.messages,
            turns_per_segment=int(settings.memory_extract_size_segment_turns),
            max_segments=int(settings.memory_extract_size_segment_max),
            chars_per_segment=char_cap,
        )
    return [Segment(idx=0, start=0, end=len(state.messages), turns=0)]


async def _existing_keys_named(state: WorkflowState, tokens: set[str]) -> set[str]:
    """Which of `tokens` are live keys of the namespace: the fetched
    neighbours first, then one bounded lookup for the rest (fail-open)."""
    known = {str(m.get("memory_key") or "") for m in state.existing_memories or []}
    found = {t for t in tokens if t in known}
    rest = sorted(t for t in tokens if t not in found)
    if not rest:
        return found
    try:
        from server.core.ch import get_client
        from server.identity.app.request_scope import request_scope
        from server.identity.domain.read_scope import scope_and

        params: dict[str, Any] = {"org": state.org, "ns": state.namespace, "keys": rest[:64]}
        # Only keys the caller may read count: another user's key is no echo.
        in_scope = scope_and(request_scope(), params)
        rows = await get_client().query_all(
            f"""
            SELECT DISTINCT memory_key
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL{in_scope}
            """,
            params=params,
        )
        found |= {str(r.get("memory_key") or "") for r in rows or []}
    except Exception as e:  # noqa: BLE001 — fail open: an unknown key is not an echo
        log.warning("recall-echo key lookup failed (non-fatal): %s", e)
    return found


async def _step_drop_recall_echoes(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Drop extracted facts that restate a memory the namespace already
    holds by name. `state.skipped_recall_echo` counts them."""
    del config
    facts = list(state.extracted_facts or [])
    tokens: set[str] = set()
    for fact in facts:
        tokens.update(key_tokens(str(fact.get("value") or "")))
    # The keys the session itself named ("saved under `coffee_type`")
    # — a paraphrased fact under a wider key is the same recall echoed.
    said: set[str] = set()
    for m in [*(state.messages or []), *(state.last_messages or [])]:
        if str(m.get("role") or "") == "assistant":
            said.update(key_tokens(str(m.get("content") or "")[:4000]))
    # What the session's tools SERVED counts as named too — a session that
    # answered "Espresso." without naming the key would still write
    # `user_coffee_type` back.
    said.update(k.strip() for k in (state.recalled_keys or []) if k and k.strip())
    tokens |= said
    if not tokens:
        return state
    existing = await _existing_keys_named(state, tokens)
    if not existing:
        return state
    drop = set(recall_echoes(facts, existing, recalled_keys=existing & said))
    if drop:
        state.extracted_facts = [f for i, f in enumerate(facts) if i not in drop]
        state.skipped_recall_echo = len(drop)
        log.info(
            "set_from_messages: dropped %d recall echo(es) naming existing keys %s",
            len(drop),
            sorted(existing)[:6],
        )
    return state


async def _step_drop_session_echoes(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Drop extracted facts that restate what the session's own Loom tool
    calls wrote, asked or were answered: the tool already
    persisted or served that text, so a re-extracted copy can only duplicate
    it — or contradict it once the session moved the key on."""
    del config
    from server.memory.domain import session_echoes

    facts = list(state.extracted_facts or [])
    test_values = (
        session_echoes.synthetic_test_values(state.messages) if _is_coding_capture(state) else []
    )
    values = [*state.session_values, *test_values]
    if not facts or not values:
        return state
    kept, dropped = session_echoes.partition(facts, values)
    if dropped:
        state.extracted_facts = kept
        state.skipped_session_echo = len(dropped)
        log.info(
            "set_from_messages: dropped %d session echo(es) %s",
            len(dropped),
            [str(f.get("key") or f.get("memory_key") or "") for f in dropped][:6],
        )
    return state


async def _step_drop_activity_narration(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Drop extracted facts that narrate a session's tool activity ("User read
    lines 1-1000 of X", "Claude ran a command on …") with no durable outcome,
    and, in a trajectory replay, name the agent where the model wrote "User".
    `state.skipped_activity` counts the drops."""
    del config
    from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS
    from server.memory.domain import activity_facts

    facts = list(state.extracted_facts or [])
    if not facts:
        return state
    kept, dropped = activity_facts.partition(facts)
    if TRAJECTORY_EXTRACTION_INSTRUCTIONS in (state.extraction_instructions or ""):
        kept = [
            {**f, "value": activity_facts.subject_to_agent(f.get("value"), state.agent)}
            for f in kept
        ]
    state.extracted_facts = kept
    if dropped:
        state.skipped_activity = len(dropped)
        log.info(
            "set_from_messages: dropped %d activity-narration fact(s) %s",
            len(dropped),
            [str(f.get("memory_key") or "") for f in dropped][:6],
        )
    return state


# The plugins tag every capture with the client that ran the session
# (`source:claude-code`, `source:codex`, …); a trajectory replay carries its
# own instructions. Only those transcripts are coding sessions.
_CODING_SOURCES = ("source:claude-code", "source:codex", "source:cursor")


def _is_coding_capture(state: WorkflowState) -> bool:
    from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS

    if TRAJECTORY_EXTRACTION_INSTRUCTIONS in (state.extraction_instructions or ""):
        return True
    return any(str(t).lower().startswith(_CODING_SOURCES) for t in (state.extra_tags or []))


def _agent_learnings_on(state: WorkflowState) -> bool:
    """Whether the extractor is asked for the assistant's rules about itself: when
    the op turned them on (`state.agent_learnings`, settled once from the deployment
    setting and the request). A coding session's assistant narrates its work, so a
    coding capture never is; nor is a request with no agent to file for (a token
    pinned to one end user that names no agent), or with no assistant turn to read a
    rule from (a user-only call, an observation replay)."""
    if _is_coding_capture(state) or not state.rule_agent:
        return False
    if not any(str(m.get("role") or "") == "assistant" for m in state.messages):
        return False
    return state.agent_learnings


def _user_turns(messages: list[dict[str, Any]] | None) -> int:
    return sum(1 for m in messages or [] if str(m.get("role") or "") == "user")


async def _step_drop_non_durable(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Drop extracted facts that are not durable: status
    snapshots, peer-session chatter, housekeeping actions, the memory tool's
    own state, code lookups the index answers, first-person plans, and
    duplicates within the extraction. Coding-session captures only — "User
    left her job" in a personal chat is a fact; "User left 98 GB of images"
    in a coding session is housekeeping. `state.skipped_non_durable` counts
    the drops."""
    del config
    facts = list(state.extracted_facts or [])
    if not facts or not _is_coding_capture(state):
        return state
    from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS
    from server.memory.domain import durable_facts

    replay = TRAJECTORY_EXTRACTION_INSTRUCTIONS in (state.extraction_instructions or "")
    kept, dropped = durable_facts.partition(
        facts, replay_agent=str(state.agent or "") if replay else None
    )
    state.extracted_facts = [{k: v for k, v in f.items() if not k.startswith("_")} for f in kept]
    if dropped:
        state.skipped_non_durable = len(dropped)
        log.info(
            "set_from_messages: dropped %d non-durable fact(s) %s",
            len(dropped),
            [
                f"{f.get('_non_durable')}:{f.get('key') or f.get('memory_key') or ''}"
                for f in dropped
            ][:8],
        )
    return state


def _verify_clients(state: WorkflowState) -> list[Any]:
    """The durable-fact verifier models, primary first; cached like the
    extractor's clients so a replayed capture is not re-billed."""
    names = [m.strip() for m in str(settings.memory_extract_verify_models).split(",")]
    if settings.llm_provider == "fake":  # a dev/test stack: no model to ask
        return []
    from server.core.llm_provider import provider_for_model
    from server.llm.client import get_llm_client_for_model, provider_key_missing
    from server.memory.app.extract import extractor_llm_cache_on

    clients: list[Any] = []
    for name in (n for n in names if n):
        if provider_key_missing(provider_for_model(name, settings.llm_provider)):
            continue  # a fake stand-in's vote would count as a real one
        client: Any = get_llm_client_for_model(name)
        if extractor_llm_cache_on(state.llm_cache):
            from server.llm.cache import CachedLLMClient

            client = CachedLLMClient(client, org=state.org, namespace=state.namespace)
        clients.append(client)
    return clients


async def _step_verify_durable(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """The durability verifier over a coding capture's
    remaining facts. Drops a fact every verifier model calls non-durable and
    folds same-claim paraphrases into the most complete member.
    `state.skipped_unverified` counts the drops. Fails open."""
    del config
    facts = list(state.extracted_facts or [])
    if not facts or not _is_coding_capture(state):
        return state
    clients = _verify_clients(state)
    if not clients:
        return state
    from server.memory.app import durability_verifier

    result = await durability_verifier.verify(
        facts,
        clients,
        timeout_s=float(settings.memory_extract_verify_timeout_s),
        # The goal and what the lookups returned
        context=durability_verifier.capture_context(state.messages, state.session_values),
    )
    state.extracted_facts = [
        {k: v for k, v in f.items() if not k.startswith("_")} for f in result.kept
    ]
    state.tokens_in = int(getattr(state, "tokens_in", 0) or 0) + result.tokens_in
    state.tokens_out = int(getattr(state, "tokens_out", 0) or 0) + result.tokens_out
    if result.dropped:
        state.skipped_unverified = len(result.dropped)
        log.info(
            "set_from_messages: verifier dropped %d fact(s) %s",
            len(result.dropped),
            [
                f"{f.get('_verify', '')[:24]}:{f.get('key') or f.get('memory_key') or ''}"
                for f in result.dropped
            ][:8],
        )
    return state


async def _step_hash_dedup(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Drop facts whose canonical hash already exists in
    state.existing_memories."""
    del config
    # The stored column and the fetch step hash the canonical JSON of the
    # value (server.core.hashing.sha256_value); this step hashes the same way.
    from server.core.hashing import sha256_value as _content_sha256

    existing_shas: set[str] = {
        hash_text(m.get("content_sha256"))
        for m in state.existing_memories
        if m.get("content_sha256")
    }
    kept: list[dict[str, Any]] = []
    skipped = 0
    for fact in state.extracted_facts:
        sha = _content_sha256(fact.get("value", ""))
        if sha in existing_shas:
            skipped += 1
            continue
        existing_shas.add(sha)
        kept.append(fact)
    state.extracted_facts = kept
    state.skipped_dedup = skipped
    return state


async def _newest_wins(state: WorkflowState) -> bool:
    """Whether the namespace's conflict policy is `newest_wins`; False when
    the lookup fails, so the batch falls back to `_2` disambiguation."""
    try:
        from server.core.ch import get_client
        from server.memory.app.conflicts import namespace_conflict_policy

        policy = await namespace_conflict_policy(state.org, state.namespace, ch=get_client())
        return policy == "newest_wins"
    except Exception as e:  # noqa: BLE001 — policy unknown: keep `_2` disambiguation
        log.debug("conflict policy lookup failed; keeping `_2` disambiguation: %s", e)
        return False


# Stored confidence for facts the coverage sweep added.
_SWEEP_CONFIDENCE = 0.85


async def _forget_plan(
    state: WorkflowState, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """The batch to write, the keys it retires, and the replacement facts that
    are written even under a retired key."""
    # A request to forget is a Contract, not a fact. Collect
    # the keys the forget entries name (their own key and the existing
    # memories they link), retire them, and keep every same-key fact in this
    # batch out of the write — the user's last word on the attribute wins.
    # The request is read from the user's own sentences too;
    # the extractor is not asked to notice it.
    forget_requests = forget_requests_from_messages(state.messages)
    state.forget_requests = len(forget_requests)
    if len(forget_requests) == MAX_FORGET_REQUESTS:
        log.info("forget requests: read the first %d of the window", MAX_FORGET_REQUESTS)
    # The batch-level fetch is similarity to the whole window; a short
    # "please forget that I keep a journal" rarely pulls its target into
    # those fifty rows. Each request fetches
    # the rows nearest its own clause as well.
    candidates = list(state.existing_memories) + await _fetch_for_forget_requests(
        state, forget_requests
    )
    # What the user said instead is written, never retired.
    facts, keep, linked_old = replacement_facts(
        facts, forget_requests, candidates, state.messages, keep_said=not state.operator_policy
    )
    forget_keys = forget_targets(facts + forget_requests, candidates, keep) | linked_old
    # A replacement's own key is retired only when a live row holds it (the
    # old version); otherwise the "retire" would be a no-op reported as forgotten.
    rows = {str(r.get("memory_key") or "") for r in candidates}
    forget_keys -= {k for k in keep if k not in rows}
    if forget_keys:
        state.forgotten = await _retire_forgotten(state, forget_keys)
        state.forgotten_keys = sorted(forget_keys)
    return facts, forget_keys, keep


async def _file_agent_rules(state: WorkflowState) -> None:
    """Move the rules the assistant stated about itself out of the batch and into
    the learnings queue. Never a memory: an end user can talk a bot into saying
    anything, and an agent-level row would reach every user of the bot."""
    agent_rules = [f for f in state.extracted_facts if f.get("about") == "agent"]
    if not agent_rules:
        return
    state.extracted_facts = [f for f in state.extracted_facts if f.get("about") != "agent"]
    state.learning_candidates = await file_assistant_rules(state, agent_rules)


async def _step_bulk_write(
    state: WorkflowState,
    config: dict[str, Any],
) -> WorkflowState:
    """Convert extracted_facts to bulk_set entries and write."""
    del config
    await _file_agent_rules(state)
    # The deterministic passes read the messages, not the extractor's output — a session
    # that is only "please forget X" / "got it" extracts to nothing and still has work
    # here. Under an operator policy every row comes from the model that was given it,
    # so no premise fact is added from the raw turns.
    premise_facts = (
        []
        if state.operator_policy
        else premise_facts_from_messages(
            state.messages, state.extracted_facts, state.existing_memories
        )
    )
    state.premise_facts = len(premise_facts)
    if premise_facts:
        state.extracted_facts = [*state.extracted_facts, *premise_facts]
    if not state.extracted_facts and not forget_requests_from_messages(state.messages):
        return state

    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.drops import record_drop

    # Under `newest_wins` two facts in one batch that share a
    # key are two statements of one attribute, and the later one is the live
    # one. The earlier is dropped with a trace instead of being written as
    # `<key>_2` next to it.
    # Under any policy, facts whose shared key came from a single-valued
    # relation are statements of one attribute too: the later one is live, as
    # it would be had the two arrived in separate batches.
    facts = state.extracted_facts
    newest_wins = await _newest_wins(state)
    facts, dropped = (
        keep_latest_per_key(facts) if newest_wins else keep_latest_relation_keyed(facts)
    )
    for key in dropped:
        record_drop(state.org, state.namespace, "write", "superseded_in_batch", item=key)
    state.key_collisions += len(dropped)

    seen_global: set[str] = set()
    entries: list[_BulkSetEntry] = []
    facts, forget_keys, keep = await _forget_plan(state, facts)
    batch_user_names = user_names_in(facts, state.existing_memories)
    for fact in facts:
        # Merge extracted tags with caller's extra_tags
        seen: set[str] = set()
        merged: list[str] = []
        role_tags = _source_role_tags(state.messages, fact)
        for t in list(fact.get("tags") or []) + list(state.extra_tags) + role_tags:
            ts = str(t)
            if ts and ts not in seen:
                seen.add(ts)
                merged.append(ts)
        key = str(fact.get("key", "")).strip()
        if not key:
            continue
        if is_forget_request(fact) or (key in forget_keys and key not in keep):
            # Never store the request itself, nor a fact the user just asked
            # us to forget.
            continue
        if key in seen_global:
            # Two facts whose keys sanitised to the same 64-char key: rather
            # than drop the second without a trace, disambiguate
            # deterministically (`_2`, `_3`, ...) and count it.
            original = key
            key = _disambiguate_key(key, seen_global)
            state.key_collisions += 1
            log.info(
                "bulk_write: memory_key collision in %s/%s; wrote %r",
                state.org,
                state.namespace,
                key,
            )
            record_drop(
                state.org,
                state.namespace,
                "write",
                "key_collision",
                item=original,
            )
        seen_global.add(key)
        rels_raw = fact.get("relations") or []
        rels: list[tuple[str, str, str]] = []
        for r in rels_raw:
            if isinstance(r, (list, tuple)) and len(r) == 3:
                rels.append((str(r[0]), str(r[1]), str(r[2])))
        derived = [d for d in (fact.get("derived") or []) if isinstance(d, dict)]
        # The judge verdict goes to source_trace (audit/UI) and nudges confidence.
        verdict = judge_verdict(fact)
        fact_confidence = judged_confidence(verdict)
        # A coverage-sweep fact fills a gap; at equal similarity
        # the first pass's fact ranks first (0.1 * confidence in the score).
        if "coverage_sweep" in (fact.get("tags") or []):
            fact_confidence = min(fact_confidence, _SWEEP_CONFIDENCE)
        prompt_version = str(fact.get("prompt_version") or "")
        trace = "pipeline:set_from_messages"
        if prompt_version:
            trace = f"{trace}|prompt={prompt_version}"
        if verdict in ("UNCERTAIN", "CORRECT"):
            trace = f"{trace}|verdict={verdict.lower()}"
        # valid_at precedence: extractor value, then per-call default, then
        # the fact's own temporal_anchor (only if non-future — a fact about a
        # plan is true from when it was stated, not the planned date), then
        # observation_date. All NULL only when no observation_date was given.
        fact_valid_at = parse_iso_dt(fact.get("valid_at")) or state.valid_at
        if fact_valid_at is None:
            obs_dt = parse_iso_dt(state.observation_date)
            if obs_dt is not None:
                anchor_dt = parse_iso_dt(fact.get("temporal_anchor"))
                fact_valid_at = (
                    anchor_dt if anchor_dt is not None and anchor_dt <= obs_dt else obs_dt
                )
        fact_valid_to = parse_iso_dt(fact.get("valid_to"))
        # Type the fact from its extracted tags so the memory_type axis is
        # informative (preference boosting, decision recall, per-type quotas)
        # instead of collapsing to "semantic". Both "preference" and "decision"
        # are searchable types, so this never drops a row from search; the
        # extractor already tags these (see extraction_prompts.py). The decision
        # rationale ("what they chose/rejected and why") lives in the value, and
        # any decision↔fact relationship rides the entity_triples emitted from
        # `relations` below.
        if "preference" in merged:
            mem_type = "preference"
        elif "decision" in merged:
            mem_type = "decision"
        else:
            mem_type = "semantic"
        # Ownership: a fact about someone other than the user
        # carries `about:other`, so the profile fold leaves it out and the
        # reader sees whose preference it is.
        about_other = str(fact.get("about") or "user").lower() == "other"
        if not about_other and ownership_of(fact, user_names=batch_user_names) == "other":
            # The extractor never fills `about`; the subject
            # of the sentence says whose fact it is.
            about_other = True
        if about_other and "about:other" not in merged:
            merged = [*merged, "about:other"]
        if about_other:
            state.about_other += 1
        # Foresight: a forward-looking or temporary state the
        # extractor bounded with `{"until", "duration_days"}` becomes a
        # `foresight` row whose valid_to is the end of the window, so the
        # default read drops it once the window has passed
        # while an as_of read inside the window still sees it.
        f_end = foresight_end(
            fact.get("foresight"), start=fact_valid_at or parse_iso_dt(state.observation_date)
        )
        if f_end is not None:
            mem_type = "foresight"
            if fact_valid_to is None or f_end < fact_valid_to:
                fact_valid_to = f_end
            if "foresight" not in merged:
                merged = [*merged, "foresight"]
            state.foresight += 1
        entries.append(
            _BulkSetEntry(
                memory_key=key,
                value=str(fact.get("value", "")),
                memory_type=mem_type,
                confidence=fact_confidence,
                source_trace=trace,
                tags=merged,
                entities=list(fact.get("entities") or []),
                relations=rels,
                derived=derived,
                temporal_anchor=fact.get("temporal_anchor"),
                temporal_confidence=float(fact.get("temporal_confidence") or 0.0),
                valid_at=fact_valid_at,
                valid_to=fact_valid_to,
                files=[str(p) for p in (fact.get("files") or []) if str(p).strip()],
                concepts=[str(c) for c in (fact.get("concepts") or []) if str(c).strip()],
                topics=[str(t) for t in (fact.get("topics") or []) if str(t).strip()][:3],
                question_topic=str(fact.get("question_topic") or ""),
                evidence=str(fact.get("evidence") or "")[:400],
                grounding=str(fact.get("grounding") or ""),
            )
        )

    if not entries:
        return state

    bulk_input = MemoryBulkSetInput(
        org=state.org,
        namespace=state.namespace,
        agent=state.agent,
        user_id=state.user_id,
        session_id=state.session_id,
        run_id=state.run_id,
        entries=entries,
    )
    _record_bulk_out(state, await handle_memory_bulk_set(bulk_input))
    return state


def _record_bulk_out(state: WorkflowState, bulk_out: Any) -> None:
    """The whole per-entry result, so set_from_messages answers what bulk_set did, but
    for a refused entry (blacklisted, pinned or locked key): it produced no row, and
    neither evidence nor chunks may attach to the stored one through it."""
    state.written = [r.model_dump() for r in bulk_out.written if r.conflict_verdict not in REFUSED]
    state.refused = [f.model_dump() for f in refused_facts(bulk_out.written)]
    for phase, ms in (getattr(bulk_out, "stage_ms", None) or {}).items():
        state.stage_ms[f"bulk.{phase}"] = float(ms)


# Register built-ins at module import
register_step("loom.fetch_namespace_categories", _step_fetch_namespace_categories)
register_step("loom.fetch_existing_memories", _step_fetch_existing_memories)
register_step("loom.llm_extract", _step_llm_extract)
register_step("loom.drop_recall_echoes", _step_drop_recall_echoes)
register_step("loom.drop_session_echoes", _step_drop_session_echoes)
register_step("loom.drop_activity_narration", _step_drop_activity_narration)
register_step("loom.drop_non_durable", _step_drop_non_durable)
register_step("loom.verify_durable", _step_verify_durable)
register_step("loom.hash_dedup", _step_hash_dedup)
register_step("loom.bulk_write", _step_bulk_write)
