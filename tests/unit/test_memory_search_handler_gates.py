"""Handler-path tests for handle_memory_search LLM gating (all externals faked)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.domain.search_types import SearchHit
from tests.unit._memory_search_pkg import patch_handler_name


class _FakeEmbedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1] * 8


class _FakeCH:
    async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
        return []

    async def query_one(self, sql: str, params: dict | None = None) -> None:
        return None


class _FakeLLM:
    model = "fake-llm"

    async def complete(self, system: str = "", user: str = "", **kw: Any) -> Any:
        return SimpleNamespace(text="hypothetical answer")


def _svc_hit(key: str, *, sim: float, rank: float | None = None) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"fact about {key}",
        distance=1.0 - sim,
        rank_score=rank if rank is not None else sim,
    )


def _input(**overrides: Any) -> MemorySearchInput:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "query": "what is my favorite color",
        "namespace_scope": "this",
        "top_k": 2,
        "auto_touch": False,
    }
    base.update(overrides)
    return MemorySearchInput(**base)


@pytest.fixture
def search_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.core.ch as ch_mod
    import server.llm.client as llm_client
    import server.memory.app.embedder_calibration as ec
    import server.memory.app.entity_resolution as er
    import server.memory.app.rerank as rerank_mod
    import server.memory.app.retrieval as msearch
    import server.memory.app.search_llm as sllm

    env = SimpleNamespace()
    env.hits = []
    env.search_queries = []
    env.derived_intent_calls = []
    env.judge_calls = []
    env.judge_result = (True, "")
    env.fetch_aggregate_calls = []
    env.fetch_aggregate_result = None
    env.fetch_temporal_diff_calls = []
    env.fetch_temporal_diff_result = None

    fake_ch = _FakeCH()
    patch_handler_name(monkeypatch, "get_embedder", lambda: _FakeEmbedder())
    patch_handler_name(monkeypatch, "get_client", lambda: fake_ch)
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake_ch)
    monkeypatch.setattr(ec, "_calibration", None)
    # Pin the abstain threshold. It is an env-tunable knob
    # (LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD), so leaving it ambient would make the
    # abstain tests depend on the developer's or runner's environment: at 0.15
    # the below-threshold gate stops firing for 0.21/0.18 hits and the
    # calibrator's confidence branch wins, so abstain_code flips to
    # low_calibrated_confidence and the assertion fails for no code reason.
    from server.settings import settings as _live_settings

    monkeypatch.setattr(_live_settings, "memory_search_abstain_threshold", 0.30, raising=False)

    async def fake_resolve_calibration(org: str, ns: str) -> None:
        return None

    monkeypatch.setattr(ec, "resolve_calibration", fake_resolve_calibration)

    async def fake_rerank_config(org: str, ns: str) -> tuple[str, str]:
        return ("", "")

    monkeypatch.setattr(rerank_mod, "get_namespace_rerank_config", fake_rerank_config)

    async def fake_vector_search(q: Any, *, ch: Any = None) -> list[SearchHit]:
        env.search_queries.append(q)
        return list(env.hits)

    monkeypatch.setattr(msearch, "vector_search", fake_vector_search)

    async def fake_fetch_aggregate(ch: Any, **kw: Any) -> dict | None:
        env.fetch_aggregate_calls.append(kw)
        return env.fetch_aggregate_result

    monkeypatch.setattr(msearch, "fetch_derived_aggregate", fake_fetch_aggregate)

    async def fake_fetch_temporal_diff(ch: Any, **kw: Any) -> dict | None:
        env.fetch_temporal_diff_calls.append(kw)
        return env.fetch_temporal_diff_result

    monkeypatch.setattr(msearch, "fetch_temporal_diff", fake_fetch_temporal_diff)

    async def fake_derived_intent(query: str, **kw: Any) -> None:
        env.derived_intent_calls.append(query)
        return None

    monkeypatch.setattr(sllm, "extract_derived_intent_llm", fake_derived_intent)

    async def fake_judge(query: str, excerpts: list[str], **kw: Any):
        env.judge_calls.append((query, list(excerpts)))
        return env.judge_result

    monkeypatch.setattr(sllm, "judge_results_iteratively", fake_judge)

    async def fake_lookup(
        ch: Any, *, org: str, namespaces: list[str], entity_texts: list[str]
    ) -> list[str]:
        return list(entity_texts)

    monkeypatch.setattr(er, "lookup_canonical_entities", fake_lookup)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda *a, **k: _FakeLLM())
    return env


async def _run(env: SimpleNamespace, **overrides: Any):
    from server.memory.app.search import handle_memory_search

    return await handle_memory_search(_input(**overrides))


async def test_fast_budget_spends_no_llm_call(search_env) -> None:
    search_env.hits = [_svc_hit("weak", sim=0.30)]
    out = await _run(search_env, retrieval_budget="fast", top_k=1)
    assert search_env.judge_calls == []
    assert out.retrieval_work["llm_calls_total"] == 0


async def test_weak_top_hit_makes_no_hyde_call(search_env) -> None:
    # A weak top hit on the default path makes no HyDE call: the read path
    # stays LLM-free.
    search_env.hits = [_svc_hit("weak", sim=0.30)]
    out = await _run(search_env, top_k=1, hyde=False)
    assert out.retrieval_work["llm_hyde_calls"] == 0


async def test_default_path_skips_iterative_judge(search_env) -> None:
    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    out = await _run(search_env)
    assert search_env.judge_calls == []
    assert out.retrieval_work["llm_iterative_judge_calls"] == 0


async def test_quality_recipe_runs_iterative_judge(search_env) -> None:
    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    out = await _run(search_env, recipe="quality")
    assert len(search_env.judge_calls) == 1
    assert out.retrieval_work["llm_iterative_judge_calls"] == 1


async def test_no_judge_call_when_the_fallback_round_is_unaffordable(
    search_env, monkeypatch
) -> None:
    """With no fallback round left in the cascade budget, the judge's answer could not buy a
    retry: the LLM call is not made at all."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_fallback_max_rounds", 0)
    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    out = await _run(search_env, recipe="quality")
    assert search_env.judge_calls == []
    assert out.retrieval_work["llm_iterative_judge_calls"] == 0


