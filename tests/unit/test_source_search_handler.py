"""Public search prepares identity before providers and routes only final checked pools."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.app import knowledge_search as app
from server.knowledge.app import source_access, source_search_passes
from server.knowledge.app.search import ChunkHit
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_ranking import RankedHit


def hit(namespace, *, mirrored=False):
    chunk = ChunkHit(
        namespace,
        "drive:allowed" if mirrored else "ordinary:" + namespace,
        0,
        "allowed text" if mirrored else namespace,
        [],
        0.05 if mirrored else 0.9 if namespace == "private" else 0.2,
        score=0.95 if mirrored else 0.1 if namespace == "private" else 0.8,
    )
    return RankedHit(chunk, Admitted(namespace, {}, {}) if mirrored else None)


@pytest.fixture
def world(monkeypatch):
    phases, collected, counts = [], [], []
    pending = {"strong": False}
    final = [[hit("private")], [hit("public")]]
    plan = SimpleNamespace(org="acme", marked=True)

    class Embedder:
        model_name = "fake"

        async def embed(self, _text):
            phases.append("embed")
            return [1.0]

    async def namespaces(*_args):
        phases.append("namespaces")
        return ["private", "public"]

    async def prepare(_org, selected):
        assert selected == ["private", "public"]
        phases.append("prepare")
        return plan

    async def finish():
        phases.append("finish")
        return final

    session = SimpleNamespace(
        finish=finish,
        proofs=SimpleNamespace(check=AsyncMock(), fresh=lambda: True, mirrored=True),
        restrict_proofs=lambda selected: collected.append(selected),
        ordinary=lambda: [[hit("private")], [hit("public")]],
    )

    def factory(_input, _embedder, _vector, _pool, dropped, **kwargs):
        collected.append(kwargs.get("checked"))

        async def search(selected):
            phases.append("search:" + ",".join(selected))
            counts.append(list(selected))
            dropped.count += 2 if selected == ["private"] else 7
            return [
                (ns, [hit(ns, mirrored=(ns == "private" and pending["strong"])).chunk])
                for ns in selected
            ]

        return search

    async def rerank(_endpoint, _query, hits, _pool):
        phases.append("rerank")
        return hits, False

    monkeypatch.setattr(app, "get_embedder", Embedder)
    monkeypatch.setattr(app, "readable_namespaces", namespaces)
    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(
        source_search_passes, "SearchPasses", lambda actual: session if actual is plan else None
    )
    monkeypatch.setattr(app, "_namespaces_search", factory)
    monkeypatch.setattr(
        app.namespace_visibility,
        "visibility_map",
        AsyncMock(return_value={"private": "private", "public": "public"}),
    )
    monkeypatch.setattr(app, "_rerank_endpoint", lambda _input: "synthetic-reranker")
    monkeypatch.setattr(app, "_rerank", rerank)
    monkeypatch.setattr(app.gaps_app, "record_miss", lambda **_kwargs: None)
    request = KnowledgeSearchInput(
        org="acme",
        namespace="private",
        namespaces=["private", "public"],
        namespace_scope="all_mine",
        query="document",
        top_k=5,
        collapse_sections=False,
    )
    return phases, counts, collected, final, session, request, pending


async def test_source_participation_is_prepared_before_query_embedding_and_provider_use(world):
    phases, counts, collected, _final, session, request, _pending = world
    out = await app.handle_knowledge_search(request)
    assert phases.index("namespaces") < phases.index("prepare") < phases.index("embed")
    assert phases.index("finish") < phases.index("rerank")
    assert counts == [["private"], ["public"]] and collected == [session, _final]
    assert {item.content for item in out.results} == {"private", "public"}
    assert out.stale_dropped == 9


async def test_surviving_strong_private_mirror_emits_first_only_after_gathering_both_passes(world):
    phases, counts, _collected, final, _session, request, pending = world
    final[0] = [hit("private", mirrored=True)]
    out = await app.handle_knowledge_search(request)
    assert counts == [["private"], ["public"]]
    assert [item.content for item in out.results] == ["allowed text"]
    assert out.stale_dropped == 2
    assert phases[-1] == "finish"  # one final hit needs no reranker


async def test_final_denial_and_initial_denial_have_identical_complete_public_replies(world):
    phases, counts, _collected, final, _session, request, pending = world
    pending["strong"] = True
    late = await app.handle_knowledge_search(request)
    phases.clear()
    counts.clear()
    pending["strong"] = False
    # The controller barrier has removed a lost or failed provisional mirror;
    # public conversion must use these same final pools for an initial denial.
    final[:] = [[hit("private")], [hit("public")]]
    initial = await app.handle_knowledge_search(request)
    assert late.model_dump() == initial.model_dump()
    assert counts == [["private"], ["public"]]


async def test_ordinary_stale_copy_is_removed_before_it_can_shadow_a_mirror(monkeypatch):
    from server.core import pg
    from server.knowledge.app import pg_serving, search_legs
    from server.knowledge.app.search import HybridSearchQuery

    stale, live = hit("docs").chunk, hit("other").chunk
    query = HybridSearchQuery("acme", ("docs", "other"), "fake", [1.0], 2)
    pools = search_legs.LegPools(query, {"semantic": [stale, live]}, 2, 1.0)
    monkeypatch.setattr(search_legs, "collect", AsyncMock(return_value=pools))
    monkeypatch.setattr(pg, "enabled", lambda: True)

    async def keep(_org, hits):
        return [item for item in hits if item is not stale], int(stale in hits)

    monkeypatch.setattr(pg_serving, "keep_served", keep)
    checked = SimpleNamespace(collect=AsyncMock())
    search = app._namespaces_search(
        KnowledgeSearchInput(
            org="acme", namespace="docs", query="document", search_mode="semantic"
        ),
        SimpleNamespace(model_name="fake"),
        [1.0],
        0,
        app._Dropped(),
        checked=checked,
    )
    await search(["docs", "other"])
    ordinary = checked.collect.await_args.args[1]
    assert ordinary == {"semantic": [live]}
    assert checked.collect.await_args.kwargs["fallback"] == [live]


async def test_ordinary_head_retired_after_legacy_search_is_removed_from_fallback(monkeypatch):
    from server.core import pg
    from server.knowledge.app import pg_serving, search_legs
    from server.knowledge.app.search import HybridSearchQuery

    retired = hit("docs").chunk
    pools = search_legs.LegPools(
        HybridSearchQuery("acme", ("docs",), "fake", [1.0], 2),
        {"semantic": [retired]},
        2,
        1.0,
    )
    monkeypatch.setattr(search_legs, "collect", AsyncMock(return_value=pools))
    monkeypatch.setattr(pg, "enabled", lambda: True)
    calls = 0

    async def keep(_org, hits):
        nonlocal calls
        calls += 1
        return (list(hits), 0) if calls == 1 else ([], len(hits))

    monkeypatch.setattr(pg_serving, "keep_served", keep)
    checked = SimpleNamespace(collect=AsyncMock())
    dropped = app._Dropped()
    search = app._namespaces_search(
        KnowledgeSearchInput(
            org="acme", namespace="docs", query="document", search_mode="semantic"
        ),
        SimpleNamespace(model_name="fake"),
        [1.0],
        0,
        dropped,
        checked=checked,
    )
    await search(["docs"])
    assert calls == 2
    assert checked.collect.await_args.args[1] == {"semantic": []}
    assert checked.collect.await_args.kwargs["fallback"] == []
    assert dropped.count == 1


async def test_new_serving_version_cannot_keep_the_retired_fallback_version(monkeypatch):
    from server.core import pg
    from server.knowledge.app import pg_serving, search_legs
    from server.knowledge.app.search import HybridSearchQuery

    old = replace(hit("docs").chunk, doc_version=10, revision=1)
    new = replace(old, doc_version=20, revision=2, content="new version")
    pools = search_legs.LegPools(
        HybridSearchQuery("acme", ("docs",), "fake", [1.0], 2),
        {"semantic": [old, new]},
        2,
        1.0,
    )
    monkeypatch.setattr(search_legs, "collect", AsyncMock(return_value=pools))
    monkeypatch.setattr(pg, "enabled", lambda: True)
    calls = 0

    async def keep(_org, hits):
        nonlocal calls
        calls += 1
        return (list(hits), 0) if calls == 1 else ([item for item in hits if item is new], 1)

    monkeypatch.setattr(pg_serving, "keep_served", keep)
    checked = SimpleNamespace(collect=AsyncMock())
    search = app._namespaces_search(
        KnowledgeSearchInput(
            org="acme", namespace="docs", query="document", search_mode="semantic"
        ),
        SimpleNamespace(model_name="fake"),
        [1.0],
        0,
        app._Dropped(),
        checked=checked,
    )
    await search(["docs"])
    assert calls == 2
    assert checked.collect.await_args.args[1] == {"semantic": [new]}
    assert checked.collect.await_args.kwargs["fallback"] == []
