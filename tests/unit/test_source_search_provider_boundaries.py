"""Permission loss while a provider waits cannot survive into public search output."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.app import knowledge_search as app
from server.knowledge.app import source_access, source_search_passes
from server.knowledge.app.search import ChunkHit, HybridSearchQuery
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_search import Hydrated
from server.knowledge.domain.serving import Serving
from tests.unit.ch_fake import RecordingCH


@pytest.mark.parametrize("provider", ["rerank", "spans"])
@pytest.mark.parametrize("boundary", ["provider", "outer"])
async def test_lost_provider_proof_rebuilds_the_whole_ordinary_search_reply(
    monkeypatch, provider, boundary
):
    state = {"allowed": True, "revoke": boundary == "provider"}
    candidate = {
        "namespace": "acl-" + "1" * 24,
        "source_uri": "gdrive:private#11111111-1111-4111-8111-111111111111",
        "chunk_idx": 0,
        "doc_version": 9,
        "revision": 0,
        "distance": 0.05,
        "lexical": 0.0,
        "matched": [],
    }
    admitted = Admitted(
        "team",
        candidate,
        {
            "namespace": candidate["namespace"],
            "source_uri": candidate["source_uri"],
            "tags": [],
            "version": 10,
            "revision": 0,
            "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
        },
        serving=Serving(False, 0, 0, 9),
    )
    hydrated = Hydrated(admitted, {"content": "Private provider text"})

    async def check(_candidates, **_kwargs):
        return [admitted] if state["allowed"] else []

    plan = SimpleNamespace(
        org="acme",
        marked=True,
        failed=False,
        unread=False,
        current=lambda _item: True,
        check=AsyncMock(side_effect=check),
    )

    class Embedder:
        model_name = "fake-model"

        async def embed(self, _query):
            return [1.0]

    ordinary = [
        ChunkHit(
            "team",
            f"ordinary:{index}",
            0,
            f"Ordinary {index}",
            [],
            0.4 + index / 10,
            score=0.6 - index / 10,
        )
        for index in range(2)
    ]

    async def recheck(_plan, pools):
        return pools if state["allowed"] else {}

    def factory(_input, embedder, vector, _pool, _dropped, *, checked):
        async def search(namespaces):
            await checked.collect(
                HybridSearchQuery("acme", tuple(namespaces), embedder.model_name, vector, 3),
                {"semantic": ordinary},
                legs=("semantic",),
                fallback=ordinary,
                top_k=3,
            )
            return [("team", ordinary)]

        return search

    async def rerank(_endpoint, _query, hits, _pool):
        await asyncio.sleep(0)
        mirrored = any(hit.content == hydrated.row["content"] for hit in hits)
        if state["revoke"]:
            state["allowed"] = False
        return (list(reversed(hits)), True) if mirrored else (hits, False)

    async def spans(_embedder, _vector, hits, *, proofs=None):
        await asyncio.sleep(0)
        mirrored = any(hit.content == hydrated.row["content"] for hit in hits)
        if state["revoke"]:
            state["allowed"] = False
        for hit in hits:
            hit.span = "Mirror-influenced span" if mirrored else "Ordinary span"

    monkeypatch.setattr(app, "readable_namespaces", AsyncMock(return_value=["team"]))
    monkeypatch.setattr(source_access, "prepare", AsyncMock(return_value=plan))
    monkeypatch.setattr(app, "get_embedder", Embedder)
    monkeypatch.setattr(app, "_namespaces_search", factory)
    monkeypatch.setattr(
        app, "_rerank_endpoint", lambda _input: "synthetic" if provider == "rerank" else ""
    )
    monkeypatch.setattr(app, "_rerank", rerank)
    monkeypatch.setattr(app, "_narrow_spans", spans)
    monkeypatch.setattr(app.gaps_app, "record_miss", lambda **_kwargs: None)
    monkeypatch.setattr(
        source_search_passes, "discover", AsyncMock(return_value={"semantic": [admitted]})
    )
    monkeypatch.setattr(
        source_search_passes, "hydrate", AsyncMock(return_value={"semantic": [hydrated]})
    )
    monkeypatch.setattr(source_search_passes, "recheck", AsyncMock(side_effect=recheck))
    ch = RecordingCH().on(
        "FROM loom.chunks",
        [
            {
                k: candidate[k]
                for k in (
                    "namespace",
                    "source_uri",
                    "chunk_idx",
                    "doc_version",
                    "revision",
                )
            }
        ],
    )
    ch.install(monkeypatch, "server.knowledge.store.source_candidates")
    request = KnowledgeSearchInput(
        org="acme",
        namespace="team",
        namespaces=["team"],
        namespace_scope="this",
        query="document",
        top_k=3,
        collapse_sections=False,
        narrow_spans=provider == "spans",
    )
    if boundary == "provider":
        late = await app.handle_knowledge_search(request)
    else:
        checked = await app.checked_knowledge_search(request)
        state["allowed"] = False
        late, concurrent = await asyncio.gather(checked.current(), checked.current())
        assert late is concurrent and await checked.current() is late
    state["allowed"] = state["revoke"] = False
    initial = await app.handle_knowledge_search(request)
    assert late.model_dump() == initial.model_dump()
    assert {hit.source_uri for hit in late.results} == {"ordinary:0", "ordinary:1"}
    assert not late.reranked_by
