"""code.ask's memory enrichment: the bound memory namespace is
read as the current principal, and an answer grounded in a non-public memory
namespace is never memoized in the per-namespace answer cache."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.code.app import answers as ca


@pytest.fixture
def bound(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ask_memory_namespace", "support-bot", raising=False)
    monkeypatch.setattr(settings, "code_ask_memory_org", "clickhouse", raising=False)
    yield settings
    nc.reset()


@pytest.mark.asyncio
async def test_enrichment_skipped_when_principal_cannot_read_memory_namespace(
    bound, monkeypatch
) -> None:
    async def _deny(*a, **k):
        return False

    monkeypatch.setattr(authz, "row_in_scope", _deny)

    async def _boom(inp):
        raise AssertionError("memory.search must not run for an unauthorized principal")

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _boom)
    assert (await ca._inject_memory_context("why?")).items == ()


@pytest.mark.asyncio
async def test_enrichment_runs_when_authorized(bound, monkeypatch) -> None:
    async def _allow(*a, **k):
        return True

    monkeypatch.setattr(authz, "row_in_scope", _allow)

    async def fake_search(inp):
        return SimpleNamespace(
            abstain=False,
            results=[SimpleNamespace(memory_key="k", content_excerpt="because A double-freed")],
        )

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", fake_search)
    memory = await ca._inject_memory_context("why?")
    assert memory.items == (("k", "because A double-freed", ""),)  # no tags read


@pytest.mark.asyncio
async def test_cacheable_only_for_a_standard_public_memory_namespace(bound, monkeypatch) -> None:
    state = {"vis": "public"}

    async def _vm(org, namespaces):
        return {ns: state["vis"] for ns in namespaces}

    monkeypatch.setattr("server.identity.app.namespace_visibility.visibility_map", _vm)
    memory = ca.MemoryContext("clickhouse", "support-bot", (ca.MemoryExcerpt("k", "text", "k"),))
    assert await ca._memory_context_cacheable("clickhouse", memory) is True
    state["vis"] = "private"
    assert await ca._memory_context_cacheable("clickhouse", memory) is False
    state["vis"] = "public"
    nc._cache["clickhouse"] = (1e12, frozenset({"support-bot"}))
    assert await ca._memory_context_cacheable("clickhouse", memory) is False


@pytest.mark.asyncio
async def test_cacheable_when_unbound_and_not_when_lookup_fails(monkeypatch) -> None:
    assert await ca._memory_context_cacheable("clickhouse", ca.MemoryContext()) is True

    async def _fail(org, namespaces):
        raise RuntimeError("down")

    monkeypatch.setattr("server.identity.app.namespace_visibility.visibility_map", _fail)
    memory = ca.MemoryContext("clickhouse", "m", (ca.MemoryExcerpt("k", "text", "k"),))
    assert await ca._memory_context_cacheable("clickhouse", memory) is False
