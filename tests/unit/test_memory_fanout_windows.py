"""Input-sized memory reads retain order while bounding owned child work."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import VectorInputTooLargeError
from server.core.vectors.private_first import each, search_private_first
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import (
    anchor_pool,
    ann_entity_boosts,
    digest_descent,
    key_match,
    side_legs,
    supplemental,
    vector_legs,
)
from server.memory.domain.search_types import SearchHit


class _Calls:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.inputs: list[Any] = []

    async def run(self, value: Any) -> None:
        self.inputs.append(value)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
        finally:
            self.active -= 1


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.5] * 4


def _ctx(**input_kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        input=MemorySearchInput(
            org="o", namespace="ns0", agent="a", user_id="u", query="worktree path", **input_kw
        ),
        embedder=_Embedder(),
        actual_work={"embedding_calls": 0, "vector_queries": 0},
        namespaces=[f"ns{i}" for i in range(9)],
        effective_entity_filter=["worktree"],
        resolved_dates=[],
        search_plan=SimpleNamespace(planes=["fact"]),
        graph_expanded_by_ns={},
        graph_paths_by_ns={},
        triple_matched_by_ns={},
        query_anchor_iso="",
        preference_attribute="",
        recency_intent=False,
        chronological_intent="",
        query_vec=[0.1] * 4,
        pool_top_k=30,
        all_hits=[],
        query_shape="simple",
        endpoint="",
        stated_value_update=True,
        namespace_order_authoritative=False,
        entity_boost_by_key={},
        sub_queries=[],
    )


def _hit(key: str, *, distance: float = 0.2) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=distance,
        rank_score=1.0 - distance,
    )


async def test_each_creates_only_the_current_window_and_returns_input_order() -> None:
    calls = _Calls()
    created = 0
    completed = 0
    retained_peak = 0

    def one(ns: str):
        nonlocal created, retained_peak
        created += 1
        retained_peak = max(retained_peak, created - completed)

        async def read():
            nonlocal completed
            await calls.run(ns)
            completed += 1
            return ns, [ns]

        return read()

    names = [f"ns{i}" for i in range(9)]
    assert await each(one)(names) == [(ns, [ns]) for ns in names]
    assert calls.inputs == names
    assert retained_peak <= 4
    assert calls.peak <= 4


async def test_main_vector_producer_bounds_namespaces_without_reordering(monkeypatch) -> None:
    calls = _Calls()
    ctx = _ctx()

    async def search_one(context, ns):
        assert context is ctx
        await calls.run(ns)
        return ns, [_hit(ns)]

    monkeypatch.setattr(vector_legs, "_search_one", search_one)
    await vector_legs._run_vector_legs(ctx, None)
    assert calls.inputs == ctx.namespaces
    assert [h.namespace for h in ctx.all_hits] == ctx.namespaces
    assert calls.peak <= 4


async def test_entity_boost_bounds_entities_and_preserves_bound_identity(monkeypatch) -> None:
    calls = _Calls()
    entities = [f"entity{i}" for i in range(9)]

    class Embedder(_Embedder):
        async def embed(self, text):
            await calls.run(text)
            return [float(entities.index(text))]

    class Client:
        async def query_all(self, sql, *, params):
            assert params["org"] == "o" and params["namespaces"] == ["ns0", "ns1"]
            assert params["agent"] == "a" and params["uid"] == "u"
            return [{"namespace": "ns1", "memory_key": str(int(params["q"][0])), "distance": 0.2}]

    monkeypatch.setattr(ann_entity_boosts, "get_client", Client)
    result = await ann_entity_boosts._ann_entity_boosts(
        org="o",
        namespaces=["ns0", "ns1"],
        agent="a",
        user_id="u",
        query_entities=entities,
        embedder=Embedder(),
    )
    assert list(result) == [("ns1", str(i)) for i in range(9)]
    assert list(result.values()) == pytest.approx([0.4] * 9)
    assert calls.inputs == entities
    assert calls.peak <= 4


@pytest.mark.parametrize("leg", ["sessions", "entities", "observations"])
async def test_side_leg_bounds_namespaces_and_keeps_scope_and_order(monkeypatch, leg) -> None:
    from server.core import ch
    from server.memory.app import retrieval

    calls = _Calls()
    ctx = _ctx(session_id="session", session_ids=["session"])
    client = object()
    monkeypatch.setattr(ch, "get_client", lambda: client)
    monkeypatch.setattr(side_legs, "get_client", lambda: client)

    async def read(actual_client, **kwargs):
        assert actual_client is client
        assert (kwargs["org"], kwargs["agent"], kwargs["user_id"]) == ("o", "a", "u")
        assert kwargs["query_vec"] == ctx.query_vec
        if leg == "sessions":
            assert kwargs["session_id"] == "session" and kwargs["session_ids"] == ["session"]
        await calls.run(kwargs["namespace"])
        return [_hit(kwargs["namespace"])]

    async def known(*args):
        return frozenset()

    monkeypatch.setattr(retrieval, "summary_entities_present", known)
    if leg == "sessions":
        monkeypatch.setattr(retrieval, "fetch_session_summaries", read)
        task = side_legs._launch_summary_leg(ctx)
    elif leg == "entities":
        monkeypatch.setattr(retrieval, "fetch_entity_summaries", read)
        task = await side_legs._launch_entity_summary_leg(ctx)
    else:
        monkeypatch.setattr(retrieval, "fetch_current_state_observation", read)
        task = side_legs._launch_observation_leg(ctx)
    assert task is not None
    result = await task
    assert [ns for ns, _ in result] == ctx.namespaces
    assert calls.inputs == ctx.namespaces
    assert calls.peak <= 4


@pytest.mark.parametrize("arm", ["anchor", "key", "digest"])
async def test_additive_producer_bounds_reads_without_changing_rows(monkeypatch, arm) -> None:
    from server.core import ch
    from server.memory.api.models_search import _MemorySearchHit
    from server.settings import settings

    calls = _Calls()
    ctx = _ctx(key_match=True)

    class Client:
        async def query_all(self, sql, *, params):
            assert params["org"] == "o"
            ns = params["ns"]
            # The key arm's keys-only probe is still a separate, scoped query.
            if set(params) == {"org", "ns", "stems"}:
                return [{"any_key": 1}]
            await calls.run(ns)
            return [
                {
                    "memory_key": ns,
                    "content_excerpt": ns,
                    "similarity": 0.7,
                    "strength": 0.8,
                    "agent": "a",
                    "user_id": "u",
                    "session_id": "session",
                    "scope": "private",
                }
            ]

    monkeypatch.setattr(ch, "get_client", Client)
    if arm == "anchor":
        await anchor_pool._run_anchor_pool(ctx)
    elif arm == "key":
        monkeypatch.setattr(key_match, "key_match_query_stems", lambda _: ["work", "tree"])
        await key_match._run_key_match_pool(ctx)
    else:
        monkeypatch.setattr(settings, "memory_search_digest_descent_top_m", 9)
        ctx.all_hits = [
            _MemorySearchHit(
                memory_key=f"summary:s{i}",
                memory_type="semantic",
                content_excerpt="digest",
                distance=0.1,
                similarity=0.9,
                rank_score=0.9 - i / 100,
                namespace=ns,
                source="summary",
            )
            for i, ns in enumerate(ctx.namespaces)
        ]
        await digest_descent._run_digest_descent(ctx)
    added = [h for h in ctx.all_hits if not h.memory_key.startswith("summary:")]
    assert [(h.namespace, h.memory_key) for h in added] == [(ns, ns) for ns in ctx.namespaces]
    assert calls.inputs == ctx.namespaces
    assert calls.peak <= 4


@pytest.mark.parametrize("close", [True, False])
async def test_private_pass_still_controls_public_reads(close) -> None:
    names = [f"ns{i}" for i in range(9)] + ["public"]
    calls: list[str] = []

    async def read(ns):
        calls.append(ns)
        await asyncio.sleep(0)
        return ns, [_hit(ns, distance=0.1 if close else 0.9)]

    async def visibility(_):
        return {ns: "public" if ns == "public" else "private" for ns in names}

    result = await search_private_first(
        "ns0",
        names,
        each(read),
        visibility=visibility,
        threshold=0.8,
        span=lambda **_: nullcontext(),
    )
    expected = names[:-1] if close else names
    assert [ns for ns, _ in result] == expected
    assert calls == expected


async def test_first_refusal_joins_owned_window_before_returning() -> None:
    started = asyncio.Event()
    calls: list[asyncio.Task[Any]] = []
    cleaned: list[int] = []
    failure = VectorInputTooLargeError("bounded input")

    async def read(i):
        calls.append(asyncio.current_task())
        if len(calls) == 4:
            started.set()
        if i == 0:
            await started.wait()
            raise failure
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(i)

    try:
        with pytest.raises(VectorInputTooLargeError) as raised:
            await each(read)(list(range(9)))
        assert raised.value is failure
        assert len(calls) == 4
        assert all(task.done() for task in calls)
        assert sorted(cleaned) == [1, 2, 3]
    finally:
        for task in calls:
            task.cancel()
        await asyncio.gather(*calls, return_exceptions=True)


async def test_repeated_cancellation_during_cleanup_keeps_first_refusal() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[asyncio.Task[Any]] = []
    cleaning: list[int] = []
    cleaned: list[int] = []
    failure = VectorInputTooLargeError("first refusal")

    async def read(i):
        calls.append(asyncio.current_task())
        if len(calls) == 4:
            started.set()
        if i == 0:
            await started.wait()
            raise failure
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.append(i)
            await release.wait()
            cleaned.append(i)

    caller = asyncio.create_task(each(read)(list(range(9))))
    try:
        await started.wait()
        for _ in range(20):
            if cleaning or caller.done():
                break
            await asyncio.sleep(0)
        assert sorted(cleaning) == [1, 2, 3]
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        release.set()
        with pytest.raises(VectorInputTooLargeError) as raised:
            await caller
        assert raised.value is failure
        assert sorted(cleaned) == [1, 2, 3]
        assert len(calls) == 4 and all(task.done() for task in calls)
    finally:
        release.set()
        for task in calls:
            if not task.done():
                task.cancel()
        await asyncio.gather(caller, *calls, return_exceptions=True)


async def test_sub_query_duplicate_keeps_the_original_phrase_score(monkeypatch) -> None:
    """The existing shared seen set awards a duplicate to the first finished phrase."""
    from server.memory.app import retrieval

    ctx = _ctx()
    ctx.sub_queries = ["first", "second"]

    async def search(q):
        ticks = (100 if q.namespace == "ns0" else 0) if q.query_text == "first" else 60
        for _ in range(ticks):
            await asyncio.sleep(0)
        return [_hit(q.namespace, distance=0.2 if q.query_text == "first" else 0.4)]

    monkeypatch.setattr(retrieval, "vector_search", search)
    await supplemental._run_sub_queries(ctx)
    assert [h.namespace for h in ctx.all_hits] == ctx.namespaces
    assert [h.rank_score for h in ctx.all_hits] == pytest.approx([0.6] * 9)
    assert ctx.actual_work["sub_query_count"] == 2


async def test_successful_window_inside_except_propagates_cleanup_cancellation(monkeypatch) -> None:
    from server.core.vectors import private_first

    cleaning, release, finished = (asyncio.Event() for _ in range(3))
    children: list[asyncio.Task[Any]] = []
    inputs: list[int] = []
    to_its_end = private_first.to_its_end

    async def held_cleanup(write, *, name):
        async def held():
            cleaning.set()
            await release.wait()
            return await write

        try:
            return await to_its_end(held(), name=name)
        finally:
            finished.set()

    async def read(i):
        inputs.append(i)
        children.append(asyncio.current_task())
        return i

    async def request_from_except():
        try:
            raise LookupError("unrelated caller exception")
        except LookupError:
            return await each(read)(list(range(8)))

    monkeypatch.setattr(private_first, "to_its_end", held_cleanup)
    request = asyncio.create_task(request_from_except())
    try:
        async with asyncio.timeout(2):
            await cleaning.wait()
            assert inputs == [0, 1, 2, 3] and all(t.done() for t in children)
            assert request.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert finished.is_set() and all(t.done() for t in children)
        assert inputs == [0, 1, 2, 3]
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, *children, return_exceptions=True)
