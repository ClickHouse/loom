"""The shared query-embedding tier: a process miss reads
loom.embedding_cache, a provider result is written there fire-and-forget,
every failure falls open to the provider, and the served tier is recorded."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

import server.embedding._cache as ec


class _CH:
    def __init__(
        self, rows: dict[tuple[str, str], list[float]] | None = None, *, fail: bool = False
    ):
        self.rows = rows or {}
        self.fail = fail
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.tags: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if self.fail:
            raise RuntimeError("ch down")
        vec = self.rows.get((params["model"], params["key"]))
        # the table's dim column is len(embedding); the read filters on it
        return {"embedding": vec} if vec is not None and len(vec) == params["dim"] else None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if self.fail:
            raise RuntimeError("ch down")
        from server.core.ch import current_query_tag

        self.inserts.append((table, rows))
        self.tags.append(current_query_tag())


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CH]:
    import server.core.ch as ch_mod
    from server.settings import settings

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(settings, "embedding_cache_shared", True)
    monkeypatch.setattr(settings, "embedding_cache_shared_max_chars", 2000)
    ec._cache._store.clear()
    # A request of one org: the cache is scoped by it.
    from server.observability import genai

    genai.set_identity(org="acme", namespace="ns", operation="memory.search")
    yield ch
    genai._identity.set(None)


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)
    if ec._SHARED_TASKS:
        await asyncio.gather(*list(ec._SHARED_TASKS), return_exceptions=True)


@pytest.mark.asyncio
async def test_process_miss_reads_the_shared_tier_and_fills_the_process_cache(env: _CH) -> None:
    calls = 0

    async def fetch(text: str) -> list[float]:
        nonlocal calls
        calls += 1
        return [9.0]

    key = ec._key("m", "what did she say", "acme")
    env.rows[("m", key)] = [1.0, 2.0]
    vec, hit = await ec.cached_embed(
        "m", "what did  she say", fetch, dim=2
    )  # whitespace-normalized key
    assert vec == [1.0, 2.0] and hit is True and calls == 0 and ec.last_tier() == "shared"
    vec2, hit2 = await ec.cached_embed("m", "what did she say", fetch, dim=2)
    assert hit2 is True and ec.last_tier() == "process" and calls == 0


@pytest.mark.asyncio
async def test_shared_miss_calls_the_provider_and_writes_the_row_without_text(env: _CH) -> None:
    async def fetch(text: str) -> list[float]:
        return [0.5, 0.25]

    vec, hit = await ec.cached_embed("m", "a brand new question", fetch, dim=2)
    assert vec == [0.5, 0.25] and hit is False and ec.last_tier() == "miss"
    await _settle()
    assert len(env.inserts) == 1
    table, rows = env.inserts[0]
    assert table == "loom.embedding_cache"
    row = rows[0]
    assert row["model"] == "m" and row["dim"] == 2 and row["embedding"] == [0.5, 0.25]
    assert row["key"] == ec._key("m", "a brand new question", "acme")
    assert "text" not in row and "query" not in row
    assert env.tags == [ec.PUT_QUERY_TAG]  # the detached put names itself in the query log


@pytest.mark.asyncio
async def test_shared_tier_failures_fall_open_to_the_provider(env: _CH) -> None:
    env.fail = True

    async def fetch(text: str) -> list[float]:
        return [3.0]

    before = ec.shared_errors
    vec, hit = await ec.cached_embed("m", "another question", fetch, dim=1)
    assert vec == [3.0] and hit is False
    await _settle()
    assert ec.shared_errors >= before + 1  # the read failed; the write failed too


@pytest.mark.asyncio
async def test_long_texts_and_the_switch_skip_the_shared_tier(env: _CH, monkeypatch) -> None:
    from server.settings import settings

    async def fetch(text: str) -> list[float]:
        return [1.0]

    env.rows[("m", ec._key("m", "x" * 3000, "acme"))] = [7.0]
    vec, hit = await ec.cached_embed("m", "x" * 3000, fetch, dim=1)
    assert vec == [1.0] and hit is False  # over the cap: never consulted
    ec._cache._store.clear()
    monkeypatch.setattr(settings, "embedding_cache_shared", False)
    env.rows[("m", ec._key("m", "short", "acme"))] = [7.0]
    vec, hit = await ec.cached_embed("m", "short", fetch, dim=1)
    assert vec == [1.0] and hit is False
    await _settle()
    assert env.inserts == []


@pytest.mark.asyncio
async def test_a_shared_row_of_another_width_is_a_miss(env: _CH) -> None:
    """Same model, new LOOM_EMBEDDING_DIM: the old width's row must not serve."""
    key = ec._key("m", "q", "acme")
    env.rows[("m", key)] = [1.0, 2.0, 3.0]

    async def fetch(text: str) -> list[float]:
        return [0.6, 0.8]

    vec, hit = await ec.cached_embed("m", "q", fetch, dim=2)
    assert vec == [0.6, 0.8] and hit is False and ec.last_tier() == "miss"


def test_a_zero_size_process_cache_holds_nothing() -> None:
    """LOOM_EMBEDDING_CACHE_SIZE=0 is valid (ge=0) and caches nothing."""
    lru = ec._LRU(max_size=0, ttl_s=60.0)
    lru.put("k", [1.0])
    lru.put("k", [2.0])
    assert len(lru._store) == 0 and lru.get("k") is None
