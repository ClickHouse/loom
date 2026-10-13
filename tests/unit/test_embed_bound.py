"""A slow embedder does not hold the search — the query
embedding is bounded per attempt, retried once, then answered as a 503 the
caller can retry."""

from __future__ import annotations

import asyncio

import pytest

from server.core.errors import EmbedTimeoutError
from server.memory.app.search.embed import _bounded_query_embed


class _Embedder:
    def __init__(self, delays: list[float]) -> None:
        self.delays = list(delays)
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        d = self.delays.pop(0) if self.delays else 0.0
        await asyncio.sleep(d)
        return [0.1, 0.2]


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_embed_bound_s", 0.05)
    monkeypatch.setattr(settings, "memory_search_embed_bound_retries", 1)
    yield


def test_a_fast_embed_passes_through(bound) -> None:
    e = _Embedder([0.0])
    assert asyncio.run(_bounded_query_embed(e, "q")) == [0.1, 0.2] and e.calls == 1


def test_one_slow_attempt_is_cancelled_and_retried(bound) -> None:
    e = _Embedder([1.0, 0.0])
    work: dict = {}
    assert asyncio.run(_bounded_query_embed(e, "q", work)) == [0.1, 0.2]
    assert e.calls == 2 and work["embed_timeouts"] == 1


def test_two_slow_attempts_answer_503_embed_timeout(bound) -> None:
    e = _Embedder([1.0, 1.0])
    with pytest.raises(EmbedTimeoutError) as ei:
        asyncio.run(_bounded_query_embed(e, "q"))
    assert ei.value.status_code == 503 and ei.value.code == "EMBED_TIMEOUT"
    assert "the search wrote nothing" in ei.value.message
    assert e.calls == 2


def test_a_zero_bound_keeps_the_unbounded_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_embed_bound_s", 0)
    e = _Embedder([0.02])
    assert asyncio.run(_bounded_query_embed(e, "q")) == [0.1, 0.2]


def test_both_query_embeds_use_the_bound() -> None:
    from tests.unit._memory_search_pkg import handler_source

    src = handler_source()
    assert src.count("_bounded_query_embed(embedder,") >= 2
    assert "query_vec = await embedder.embed(" not in src