async def test_fast_budget_overrides_quality_judge(search_env) -> None:
    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    await _run(search_env, recipe="quality", retrieval_budget="fast")
    assert search_env.judge_calls == []


async def test_parser_handles_grammar_shape_without_enrichment_llm(search_env) -> None:
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0), _svc_hit("b", sim=0.70, rank=0.5)]
    out = await _run(
        search_env,
        query="how many books did I buy last month?",
        observation_date="2023-07-15",
    )
    assert out.retrieval_work["intent_parser_used"] is True


async def test_parser_comparative_shape_fans_out_sub_queries(search_env) -> None:
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0), _svc_hit("b", sim=0.78, rank=0.98)]
    out = await _run(
        search_env,
        query="did I adopt the cat or buy the couch first?",
        observation_date="2023-07-15",
    )
    assert out.retrieval_work["intent_parser_used"] is True
    assert out.retrieval_work["sub_query_count"] == 2
    assert search_env.judge_calls == []


async def test_stated_value_update_never_reaches_count_aggregate(search_env) -> None:
    search_env.hits = [_svc_hit("count_note", sim=0.80)]
    out = await _run(
        search_env,
        query="how many books do I have now?",
        observation_date="2023-07-15",
        top_k=1,
    )
    assert search_env.fetch_aggregate_calls == []
    assert search_env.derived_intent_calls == []
    assert out.derived_aggregate is None
    assert all(h.source != "derived" for h in out.results)
    assert search_env.search_queries[0].recency_intent is True


async def test_derived_aggregate_injected_as_first_result_row(search_env) -> None:
    search_env.hits = [_svc_hit("kit_a", sim=0.80, rank=0.8), _svc_hit("kit_b", sim=0.70, rank=0.7)]
    search_env.fetch_aggregate_result = {
        "count": 3,
        "sum": 7800.0,
        "avg": 2600.0,
        "first_at": datetime(2023, 5, 24),
        "last_at": datetime(2023, 6, 10),
        "span_days": 17.0,
        "items": [
            {
                "object": "vase",
                "numeric_value": 2000.0,
                "unit": "usd",
                "occurred_at": datetime(2023, 5, 24),
            },
        ],
    }
    out = await _run(
        search_env,
        query="how many model kits have I bought?",
        observation_date="2023-07-15",
    )
    assert search_env.derived_intent_calls == []
    assert len(search_env.fetch_aggregate_calls) == 1
    first = out.results[0]
    assert first.source == "derived"
    assert first.memory_key == "derived_aggregate"
    assert first.rank_score > out.results[1].rank_score
    assert "count = 3" in first.content_excerpt
    assert first.content_excerpt.startswith("Computed from 3 dated records")
    assert out.derived_aggregate is not None
    assert out.derived_aggregate["count"] == 3
    assert out.derived_aggregate["op"] == "count"
    assert {h.memory_key for h in out.results[1:]} == {"kit_a", "kit_b"}
    assert out.abstain is False


