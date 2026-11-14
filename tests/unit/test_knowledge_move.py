"""knowledge.move's answers that need no store: where the caller cannot write, the same
place, a synced document, a held source, an earlier move's copy, an erasure, and one
source failing among others."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.core.leases import ClaimExpired
from server.knowledge.api.models_knowledge import KnowledgeMoveInput
from server.knowledge.app import move
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.knowledge.store.documents import DocumentRow
from server.knowledge.store.pg_documents import HeadMoved

ORG = "acme"
URI = "doc://a.md"
STARTED = 1_800_000_000


def _doc(namespace: str, uri: str = URI, **over: Any) -> DocumentRow:
    fields: dict[str, Any] = {
        "org": ORG,
        "namespace": namespace,
        "source_uri": uri,
        "doc_id": "d1",
        "title": "A",
        "content": "text",
        "content_sha256": "sha",
        "version": 10,
        "user_id": "u1",
        "ingested_at": datetime(2026, 1, 1),
        **over,
    }
    return DocumentRow(**fields)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Live documents in `src` (one by default) and nothing in `dst`; what the move did."""
    state: dict[str, Any] = {
        "src": {URI: _doc("src")},
        "dst": {},
        "held": True,
        "reach": True,
        "ledger": [[], []],
        "fail": {},
        "copied": [],
        "deleted": [],
        "taken_back": [],
        "audited": [],
    }

    async def get_one(org: str, ns: str, uri: str, *, include_superseded: bool = False) -> Any:
        doc = state[ns].get(uri)
        if doc is not None and doc.superseded_at is not None and not include_superseded:
            return None
        return doc

    async def get_many(org: str, ns: str, uris: list[str]) -> list[DocumentRow]:
        return [state[ns][u] for u in uris if u in state[ns]]

    @asynccontextmanager
    async def claim(org: str, ns: str, uri: str):
        yield state["held"]

    async def no(*args: Any) -> bool:
        return False

    async def now() -> int:
        return STARTED

    async def ledger(org: str, since: int, margin: int) -> list[dict[str, Any]]:
        return state["ledger"].pop(0)

    async def originals(doc: DocumentRow, dst: str) -> dict[str, Any]:
        return {}

    async def copy(doc: DocumentRow, dst: str, there: Any, originals: Any) -> move._Copy:
        failure = state["fail"].get(doc.source_uri)
        if failure is not None:
            raise failure
        state["copied"].append(doc.source_uri)
        return move._Copy(doc, dst, 99, [])

    async def delete(doc: DocumentRow) -> bool:
        state["deleted"].append(doc.source_uri)
        return True

    async def take_back(copy: move._Copy) -> None:
        state["taken_back"].append(copy.doc.source_uri)

    async def reach(org: str, ns: str, role: Any) -> bool:
        return state["reach"]

    async def emit(**row: Any) -> str:
        state["audited"].append(row)
        return ""

    monkeypatch.setattr(move.documents_repo, "get_one", get_one)
    monkeypatch.setattr(move.documents_repo, "get_many_by_uris", get_many)
    monkeypatch.setattr(move.documents_repo, "publication_may_land", no)
    monkeypatch.setattr(move.ingest_jobs, "open_for_source", no)
    monkeypatch.setattr(move.ingest_run, "claim_source", claim)
    monkeypatch.setattr(move.blobs, "ch_now_s", now)
    monkeypatch.setattr(move.ledger_reads, "unfinished_since", ledger)
    monkeypatch.setattr(move, "_store_originals", originals)
    monkeypatch.setattr(move, "_copy", copy)
    monkeypatch.setattr(move, "_delete_source", delete)
    monkeypatch.setattr(move, "_take_back", take_back)
    monkeypatch.setattr("server.auth.authz.row_in_scope", reach)
    monkeypatch.setattr("server.observability.audit.emit", emit)
    return state


async def _run(to: str = "dst", uris: list[str] | None = None) -> Any:
    return await move.handle_knowledge_move(
        KnowledgeMoveInput(org=ORG, namespace="src", to_namespace=to, source_uris=uris or [URI])
    )


def _answers(out: Any) -> list[tuple[str, bool, str]]:
    return [(r.source_uri, r.moved, r.reason) for r in out.results]


async def test_moves_deletes_where_it_was_and_audits_where_it_went(
    store: dict[str, Any],
) -> None:
    out = await _run(uris=[URI, URI])
    assert out.found and out.to_namespace == "dst"
    assert _answers(out) == [(URI, True, "")]
    assert store["copied"] == [URI] and store["deleted"] == [URI]
    [row] = store["audited"]
    assert (row["namespace"], row["subject"]) == (
        "dst",
        {"from_namespace": "src", "source_uris": [URI]},
    )


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
    store["src"][URI] = _doc("src", actor=SYNC_WORKER_ACTOR)
    assert _answers(await _run()) == [(URI, False, "synced")]
    assert store["deleted"] == []


