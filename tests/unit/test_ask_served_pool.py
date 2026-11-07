"""memory.ask's own legs read ClickHouse after the search's page was checked: under
Postgres the finished pool keeps only the rows the authority still serves."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.core import pg
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.ask import completion, identifiers, pool
from server.memory.store import pg_memory


def _row(key: str, version: int) -> dict[str, Any]:
    return {
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "content_excerpt": f"text of {key}",
        "created_at": "2026-09-01",
        "valid_at": "2026-09-01",
        "version": version,
    }


def _chunk() -> Any:
    return _MemorySearchHit(
        memory_key="chunk:s:0",
        memory_type="episodic",
        content_excerpt="a transcript line",
        distance=0.05,
        similarity=0.95,
        rank_score=0.95,
        source="chunk",
    )


def test_a_completion_hit_carries_the_row_it_was_read_from() -> None:
    (hit,) = completion._completion_hits([_row("k1", 5)], [], "facet completion")
    assert hit._row_identity == ("a", "-", "-") and hit.version == 5


@pytest.mark.asyncio
async def test_an_injected_fact_carries_its_row_and_a_chunk_none(monkeypatch) -> None:
    import server.memory.store as memory_repo
    from server.settings import settings

    async def _facts(**_k: Any) -> list[dict[str, Any]]:
        return [_row("user_117831", 7)]

    async def _chunks(**_k: Any) -> list[dict[str, Any]]:
        return [{"memory_key": "chunk:s:0", "content_excerpt": "c", "created_at": "2026-09-02"}]

    class _Emb:
        async def embed(self, q: str) -> list[float]:
            return [0.1, 0.2]

    monkeypatch.setattr(memory_repo, "find_by_identifier", _facts)
    monkeypatch.setattr(memory_repo, "find_chunks_by_identifier", _chunks)
    monkeypatch.setattr(settings, "memory_ask_identifier_inject_chunks", 1)
    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Emb())
    si = SimpleNamespace(
        query="what is 117831", org="o", namespace="n", scope="private", agent="a", user_id=None
    )
    hits = await identifiers._inject_identifier_memories(si, ["117831"], [])
    by_key = {h.memory_key: h for h in hits}
    assert by_key["user_117831"]._row_identity == ("a", "-", "-")
    assert by_key["user_117831"].version == 7
    assert by_key["chunk:s:0"]._row_identity is None


@pytest.mark.asyncio
async def test_the_pool_keeps_only_what_the_authority_serves(monkeypatch) -> None:
    served, deleted = completion._completion_hits(
        [_row("k1", 5), _row("k2", 5)], [], "bridge completion"
    )
    asked: list[Any] = []

    async def _live(org: str, ids: list[Any]) -> set[Any]:
        asked.extend(ids)
        return {i for i in ids if i[4] == "k1"}

    async def _chunk_heads(org: str, keys: list[Any]) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "live_versions", _live)
    monkeypatch.setattr(pg_memory, "chunk_heads", _chunk_heads)
    chunk = _chunk()
    si = SimpleNamespace(org="o", namespace="n", as_of=None)
    assert await pool._served_pool([served, deleted, chunk], si) == [served, chunk]
    assert sorted(asked) == [("n", "a", "-", "-", k, "semantic", 5) for k in ("k1", "k2")]
    # An as_of replay reads the past: nothing is checked against the present.
    replay = SimpleNamespace(org="o", namespace="n", as_of="2026-01-01")
    assert await pool._served_pool([served, deleted], replay) == [served, deleted]


@pytest.mark.asyncio
async def test_on_clickhouse_the_pool_is_left_as_it_is(monkeypatch) -> None:
    async def _never(*_a: Any) -> Any:
        raise AssertionError("Postgres is not read")

    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(pg_memory, "live_versions", _never)
    hits = completion._completion_hits([_row("k1", 5)], [], "facet completion")
    si = SimpleNamespace(org="o", namespace="n", as_of=None)
    assert await pool._served_pool(hits, si) == hits


@pytest.mark.asyncio
async def test_the_pool_is_checked_after_the_last_leg_that_adds_a_row(monkeypatch) -> None:
    from server.memory.app.ask import pipeline

    served, deleted = completion._completion_hits(
        [_row("k1", 5), _row("k2", 5)], [], "facet completion"
    )

    async def _retrieve(run: Any) -> None:
        run.ranked = [served]

    async def _widen(run: Any, **_k: Any) -> tuple[bool, bool, bool]:
        return False, False, False

    async def _nothing(run: Any, *_a: Any) -> None:
        return None

    async def _facets(run: Any) -> None:
        run.ranked.append(deleted)

    async def _belt(ranked: list[Any], search_input: Any) -> list[Any]:
        return ranked

    async def _live(org: str, ids: list[Any]) -> set[Any]:
        return {i for i in ids if i[4] == "k1"}

    async def _chunk_heads(org: str, keys: list[Any]) -> dict[Any, Any]:
        return {}

    for name, fake in {
        "_retrieve": _retrieve,
        "_widen_by_question": _widen,
        "_order_pool": _nothing,
        "_inject_named_evidence": _nothing,
        "_complete_options": _nothing,
        "_complete_bridges": _nothing,
        "_complete_facets": _facets,
        "_tag_belt": _belt,
    }.items():
        monkeypatch.setattr(pipeline, name, fake)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "live_versions", _live)
    monkeypatch.setattr(pg_memory, "chunk_heads", _chunk_heads)
    run = SimpleNamespace(
        ranked=[],
        target_k=5,
        window=5,
        search_input=SimpleNamespace(org="o", namespace="n", as_of=None),
    )
    await pipeline._fill_pool(run, llm=None, llm_cheap=None, decompose=None)
    assert run.results == [served] and run.synth_pool == [served]
