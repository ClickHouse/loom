"""LoomMemoryStore: the Strands MemoryStore protocol over a loom Agent."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from loom import Agent
from loom.integrations.strands import LoomMemoryStore, MemoryEntry, _content_key
from loom.memory import MemorySearchHit, MemorySearchResult
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)


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


# These exercise the structural store and real Loom SDK, without a native MemoryManager.
@asynccontextmanager
async def _protected_agent(
    family: SessionProtection | None,
    respond: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[Agent]:
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("NO_PROXY", "*")
        environment.setenv("no_proxy", "*")
        async with Agent(
            org="acme",
            namespace="support",
            agent="bot",
            base_url="http://inert.invalid",
            api_key="test-token",
            session_protection=family,
            pacing=False,
            coalesce_sets=False,
            auto_run=False,
            mentor_brief=False,
            retry={"max_attempts": 1},
        ) as agent:
            mock = httpx.MockTransport(respond)
            agent.transport._client._transport = mock
            assert (
                agent.transport._client._transport_for_url(
                    httpx.URL("http://inert.invalid/v1/memory.search")
                )
                is mock
            )
            yield agent


def _store_reply(request: httpx.Request, *, mirrored: bool = False) -> httpx.Response:
    if request.url.path == "/v1/memory.set":
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "version": 1,
                "memory_id": "00000000-0000-4000-8000-000000000001",
                "status": "active",
                "namespace": body["namespace"],
                "memory_key": body["memory_key"],
            },
        )
    assert request.url.path == "/v1/memory.search"
    return httpx.Response(
        200,
        json={
            "mirrored": mirrored,
            "results": [
                {
                    "memory_key": "seat",
                    "memory_type": "preference",
                    "content_excerpt": "Alice prefers aisle seats",
                    "distance": 0.1,
                    "similarity": 0.9,
                    "source": "fact",
                    "valid_at": "2026-09-01",
                }
            ],
        },
    )


async def _store_call(store: LoomMemoryStore, operation: str) -> Any:
    if operation == "search":
        return await store.search("seat", {"max_search_results": 1})
    assert operation == "add"
    return await store.add(
        "  Alice prefers aisle seats  ", {"kind": "preference", "topic": "travel", "n": 3}
    )


@pytest.mark.parametrize("operation", ["add", "search"])
@pytest.mark.parametrize("constructor", ["bound", "ambient", "both_bound", "both_ambient"])
def test_automatic_store_calls_retain_constructor_contributors(
    tmp_path: Path, operation: str, constructor: str
) -> None:
    bound = SessionProtection.create(base=tmp_path / "bound")
    ambient = SessionProtection.create(base=tmp_path / "ambient")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _store_reply(request)

    async def run() -> None:
        async with _protected_agent(None if constructor == "ambient" else bound, respond) as agent:
            with observation_scope(() if constructor == "bound" else (ambient,)):
                store = LoomMemoryStore(agent)
            agent.transport.session_protection = current
            marked = ambient if constructor in {"ambient", "both_ambient"} else bound
            marked.mark()
            with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
                await _store_call(store, operation)
            assert posts == []
            assert not current.protected and not producer.protected
            assert not (ambient if marked == bound else bound).protected

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["add", "search"])
def test_store_calls_retain_an_earlier_callback_contributor(tmp_path: Path, operation: str) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _store_reply(request)

    async def run() -> None:
        async with _protected_agent(original, respond) as agent:
            store = LoomMemoryStore(agent)
            with observation_scope((earlier,)):
                assert (await store.search("seat"))[0].content == "Alice prefers aisle seats"
            earlier.mark()
            agent.transport.session_protection = current
            with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
                await _store_call(store, operation)
            assert posts == ["/v1/memory.search"]
            assert not original.protected and not current.protected and not producer.protected

    asyncio.run(run())


def test_marked_store_recall_observes_every_contributor_before_later_capture(
    tmp_path: Path,
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    later = SessionProtection.create(base=tmp_path / "later")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _store_reply(request, mirrored=len(posts) == 2)

    async def run() -> None:
        async with _protected_agent(original, respond) as agent:
            store = LoomMemoryStore(agent)
            with observation_scope((earlier,)):
                await store.search("earlier query")
            agent.transport.session_protection = current
            with producer.automatic():
                (entry,) = await store.search("seat")
            assert entry.content == "Alice prefers aisle seats"
            assert all(
                SessionProtection.open(root.pointer()).protected
                for root in (original, earlier, current, producer)
            )
            agent.transport.session_protection = later
            with later.automatic(), pytest.raises(AutomaticCaptureSuppressed):
                await store.add("Recalled seat preference")
            assert posts == ["/v1/memory.search"] * 2
            assert not later.protected

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["add", "search"])
@pytest.mark.parametrize("marked", ["constructor", "entry"])
def test_automatic_store_calls_keep_contributors_across_auth_await(
    tmp_path: Path, operation: str, marked: str
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    entry = SessionProtection.create(base=tmp_path / "entry")
    current = SessionProtection.create(base=tmp_path / "current")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _store_reply(request)

    async def run() -> None:
        async with _protected_agent(original, respond) as agent:
            store = LoomMemoryStore(agent)
            agent.transport.session_protection = current

            class RebindingAuth(httpx.Auth):
                async def async_auth_flow(
                    self, request: httpx.Request
                ) -> AsyncIterator[httpx.Request]:
                    await asyncio.sleep(0)
                    (original if marked == "constructor" else entry).mark()
                    agent.transport.session_protection = replacement
                    yield request

            agent.transport._client.auth = RebindingAuth()
            with (
                observation_scope((entry,)),
                producer.automatic(),
                pytest.raises(AutomaticCaptureSuppressed),
            ):
                await _store_call(store, operation)
            assert posts == []
            assert not current.protected and not replacement.protected and not producer.protected
            assert not (entry if marked == "constructor" else original).protected

    asyncio.run(run())


def test_failed_retained_store_observation_withholds_memory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = SessionProtection.create(base=tmp_path / "broken")
    current = SessionProtection.create(base=tmp_path / "current")
    mark = SessionProtection.mark
    posts: list[str] = []

    def fail_one(family: SessionProtection) -> None:
        if family == broken:
            raise SessionProtectionUnavailable()
        mark(family)

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return _store_reply(request, mirrored=True)

    async def run() -> None:
        async with _protected_agent(broken, respond) as agent:
            store = LoomMemoryStore(agent)
            agent.transport.session_protection = current
            monkeypatch.setattr(SessionProtection, "mark", fail_one)
            with pytest.raises(SessionProtectionUnavailable):
                await store.search("seat")
            assert current.protected and posts == ["/v1/memory.search"]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["add", "search"])
def test_protected_direct_store_calls_remain_explicit(tmp_path: Path, operation: str) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    current = SessionProtection.create(base=tmp_path / "current")
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        return _store_reply(request, mirrored=True)

    async def run() -> None:
        async with _protected_agent(original, respond) as agent:
            store = LoomMemoryStore(agent, tags=["customer"])
            agent.transport.session_protection = current
            original.mark()
            result = await _store_call(store, operation)
            ((path, body),) = posts
            if operation == "search":
                assert path == "/v1/memory.search" and body["query"] == "seat"
                assert result[0].content == "Alice prefers aisle seats"
            else:
                assert path == "/v1/memory.set" and body["value"] == "Alice prefers aisle seats"
                assert body["tags"] == ["customer", "kind:preference", "topic:travel"]
            assert original.protected
            assert current.protected is (operation == "search")

    asyncio.run(run())


def test_clean_automatic_store_calls_preserve_payloads_and_entries(tmp_path: Path) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        return _store_reply(request)

    async def run() -> None:
        async with _protected_agent(original, respond) as agent:
            store = LoomMemoryStore(agent, tags=["customer"], memory_type="preference")
            with producer.automatic():
                (entry,) = await _store_call(store, "search")
                await _store_call(store, "add")
            assert [path for path, _ in posts] == ["/v1/memory.search", "/v1/memory.set"]
            search, capture = (body for _, body in posts)
            assert (search["org"], search["namespace"], search["query"], search["top_k"]) == (
                "acme",
                "support",
                "seat",
                1,
            )
            assert (search["tags"], search["memory_type"], search["bodies"], search["explain"]) == (
                ["customer"],
                "preference",
                "full",
                False,
            )
            assert capture["memory_key"].startswith("strands/")
            assert capture["value"] == "Alice prefers aisle seats"
            assert capture["tags"] == ["customer", "kind:preference", "topic:travel"]
            assert entry.content == "Alice prefers aisle seats"
            assert entry.metadata == {
                "memory_key": "seat",
                "memory_type": "preference",
                "similarity": 0.9,
                "source": "fact",
                "valid_at": "2026-09-01",
            }
            assert not original.protected and not producer.protected

    asyncio.run(run())
