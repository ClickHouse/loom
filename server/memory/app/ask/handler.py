"""memory.ask handler — retrieve then optionally synthesize a grounded answer.

Thin wire adapter over memory.app.ask.ask, mirroring handle_code_ask:
it resolves the LLM the SAME way the code/search paths do
(server.llm.client.get_llm_client) and gates synthesis on the namespace cost
cap (server.observability.budget.namespace_over_cap) BEFORE ever constructing
the client. Retrieval reuses the existing memory search path
(handle_memory_search) so namespace_scope / search_mode / agent+user filters
behave exactly as memory.search.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from server.core.llm_free import refuse_synthesis
from server.core.naming import named
from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
from server.memory.api.models_common import run_next_hint
from server.memory.api.models_search import MemorySearchInput
from server.memory.app import after_write

if TYPE_CHECKING:
    from server.memory.app.ask.result import MemoryAskResult

log = logging.getLogger(__name__)


def _body_kwargs(input: MemoryAskInput) -> dict[str, Any]:
    """The ask's body mode rides on its retrieval leg — `bodies`, `body_budget_tokens`,
    `source_span_chars` when set. The pool read pins bodies="none" after this and is
    unaffected.

    An unset `bodies` pins "full" on the leg — the reader synthesizes from whole bodies at
    every k, never from the excerpt tier memory.search serves an unpinned caller above
    memory_search_excerpt_bodies_k."""
    out: dict[str, Any] = {"bodies": input.bodies or "full"}
    if input.body_budget_tokens is not None:
        out["body_budget_tokens"] = int(input.body_budget_tokens)
    if int(input.source_span_chars or 0) > 0:
        out["source_span_chars"] = int(input.source_span_chars)
    return out


def _tag_kwargs(input: MemoryAskInput) -> dict[str, Any]:
    """The ask's `tags` ride the retrieval leg as memory.search's own tag predicate — a hard
    OR filter (`hasAny(tags, …)`) by default, a rank boost of `tag_boost` under `tags_soft`.
    Empty tags forward nothing, so the leg is unfiltered."""
    tags = [str(t) for t in (input.tags or []) if t]
    if not tags:
        return {}
    out: dict[str, Any] = {"tags": tags, "tags_soft": bool(input.tags_soft)}
    if input.tag_boost is not None:
        out["tag_boost"] = float(input.tag_boost)
    return out


def _hard_tags(input: MemoryAskInput) -> list[str]:
    """The ask's tags as a hard filter for a leg that has no soft form: the request's `tags`
    unless `tags_soft` made them a boost, in which case the leg is unfiltered."""
    if bool(input.tags_soft):
        return []
    return [str(t) for t in (input.tags or []) if t]


def _ask_search_input(
    input: MemoryAskInput,
    retrieval_query: str,
    *,
    uid: str | None,
    rid: str | None,
    sid: str | None,
) -> MemorySearchInput:
    """The memory.search the ask retrieves with."""
    # Built as a dict so a field can be left UNSET rather than passed as its
    # wire default: `search_mode` is forwarded only when the caller pinned it
    # on the ask. Both models default to "rrf", and passing it unconditionally would mark it
    # "set" downstream, so the deployment's unpinned default (memory_search_default_mode)
    # would never reach the ask path.
    search_kwargs: dict[str, Any] = dict(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        query=retrieval_query,
        top_k=input.top_k,
        memory_type=input.memory_type,
        namespace_scope=input.namespace_scope,
        user_id=uid,
        run_id=rid,
        session_id=sid,
        read_scope=input.read_scope,
        personalization=input.personalization,
        sensitivity_penalty=input.sensitivity_penalty,
        reinforcement_weight=input.reinforcement_weight,
        # As-of replay: scope retrieval to memories known by the anchor so the
        # synthesizer never sees a later fact. search_sql applies the filter.
        as_of=input.as_of,
        as_of_basis=input.as_of_basis,
        observation_date=input.observation_date,
        # Recall telemetry belongs to explicit memory.search calls; don't
        # double-count auto-touch for the retrieval leg of an ask.
        auto_touch=False,
        include_superseded=input.include_superseded,
    )
    if "search_mode" in input.model_fields_set:
        search_kwargs["search_mode"] = input.search_mode
    # Same rule for the per-request lever switches: pinned on the ask → pinned on the search;
    # unpinned → the deployment setting.
    if "rrf_adaptive_lexical" in input.model_fields_set:
        search_kwargs["rrf_adaptive_lexical"] = input.rrf_adaptive_lexical
    if input.digest_descent is not None:
        search_kwargs["digest_descent"] = input.digest_descent
    if input.summary_cap is not None:
        search_kwargs["summary_cap"] = input.summary_cap
    search_kwargs.update({**_body_kwargs(input), **_tag_kwargs(input)})
    search_input = MemorySearchInput(**search_kwargs)
    return search_input


async def _ask_clients(
    input: MemoryAskInput, settings: Any
) -> tuple[Any, Any, Any, Any, bool, float]:
    """The synthesis clients an ask routes across — strong, cheap, mid, and the
    response-cache wrapper of the strong one — and the namespace's budget
    state: (llm, llm_cheap, llm_mid, llm_cached, budget_capped, budget_ratio).
    None for a client the ask does not get."""
    # Cost-cap gate first, exactly as handle_code_ask: never build the client
    # for a capped namespace. Only paid when synthesis was requested.
    llm = None
    llm_cheap = None
    llm_mid = None
    budget_capped = False
    budget_ratio = 0.0
    if input.allow_synthesis:
        from server.settings import settings as _s

        cheap_model = _s.memory_ask_synth_model_cheap.strip()
        if cheap_model:
            # Cascade on: the routing policy wants budget PRESSURE, not just
            # the binary cap, and one ratio read answers both questions.
            from server.observability.budget import namespace_budget_ratio

            budget_ratio = await namespace_budget_ratio(input.org, input.namespace)
            budget_capped = budget_ratio >= 1.0
        else:
            from server.observability.budget import namespace_over_cap

            budget_capped = await namespace_over_cap(input.org, input.namespace)
        if not budget_capped:
            synth_model = _s.memory_ask_synth_model.strip()
            if synth_model:
                # Synthesis uses a dedicated (typically stronger) model,
                # independent of the extraction model on LOOM_LLM_MODEL.
                from server.llm.client import get_llm_client_for_model

                llm = get_llm_client_for_model(synth_model)
            else:
                from server.llm.client import get_llm_client

                llm = get_llm_client()
            if cheap_model and cheap_model != synth_model:
                from server.llm.client import get_llm_client_for_model

                llm_cheap = get_llm_client_for_model(cheap_model)
            # The mid tier — the request's pin over the setting; "off" is the control arm of an
            # A/B on a server that has one.
            mid_model = str(input.mid_model or "").strip() or _s.memory_ask_mid_model.strip()
            if mid_model and mid_model.lower() != "off" and mid_model != synth_model:
                from server.llm.client import get_llm_client_for_model

                llm_mid = get_llm_client_for_model(mid_model)

    # The traffic class's tier policy. A class the map marks `strong: false` answers on the
    # cheap tier alone (no escalation); the default map leaves every tier eligible for every
    # class.
    _tier_policy = _class_llm_policy()
    if llm is not None and llm_cheap is not None and not _tier_policy.strong:
        llm, llm_cheap = llm_cheap, None
    if llm_mid is not None and not _tier_policy.tier_allowed("mid"):
        llm_mid = None

    # Exact-prompt response cache: wrap the routed clients so every synthesis-path call —
    # cheap, strong, deep selector, judges — is a point read before it is a provider call.
    # Per request: the wrapper's counters are this ask's and ride the output.
    llm_cached = None
    if llm is not None and _llm_cache_on(input, settings):
        from server.llm.cache import CachedLLMClient

        llm = llm_cached = CachedLLMClient(llm, org=input.org, namespace=input.namespace)
        if llm_cheap is not None:
            llm_cheap = CachedLLMClient(llm_cheap, org=input.org, namespace=input.namespace)
            llm_cheap._peer = llm_cached  # type: ignore[attr-defined]
        if llm_mid is not None:
            llm_mid = CachedLLMClient(llm_mid, org=input.org, namespace=input.namespace)
            llm_mid._peer = llm_cached  # type: ignore[attr-defined]
    return llm, llm_cheap, llm_mid, llm_cached, budget_capped, budget_ratio


def _ask_precedent(input: MemoryAskInput, llm: Any, settings: Any) -> tuple[str, str]:
    """(precedent key, request-shape digest); both '' when precedent reuse is off."""
    # Precedent reuse: per-request switch, else the server flag; only an ask that would
    # synthesize can be answered by a precedent.
    precedent_key = ""
    precedent_shape = ""
    _prec_on = input.precedents
    if _prec_on is None:
        _prec_on = bool(settings.memory_ask_precedents)
    if _prec_on and input.precedents is None:
        _prec_on = _precedents_namespace_allowed(
            input.namespace, str(settings.memory_ask_precedents_namespaces)
        )
    if _prec_on and input.allow_synthesis and llm is not None:
        from server.memory.domain.precedents import precedent_key as _pkey

        precedent_shape = ask_shape_sha256(input, settings)
        precedent_key = _pkey(input.query, precedent_shape)
    return precedent_key, precedent_shape


async def handle_memory_ask(input: MemoryAskInput) -> MemoryAskOutput:
    from server.memory.app.ask import ask
    from server.settings import settings

    if input.allow_synthesis:
        refuse_synthesis("memory.ask", "allow_synthesis=false")
    # A summary-shaped question over documents goes to the whole-document pass, not to
    # fifteen memories. Deterministic detector, then one knowledge.search to find which
    # documents; falls through when nothing concentrates or the flag is off.
    routed = await _maybe_route_to_synthesize(input)
    if routed is not None:
        return routed

    # Identity -> search filters. memory.search treats user_id/run_id/
    # session_id=None as "no filter"; the identity SENTINEL ("-") means
    # "unset", so map it to None rather than filtering on a literal "-".
    # read_scope rides along: the search handler resolves it against the
    # token's claim and turns it into user/session predicates.
    uid = named(input.user_id)
    rid = named(input.run_id)
    sid = named(input.session_id)

    # Retrieval sees the question; synthesis sees the whole contract. Option blocks, "answer
    # with the letter", stamped dates are for the model, and embedded with the question they
    # pull the top hit under the abstain gate with the evidence in hand.
    from server.memory.domain.query_scaffold import split_answer_scaffold

    retrieval_query, scaffold = split_answer_scaffold(input.query)
    if not scaffold:
        retrieval_query = input.query

    search_input = _ask_search_input(input, retrieval_query, uid=uid, rid=rid, sid=sid)

    llm, llm_cheap, llm_mid, llm_cached, budget_capped, budget_ratio = await _ask_clients(
        input, settings
    )

    precedent_key, precedent_shape = _ask_precedent(input, llm, settings)

    # The run's own notes precede the memories in the prompt.
    scratchpad = ""
    if rid:
        from server.memory.app.working_memory import render_run_blocks

        scratchpad = await render_run_blocks(input.org, input.namespace, rid)

    res = await ask(
        search_input,
        question=input.query,
        llm=llm,
        allow_synthesis=input.allow_synthesis,
        budget_capped=budget_capped,
        max_answer_chars=input.max_answer_chars,
        llm_cheap=llm_cheap,
        llm_mid=llm_mid,
        mid_routes=(input.mid_routes or None),
        budget_ratio=budget_ratio,
        depth=input.depth or "auto",
        **_deep_overrides(input),
        recount=input.recount,
        token_budget=input.token_budget,
        personalize=input.personalize,
        window_rel_floor=input.window_rel_floor,
        decompose=(input.decompose or None),
        gh_items_fallback=input.gh_items_fallback,
        precedent_key=precedent_key,
        precedent_shape=precedent_shape,
        world_knowledge=(input.world_knowledge or None),
        recent_messages=[m.model_dump() for m in (input.recent_messages or [])],
        presupposition_rule=input.presupposition_rule,
        set_window=input.set_window,
        knowledge=input.knowledge,
        scratchpad=scratchpad,
        consistency_mode=(input.consistency or None),
        route_prior=(input.route_prior or None),
        route_prior_max_cheap=input.route_prior_max_cheap,
        default_answer_chars=input.default_answer_chars,
    )

    _schedule_cited_used(input, res)
    _record_contradiction(input, res)
    _schedule_deferred_consistency(input, res)

    _grounding = _grounding_for(res, retrieval_query, input)
    return MemoryAskOutput(
        answer=res.answer,
        source=res.source,  # type: ignore[arg-type]
        abstain=res.abstain,
        abstain_reason=res.abstain_reason,
        memory_keys=res.memory_keys,
        results=res.results,
        next=run_next_hint(rid) if res.memory_keys else "",
        synth_model=res.synth_model,
        route_tier=res.route_tier,  # type: ignore[arg-type]
        route_reason=res.route_reason,
        **_ask_cache_fields(llm_cached, llm_cheap, llm_mid),
        temporal_consistency=_grounding.temporal_consistency,
        citation_concentration=_grounding.citation_concentration,
        escalated=res.escalated,
        gate_mode=res.gate_mode,
        gate_kept=res.gate_kept,
        gate_dropped=res.gate_dropped,
        pending_extraction=await _pending_extraction(input),
        pending_evidence=int(res.pending_evidence or 0),
        retrieval_query=retrieval_query if scaffold else "",
        facets=list(res.facets),
        facet_memories_added=res.facet_memories_added,
        consistency=res.consistency,  # type: ignore[arg-type]
        consistency_score=res.consistency_score,
        consistency_note=res.consistency_note,
        identifier_chase_hops=int(res.identifier_chase_hops or 0),
        identifier_chase_added=int(res.identifier_chase_added or 0),
        premise_conflict=res.premise_conflict,
        premise_note=res.premise_note,
        premise_verdict=res.premise_verdict or "",
        attribution_note=res.attribution_note or "",
        depth=res.depth,  # type: ignore[arg-type]
        **_deep_fields(res),
        synth_calls=int(res.synth_calls or 0),
        **_synth_accounting(res.synth_model),
        deep_fired=res.deep_fired,
        decomposed=list(res.decomposed or []),
        decomposed_parts=len(list(res.decomposed or [])),
        superseded_dropped=int(res.superseded_dropped or 0),
        stale_sentences_masked=int(res.stale_sentences_masked or 0),
        entity_followed=list(res.entity_followed or []),
        when_evidence_turns=int(res.when_evidence_turns or 0),
        decompose_failed=int(res.decompose_failed or 0),
        pool_k=int(res.pool_k or 0),
        grounding=(res.grounding or ""),
        deep_reason=res.deep_reason,
        deep_keys=list(res.deep_keys or []),
        deep_chars=int(res.deep_chars or 0),
        code_hits=int(res.code_hits or 0),
        **_pool_fields(res),
        timeline_rows=int(res.timeline_rows or 0),
        context_tokens_est=int(res.context_tokens_est or 0),
        context_trimmed=int(res.context_trimmed or 0),
        summary_hits=int(res.summary_hits or 0),
        knowledge_hits=int(res.knowledge_hits or 0),
        count_recomputed=bool(res.count_recomputed),
    )


def _grounding_for(res: MemoryAskResult, retrieval_query: str, input: MemoryAskInput) -> Any:
    """Score the citations (sessions, window consistency)."""
    from server.memory.domain.answer_grounding import Grounding, grounding_of
    from server.memory.domain.question_window import resolve_time_window

    try:
        return grounding_of(
            list(res.memory_keys or []),
            list(res.results or []),
            window=resolve_time_window(retrieval_query, input.observation_date),
        )
    except Exception as e:  # noqa: BLE001 - telemetry, never blocks the answer
        log.debug("answer grounding skipped: %s", e)
        return Grounding()


async def _maybe_route_to_synthesize(input: MemoryAskInput) -> MemoryAskOutput | None:
    """None = not routed (the caller asked for memories only or for an as-of
    replay, not a corpus task, no document concentrates, or the whole-document pass
    abstained); otherwise the whole-document answer as an ask result whose `route_reason`
    names the route and the document count."""
    from server.settings import settings

    # The route is a synthesis over today's documents: `allow_synthesis=False` asked
    # for no model answer, and an `as_of` replay must not see a later document.
    if not input.allow_synthesis or input.as_of is not None:
        return None
    from server.memory.domain import ask_routing

    if not ask_routing.is_corpus_task(input.query):
        return None
    try:
        from server.knowledge.api.models_knowledge import (
            KnowledgeSearchInput,
            KnowledgeSynthesizeInput,
        )
        from server.knowledge.app.knowledge_search import handle_knowledge_search
        from server.knowledge.app.synthesize import handle_knowledge_synthesize
        from server.memory.api.models_ask import MemoryAskOutput
        from server.memory.app.agent_rules import request_rule_agent, served_hits

        async def search(top_k: int) -> list[Any]:
            found = await handle_knowledge_search(
                KnowledgeSearchInput(
                    org=input.org,
                    namespace=input.namespace,
                    query=input.query,
                    top_k=top_k,
                    namespace_scope="this",
                    collapse_sections=False,
                    # The ask's hard tag filter reaches every leg.
                    tags=_hard_tags(input),
                    # It only counts documents for the route: no rerank call.
                    rerank="none",
                )
            )
            return list(found.results)

        top_k = 20  # not a setting: the hit pool pick_documents narrows to a few documents
        # Another bot's promoted rule is not a rule about this agent.
        hits = await served_hits(
            await search(top_k),
            search,
            lambda h: list(getattr(h, "tags", None) or []),
            request_rule_agent(input.agent),
            top_k,
        )
        docs = ask_routing.pick_documents(
            hits,
            max_docs=int(settings.memory_ask_synthesize_route_max_docs),
        )
        if not docs:
            return None
        synth = await handle_knowledge_synthesize(
            KnowledgeSynthesizeInput(
                org=input.org,
                namespace=input.namespace,
                source_uris=docs,
                task=input.query,
            )
        )
        if synth.abstain or not synth.answer:
            return None
        return MemoryAskOutput(
            answer=synth.answer,
            source="synthesized",
            abstain=False,
            memory_keys=[],
            results=[],
            synth_model=synth.model,
            route_tier="strong",
            route_reason=ask_routing.route_reason(docs),
        )
    except Exception as e:  # noqa: BLE001 - the memory path is always available
        log.debug("memory.ask synthesize route skipped: %s", e)
        return None


async def _pending_extraction(input: MemoryAskInput) -> int:
    """Deferred batches in flight for the asked scope; 0 on any failure — disclosure must
    never fail an answer."""
    try:
        from server.memory.app.pending_disclosure import pending_extraction

        uid = named(input.user_id)
        return await pending_extraction(
            org=input.org, namespace=input.namespace, agent=input.agent, user_id=uid
        )
    except Exception:  # noqa: BLE001 — an advisory count; 0 on failure
        return 0


def _deep_overrides(input: MemoryAskInput) -> dict[str, Any]:
    """The request's deep-pass pins: the selector and the deep-first order, None = setting."""
    return {
        "deep_select": input.deep_select,
        "deep_first": input.deep_first,
    }


