"""memory.search reads `run_id: "-"` (the identity sentinel the SDKs stamp when a
call names no run) as no run filter, as memory.ask and context.pack do."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput, MemorySearchTraceInput
from server.memory.app.search.request import _resolve_search_input


@pytest.fixture(autouse=True)
def _no_namespace_defaults(monkeypatch):
    from server.memory.app import namespace_retrieval_defaults

    async def none(_org: str, _ns: str) -> None:
        return None

    monkeypatch.setattr(namespace_retrieval_defaults, "resolve", none)


@pytest.mark.parametrize(("sent", "served"), [("-", None), ("run-7", "run-7"), (None, None)])
async def test_memory_search_reads_the_run_sentinel_as_no_run_filter(sent, served):
    inp = MemorySearchInput(org="o", namespace="ns", agent="*", query="q", run_id=sent)
    resolved, *_ = await _resolve_search_input(inp)
    assert resolved.run_id == served


async def test_memory_search_trace_reads_the_run_sentinel_as_no_run_filter(monkeypatch):
    import server.memory.app.retrieval as retrieval
    from server.memory.app.search import trace

    class _Embedder:
        model_name = "m"

    seen: list[Any] = []

    async def capture(q: Any) -> list[Any]:
        seen.append(q)
        return []

    async def embed_query(_embed: Any, _text: str, _inp: Any, _model: str) -> Any:
        return [1.0], {}

    monkeypatch.setattr(trace, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(trace, "embed_query", embed_query)
    monkeypatch.setattr(retrieval, "vector_search_trace", capture)
    await trace.handle_memory_search_trace(
        MemorySearchTraceInput(org="o", namespace="ns", agent="*", query="q", run_id="-")
    )
    assert [q.run_id for q in seen] == [None]