async def test_temporal_diff_injected_as_pinned_first_row(search_env) -> None:
    search_env.hits = [_svc_hit("ev_a", sim=0.80, rank=0.8), _svc_hit("ev_b", sim=0.70, rank=0.7)]
    search_env.fetch_temporal_diff_result = {
        "interval_days": 14,
        "from_date": "2023-04-29",
        "to_date": "2023-05-13",
        "from_excerpt": "binoculars",
        "to_excerpt": "goldfinches",
        "to_observation": False,
    }
    out = await _run(
        search_env,
        query=(
            "how many days passed between the day I got my binoculars and "
            "the day I noticed the goldfinches?"
        ),
        observation_date="2023-05-20",
    )
    assert len(search_env.fetch_temporal_diff_calls) >= 1
    first = out.results[0]
    assert first.source == "computed"
    assert first.memory_key == "temporal_diff"
    assert first.rank_score > out.results[1].rank_score
    assert "[computed] interval = 14 days" in first.content_excerpt
    assert {h.memory_key for h in out.results[1:]} == {"ev_a", "ev_b"}
    assert out.abstain is False


async def test_temporal_diff_suppresses_derived_span(search_env) -> None:
    search_env.hits = [_svc_hit("ev_a", sim=0.80, rank=0.8)]
    search_env.fetch_temporal_diff_result = {
        "interval_days": 19,
        "from_date": "2023-02-10",
        "to_date": "2023-03-01",
        "from_excerpt": "a",
        "to_excerpt": "b",
        "to_observation": False,
    }
    # A span aggregate is also available but must not be injected.
    search_env.fetch_aggregate_result = {
        "count": 5,
        "sum": None,
        "avg": None,
        "first_at": datetime(2023, 2, 10),
        "last_at": datetime(2023, 3, 1),
        "span_days": 19.0,
        "items": [],
    }
    out = await _run(
        search_env,
        query=(
            "how many days passed between the day I launched my site and the "
            "day I signed my first client?"
        ),
        observation_date="2023-03-25",
    )
    computed_rows = [h for h in out.results if h.source == "computed"]
    derived_rows = [h for h in out.results if h.source == "derived"]
    assert len(computed_rows) == 1
    assert derived_rows == []
    assert out.results[0].source == "computed"
    assert out.derived_aggregate is None


async def test_temporal_diff_fails_open_on_unresolved_operands(search_env) -> None:
    search_env.hits = [_svc_hit("ev_a", sim=0.80, rank=0.8), _svc_hit("ev_b", sim=0.70, rank=0.7)]
    search_env.fetch_temporal_diff_result = None  # operands didn't resolve
    out = await _run(
        search_env,
        query=("how many days passed between the day I did thing one and the day I did thing two?"),
        observation_date="2023-05-20",
    )
    assert all(h.source != "computed" for h in out.results)
    assert {h.memory_key for h in out.results} == {"ev_a", "ev_b"}


async def test_non_temporal_query_skips_temporal_diff_leg(search_env) -> None:
    search_env.hits = [_svc_hit("color", sim=0.80, rank=0.8)]
    out = await _run(
        search_env,
        query="what is my favorite color now?",
        observation_date="2023-07-15",
        top_k=1,
    )
    assert search_env.fetch_temporal_diff_calls == []
    assert all(h.source != "computed" for h in out.results)


def test_parser_gate_helper_keys_off_fast_posture() -> None:
    from server.memory.app.search.query_plan import _enrichment_parser_gate_active

    assert _enrichment_parser_gate_active(_input()) is False
    assert _enrichment_parser_gate_active(_input(retrieval_budget="standard")) is False
    assert _enrichment_parser_gate_active(_input(retrieval_budget="deep")) is False
    assert _enrichment_parser_gate_active(_input(recipe="quality")) is False
    assert _enrichment_parser_gate_active(_input(recipe="fast")) is True
    assert _enrichment_parser_gate_active(_input(retrieval_budget="fast")) is True


async def test_parser_gate_skips_enrichment_llm_on_confident_spec(search_env) -> None:
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0), _svc_hit("b", sim=0.70, rank=0.5)]
    out = await _run(
        search_env,
        recipe="fast",
        query="how many books did I buy last month?",
        observation_date="2023-07-15",
    )
    assert out.retrieval_work["intent_parser_used"] is True
    assert out.retrieval_work["query_enrichment_used"] is True
    assert out.retrieval_work["llm_calls_total"] == 0


async def test_a_standard_read_estimates_no_llm_call_for_its_parser(search_env) -> None:
    # Enrichment is the deterministic parser: a standard read that planned no LLM step
    # (no HyDE, graph triples or LLM reranker) estimates no LLM call.
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0), _svc_hit("b", sim=0.70, rank=0.5)]
    out = await _run(
        search_env, query="how many books did I buy last month?", observation_date="2023-07-15"
    )
    assert out.retrieval_work["intent_parser_used"] is True
    assert "hyde=off" in out.retrieval_plan
    assert out.retrieval_cost_estimate["llm_calls"] == 0


