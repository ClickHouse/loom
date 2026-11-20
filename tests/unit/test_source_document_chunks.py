"""Document chunks retain embedding stubs and invalidate a failed or expired outline."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import source_documents as app
from server.knowledge.app import source_search
from server.knowledge.app.source_access import Admitted

NS = "acl-" + "1" * 24
URI = "gdrive:file#11111111-1111-4111-8111-111111111111"


@pytest.fixture
def world(monkeypatch):
    state = {"size": 2, "stream_error": False, "expired": False}
    rows = [
        {"namespace": NS, "source_uri": URI, "chunk_idx": i, "doc_version": 10, "revision": 0}
        for i in range(129)
    ]
    row = SimpleNamespace(org="acme", namespace=NS, source_uri=URI, version=10, revision=0)
    doc = Admitted("team", {"namespace": NS, "source_uri": URI}, {"version": 10, "revision": 0})
    checked = app.CheckedDocument(doc, row)
    plan = SimpleNamespace(
        org="acme", failed=False, unread=False, current=lambda _item: not state["expired"]
    )

    async def check(candidates, *, chunks=False):
        return [Admitted("team", c, {"version": 10, "revision": 0}) for c in candidates]

    @asynccontextmanager
    async def candidates(org, namespace, uri, model):
        assert (org, namespace, uri, model) == ("acme", NS, URI, "fixture")

        async def stream():
            for selected in rows[: state["size"]]:
                yield selected
            if state["stream_error"]:
                raise RuntimeError("private stream failure")

        yield stream()

    async def chunks(org, model, keys, *, embedded_only=True):
        assert not embedded_only, "Outline text includes chunks awaiting an embedding"
        return [
            {
                "namespace": ns,
                "source_uri": uri,
                "chunk_idx": idx,
                "doc_version": version,
                "revision": revision,
                "content": "stub" if idx == 1 else "embedded",
                "chunk_metadata": "{}",
            }
            for ns, uri, idx, version, revision in keys
        ]

    plan.check = AsyncMock(side_effect=check)
    monkeypatch.setattr(app.store, "chunk_candidates", candidates, raising=False)
    monkeypatch.setattr(source_search.store, "chunks", AsyncMock(side_effect=chunks))
    return plan, checked, rows, state


async def test_outline_hydrates_embedded_and_stub_sections_in_document_order(world):
    plan, checked, _rows, _state = world
    result = await app.chunks(plan, checked, model="fixture")
    assert [r.row["content"] for r in result] == ["embedded", "stub"]
    assert [r.admitted.candidate["chunk_idx"] for r in result] == [0, 1]
    assert not plan.failed and not plan.unread


async def test_empty_document_has_a_valid_empty_outline(world):
    plan, checked, _rows, state = world
    state["size"] = 0
    assert await app.chunks(plan, checked, model="fixture") == []
    source_search.store.chunks.assert_not_awaited()


async def test_late_stream_error_discards_already_hydrated_chunks_without_unread(world):
    plan, checked, _rows, state = world
    state.update(size=65, stream_error=True)
    assert await app.chunks(plan, checked, model="fixture") is None
    assert source_search.store.chunks.await_count >= 1
    assert plan.failed and not plan.unread


@pytest.mark.parametrize("change", ["denied", "version", "revision", "expired"])
async def test_final_document_permission_and_identity_are_required(world, change):
    plan, checked, _rows, state = world
    original = plan.check.side_effect

    async def final(candidates, *, chunks=False):
        out = await original(candidates, chunks=chunks)
        if not chunks:
            if change == "denied":
                return []
            if change == "expired":
                state["expired"] = True
            else:
                out[0].document[change] += 1
        return out

    plan.check.side_effect = final
    assert await app.chunks(plan, checked, model="fixture") is None
    assert not plan.unread


async def test_final_expiry_drops_early_batches_too(world):
    plan, checked, _rows, state = world
    state["size"] = 129
    original = plan.check.side_effect

    async def final(candidates, *, chunks=False):
        out = await original(candidates, chunks=chunks)
        if not chunks:
            state["expired"] = True
        return out

    plan.check.side_effect = final
    assert await app.chunks(plan, checked, model="fixture") is None


async def test_unexpected_chunk_source_invalidates_the_whole_copy(world):
    plan, checked, rows, _state = world
    rows[1]["source_uri"] = "different-source"
    assert await app.chunks(plan, checked, model="fixture") is None
    assert plan.failed and not plan.unread
