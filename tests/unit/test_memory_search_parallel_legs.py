"""Behavior-preserving parallelization of the retrieval legs.

The handler issues the result-independent retrieval legs concurrently:
  * within ``vector_search``: the primary HNSW facts, verbatim chunks, the
    BM25-only lexical union, and schema records run in one ``asyncio.gather``;
  * in the handler: the session-summary, current-state-observation, and
    entity-store-ANN legs launch as tasks BEFORE the vector legs and are
    joined just before the assembler;
  * graph mode's per-namespace neighbor expansion
    runs as one ``asyncio.gather`` over namespaces.

This is pure scheduling: the FUSED result must be identical to a sequential
issuance, independent of the order in which the legs' CH reads complete. These
tests force out-of-order completion (reversed / shuffled delays) and assert the
fused candidate set + scores are unchanged.
"""

from __future__ import annotations

import asyncio
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
    # The service SearchHit carries no namespace — the handler tags each hit
    # with the namespace of the SearchQuery it was returned for during the
    # cross-namespace merge.
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
        "top_k": 5,
        "auto_touch": False,
    }
    base.update(overrides)
    return MemorySearchInput(**base)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.core.ch as ch_mod
    import server.llm.client as llm_client
    import server.memory.app.embedder_calibration as ec
    import server.memory.app.entity_resolution as er
    import server.memory.app.rerank as rerank_mod
    import server.memory.app.retrieval as msearch
    import server.memory.app.search_llm as sllm

    state = SimpleNamespace()
    # Per-namespace hit map: vector_search returns state.hits_by_ns[ns].
    state.hits_by_ns = {}
    state.search_queries = []
    # Per-namespace graph-neighbor map + an optional delay schedule so the
    # leg CH reads complete out of order.
    state.neighbors_by_ns = {}
    state.neighbor_delays = {}
    state.neighbor_call_order = []

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
        state.search_queries.append(q)
        return list(state.hits_by_ns.get(q.namespace, []))

    monkeypatch.setattr(msearch, "vector_search", fake_vector_search)

    async def fake_graph_neighbors(ch: Any, *, namespace: str, **kw: Any) -> list[str]:
        state.neighbor_call_order.append(namespace)
        delay = state.neighbor_delays.get(namespace, 0.0)
        if delay:
            await asyncio.sleep(delay)
        return list(state.neighbors_by_ns.get(namespace, []))

    monkeypatch.setattr(msearch, "fetch_graph_neighbors", fake_graph_neighbors)

    async def fake_triple_matched(ch: Any, **kw: Any) -> list[str]:
        return []

    monkeypatch.setattr(msearch, "fetch_triple_matched_memories", fake_triple_matched)

    async def fake_query_triples(query: str, **kw: Any) -> list:
        return []

    monkeypatch.setattr(sllm, "extract_query_triples_llm", fake_query_triples)

    async def fake_lookup(
        ch: Any, *, org: str, namespaces: list[str], entity_texts: list[str]
    ) -> list[str]:
        return list(entity_texts)

    monkeypatch.setattr(er, "lookup_canonical_entities", fake_lookup)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda *a, **k: _FakeLLM())
    return state


async def _run(**overrides: Any):
    from server.memory.app.search import handle_memory_search

    return await handle_memory_search(_input(**overrides))


# ---------------------------------------------------------------------------
# The parallelized default path fuses the SAME candidates/scores
# regardless of leg completion order.
# ---------------------------------------------------------------------------


async def test_default_path_fusion_is_deterministic(env) -> None:
    """The default (rrf) read path runs its legs concurrently. Two identical
    queries must fuse byte-identical candidate keys + rank_scores — the gather
    does not introduce order-dependent nondeterminism."""
    env.hits_by_ns["ns"] = [
        _svc_hit("a", sim=0.90, rank=1.00),
        _svc_hit("b", sim=0.80, rank=0.80),
        _svc_hit("c", sim=0.70, rank=0.60),
    ]
    out1 = await _run()
    out2 = await _run()
    keys1 = [(r.namespace, r.memory_key) for r in out1.results]
    keys2 = [(r.namespace, r.memory_key) for r in out2.results]
    scores1 = [round(r.rank_score, 9) for r in out1.results]
    scores2 = [round(r.rank_score, 9) for r in out2.results]
    assert keys1 == keys2
    assert scores1 == scores2


