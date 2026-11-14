"""knowledge.move's answers that need no store: where the caller cannot write, the same
place, a synced document, a held source, an earlier move's copy, an erasure, and one
source failing among others."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.core.leases import ClaimExpired
from server.core.object_store import ObjectStoreError
from server.knowledge.api.models_knowledge import KnowledgeMoveInput
from server.knowledge.app import move
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.knowledge.store import blobs
from server.knowledge.store import documents as documents_repo
from server.knowledge.store.documents import DocumentRow
from server.knowledge.store.pg_documents import DocumentErased, HeadMoved

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
        # The erasure ledger: the call's first read, and every read after a copy.
        "ledger": [],
        "ledger_later": [],
        "ledger_reads": 0,
        "fail": {},
        "copied": [],
        "deleted": [],
        "taken_back": [],
        "audited": [],
        "originals": [],
        "tables": {"src": [], "dst": []},
        "head_moved": False,
        "on_claim": None,
        # Live chunks of the revision, by namespace: equal, a copy there is whole.
        "chunks": {"src": 3, "dst": 3},
        # What happened, in order: claims taken and given up, take-backs started and done.
        "events": [],
    }

    async def get_one(org: str, ns: str, uri: str, *, include_superseded: bool = False) -> Any:
        doc = state[ns].get(uri)
        if doc is not None and doc.superseded_at is not None and not include_superseded:
            return None
        return doc

    async def get_many(org: str, ns: str, uris: list[str]) -> list[DocumentRow]:
        return [state[ns][u] for u in uris if u in state[ns]]

    @asynccontextmanager
    async def claim(org: str, ns: str, uri: str, *, wait_s: float = 10):
        if state["on_claim"] is not None:
            state.pop("on_claim")()
            state["on_claim"] = None
        state["events"].append(f"claim {ns}")
        try:
            yield state["held"]
        finally:
            state["events"].append(f"release {ns}")

    async def count(org: str, ns: str, uri: str, *, revision: int, upto_version: int) -> int:
        return state["chunks"][ns]

    async def no(*args: Any) -> bool:
        return False

    async def now() -> int:
        return STARTED

    async def ledger(org: str, since: int, margin: int) -> list[dict[str, Any]]:
        state["ledger_reads"] += 1
        return state["ledger"] if state["ledger_reads"] == 1 else state["ledger_later"]

    async def originals(doc: DocumentRow, dst: str) -> dict[str, Any]:
        state["originals"].append(doc.source_uri)
        return {}

    async def copy(doc: DocumentRow, dst: str, there: Any, originals: Any) -> move._Copy:
        failure = state["fail"].get(doc.source_uri)
        if failure is not None:
            raise failure
        state["copied"].append(doc.source_uri)
        return move._Copy(doc, dst, 99, [], ["m"])

    async def delete(doc: DocumentRow) -> None:
        state["deleted"].append(doc.source_uri)

    async def head_is(doc: DocumentRow) -> bool:
        return not state["head_moved"]

    async def tables(org: str, ns: str, uri: str, version: int) -> list[dict[str, Any]]:
        return state["tables"][ns]

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
    monkeypatch.setattr(move, "_head_is", head_is)
    monkeypatch.setattr(move.chunks_repo, "count_revision", count)
    monkeypatch.setattr(move.datasets_repo, "at_version", tables)
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
    store["ledger"] = [_erasure("namespace", "src")]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["copied"] == [] and store["deleted"] == []


async def test_an_erasure_registered_while_it_copied_takes_the_copy_back_before_the_delete(
    store: dict[str, Any],
) -> None:
    store["ledger_later"] = [_erasure("namespace", "dst")]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["taken_back"] == [URI]
    # Where it was, it stays: an erased answer never leaves it deleted there.
    assert store["deleted"] == [] and store["audited"] == []


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


async def test_a_failure_of_the_request_answers_as_itself_after_the_others_moved(
    store: dict[str, Any],
) -> None:
    uris = ["doc://1", "doc://2", "doc://3"]
    store["src"] = {u: _doc("src", u) for u in uris}
    store["fail"] = {"doc://2": RuntimeError("the store failed")}
    # An erasure of the place registered while they copied: each copy that landed is
    # checked before its delete, the one that failed changes nothing of that.
    store["ledger_later"] = [_erasure("namespace", "dst")]
    with pytest.raises(RuntimeError, match="the store failed"):
        await _run(uris=uris)
    assert sorted(store["taken_back"]) == ["doc://1", "doc://3"]
    assert store["deleted"] == []


async def test_the_moves_that_finished_before_a_failure_are_audited(
    store: dict[str, Any],
) -> None:
    uris = ["doc://1", "doc://2"]
    store["src"] = {u: _doc("src", u) for u in uris}
    store["fail"] = {"doc://2": RuntimeError("the store failed")}
    with pytest.raises(RuntimeError):
        await _run(uris=uris)
    assert store["deleted"] == ["doc://1"]
    [row] = store["audited"]
    assert row["subject"]["source_uris"] == ["doc://1"]


async def test_an_erasure_meanwhile_under_postgres_answers_erased(store: dict[str, Any]) -> None:
    store["fail"] = {URI: DocumentErased()}
    assert _answers(await _run()) == [(URI, False, "erased")]


async def test_an_original_the_object_store_refused_is_busy(store: dict[str, Any]) -> None:
    store["fail"] = {URI: ObjectStoreError("the bucket is down", operation="write")}
    assert _answers(await _run()) == [(URI, False, "busy")]


async def test_an_open_ingest_job_of_the_source_is_busy(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def open_job(org: str, ns: str, uri: str) -> bool:
        return ns == "src"

    monkeypatch.setattr(move.ingest_jobs, "open_for_source", open_job)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["copied"] == []


@pytest.mark.parametrize("where", ["src", "dst"])
async def test_a_publication_that_may_still_land_is_busy(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    async def may_land(org: str, ns: str, uri: str) -> bool:
        return ns == where

    monkeypatch.setattr(move.documents_repo, "publication_may_land", may_land)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["copied"] == []


async def test_a_source_written_since_it_was_read_is_busy(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def text_write() -> None:  # after the read, before the claims
        store["src"][URI] = _doc("src", version=11)

    store["on_claim"] = text_write
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
    store["head_moved"] = True
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["taken_back"] == [URI] and store["deleted"] == []


async def test_a_delete_refused_under_postgres_takes_the_copy_back(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(doc: DocumentRow) -> None:
        raise HeadMoved(doc.source_uri)

    monkeypatch.setattr(move, "_delete_source", refused)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["taken_back"] == [URI]


async def test_a_delete_that_fails_after_its_tombstone_leaves_the_copy(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def after_tombstone(doc: DocumentRow) -> None:
        raise RuntimeError("forget_published failed")

    monkeypatch.setattr(move, "_delete_source", after_tombstone)
    with pytest.raises(RuntimeError):
        await _run()
    # The source may be deleted already: the copy stays, and a retry only deletes.
    assert store["copied"] == [URI] and store["taken_back"] == []


async def test_no_original_is_stored_for_a_document_already_there(store: dict[str, Any]) -> None:
    store["dst"][URI] = _doc("dst", content_sha256="other")
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert store["originals"] == []


@pytest.mark.parametrize(
    "edit",
    [
        {"title": "B"},
        {"description": "new"},
        {"tags": ["x"]},
        {"metadata": '{"k": 1}'},
    ],
)
async def test_a_copy_edited_on_either_side_is_another_document(
    store: dict[str, Any], edit: dict[str, Any]
) -> None:
    # A cut-off move's copy, then an edit of the source: deleting it would lose the edit.
    store["dst"][URI] = _doc("dst", version=20)
    store["src"][URI] = _doc("src", **edit)
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert store["deleted"] == []


async def test_a_table_approved_since_the_copy_makes_it_another_document(
    store: dict[str, Any],
) -> None:
    store["dst"][URI] = _doc("dst", version=20)
    table = {"dataset_id": "t", "schema": "[]", "schema_state": "draft", "clarifications": "[]"}
    store["tables"]["dst"] = [{**table, "row_count": 2}]
    store["tables"]["src"] = [{**table, "row_count": 2, "schema_state": "approved"}]
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert store["deleted"] == []


async def test_an_ingest_job_open_where_it_goes_is_busy(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def open_job(org: str, ns: str, uri: str) -> bool:
        return ns == "dst"

    monkeypatch.setattr(move.ingest_jobs, "open_for_source", open_job)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["copied"] == []


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


@pytest.fixture
def originals(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An original held as a ClickHouse row in `src` whose copy does not land, and what
    `locate` answers after that; what was stored again."""
    state: dict[str, Any] = {"after": None, "put": [], "located": 0}

    async def locate(org: str, ns: str, sha: str) -> Any:
        state["located"] += 1
        return blobs.Stored(5, "") if state["located"] == 1 else state["after"]

    async def copy_held(*args: Any, **kwargs: Any) -> bool:
        return False

    async def read(org: str, ns: str, sha: str, *, stored: Any) -> bytes:
        assert stored.location
        return b"bytes"

    async def put(org: str, ns: str, data: bytes, *, uploader: str) -> str:
        state["put"].append((ns, data))
        return "sha"

    for name, fake in (("locate", locate), ("copy_held", copy_held), ("read", read), ("put", put)):
        monkeypatch.setattr(move.blobs, name, fake)
    return state


