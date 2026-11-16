"""LoomMemoryStore: the Strands MemoryStore protocol over a loom Agent."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

from loom.integrations.strands import LoomMemoryStore, MemoryEntry, _content_key
from loom.memory import MemorySearchHit, MemorySearchResult


class _FakeMemory:
    def __init__(self, hits, abstain=False):
        self.sets: list[tuple] = []
        self.searches: list[dict] = []
        self._hits = hits
        self._abstain = abstain

    async def set(self, key, value, **kw):
        self.sets.append((key, value, kw))

    async def search(self, query, **kw):
        self.searches.append({"query": query, **kw})
        res = MemorySearchResult(self._hits)
        res.abstain = self._abstain
        return res


def _hit(key, text, sim=0.8):
    return MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=text,
        distance=1 - sim,
        similarity=sim,
        source="fact",
        valid_at="2026-09-01",
    )


def test_add_is_content_addressed_and_carries_metadata_as_tags() -> None:
    mem = _FakeMemory([])
    store = LoomMemoryStore(SimpleNamespace(memory=mem), tags=["customer"])
    asyncio.run(store.add("Alice drinks oat-milk lattes", {"kind": "preference", "n": 3}))
    asyncio.run(store.add("  Alice drinks oat-milk lattes  ", {"kind": "preference"}))
    asyncio.run(store.add("", None))
    assert len(mem.sets) == 2
    k1, v1, kw1 = mem.sets[0]
    assert k1 == _content_key("strands", "Alice drinks oat-milk lattes") and k1.startswith(
        "strands/"
    )
    assert mem.sets[1][0] == k1  # same fact, same key — dedup by construction
    assert kw1["memory_type"] == "semantic"
    assert kw1["tags"] == ["customer", "kind:preference"]  # non-string metadata dropped


def test_search_maps_hits_to_memory_entries_and_honours_max_search_results() -> None:
    mem = _FakeMemory([_hit("k1", "oat-milk lattes", 0.91), _hit("k2", "double espresso", 0.7)])
    store = LoomMemoryStore(SimpleNamespace(memory=mem), tags=["customer"], max_search_results=3)
    out = asyncio.run(store.search("coffee", {"max_search_results": 1}))
    assert mem.searches[0]["top_k"] == 1 and mem.searches[0]["tags"] == ["customer"]
    # The stored clip whatever the top_k: unpinned, a large top_k serves excerpts.
    assert (mem.searches[0]["bodies"], mem.searches[0]["explain"]) == ("full", False)
    assert len(out) == 1 and isinstance(out[0], MemoryEntry)
    assert out[0].content == "oat-milk lattes"
    assert out[0].metadata["memory_key"] == "k1" and out[0].metadata["similarity"] == 0.91
    assert out[0].metadata["valid_at"] == "2026-09-01"


def test_search_without_a_per_call_limit_uses_the_stores() -> None:
    mem = _FakeMemory([_hit("k1", "oat-milk lattes", 0.91)])
    store = LoomMemoryStore(SimpleNamespace(memory=mem), max_search_results=4)
    asyncio.run(store.search("coffee"))
    asyncio.run(store.search("coffee", {}))
    assert [s["top_k"] for s in mem.searches] == [4, 4]


def test_abstain_yields_no_entries_unless_disabled() -> None:
    hits = [_hit("k1", "weak", 0.2)]
    strict = LoomMemoryStore(SimpleNamespace(memory=_FakeMemory(hits, abstain=True)))
    assert asyncio.run(strict.search("anything")) == []
    loose = LoomMemoryStore(
        SimpleNamespace(memory=_FakeMemory(hits, abstain=True)), honour_abstain=False
    )
    assert len(asyncio.run(loose.search("anything"))) == 1


def test_protocol_attributes_present() -> None:
    store = LoomMemoryStore(SimpleNamespace(memory=_FakeMemory([])))
    assert store.name == "loom" and store.writable is True and store.extraction is None
    assert store.max_search_results == 3 and store.description


def test_a_hit_below_the_floor_is_left_out_unless_disabled() -> None:
    weak = dataclasses.replace(_hit("k2", "weak", 0.2), below_threshold=True)
    # three rows for two results: the floor is applied before the cut
    hits = [_hit("k1", "oat-milk lattes", 0.9), weak, _hit("k3", "espresso", 0.8)]
    strict = LoomMemoryStore(SimpleNamespace(memory=_FakeMemory(hits)), max_search_results=2)
    assert [e.metadata["memory_key"] for e in asyncio.run(strict.search("coffee"))] == ["k1", "k3"]
    loose = LoomMemoryStore(
        SimpleNamespace(memory=_FakeMemory(hits)), max_search_results=2, honour_abstain=False
    )
    assert [e.metadata["memory_key"] for e in asyncio.run(loose.search("coffee"))] == ["k1", "k2"]
