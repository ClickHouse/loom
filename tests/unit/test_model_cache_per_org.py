"""The model caches are per org: one org is never served another's cached
answer or vector, nor shares an in-flight call with it; one org keeps its own
hits; and a call outside a request (no org to scope by) shares nothing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.embedding._cache as ec
from server.embedding import _provider
from server.llm import cache as lc
from server.llm.cache import CachedLLMClient
from server.llm.client import LLMResult
from server.observability import genai


class _CH:
    """The two cache tables: rows by (model, key)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        row = self.rows.get((params["model"], params["key"]))
        if row is None or ("dim" in params and len(row.get("embedding", [])) != params["dim"]):
            return None
        return row

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        for r in rows:
            self.rows[(r["model"], r["key"])] = r


class _Inner:
    provider, model = "fake", "m"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any) -> Any:
        self.calls += 1
        return LLMResult(
            text=f"answer {self.calls}", tokens_in=1, tokens_out=1, provider="fake", model="m"
        )


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)
    for tasks in (lc._TASKS, ec._SHARED_TASKS):
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    import server.core.ch as ch_mod
    from server.settings import settings

    fake = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "embedding_cache_shared", True)
    monkeypatch.setattr(settings, "embedding_cache_shared_max_chars", 2000)
    ec._cache._store.clear()
    yield fake
    genai._identity.set(None)


def _as(org: str) -> None:
    genai.set_identity(org=org, namespace="ns", operation="memory.ask")


@pytest.mark.asyncio
async def test_an_llm_answer_is_never_served_to_another_org(ch: _CH) -> None:
    inner = _Inner()
    a = await CachedLLMClient(inner, org="acme", ch=ch).complete("s", "the same prompt")
    await _settle()
    b = await CachedLLMClient(inner, org="beta", ch=ch).complete("s", "the same prompt")
    again = await CachedLLMClient(inner, org="acme", ch=ch).complete("s", "the same prompt")
    assert (a.from_cache, b.from_cache, again.from_cache) == (False, False, True)
    assert inner.calls == 2 and b.text == "answer 2" and again.text == "answer 1"


@pytest.mark.asyncio
async def test_an_llm_client_with_no_org_neither_reads_nor_writes(ch: _CH) -> None:
    inner = _Inner()
    await CachedLLMClient(inner, org="acme", ch=ch).complete("s", "p")
    await _settle()
    rows = dict(ch.rows)
    res = await CachedLLMClient(inner, ch=ch).complete("s", "p")
    await _settle()
    assert res.from_cache is False and inner.calls == 2 and ch.rows == rows


@pytest.mark.asyncio
async def test_a_vector_is_never_served_to_another_org(ch: _CH) -> None:
    fetched: list[str] = []

    async def fetch(text: str) -> list[float]:
        fetched.append(genai.request_org())
        return [1.0, 0.0]

    _as("acme")
    _, hit = await ec.cached_embed("m", "the same query", fetch, dim=2)
    await _settle()
    _as("beta")
    _, beta_hit = await ec.cached_embed("m", "the same query", fetch, dim=2)
    await _settle()
    ec._cache._store.clear()  # past the process tier: the shared table
    _, beta_shared = await ec.cached_embed("m", "the same query", fetch, dim=2)
    _as("acme")
    _, acme_again = await ec.cached_embed("m", "the same query", fetch, dim=2)
    assert (hit, beta_hit, acme_again) == (False, False, True)
    assert beta_shared is True and ec.last_tier() == "shared"  # beta's own row
    assert fetched == ["acme", "beta"]


@pytest.mark.asyncio
async def test_two_orgs_never_share_an_in_flight_call(ch: _CH) -> None:
    started: list[str] = []
    gate = asyncio.Event()

    async def fetch(text: str) -> list[float]:
        started.append(genai.request_org())
        await gate.wait()
        return [1.0, 0.0]

    async def embed(org: str) -> bool:
        _as(org)
        return (await ec.cached_embed("m", "together", fetch, dim=2))[1]

    tasks = [asyncio.create_task(embed(o)) for o in ("acme", "acme", "beta")]
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    hits = await asyncio.gather(*tasks)
    assert sorted(started) == ["acme", "beta"]  # acme's second call waited on its own
    assert hits == [False, True, False]


@pytest.mark.asyncio
async def test_a_call_outside_a_request_shares_nothing(ch: _CH) -> None:
    calls = 0

    async def fetch(text: str) -> list[float]:
        nonlocal calls
        calls += 1
        return [1.0, 0.0]

    _as("acme")
    await ec.cached_embed("m", "q", fetch, dim=2)
    await _settle()
    entries = (len(ec._cache._store), len(ch.rows))
    genai._identity.set(None)
    genai.set_worker_identity("embedding", org="acme")  # a worker's stamp is not a scope
    _, hit = await ec.cached_embed("m", "q", fetch, dim=2)
    await _settle()
    assert hit is False and calls == 2 and (len(ec._cache._store), len(ch.rows)) == entries


def test_a_batch_vector_is_kept_per_org() -> None:
    ec._cache._store.clear()
    try:
        _as("acme")
        results, misses, _ = _provider.split_cached("m", ["stored text"])
        _provider.store("m", results, misses[0][0], "stored text", [1.0])
        assert _provider.split_cached("m", ["stored text"])[1] == []  # acme's own hit
        _as("beta")
        assert _provider.split_cached("m", ["stored text"])[1] == [(0, "stored text")]
        genai._identity.set(None)
        genai.set_worker_identity("embedding", org="acme")
        _provider.store("m", [None], 0, "worker text", [2.0])
        assert len(ec._cache._store) == 1  # nothing kept without an org
    finally:
        genai._identity.set(None)