async def test_an_original_moved_to_the_bucket_meanwhile_is_read_from_there(
    originals: dict[str, Any],
) -> None:
    originals["after"] = blobs.Stored(5, "s3://bucket/key")
    assert await move._store_originals(_doc("src", blob_path="sha"), "dst") == {}
    assert originals["put"] == [("dst", b"bytes")]


async def test_an_original_swept_meanwhile_is_cleared_from_the_copy(
    originals: dict[str, Any],
) -> None:
    cleared = await move._store_originals(_doc("src", blob_path="sha"), "dst")
    assert cleared == documents_repo.NO_ORIGINAL and originals["put"] == []


async def test_a_copy_there_with_chunks_missing_is_copied_again_before_the_delete(
    store: dict[str, Any],
) -> None:
    # A cut-off row insert landed after its take-back: the row is there, its chunks are not.
    store["dst"][URI] = _doc("dst", version=20)
    store["chunks"]["dst"] = 0
    assert _answers(await _run()) == [(URI, True, "")]
    assert store["copied"] == [URI] and store["deleted"] == [URI]
    assert store["originals"] == [URI]


async def test_a_copy_there_with_chunks_missing_stays_where_it_was_if_the_copy_fails(
    store: dict[str, Any],
) -> None:
    store["dst"][URI] = _doc("dst", version=20)
    store["chunks"]["dst"] = 0
    store["fail"] = {URI: ClaimExpired("cut off")}
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert store["deleted"] == []