def _deep_fields(res: MemoryAskResult) -> dict[str, Any]:
    """What the deep pass reports on the wire."""
    return {
        "deep_selector": res.deep_selector or "",
        "deep_first": res.deep_first or "",
    }


def _class_llm_policy() -> Any:
    """The current request's traffic-class tier policy."""
    from server.identity.domain.traffic_class_policy import llm_policy_for
    from server.observability.traffic_class import current_traffic_class

    return llm_policy_for(current_traffic_class())


def _llm_cache_on(input: MemoryAskInput, settings: Any) -> bool:
    """The per-request `llm_cache` wins; unset, the traffic
    class's policy (`bench` on by default) decides when it names
    `llm_cache`; otherwise the deployment default applies to the namespaces
    its globs name (empty = all)."""
    pinned = input.llm_cache
    if pinned is not None:
        return bool(pinned)
    class_default = _class_llm_policy().llm_cache
    if class_default is not None:
        return bool(class_default)
    if not bool(settings.memory_ask_llm_cache_default):
        return False
    return _precedents_namespace_allowed(
        input.namespace, str(settings.memory_ask_llm_cache_namespaces)
    )


def _synth_accounting(synth_model: str) -> dict[str, Any]:
    """The answering model's tally from the request's cost accumulator: calls, whole-prompt
    input tokens, the provider's prompt-cache read and write shares, dollars. Empty tally (no
    provider call — a response-cache hit, or no synthesis) reads as zeros."""
    out: dict[str, Any] = {
        "synth_input_tokens": 0,
        "synth_cached_input_tokens": 0,
        "synth_cache_write_tokens": 0,
        "synth_cost_usd": 0.0,
    }
    try:
        from server.observability.cost_accumulator import current

        acc = current()
        tally = (acc.by_model.get(synth_model) if acc is not None and synth_model else None) or []
        if len(tally) >= 6:
            out["synth_input_tokens"] = int(tally[1])
            out["synth_cost_usd"] = round(float(tally[3]), 6)
            out["synth_cached_input_tokens"] = int(tally[4])
            out["synth_cache_write_tokens"] = int(tally[5])
    except Exception:  # noqa: BLE001 — accounting never blocks the answer
        pass
    return out


