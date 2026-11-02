"""Fakes for one real `memory.search` run with no ClickHouse, embedder or
LLM: every collaborator the handler reaches is stubbed, and the vector leg
answers from `state.hits_by_ns`. Tests that drive the real handler install
them with `install_search_fakes(monkeypatch)`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

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


def install_search_fakes(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub the handler's collaborators; returns the state the fakes read
    and record: `hits_by_ns`, `search_queries`, the graph-neighbor maps."""
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
