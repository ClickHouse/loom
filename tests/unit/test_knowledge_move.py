"""knowledge.move's answers that need no store: where the caller cannot write, the same
place, a synced document, a held source, the same text already there, an erasure."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.knowledge.api.models_knowledge import KnowledgeMoveInput
from server.knowledge.app import move
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.knowledge.store.documents import DocumentRow

ORG = "acme"
URI = "doc://a.md"


def _doc(namespace: str, **over: Any) -> DocumentRow:
    fields: dict[str, Any] = {
        "org": ORG,
        "namespace": namespace,
        "source_uri": URI,
        "doc_id": "d1",
        "title": "A",
        "content": "text",
        "content_sha256": "sha",
        "version": 10,
        "ingested_at": datetime(2026, 1, 1),
        **over,
    }
    return DocumentRow(**fields)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A source `src` with one live document and nothing in `dst`; what the move wrote."""
    state: dict[str, Any] = {
        "docs": {"src": _doc("src"), "dst": None},
        "held": True,
        "reach": True,
        "erasing": False,
        "copied": [],
        "deleted": [],
    }

    async def get_one(org: str, ns: str, uri: str, *, include_superseded: bool = False) -> Any:
        doc = state["docs"].get(ns)
        if doc is not None and doc.superseded_at is not None and not include_superseded:
            return None
        return doc

    @asynccontextmanager
    async def claim(org: str, ns: str, uri: str):
        yield state["held"]

    async def no_publication(org: str, ns: str, uri: str) -> bool:
        return False

    async def copy(doc: DocumentRow, dst: str, there: Any) -> bool:
        if state["erasing"]:
            return False
        state["copied"].append((doc.namespace, dst))
        return True

    async def delete(inp: Any) -> Any:
        state["deleted"].append(inp.namespace)

    async def reach(org: str, ns: str, role: Any) -> bool:
        return state["reach"]

    monkeypatch.setattr(move.documents_repo, "get_one", get_one)
    monkeypatch.setattr(move.documents_repo, "publication_may_land", no_publication)
    monkeypatch.setattr(move.ingest_run, "claim_source", claim)
    monkeypatch.setattr(move, "_copy", copy)
    monkeypatch.setattr(move.writes, "delete_document", delete)
    monkeypatch.setattr("server.auth.authz.row_in_scope", reach)
    return state


async def _run(to: str = "dst", uris: list[str] | None = None) -> Any:
    return await move.handle_knowledge_move(
        KnowledgeMoveInput(org=ORG, namespace="src", to_namespace=to, source_uris=uris or [URI])
    )


async def test_moves_and_deletes_where_it_was(store: dict[str, Any]) -> None:
    out = await _run(uris=[URI, URI])
    assert out.found and out.to_namespace == "dst"
    assert [(r.source_uri, r.moved, r.reason) for r in out.results] == [(URI, True, "")]
    assert store["copied"] == [("src", "dst")]
    assert store["deleted"] == ["src"]


async def test_a_place_the_caller_cannot_write_moves_nothing_and_says_found_false(
    store: dict[str, Any],
) -> None:
    store["reach"] = False
    out = await _run()
    assert (out.found, out.results) == (False, [])
    assert store["copied"] == [] and store["deleted"] == []


async def test_the_same_place_is_refused(store: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        await _run(to="src")


async def test_me_from_a_caller_that_is_not_an_end_user_is_refused(store: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        await _run(to="@me")


async def test_a_synced_document_stays(store: dict[str, Any]) -> None:
    store["docs"]["src"] = _doc("src", actor=SYNC_WORKER_ACTOR)
    assert (await _run()).results[0].reason == "synced"
    assert store["deleted"] == []


async def test_a_held_source_is_busy(store: dict[str, Any]) -> None:
    store["held"] = False
    assert (await _run()).results[0].reason == "busy"


async def test_another_live_document_there_is_kept(store: dict[str, Any]) -> None:
    store["docs"]["dst"] = _doc("dst", content_sha256="other")
    assert (await _run()).results[0].reason == "exists"
    assert store["copied"] == [] and store["deleted"] == []


async def test_the_same_text_there_finishes_an_earlier_move(store: dict[str, Any]) -> None:
    # A move cut off after its copy: only the delete where it was is left.
    store["docs"]["dst"] = _doc("dst")
    assert (await _run()).results[0].moved is True
    assert store["copied"] == [] and store["deleted"] == ["src"]


async def test_an_erasure_leaves_the_document_where_it_was(store: dict[str, Any]) -> None:
    store["erasing"] = True
    assert (await _run()).results[0].reason == "erased"
    assert store["deleted"] == []


def test_revision_is_rewritten_in_each_chunk() -> None:
    meta = move._at({"chunk_metadata": '{"page": 3, "revision": 1}'}, 4)
    assert meta == '{"page": 3, "revision": 4}'