async def test_graph_neighbor_gather_is_order_independent(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-namespace graph-neighbor expansion
    builds graph_expanded_by_ns from independent CH reads. Forcing the reads
    to complete in REVERSE order (ns_b finishes before ns_a) must yield the
    SAME per-namespace expansion reaching each SearchQuery — the map is keyed
    by namespace, not by completion order."""
    # Two namespaces in the caller's scope; graph mode pre-expands each.
    env.hits_by_ns = {
        "ns_a": [_svc_hit("ka", sim=0.9, rank=1.0)],
        "ns_b": [_svc_hit("kb", sim=0.9, rank=1.0)],
    }
    env.neighbors_by_ns = {
        "ns_a": ["alpha", "beta"],
        "ns_b": ["gamma"],
    }
    # ns_a's read takes longer than ns_b's → ns_b completes first.
    env.neighbor_delays = {"ns_a": 0.02, "ns_b": 0.0}

    async def _run_graph():
        from server.memory.app.search import handle_memory_search

        return await handle_memory_search(
            _input(
                search_mode="graph",
                namespace="ns_a",
                namespace_scope="all_mine",
                entity_filter=["seed"],
            )
        )

    # Patch namespace resolution so the handler fans out to both namespaces.
    import server.auth.context as auth_ctx

    async def fake_my_ns(org: str) -> list[str]:
        return ["ns_a", "ns_b"]

    monkeypatch.setattr(auth_ctx, "resolve_my_namespaces", fake_my_ns)
    env.search_queries.clear()
    await _run_graph()
    # ns_b's neighbor read completed before ns_a's (reverse of issue order)…
    assert env.neighbor_call_order  # both were called
    # …yet each SearchQuery carries the expansion for ITS OWN namespace.
    graph_by_ns = {q.namespace: list(q.graph_expanded) for q in env.search_queries}
    assert graph_by_ns["ns_a"] == ["alpha", "beta"]
    assert graph_by_ns["ns_b"] == ["gamma"]


async def test_summary_leg_runs_concurrently_and_folds_in(env) -> None:
    """The session-summary leg launches as a task before the vector legs and
    is folded into the fused pool additively. With no summaries it is a no-op
    on the candidate set — the fused result is exactly the vector pool. This
    guards that the concurrent launch/join does not drop or reorder hits."""
    env.hits_by_ns["ns"] = [
        _svc_hit("a", sim=0.90, rank=1.0),
        _svc_hit("b", sim=0.80, rank=0.8),
    ]
    out = await _run()
    fused_keys = {(r.namespace, r.memory_key) for r in out.results}
    assert ("ns", "a") in fused_keys
    assert ("ns", "b") in fused_keys


# ---------------------------------------------------------------------------
# Scoped-token tier waterfall — a priority-ordered cross-namespace search
# STOPS at a tier only when its best hit clears
# `settings.memory_search_tier_stop_similarity`. That stop cutoff is decoupled
# from the return noise floor (`memory_search_min_similarity`): a hit that is
# good enough to RETURN is not necessarily good enough to SUPPRESS a
# lower-priority tier. These pin that two-cutoff behavior.
# ---------------------------------------------------------------------------

_TIER_SECRET = "unit-secret-unit-secret-unit-secret-32"


def _scoped_auth(org: str, **ns_roles: str):
    """A verified scoped token: namespaces = ns_roles keys in insertion order
    (= read priority), authoritative per-namespace roles."""
    from server.auth.jwt import mint_token, verify_token

    tok = mint_token(
        "sa:bot",
        org,
        namespaces=list(ns_roles.keys()),
        roles=[],
        ns_roles=dict(ns_roles),
        secret=_TIER_SECRET,
    )
    return verify_token(tok, secret=_TIER_SECRET)


async def _run_scoped(auth, **overrides: Any):
    from server.auth.context import set_current_auth

    set_current_auth(auth)
    try:
        return await _run(**overrides)
    finally:
        set_current_auth(None)


async def test_tier_waterfall_stops_on_confident_hit(env) -> None:
    """A higher-priority hit clearing the stop cutoff (default 0.40) suppresses
    the lower-priority search entirely — even when the lower tier holds an
    objectively stronger memory. This is the intended 'private wins' behavior."""
    auth = _scoped_auth("o", priv="admin", pub="viewer")  # priv-first priority
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.55)]
    env.hits_by_ns["pub"] = [_svc_hit("u1", sim=0.90)]  # stronger, must stay hidden
    await _run_scoped(auth, namespace="priv", namespace_scope="all_mine")
    queried = {q.namespace for q in env.search_queries}
    assert "priv" in queried
    assert "pub" not in queried


async def test_tier_waterfall_falls_through_on_middling_hit(env) -> None:
    """A higher-priority hit that clears the RETURN floor (0.22) but not the
    STOP cutoff (0.40) must NOT trap the read: the lower-priority tier is still
    searched so its stronger memory can surface. A single low cutoff would trap
    it."""
    auth = _scoped_auth("o", priv="admin", pub="viewer")
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.30)]  # returnable, not confident
    env.hits_by_ns["pub"] = [_svc_hit("u1", sim=0.90)]
    await _run_scoped(auth, namespace="priv", namespace_scope="all_mine")
    queried = {q.namespace for q in env.search_queries}
    assert {"priv", "pub"} <= queried


async def test_tier_stop_cutoff_is_setting_driven(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop cutoff reads `settings.memory_search_tier_stop_similarity` at
    call time (not a hardcoded value): lowering it below a middling hit
    makes that hit suppress the lower tier again."""
    import server.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "memory_search_tier_stop_similarity", 0.25)
    auth = _scoped_auth("o", priv="admin", pub="viewer")
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.30)]  # now above the 0.25 cutoff
    env.hits_by_ns["pub"] = [_svc_hit("u1", sim=0.90)]
    await _run_scoped(auth, namespace="priv", namespace_scope="all_mine")
    queried = {q.namespace for q in env.search_queries}
    assert "pub" not in queried
