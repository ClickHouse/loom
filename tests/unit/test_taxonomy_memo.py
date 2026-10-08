"""Namespace.taxonomy is remembered per namespace
version. Four FINAL scans (tens of MB on a large namespace) per Graph open become
one per change of the namespace: the memo key carries `max(version)` over
the namespace, the top-N arguments, and expires after five minutes so the
communities (their own table) are not frozen."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.api.models_namespace_catalog import NamespaceTaxonomyInput
from server.identity.app import namespace_catalog as ns_handlers


class _CH:
    def __init__(self, version: int | None) -> None:
        self.version = version
        self.taxonomy_reads = 0
        self.version_reads = 0

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        assert "max(version)" in sql
        self.version_reads += 1
        return {"v": self.version}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.taxonomy_reads += 1
        return [
            {"kind": "tag", "value": "ops", "count": 3},
            {"kind": "entity", "value": "lane-1", "count": 2},
            {"kind": "type", "value": "semantic", "count": 5},
            {"kind": "count", "value": "", "count": 5},
        ]


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ns_handlers.reset_taxonomy_cache()


@pytest.mark.asyncio
async def test_same_version_is_answered_from_the_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(version=41)
    monkeypatch.setattr(ns_handlers, "get_client", lambda: ch)
    inp = NamespaceTaxonomyInput(org="o", namespace="n", top_n_communities=0)
    first = await ns_handlers.handle_namespace_taxonomy(inp)
    second = await ns_handlers.handle_namespace_taxonomy(inp)
    assert first.memory_count == 5 and second.memory_count == 5
    assert [t.value for t in second.tags] == ["ops"]
    assert (
        ch.taxonomy_reads == 1 and ch.version_reads == 2
    )  # one 90 kB watermark per call, one scan


@pytest.mark.asyncio
async def test_a_new_version_or_other_arguments_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(version=41)
    monkeypatch.setattr(ns_handlers, "get_client", lambda: ch)
    inp = NamespaceTaxonomyInput(org="o", namespace="n", top_n_communities=0)
    await ns_handlers.handle_namespace_taxonomy(inp)
    await ns_handlers.handle_namespace_taxonomy(
        NamespaceTaxonomyInput(org="o", namespace="n", top_n_tags=500, top_n_communities=0)
    )
    assert ch.taxonomy_reads == 2  # a different top-N is a different answer
    ch.version = 42
    await ns_handlers.handle_namespace_taxonomy(inp)
    assert ch.taxonomy_reads == 3  # the namespace changed


@pytest.mark.asyncio
async def test_the_memo_is_a_copy_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(version=41)
    monkeypatch.setattr(ns_handlers, "get_client", lambda: ch)
    inp = NamespaceTaxonomyInput(org="o", namespace="n", top_n_communities=0)
    first = await ns_handlers.handle_namespace_taxonomy(inp)
    first.tags.clear()  # a caller mutating its answer must not poison the memo
    second = await ns_handlers.handle_namespace_taxonomy(inp)
    assert [t.value for t in second.tags] == ["ops"]
    for key, (expires, out) in list(ns_handlers._taxonomy_cache.items()):
        ns_handlers._taxonomy_cache[key] = (expires - 10_000.0, out)
    await ns_handlers.handle_namespace_taxonomy(inp)
    assert ch.taxonomy_reads == 2  # expired → recomputed


@pytest.mark.asyncio
async def test_a_client_without_a_version_is_never_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoVersion:
        reads = 0

        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
            self.reads += 1
            return []

    ch = _NoVersion()
    monkeypatch.setattr(ns_handlers, "get_client", lambda: ch)
    inp = NamespaceTaxonomyInput(org="o", namespace="n", top_n_communities=0)
    await ns_handlers.handle_namespace_taxonomy(inp)
    await ns_handlers.handle_namespace_taxonomy(inp)
    assert ch.reads == 2 and ns_handlers._taxonomy_cache == {}
