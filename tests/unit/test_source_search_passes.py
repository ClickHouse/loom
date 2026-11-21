"""Late permission changes cannot leave provisional mirrors controlling a search route."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core.vectors.private_first import best_similarity
from server.knowledge.app import source_search_passes as app
from server.knowledge.app.search import ChunkHit, HybridSearchQuery
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_search import Hydrated


def mirror(namespace):
    candidate = {
        "namespace": "acl-" + "1" * 24,
        "source_uri": "drive:" + namespace + "#11111111-1111-4111-8111-111111111111",
        "chunk_idx": 0,
        "doc_version": 10,
        "revision": 0,
        "distance": 0.05,
        "lexical": 0.0,
        "matched": [],
    }
    return Hydrated(
        Admitted(
            namespace,
            candidate,
            {"version": 10, "revision": 0, "ingested_at": datetime(2025, 1, 1, tzinfo=UTC)},
        ),
        {"content": "private text"},
    )


def ordinary(namespace):
    distance = 0.9 if namespace == "private" else 0.2
    return ChunkHit(
        namespace, "ordinary:" + namespace, 0, namespace, [], distance, score=1 - distance
    )


def routed(passes):
    first = [(r.chunk.namespace, [r.chunk]) for r in passes[0]]
    selected = passes[:1] if best_similarity(first, lambda hit: not hit.noise) >= 0.8 else passes
    return [dict(asdict(r.chunk), mirrored=r.mirrored) for pool in selected for r in pool]


@pytest.fixture
def world(monkeypatch):
    plan = SimpleNamespace(org="acme", failed=False, unread=False)
    source = {namespace: mirror(namespace) for namespace in ("private", "public")}
    seen = []

    async def discover(_plan, query, **_kwargs):
        namespace = query.namespaces[0]
        seen.append(namespace)
        return {"semantic": [source[namespace].admitted]} if namespace in source else {}

    async def hydrate(_plan, pools, **_kwargs):
        return {leg: [source[item.namespace] for item in pool] for leg, pool in pools.items()}

    async def recheck(_plan, pools):
        return pools

    monkeypatch.setattr(app, "discover", AsyncMock(side_effect=discover))
    monkeypatch.setattr(app, "hydrate", AsyncMock(side_effect=hydrate))
    monkeypatch.setattr(app, "recheck", AsyncMock(side_effect=recheck))

    async def collect(session, namespace):
        hit = ordinary(namespace)
        assert (
            await session.collect(
                HybridSearchQuery("acme", (namespace,), "fake", [1.0], 2),
                {"semantic": [hit]},
                legs=("semantic",),
                fallback=[hit],
                top_k=1,
            )
            is None
        )

    return plan, source, seen, collect


async def test_all_possible_passes_are_collected_before_final_route_selection(world):
    plan, source, seen, collect = world
    del source["public"]
    session = app.SearchPasses(plan)
    await collect(session, "private")
    await collect(session, "public")
    final = await session.finish()
    assert seen == ["private", "public"]
    assert len(routed(final)) == 1 and routed(final)[0]["mirrored"]
    assert final[1][0].chunk.content == "public"


@pytest.mark.parametrize("failure", ["denied", "expired", "candidate_failure"])
async def test_discarded_private_mirror_has_same_complete_fallback_as_initially_denied(
    world, monkeypatch, failure
):
    plan, source, _seen, collect = world
    del source["public"]
    late = app.SearchPasses(plan)
    await collect(late, "private")
    await collect(late, "public")

    async def changed(_plan, pools):
        if failure == "candidate_failure":
            plan.failed = True
            return {}
        return {
            leg: [item for item in pool if item.namespace != "private"]
            for leg, pool in pools.items()
        }

    monkeypatch.setattr(app, "recheck", AsyncMock(side_effect=changed))
    actual = routed(await late.finish())
    plan.failed = False
    del source["private"]
    initial = app.SearchPasses(plan)
    await collect(initial, "private")
    await collect(initial, "public")
    expected = routed(await initial.finish())
    assert actual == expected
    assert [row["content"] for row in actual] == ["private", "public"]
    assert not plan.unread


@pytest.mark.parametrize("unread", [False, True])
async def test_second_pass_failure_removes_first_pass_mirrors_too(world, monkeypatch, unread):
    plan, _source, seen, collect = world
    session = app.SearchPasses(plan)
    await collect(session, "private")
    original = app.discover.side_effect

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        plan.failed, plan.unread = True, unread
        return {}

    monkeypatch.setattr(app, "discover", AsyncMock(side_effect=fail))
    await collect(session, "public")
    out = await session.finish()
    assert seen == ["private", "public"]
    assert all(not hit.mirrored for pool in out for hit in pool)
    assert [pool[0].chunk.content for pool in out] == ["private", "public"]


async def test_prior_candidate_failure_does_not_skip_a_statically_selected_pass(world):
    plan, _source, seen, collect = world
    plan.failed = True
    session = app.SearchPasses(plan)
    await collect(session, "private")
    await collect(session, "public")
    assert seen == ["private", "public"]
    assert all(not r.mirrored for pool in await session.finish() for r in pool)


async def test_final_proof_refresh_preserves_the_checked_candidates_own_scores(world, monkeypatch):
    plan, _source, _seen, collect = world
    session = app.SearchPasses(plan)
    await collect(session, "private")

    async def latest(_plan, pools):
        return {
            leg: [
                replace(
                    item,
                    candidate={**item.candidate, "distance": 0.99},
                    document={**item.document, "version": 11},
                )
                for item in pool
            ]
            for leg, pool in pools.items()
        }

    monkeypatch.setattr(app, "recheck", AsyncMock(side_effect=latest))
    [[hit]] = await session.finish()
    assert hit.chunk.distance == 0.05
    assert hit.admitted.document == {**_source["private"].admitted.document, "version": 11}
    assert hit.admitted.candidate["distance"] == 0.05


async def test_late_final_ranking_error_discards_all_mirrors(world, monkeypatch):
    plan, _source, _seen, collect = world
    session = app.SearchPasses(plan)
    await collect(session, "private")
    await collect(session, "public")
    rank = app.rank

    def broken(ordinary, mirrored, **kwargs):
        if any(item.admitted.namespace == "public" for pool in mirrored.values() for item in pool):
            raise ValueError("private title")
        return rank(ordinary, mirrored, **kwargs)

    monkeypatch.setattr(app, "rank", broken)
    assert all(not r.mirrored for pool in await session.finish() for r in pool)
    assert plan.failed and not plan.unread


async def test_collection_cancellation_propagates_without_becoming_a_private_failure(
    world, monkeypatch
):
    plan, _source, _seen, collect = world
    session = app.SearchPasses(plan)
    monkeypatch.setattr(app, "discover", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await collect(session, "private")
    assert not plan.failed and not plan.unread