def _pool_fields(res: MemoryAskResult) -> dict[str, Any]:
    """What the synthesizer's prompt was made of: the pool's fact and chunk rows and the
    estimated tokens per prompt block, for the wire."""
    return {
        "pool_facts": int(res.pool_facts or 0),
        "pool_chunks": int(res.pool_chunks or 0),
        "prompt_blocks": dict(res.prompt_blocks or {}),
    }


def _cache_sum(strong: Any, cheap: Any, field: str, mid: Any = None) -> float:
    """Sum a wrapper field over the request's cache wrappers (strong, and the
    cheap and mid tiers when the cascade built them). No strong wrapper = the
    cache was off for the ask = 0."""
    if strong is None:
        return 0.0
    total = float(getattr(strong, field, 0) or 0)
    for tier in (cheap, mid):
        if tier is not None and getattr(tier, "_peer", None) is strong:
            total += float(getattr(tier, field, 0) or 0)
    return total


def _cache_count(strong: Any, cheap: Any, field: str, mid: Any = None) -> int:
    """Integer counters (`hits`, `misses`) over the request's cache wrappers."""
    return int(_cache_sum(strong, cheap, field, mid))


def _ask_cache_fields(strong: Any, cheap: Any, mid: Any = None) -> dict[str, Any]:
    """The ask output's response-cache fields: whether the wrapper was built at all
    (`llm_cache`), the calls it served and sent on, and the price-table dollars the hits did
    not spend."""
    return {
        "llm_cache": strong is not None,
        "llm_cache_hits": _cache_count(strong, cheap, "hits", mid),
        "llm_cache_misses": _cache_count(strong, cheap, "misses", mid),
        "llm_cache_saved_usd": round(_cache_sum(strong, cheap, "saved_usd", mid), 6),
    }


