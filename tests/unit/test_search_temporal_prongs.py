"""Handler-path tests for the deterministic date-arithmetic answer (shipped).

The two-event dateDiff pin (source="computed") is shipped current behavior:
a "how many days between X and Y" with RESOLVABLE operands yields the
server-side dateDiff interval as a pinned computed-evidence row; unresolvable
operands ⇒ no computed row (abstain). A single-predicate COUNT / SUM /
date_span aggregate rides results[] as a source="derived" evidence row.

Every external surface (CH / embedder / LLM helpers) is faked so these assert
routing + assembly, not retrieval quality or a live cluster.
"""

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
        "query": "what order did I visit the museums",
        "namespace_scope": "this",
        "top_k": 5,
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
    env.fetch_aggregate_result = None
    env.fetch_temporal_diff_result = None

    fake_ch = _FakeCH()
    patch_handler_name(monkeypatch, "get_embedder", lambda: _FakeEmbedder())
    patch_handler_name(monkeypatch, "get_client", lambda: fake_ch)
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake_ch)
    monkeypatch.setattr(ec, "_calibration", None)

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
        return env.fetch_aggregate_result

    monkeypatch.setattr(msearch, "fetch_derived_aggregate", fake_fetch_aggregate)

    async def fake_fetch_temporal_diff(ch: Any, **kw: Any) -> dict | None:
        return env.fetch_temporal_diff_result

    monkeypatch.setattr(msearch, "fetch_temporal_diff", fake_fetch_temporal_diff)

    async def fake_derived_intent(query: str, **kw: Any) -> None:
        return None

    monkeypatch.setattr(sllm, "extract_derived_intent_llm", fake_derived_intent)

    async def fake_judge(query: str, excerpts: list[str], **kw: Any):
        return (True, "")

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


# ---------------------------------------------------------------------------
# Deterministic date-arithmetic answer
# ---------------------------------------------------------------------------


async def test_prong_b_days_between_yields_pinned_computed_row(search_env) -> None:
    """A "how many days between X and Y" with RESOLVABLE operands yields the
    server-side dateDiff interval as a pinned computed-evidence row with the
    correct number (this is shipped current behavior; the flag does not change
    the two-event dateDiff path)."""
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
    first = out.results[0]
    assert first.source == "computed"
    assert first.memory_key == "temporal_diff"
    assert "[computed] interval = 14 days" in first.content_excerpt
    assert first.rank_score > out.results[1].rank_score
    assert out.abstain is False


async def test_prong_b_unresolvable_operands_abstain(search_env) -> None:
    """Unresolvable operands ⇒ the leg fails open to None ⇒ NO computed row
    (never a guessed value); search falls through to normal retrieval."""
    search_env.hits = [_svc_hit("ev_a", sim=0.80, rank=0.8), _svc_hit("ev_b", sim=0.70, rank=0.7)]
    search_env.fetch_temporal_diff_result = None  # operands didn't resolve
    out = await _run(
        search_env,
        query=("how many days passed between the day I did thing one and the day I did thing two?"),
        observation_date="2023-05-20",
    )
    assert all(h.source == "computed" or h.memory_key != "temporal_diff" for h in out.results)
    assert all(h.memory_key != "temporal_diff" for h in out.results)
    assert {h.memory_key for h in out.results} == {"ev_a", "ev_b"}


async def test_derived_aggregate_is_source_derived(
    search_env,
) -> None:
    """A COUNT aggregate is pinned with source="derived" (shipped behavior)."""
    search_env.hits = [_svc_hit("kit_a", sim=0.80, rank=0.8), _svc_hit("kit_b", sim=0.70, rank=0.7)]
    search_env.fetch_aggregate_result = {
        "count": 3,
        "sum": None,
        "avg": None,
        "first_at": datetime(2023, 5, 24),
        "last_at": datetime(2023, 6, 10),
        "span_days": 17.0,
        "items": [],
    }
    out = await _run(
        search_env,
        query="how many model kits have I bought?",
        observation_date="2023-07-15",
    )
    first = out.results[0]
    assert first.source == "derived"
    assert first.memory_key == "derived_aggregate"
    assert "count = 3" in first.content_excerpt
    assert first.content_excerpt.startswith("Computed from 3 dated records")