async def test_parser_gate_abstention_stays_llm_free_on_fast(search_env) -> None:
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0)]
    out = await _run(
        search_env,
        retrieval_budget="fast",
        query="compare my project assignments across the two teams",
        top_k=1,
    )
    assert out.retrieval_work["intent_parser_used"] is False
    assert out.retrieval_work["query_enrichment_used"] is False
    assert out.retrieval_work["llm_calls_total"] == 0
    assert len(search_env.search_queries) >= 1


async def test_parser_abstention_takes_the_empty_signal(
    search_env,
) -> None:
    # A free-form query whose deterministic parser abstains gets no LLM call:
    # the read path stays LLM-free (RRF and the lexical plane still run).
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0)]
    out = await _run(
        search_env,
        query="compare my project assignments across the two teams",
        top_k=1,
    )
    assert out.retrieval_work["query_enrichment_used"] is False


@pytest.fixture
def llm_attempts(search_env, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every LLM call a search tries, each refused: the model client, the
    derived-intent extractor and the judge record and raise. The real derived
    aggregate runs (on the empty fake ClickHouse), so its synonym expansion
    would reach the client, with an empty synonym cache."""
    import server.llm.client as llm_client
    import server.memory.app.retrieval as msearch
    import server.memory.app.search_llm as sllm
    from server.memory.app.retrieval.derived import fetch_derived_aggregate

    attempts: list[str] = []

    class _RefusingLLM:
        model = "refusing"

        async def complete(self, system: str = "", user: str = "", **kw: Any) -> Any:
            attempts.append(f"complete: {user[:60]}")
            raise AssertionError("an LLM call")

    async def refused(name: str) -> Any:
        attempts.append(name)
        raise AssertionError("an LLM call")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _RefusingLLM())
    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda *a, **k: _RefusingLLM())
    monkeypatch.setattr(sllm, "extract_derived_intent_llm", lambda *a, **k: refused("intent"))
    monkeypatch.setattr(sllm, "judge_results_iteratively", lambda *a, **k: refused("judge"))

    async def no_triples(*a: Any, **k: Any) -> list[tuple[str, str, str]]:
        attempts.append("triples")  # recorded; empty, as the extractor fails open
        return []

    monkeypatch.setattr(sllm, "extract_query_triples_llm", no_triples)
    monkeypatch.setattr(sllm, "_VERB_SYNONYM_CACHE", type(sllm._VERB_SYNONYM_CACHE)())
    monkeypatch.setattr(msearch, "fetch_derived_aggregate", fetch_derived_aggregate)
    search_env.hits = [_svc_hit("a", sim=0.80, rank=0.8), _svc_hit("b", sim=0.40, rank=0.4)]
    return attempts


@pytest.mark.parametrize(
    "query",
    [
        "what is my favorite color",
        "how many model kits have I bought?",  # a count: the parser's intent
        "how much did I spend on books in total?",
        # A date difference the parser gives no aggregate intent for.
        "how many days passed between the day I got my binoculars and the day I "
        "noticed the goldfinches?",
        "what changed about my job since last year?",
    ],
)
@pytest.mark.parametrize(
    "posture",
    [
        {"retrieval_budget": "fast"},
        {"recipe": "fast"},
        {"retrieval_budget": "fast", "recipe": "quality"},  # quality turns HyDE on
        {"retrieval_budget": "fast", "recipe": "graph"},  # graph extracts triples
        {"recipe": "fast", "hyde": True},
        {"recipe": "fast", "rerank": "builtin:openai"},
    ],
    ids=["budget", "recipe", "budget-quality", "budget-graph", "recipe-hyde", "builtin-rerank"],
)
async def test_a_fast_read_makes_no_llm_call(
    search_env, llm_attempts, query: str, posture: dict[str, Any]
) -> None:
    out = await _run(search_env, query=query, observation_date="2023-07-15", **posture)
    assert llm_attempts == []
    assert out.retrieval_work["llm_calls_total"] == 0
    _assert_plans_no_llm(out)
    assert out.results  # still answered, from the vector query alone


def _assert_plans_no_llm(out: Any) -> None:
    # What the read reports it planned and costed shows no LLM step either.
    assert "hyde=off" in out.retrieval_plan and "hyde=on" not in out.score_explanation
    assert "rerank=on" not in out.retrieval_plan  # a builtin:openai rerank is dropped
    assert out.retrieval_cost_estimate["llm_calls"] == 0


_PACK_QUESTIONS = [
    "what is my favorite color",  # plain
    "how many model kits have I bought?",  # a count
    "how much did I spend on books in total?",  # a sum
    # A date difference the parser gives no aggregate intent for.
    "how many days passed between the day I got my binoculars and the day I "
    "noticed the goldfinches?",
    "what changed about my job since last year?",  # a change over time
    # Multi-hop: two clauses, compared.
    "which city did my sister move to, and how far is it from where my brother lives?",
]


@pytest.mark.parametrize("query", _PACK_QUESTIONS)
@pytest.mark.parametrize("mode", ["task", "startup", "debug", "handoff"])
@pytest.mark.parametrize(
    "reranker",
    ["", "builtin:openai", "builtin:cohere"],
    ids=["no-rerank", "llm-rerank", "service-rerank"],
)
async def test_a_context_pack_makes_no_llm_call(
    search_env,
    llm_attempts,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    mode: str,
    reranker: str,
) -> None:
    # A chat client packs context on every turn in the default task mode, whose
    # balanced recipe (and debug's quality recipe) would otherwise spend LLM calls.
    import server.memory.app.rerank as rerank_mod
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def namespace_rerank(org: str, ns: str) -> tuple[str, str]:
        return (reranker, "")  # builtin:openai is an LLM; builtin:cohere a rerank service

    monkeypatch.setattr(rerank_mod, "get_namespace_rerank_config", namespace_rerank)
    import server.knowledge.app.conversations as conversations_mod
    import server.knowledge.app.knowledge_search as knowledge_mod
    import server.memory.app.search as search_mod

    searched: list[Any] = []
    ran: list[str] = []

    def recorded(name: str, handler: Any, outputs: list[Any] | None = None) -> Any:
        async def _h(*a: Any, **k: Any) -> Any:
            ran.append(name)
            out = await handler(*a, **k)
            if outputs is not None:
                outputs.append(out)
            return out

        return _h

    for mod, name in (
        (knowledge_mod, "handle_knowledge_search"),
        (conversations_mod, "handle_conversation_search"),
    ):
        monkeypatch.setattr(mod, name, recorded(name, getattr(mod, name)))
    search = search_mod.handle_memory_search
    monkeypatch.setattr(
        search_mod, "handle_memory_search", recorded("memory_search", search, searched)
    )
    out = await handle_context_pack(
        ContextPackInput(
            org="o",
            namespace="ns",
            agent="a",
            query=query,
            mode=mode,
            include_knowledge=True,
            include_conversations=True,
        )
    )
    assert llm_attempts == []
    assert "memories" in out.sections  # answered from the vector query alone
    # The opt-in sections ran (an empty one that raised would pass the check above).
    assert {"handle_knowledge_search", "handle_conversation_search"} <= set(ran)
    assert searched  # the memories search, and the procedures section's
    for search_out in searched:
        _assert_plans_no_llm(search_out)


async def test_a_debug_pack_never_asks_the_retry_judge(search_env, llm_attempts) -> None:
    # Mid-range hits open the judge on the quality recipe (debug mode's) off the
    # LLM-free posture; the pack keeps it closed.
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    await handle_context_pack(
        ContextPackInput(org="o", namespace="ns", agent="a", query="what is my plan", mode="debug")
    )
    assert "judge" not in llm_attempts


async def test_off_the_llm_free_posture_the_judge_runs_on_the_same_hits(
    search_env, llm_attempts
) -> None:
    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    with pytest.raises(AssertionError, match="an LLM call"):  # the judge does not fail open
        await _run(search_env, recipe="quality", query="what is my plan")
    assert "judge" in llm_attempts


async def test_off_the_llm_free_posture_graph_mode_extracts_triples(
    search_env, llm_attempts
) -> None:
    await _run(search_env, recipe="graph")
    assert "triples" in llm_attempts


async def test_off_the_llm_free_posture_a_count_expands_its_verb(search_env, llm_attempts) -> None:
    await _run(
        search_env, query="how many model kits have I bought?", observation_date="2023-07-15"
    )
    assert any(a.startswith('complete: verb: "buy"') for a in llm_attempts)


async def test_off_the_llm_free_posture_the_llm_reranker_runs(search_env, llm_attempts) -> None:
    # A positive control: without the fast posture or the pack's flag, a requested
    # builtin:openai rerank reaches the model client.
    await _run(search_env, recipe="balanced", rerank="builtin:openai")
    assert any(a.startswith("complete: Query:") for a in llm_attempts)


async def test_off_the_llm_free_posture_the_quality_recipe_runs_hyde_once(
    search_env, llm_attempts
) -> None:
    await _run(search_env, recipe="quality")
    hyde = [a for a in llm_attempts if a.startswith("complete: Question:")]
    assert len(hyde) == 1


async def test_the_standard_budget_still_asks_the_llm_for_a_count_intent(
    search_env, llm_attempts
) -> None:
    # The same date-difference question off the fast posture keeps its LLM leg.
    await _run(
        search_env,
        query=(
            "how many days passed between the day I got my binoculars and the day I "
            "noticed the goldfinches?"
        ),
        observation_date="2023-07-15",
    )
    assert "intent" in llm_attempts


def _canonical_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    # Recording canonical-lookup stub; prefixes 'C:' to mark the resolved set.
    import server.memory.app.entity_resolution as er

    calls: list[dict] = []

    async def fake_lookup(
        ch: Any, *, org: str, namespaces: list[str], entity_texts: list[str]
    ) -> list[str]:
        calls.append(
            {"org": org, "namespaces": list(namespaces), "entity_texts": list(entity_texts)}
        )
        return [f"C:{e}" for e in entity_texts]

    monkeypatch.setattr(er, "lookup_canonical_entities", fake_lookup)
    return calls


async def test_canonical_lookup_serial_default_path(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _canonical_recorder(monkeypatch)
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0)]
    await _run(
        search_env,
        query="what is my favorite color",
        entity_filter=["Acme", "Bowie"],
        top_k=1,
    )
    assert len(calls) == 1
    assert calls[0]["entity_texts"] == ["Acme", "Bowie"]
    assert calls[0]["namespaces"] == ["ns"]
    assert search_env.search_queries[0].entity_filter == ["C:Acme", "C:Bowie"]


async def test_canonical_lookup_parallel_path_returns_identical_set(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _canonical_recorder(monkeypatch)
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0)]
    await _run(
        search_env,
        recipe="fast",
        query="what is my favorite color",
        entity_filter=["Acme", "Bowie"],
        top_k=1,
    )
    assert len(calls) == 1
    assert calls[0]["entity_texts"] == ["Acme", "Bowie"]
    assert calls[0]["namespaces"] == ["ns"]
    assert search_env.search_queries[0].entity_filter == ["C:Acme", "C:Bowie"]


async def test_canonical_lookup_fail_open_identical_both_paths(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.memory.app.entity_resolution as er

    async def boom(
        ch: Any, *, org: str, namespaces: list[str], entity_texts: list[str]
    ) -> list[str]:
        raise RuntimeError("canonical backend down")

    monkeypatch.setattr(er, "lookup_canonical_entities", boom)

    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0)]
    await _run(search_env, query="what is my favorite color", entity_filter=["Acme"], top_k=1)
    serial_filter = list(search_env.search_queries[-1].entity_filter)

    search_env.search_queries.clear()
    await _run(
        search_env,
        recipe="fast",
        query="what is my favorite color",
        entity_filter=["Acme"],
        top_k=1,
    )
    parallel_filter = list(search_env.search_queries[-1].entity_filter)

    assert serial_filter == ["Acme"]
    assert parallel_filter == ["Acme"]
    assert serial_filter == parallel_filter


async def test_canonical_lookup_empty_filter_skips_lookup_both_paths(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _canonical_recorder(monkeypatch)
    search_env.hits = [_svc_hit("a", sim=0.80, rank=1.0)]
    await _run(search_env, query="what is my favorite color", top_k=1)
    await _run(search_env, recipe="fast", query="what is my favorite color", top_k=1)
    assert calls == []
    assert all(q.entity_filter == [] for q in search_env.search_queries)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN ISSUE: the abstain gate, the calibrator and the HyDE/query-rewrite "
        "gates read rank 0, which RRF fusion does not guarantee holds the best "
        "cosine. Max-by-value is not the fix: record-plane hits carry a synthesized "
        "distance (1 - lexical_rank_score). A fix must tell cosine from synthesized "
        "distances and move the upstream gates in the same change."
    ),
)
async def test_abstain_gate_reads_the_best_hit_not_rank_one(search_env) -> None:
    """A weak row winning rank 1 should not hide a strong hit below it."""
    search_env.hits = [
        _svc_hit("weak_but_top_ranked", sim=0.23, rank=0.9),
        _svc_hit("strong_but_second", sim=0.63, rank=0.1),
    ]
    out = await _run(search_env, top_k=2)

    assert out.abstain is False
    assert out.abstain_code == ""


async def test_abstain_when_every_hit_is_below_the_threshold(search_env) -> None:
    """The gate still fires when nothing in the set is good enough."""
    search_env.hits = [
        _svc_hit("weak_a", sim=0.21, rank=0.9),
        _svc_hit("weak_b", sim=0.18, rank=0.1),
    ]
    out = await _run(search_env, top_k=2)

    assert out.abstain is True
    assert out.abstain_code == "below_threshold"
    # Reports the best available similarity, not rank 1's.
    assert "0.21" in out.abstain_reason


async def test_stage_timings_ride_retrieval_work(search_env) -> None:
    # Every response names its stages and samples loop lag.
    search_env.hits = [_svc_hit("k1", sim=0.9)]
    out = await _run(search_env, top_k=1)
    stages = out.retrieval_work["stage_ms"]
    for name in (
        "plan",
        "embed",
        "vector_legs",
        # The additive arms name their own time instead of
        # hiding in a neighbour (set expansion in anchor_pool, key match in
        # fusion, digest descent in assemble).
        "sub_queries",
        "set_expansion",
        "anchor_pool",
        "key_match",
        "digest_descent",
        "assemble",
        "computed",
        "evidence_support",
        "total",
    ):
        assert name in stages, name
        assert stages[name] >= 0.0
    assert stages["total"] >= max(v for k, v in stages.items() if k != "total")
    assert out.retrieval_work["loop_lag_ms"] >= 0.0


async def test_namespace_scope_unset_takes_the_deployment_default(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unset scope resolves to the setting; "this" reads the
    # addressed namespace only and the response says so.
    from server.settings import settings as _live

    monkeypatch.setattr(_live, "memory_search_default_namespace_scope", "this", raising=False)
    search_env.hits = [_svc_hit("a", sim=0.9)]
    out = await _run(search_env, namespace_scope=None, top_k=1)
    assert out.namespaces_searched == ["ns"]
    assert out.results and "foreign" not in (out.results[0].score_components or {})


async def test_all_mine_fan_out_is_named_and_foreign_hits_are_marked(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under all_mine the response lists every namespace read, the addressed
    # one first, and a hit that came from another namespace carries
    # score_components.foreign = 1.0.
    import server.auth.context as authctx
    import server.memory.app.retrieval as msearch

    async def fake_mine(org: str, subject: str | None = None) -> list[str]:
        return ["other-ns", "ns"]

    monkeypatch.setattr(authctx, "resolve_my_namespaces", fake_mine)

    async def per_namespace_search(q: Any, *, ch: Any = None) -> list[SearchHit]:
        search_env.search_queries.append(q)
        if q.namespace == "other-ns":
            return [_svc_hit("foreign", sim=0.85)]
        return [_svc_hit("local", sim=0.9)]

    monkeypatch.setattr(msearch, "vector_search", per_namespace_search)
    out = await _run(search_env, namespace_scope="all_mine", top_k=2)
    assert out.namespaces_searched[0] == "ns"
    assert "other-ns" in out.namespaces_searched
    by_key = {h.memory_key: h for h in out.results}
    assert by_key["foreign"].namespace == "other-ns"
    assert by_key["foreign"].score_components.get("foreign") == 1.0
    assert "foreign" not in (by_key["local"].score_components or {})


def test_body_budget_accepts_three_whole_traces() -> None:
    # Three ~4,000-token traces plus a base plan must fit one bodies="unit"
    # request; a 12,000 cap would clip the third.
    from pydantic import ValidationError

    assert _input(bodies="unit", body_budget_tokens=32000).body_budget_tokens == 32000
    with pytest.raises(ValidationError):
        _input(bodies="unit", body_budget_tokens=32001)


@pytest.mark.asyncio
async def test_exclude_keys_drop_from_the_pool_and_the_window_refills(search_env) -> None:
    """Keys the caller already holds leave the pool before the
    window is cut, so the window is still top_k long, filled from the
    next-best candidates; the count rides actual_work."""
    import server.memory.app.search as h

    search_env.hits = [
        _svc_hit("seen_a", sim=0.95),
        _svc_hit("seen_b", sim=0.9),
        _svc_hit("fresh_c", sim=0.85),
        _svc_hit("fresh_d", sim=0.8),
    ]
    out = await h.handle_memory_search(_input(top_k=2, exclude_keys=["seen_a", "seen_b", ""]))
    keys = [r.memory_key for r in out.results]
    assert keys == ["fresh_c", "fresh_d"]
    base = await h.handle_memory_search(_input(top_k=2))
    assert [r.memory_key for r in base.results] == ["seen_a", "seen_b"], "empty = unchanged"


def test_exclude_keys_is_bounded() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        _input(exclude_keys=[f"k{i}" for i in range(201)])
    assert _input().exclude_keys == []


async def test_the_iterative_retry_is_one_level_deep_and_credits_once(
    search_env, monkeypatch
) -> None:
    """The judge says "not answered": one child pass runs the suggested query, never
    re-enters again, and leaves recall credit and the trace/exposure side channels to the
    parent, which records them once for what it serves."""
    from server.memory.app.search import handler, output

    search_env.hits = [_svc_hit("mid", sim=0.50, rank=1.0), _svc_hit("other", sim=0.48, rank=0.2)]
    search_env.judge_result = (False, "a better question")
    touched: list[tuple[str, bool]] = []
    finished: list[tuple[str, bool]] = []

    async def _touch(ctx: Any) -> None:
        touched.append((ctx.input.query, getattr(ctx.input, "_merged_into_parent", False)))

    real_finish = output._finish_search_output

    async def _finish(ctx: Any, out: Any, hits: Any) -> None:
        finished.append((ctx.input.query, getattr(ctx.input, "_merged_into_parent", False)))
        await real_finish(ctx, out, hits)

    monkeypatch.setattr(handler, "_schedule_auto_touch", _touch)
    monkeypatch.setattr(handler, "_finish_search_output", _finish)
    await _run(search_env, recipe="quality")
    assert "a better question" in [q for q, _m in finished]
    assert len(search_env.judge_calls) == 1  # the child pass does not judge again
    assert [q for q, merged in touched] == [touched[0][0]] and touched[0][1] is False
    assert sorted(merged for _q, merged in finished) == [False, True]


async def test_a_re_entered_pass_never_re_reads(monkeypatch) -> None:
    import server.memory.app.search as pkg
    from server.memory.app.search.budget import SearchBudget
    from server.memory.app.search.consistency import _consistency_reread

    async def _search(inp: Any) -> Any:
        raise AssertionError("a re-entered pass must not re-read")

    monkeypatch.setattr(pkg, "handle_memory_search", _search)
    monkeypatch.setattr("server.memory.store.read_your_writes_enabled", lambda: True)
    inp = _input().model_copy(update={"_pass_depth": 1})
    ctx = SimpleNamespace(
        input=inp, search_budget=SearchBudget(max_wall_ms=60_000), namespaces=["n"]
    )
    assert await _consistency_reread(ctx, []) is None  # type: ignore[arg-type]


async def test_a_memory_whose_code_changed_is_served_lower_after_the_verdict(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import code_link_reads

    def hits() -> list[SearchHit]:
        sims = {"stale": 0.9, "b": 0.6, "c": 0.5, "d": 0.4, "e": 0.35}
        return [_svc_hit(k, sim=s) for k, s in sims.items()]

    marked: set[str] = set()

    async def annotate(input: Any, pool: list[Any]) -> None:
        for h in pool:
            h._code_ranked_down = h.memory_key in marked

    monkeypatch.setattr(code_link_reads, "annotate_hits", annotate)
    search_env.hits = hits()
    plain = await _run(search_env, top_k=5)
    assert [h.memory_key for h in plain.results][0] == "stale"
    marked.add("stale")
    search_env.hits = hits()
    out = await _run(search_env, top_k=5)
    assert [h.memory_key for h in out.results] == ["b", "c", "d", "stale", "e"]
    assert out.results[3].score_components["code_changed_places"] == -3.0
    # the verdict read the results in relevance order
    assert (out.abstain, out.retrieval_confidence) == (plain.abstain, plain.retrieval_confidence)
    # 0 places moves nothing
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_code_changed_positions", 0)
    search_env.hits = hits()
    off = await _run(search_env, top_k=5)
    assert [h.memory_key for h in off.results][0] == "stale"


async def test_a_moved_memory_is_never_cut_by_a_tier_and_a_date_order_stays(
    search_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import code_link_reads
    from server.memory.app.search import handler, output

    async def annotate(input: Any, pool: list[Any]) -> None:
        for h in pool:
            h._code_ranked_down = h.memory_key == "stale"

    async def recent(hits: list[Any], input: Any, counted: Any) -> tuple[list[Any], int]:
        # two tier rows ahead of the similarity hits, cut to top_k as the tier window does
        tier = []
        for key in ("r1", "r2"):
            row = hits[0].model_copy()
            row.memory_key, row.score_components, row.rank_score = key, {"recent_first": 1.0}, 0.0
            tier.append(row)
        return (tier + hits)[: input.top_k], 0

    def hits() -> list[SearchHit]:
        sims = {"stale": 0.9, "b": 0.6, "c": 0.5, "d": 0.4, "e": 0.35}
        return [_svc_hit(k, sim=s) for k, s in sims.items()]

    monkeypatch.setattr(code_link_reads, "annotate_hits", annotate)
    monkeypatch.setattr(output, "_prepend_recent", recent)
    search_env.hits = hits()
    out = await _run(search_env, top_k=5)
    assert [h.memory_key for h in out.results] == ["r1", "r2", "b", "c", "stale"]

    enrich = handler._resolve_query_enrichment

    async def dated(input: Any, work: Any) -> Any:
        obs, parsed, (dates, _intent, *rest) = await enrich(input, work)
        return obs, parsed, (dates, "asc", *rest)

    monkeypatch.setattr(handler, "_resolve_query_enrichment", dated)
    search_env.hits = hits()
    out = await _run(search_env, top_k=5)
    assert "code_changed_places" not in out.results[2].score_components
    assert [h.memory_key for h in out.results][:3] == ["r1", "r2", "stale"]