def _precedents_namespace_allowed(namespace: str, globs: str) -> bool:
    """The server flag applies to the namespaces named by comma-separated
    fnmatch globs; empty = all. A per-request `precedents` value bypasses
    this (the caller said so)."""
    import fnmatch

    patterns = [g.strip() for g in (globs or "").split(",") if g.strip()]
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(namespace or "", pat) for pat in patterns)


# Fields that identify WHO asks or WHAT text, not the shape of the ask. The
# shape is every other non-default field: search mode, top_k, scope, as_of,
# depth, budgets, gates... Two asks with the same text and the same shape
# read the same window, which is what a precedent needs.
_ASK_SHAPE_EXCLUDE = frozenset({"query", "org", "namespace", "run_id", "precedents"})
# A request field left unset takes its setting, so the settings that shape retrieval
# and synthesis are part of the shape too: a live change (world knowledge allow ->
# forbid) misses every precedent written under the old value. Like the request
# fields, only a value off its default is hashed, so a release that adds a setting
# keeps the stored precedents. The precedent settings only decide whether to reuse.
_ASK_SHAPE_SETTINGS = ("memory_ask_", "memory_search_", "memory_rrf_")


# Names of the settings that bound how long a call waits or retries, not what it answers.
_OPERATIONAL_SUFFIXES = ("_timeout_s", "_timeout_ms", "_bound_s", "_bound_retries", "_wall_ms")