@pytest.fixture
def real_copy(store: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`store` with the move's own copy and take-back, over fake statements: the chunk copy
    waits on `store["copy_gate"]` and the take-back on `store["take_back_gate"]`."""
    store["copy_gate"] = asyncio.Event()
    store["take_back_gate"] = asyncio.Event()
    store["take_back_gate"].set()
    real_take_back = move._take_back

    async def copy_revision(*args: Any, **kwargs: Any) -> bool:
        store["events"].append("copying")
        await store["copy_gate"].wait()
        return True

    async def take_back(copy: move._Copy) -> None:
        store["events"].append("taking back")
        await store["take_back_gate"].wait()
        await real_take_back(copy)
        store["events"].append("taken back")

    async def nothing(*args: Any, **kwargs: Any) -> Any:
        return None

    async def models(*args: Any) -> list[str]:
        return ["m"]

    monkeypatch.setattr(move, "_copy", _REAL_COPY)
    monkeypatch.setattr(move, "_take_back", take_back)
    monkeypatch.setattr(move.chunks_repo, "copy_revision", copy_revision)
    monkeypatch.setattr(move.chunks_repo, "embedding_models", models)
    for repo, names in (
        (move.chunks_repo, ("retire_versions", "delete_versions")),
        (move.documents_repo, ("insert_one", "drop_committed")),
        (move.restore_repo, ("delete_row",)),
        (move.datasets_repo, ("forget", "forget_snapshot", "insert_catalog")),
    ):
        for name in names:
            monkeypatch.setattr(repo, name, nothing)
    return store


_REAL_COPY = move._copy


async def _running_move() -> asyncio.Task[Any]:
    task = asyncio.create_task(_run())
    for _ in range(50):
        await asyncio.sleep(0)
    return task


async def test_a_cancel_during_the_chunk_copy_takes_the_copy_back_under_the_claims(
    real_copy: dict[str, Any],
) -> None:
    task = await _running_move()
    assert real_copy["events"][-1] == "copying"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert real_copy["events"][-4:] == [
        "taking back",
        "taken back",
        "release src",
        "release dst",
    ]
    assert real_copy["deleted"] == []


async def test_a_claim_cut_off_during_the_copy_takes_it_back_and_answers_busy(
    real_copy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events = real_copy["events"]

    @asynccontextmanager
    async def cut_off_claim(org: str, ns: str, uri: str, *, wait_s: float = 10):
        # As `leases.holding`: the block is cut off at its share of the TTL.
        events.append(f"claim {ns}")
        try:
            async with asyncio.timeout(0.05):
                yield True
        except TimeoutError as e:
            raise ClaimExpired("cut off") from e
        finally:
            events.append(f"release {ns}")

    monkeypatch.setattr(move.ingest_run, "claim_source", cut_off_claim)
    assert _answers(await _run()) == [(URI, False, "busy")]
    assert "taken back" in events
    assert events.index("taken back") < min(
        i for i, e in enumerate(events) if e.startswith("release")
    )
    assert real_copy["deleted"] == []


async def test_a_second_cancel_during_the_take_back_still_waits_for_it(
    real_copy: dict[str, Any],
) -> None:
    real_copy["take_back_gate"].clear()
    task = await _running_move()
    task.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    assert real_copy["events"][-1] == "taking back"
    task.cancel()  # the request is cancelled again while the take-back runs
    for _ in range(20):
        await asyncio.sleep(0)
    # The claims are still held: a retry cannot find the copy half removed.
    assert not any(e.startswith("release") for e in real_copy["events"]) and not task.done()
    real_copy["take_back_gate"].set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert real_copy["events"][-3:] == ["taken back", "release src", "release dst"]
