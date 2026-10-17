"""A historical search reads the same who and where as a current one: the agent and
namespace_scope defaults reach it before it branches off."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app import temporal_search
from server.memory.app.search import handler
from server.settings import settings


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_temporal_enabled", True)


@pytest.mark.asyncio
async def test_a_historical_search_gets_the_resolved_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[MemorySearchInput] = []

    async def _search(input: MemorySearchInput) -> Any:
        seen.append(input)
        return "answered"

    monkeypatch.setattr(temporal_search, "search", _search)
    asked = MemorySearchInput(
        org="o", namespace="n", agent="", query="q", known_as_of=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert await handler.handle_memory_search(asked) == "answered"
    (got,) = seen
    assert got.agent == "*" and got.namespace_scope is not None


@pytest.mark.asyncio
async def test_a_scoped_tokens_historical_search_stops_at_the_first_confident_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.auth.fanout import Fanout, Split
    from server.memory.domain.temporal_reads import Axes

    read: list[str] = []

    async def _split(org: str, ns: str, scope: Any) -> Split:
        return Split(["n3", "n1", "n5"], [])

    async def _fanout(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("n3", "n1", "n5"), cap=2, ordered=True)

    async def _hits(input: Any, ns: str, *_a: Any) -> Any:
        read.append(ns)
        sim = 0.9 if ns == "n1" else 0.1
        return [SimpleNamespace(similarity=sim, memory_key=ns)], SimpleNamespace()

    class _Embedder:
        model_name = "fake"

        async def embed(self, q: str) -> list[float]:
            return [0.0]

    async def _cal(*_a: Any) -> None:
        return None

    monkeypatch.setattr(temporal_search, "namespaces_to_search", _split)
    monkeypatch.setattr(temporal_search, "resolve_fanout", _fanout)
    monkeypatch.setattr(temporal_search, "_namespace_hits", _hits)
    monkeypatch.setattr(temporal_search, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(temporal_search, "resolve_calibration", _cal)
    monkeypatch.setattr(temporal_search.reads, "axes_of", lambda _i: Axes(valid=None, known=None))
    monkeypatch.setattr(temporal_search, "_record_exposure", lambda *_a: None)
    asked = MemorySearchInput(
        org="o", namespace="n3", agent="*", query="q", namespace_scope="all_mine",
        known_as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )  # fmt: skip
    try:
        await temporal_search.search(asked)
    except Exception:  # noqa: BLE001 — only which namespaces were read is asserted
        pass
    assert read == ["n3", "n1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("historical", "read_token"), [(True, ""), (False, ""), (False, "t")])
async def test_a_historical_asks_pool_takes_no_pending_turns(
    monkeypatch: pytest.MonkeyPatch, historical: bool, read_token: str
) -> None:
    # A read token is read-your-writes today: its ask still takes pending turns.
    from types import SimpleNamespace

    from server.memory.app.ask import pool

    async def _pending(search_input: Any, question: str, ranked: list) -> tuple[list, int]:
        return [*ranked, "a turn queued after the cutoff"], 1

    monkeypatch.setattr(pool, "_pending_evidence_stage", _pending)
    asked = MemorySearchInput(
        org="o", namespace="n", agent="*", query="q",
        known_as_of=datetime(2026, 1, 1, tzinfo=UTC) if historical else None,
        read_token=read_token,
    )  # fmt: skip
    run = SimpleNamespace(
        search_input=asked, question="q", settings=None, ranked=[], recency_weight=0,
        window=8, target_k=8, retrieval=None, pending_evidence_n=0,
    )  # fmt: skip
    await pool._order_pool(run)
    assert (run.ranked, run.pending_evidence_n) == (
        ([], 0) if historical else (["a turn queued after the cutoff"], 1)
    )