def _shape_settings(settings: Any) -> dict[str, Any]:
    shape = {}
    for name, field in type(settings).model_fields.items():
        if not name.startswith(_ASK_SHAPE_SETTINGS) or name.startswith("memory_ask_precedent"):
            continue
        # A timeout, a wait bound or a cache lifetime changes no answer a precedent holds:
        # a call cut short already changes the window the precedent is checked against.
        if name.endswith(_OPERATIONAL_SUFFIXES) or "_ttl" in name:
            continue
        value = getattr(settings, name)
        if value != field.get_default(call_default_factory=True):
            shape[name] = value
    return shape


def ask_shape_sha256(input: MemoryAskInput, settings: Any) -> str:
    """sha256 over the ask's non-default, non-identity fields. A `user_id` or
    `session_id` the read filters on is part of the shape, whatever the read
    scope: a namespace-scoped ask that names no user shares its precedent
    with the namespace's readers, and one filtered to a user (by the body or
    by the token) never shares it with another user. The identity hashed is
    the one the read runs under: the token's read filters are pinned first,
    so a token bound to one end user never shares a precedent with another
    user's token whatever the body names. The agent whose promoted rules the
    knowledge passages serve is part of the shape when it is not the body's
    agent ('-' when the body sends none), so a bot's token never reuses an
    answer drawn from another bot's rule."""
    import hashlib
    import json

    from server.auth.context import current_auth
    from server.identity.domain.read_scope import pin_read_filters
    from server.memory.app.agent_rules import request_rule_agent

    input, _ = pin_read_filters(input, current_auth())
    rule_agent = request_rule_agent(input.agent)
    exclude = set(_ASK_SHAPE_EXCLUDE)
    # Only a user or session the read filters on is part of the shape.
    exclude |= {f for f in ("user_id", "session_id") if getattr(input, f) in (None, "-")}
    shape = input.model_dump(exclude=exclude, exclude_defaults=True)
    # An empty body agent is served as '-', so only a pin that changes the agent adds the key.
    if rule_agent != (input.agent or "-"):
        shape["rule_agent"] = rule_agent
    # Absent, not empty, at every default: the shape a build without this key hashed.
    if shaped := _shape_settings(settings):
        shape["settings"] = shaped
    canonical = json.dumps(shape, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _schedule_cited_used(input: MemoryAskInput, res: Any) -> None:
    """An answer's citations are the `used` signal.

    Retrieval marks a memory `recall`ed; otherwise only a client calling
    run.complete(memories_used=...) marks one `used`, and clients rarely do. But memory.ask
    already knows which memories its synthesized answer drew on: the keys the answer
    text cites. Credit exactly those, only when the LLM actually cited
    (`_keys_cited`, not the "all recalled" fallback the output carries), and
    never on abstain, retrieval-only or the record path. Best-effort and off
    the request path, like the recall auto-touch."""
    # An evaluation read credits nothing.
    if not input.reinforce:
        return
    from server.memory.app.recall_credit import is_measurement_read

    if is_measurement_read(agent=input.agent):
        return  # a bench / probe label is a measurement read
    # A precedent is the earlier synthesized answer served again; its citations were used
    # again.
    if res.abstain:
        return
    if res.source not in ("synthesized", "precedent"):
        return
    from server.memory.app.ask.result import _keys_cited

    hits = list(res.results or [])
    cited = set(_keys_cited(str(res.answer or ""), hits))
    if not cited:
        return
    ns_keys: dict[str, list[str]] = {}
    for h in hits:
        key = str(getattr(h, "memory_key", "") or "")
        if key in cited:
            ns_keys.setdefault(getattr(h, "namespace", "") or input.namespace, []).append(key)

    async def _bg() -> None:
        try:
            from server.memory.store import usage as memory_repo_usage

            # The ask's own credit is `used_cited`, not `used` — `used` is what an agent
            # reports (memory.outcome / run.complete).
            await memory_repo_usage.record_usage_for_keys(
                input.org,
                ns_keys,
                event_type=memory_repo_usage.USED_CITED_EVENT,
                agent=input.agent,
            )
        except Exception as e:  # noqa: BLE001 — credit never fails an answer
            log.warning("memory.ask: cited-used credit failed (non-fatal): %s", e)

    after_write.schedule(_bg(), name="memory.ask.cited_used")


def _schedule_deferred_consistency(input: MemoryAskInput, res: MemoryAskResult) -> None:
    """Under consistency_mode="deferred" the answer left without its answer-vs-citation
    verdict. Run the judge now, off the response path, and land the verdict where the inline
    mode would have: a `memory.ask.consistency` ledger row (kind='verdict', the request's
    request_id, the judge's tokens and cost) and — for CONTRADICTED — the `contradicted`
    usage event on the named memory. The precedent store rides inside `run` and waits for
    the verdict."""
    run = res.deferred_consistency
    if run is None:
        return
    import time
    from types import SimpleNamespace

    from server.core.logging_config import request_id_var

    request_id = request_id_var.get() or ""
    identity = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id or "-",
        "session_id": input.session_id or "-",
    }
    hits = list(res.results or [])

    async def _bg() -> None:
        from server.observability import cost_accumulator

        # A fresh accumulator: the request's own was drained with its row.
        cost_accumulator.begin_op()
        t0 = time.perf_counter()
        try:
            verdict = await run()
        except Exception as e:  # noqa: BLE001 — a dropped verdict never surfaces
            log.warning("memory.ask: deferred consistency judge failed (non-fatal): %s", e)
            return
        label = str(getattr(verdict, "label", "") or "")
        contradicted = bool(getattr(verdict, "contradicted", False))
        _record_contradiction(
            input,
            SimpleNamespace(
                consistency=label,
                contradicted_key=(
                    str(getattr(verdict, "contradicted_key", "") or "") if contradicted else ""
                ),
                results=hits,
            ),
        )
        try:
            from server.observability.ledger import emit_agent_event

            emit_agent_event(
                op_name="memory.ask.consistency",
                identity=identity,
                request_id=request_id,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                success=bool(getattr(verdict, "ran", False)),
                error_code="" if getattr(verdict, "ran", False) else "JUDGE_UNAVAILABLE",
                kind="verdict",
                result={
                    "consistency": label,
                    "consistency_score": float(getattr(verdict, "score", 0.0) or 0.0),
                },
            )
        except Exception as e:  # noqa: BLE001 — the ledger row is best-effort
            log.warning("memory.ask: deferred consistency row failed (non-fatal): %s", e)

    after_write.schedule(_bg(), name="memory.ask.deferred_consistency")