async def test_a_held_source_is_busy(store: dict[str, Any]) -> None:
    store["held"] = False
    assert _answers(await _run()) == [(URI, False, "busy")]


async def test_another_live_document_there_is_kept(store: dict[str, Any]) -> None:
    store["dst"][URI] = _doc("dst", content_sha256="other")
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert store["copied"] == [] and store["deleted"] == []


async def test_the_same_text_written_apart_there_is_another_document(
    store: dict[str, Any],
) -> None:
    # Same text and title, but not this document's copy: its own ingest time.
    store["dst"][URI] = _doc("dst", ingested_at=datetime(2026, 2, 1))
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert store["deleted"] == []


async def test_an_earlier_moves_copy_there_finishes_that_move(store: dict[str, Any]) -> None:
    # A move cut off after its copy: only the delete where it was is left.
    store["dst"][URI] = _doc("dst", version=20)
    assert _answers(await _run()) == [(URI, True, "")]
    assert store["copied"] == [] and store["deleted"] == [URI]


async def test_an_erasure_leaves_the_document_where_it_was(store: dict[str, Any]) -> None:
    store["ledger"][0] = [_erasure("namespace", "src")]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["copied"] == [] and store["deleted"] == []


async def test_an_erasure_registered_while_it_ran_takes_the_copy_back(
    store: dict[str, Any],
) -> None:
    store["ledger"][1] = [_erasure("namespace", "dst")]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["taken_back"] == [URI]
    assert store["audited"] == []


async def test_one_source_failing_leaves_the_others_answered(store: dict[str, Any]) -> None:
    uris = ["doc://1", "doc://2", "doc://3", "doc://4"]
    store["src"] = {u: _doc("src", u) for u in uris}
    store["fail"] = {"doc://2": ClaimExpired("cut off"), "doc://3": HeadMoved("doc://3")}
    out = await _run(uris=uris)
    assert _answers(out) == [
        ("doc://1", True, ""),
        ("doc://2", False, "busy"),
        ("doc://3", False, "busy"),
        ("doc://4", True, ""),
    ]
    assert store["deleted"] == ["doc://1", "doc://4"]


async def test_a_failure_of_the_request_answers_as_itself(store: dict[str, Any]) -> None:
    store["fail"] = {URI: RuntimeError("the store failed")}
    with pytest.raises(RuntimeError, match="the store failed"):
        await _run()


async def test_a_source_written_since_it_was_read_is_busy(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = store["src"][URI]

    async def get_many(org: str, ns: str, uris: list[str]) -> list[DocumentRow]:
        store["src"][URI] = _doc("src", version=11)  # a text write after the read
        return [read]

    monkeypatch.setattr(move.documents_repo, "get_many_by_uris", get_many)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["copied"] == [] and store["deleted"] == []


async def test_a_source_deleted_meanwhile_is_not_found_and_no_delete_runs(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = store["src"][URI]

    async def get_many(org: str, ns: str, uris: list[str]) -> list[DocumentRow]:
        store["src"][URI] = _doc("src", version=11, superseded_at=datetime(2026, 3, 1))
        return [read]

    monkeypatch.setattr(move.documents_repo, "get_many_by_uris", get_many)
    assert _answers(await _run()) == [(URI, False, "not_found")]
    assert store["deleted"] == []


async def test_a_copy_whose_source_moved_before_its_delete_is_taken_back(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def moved(doc: DocumentRow) -> bool:
        return False

    monkeypatch.setattr(move, "_delete_source", moved)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["taken_back"] == [URI]


def _erasure(scope: str, value: str, **over: Any) -> dict[str, Any]:
    return {"scope_type": scope, "scope_value": value, "state": "in_progress", **over}


@pytest.mark.parametrize(
    ("row", "reaches"),
    [
        (_erasure("namespace", "src"), True),
        (_erasure("namespace", "other"), False),
        (_erasure("user_id", "u1"), True),
        (_erasure("user_id", "u2"), False),
        (_erasure("namespace_list", '["src"]'), True),
        # Finished before the document was written: it cannot have reached it.
        (_erasure("user_id", "u1", state="completed", completed_at=datetime(2025, 1, 1)), False),
        (_erasure("user_id", "u1", state="completed", completed_at=datetime(2026, 6, 1)), True),
    ],
)
def test_which_erasures_reach_a_document(row: dict[str, Any], reaches: bool) -> None:
    doc = _doc("src")
    since = move._written_s(doc)
    assert move._erasing([row], doc, "dst", since + 10**9) is reaches