def _record_contradiction(input: MemoryAskInput, res: Any) -> None:
    """A CONTRADICTED consistency verdict is a prediction error about one memory: the reader,
    grounded in the pool, said the opposite of what the cited row says. Record it as a
    `contradicted` usage event on the named memory, the same funnel `used` and `harm` take,
    so the usage ledger and the review surfaces see which rows the reader keeps disagreeing
    with. Off the request path, best-effort, never folded into ranking here — the fold
    weight is tuned separately."""
    if not input.reinforce:
        return
    from server.memory.app.recall_credit import is_measurement_read

    if is_measurement_read(agent=input.agent):
        return  # a bench / probe label is a measurement read
    if str(res.consistency or "") != "contradicted":
        return
    key = str(res.contradicted_key or "")
    if not key:
        return
    hits = list(res.results or [])
    ns_keys: dict[str, list[str]] = {}
    for h in hits:
        if str(getattr(h, "memory_key", "") or "") == key:
            ns_keys.setdefault(getattr(h, "namespace", "") or input.namespace, []).append(key)
            break
    if not ns_keys:
        return  # the judge named a key the pool did not carry: nothing to mark

    async def _bg() -> None:
        try:
            from server.memory.store import usage as memory_repo_usage

            await memory_repo_usage.record_usage_for_keys(
                input.org,
                ns_keys,
                event_type="contradicted",
                agent=input.agent,
            )
        except Exception as e:  # noqa: BLE001 — a dropped mark never fails an answer
            log.warning("memory.ask: contradicted mark failed (non-fatal): %s", e)

    after_write.schedule(_bg(), name="memory.ask.contradicted")
