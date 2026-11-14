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
        # Whether an erasure may reach each document, at its reads in order (before its
        # original is stored, then under the claims); past the list, its last entry.
        "erasing": [],
        "erasing_reads": {},
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
        "forgotten": [],
        "blobs_deleted": False,
        # When the source's text was written where it is, by its chunks; None: none.
        "arrived": None,
        "forget_wait_s": 0.0,
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

    async def erasing(doc: DocumentRow, arrived: int, dst: str, started: int) -> bool:
        n = state["erasing_reads"].get(doc.source_uri, 0)
        state["erasing_reads"][doc.source_uri] = n + 1
        seq = state["erasing"]
        return bool(seq[min(n, len(seq) - 1)]) if seq else False

    async def originals(doc: DocumentRow, dst: str) -> dict[str, Any]:
        state["originals"].append(doc.source_uri)
        return {}

    async def deleted_blob(*args: Any, **kwargs: Any) -> None:
        state["blobs_deleted"] = True

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

    async def forget(org: str, ns: str, through: dict[str, int]) -> None:
        await asyncio.sleep(state["forget_wait_s"])
        state["forgotten"].extend(through)

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
    monkeypatch.setattr(move, "_erasing", erasing)
    monkeypatch.setattr(move, "_store_originals", originals)
    monkeypatch.setattr(move.blobs, "delete", deleted_blob)
    monkeypatch.setattr(move, "_copy", copy)
    monkeypatch.setattr(move, "_delete_source", delete)
    monkeypatch.setattr(move, "_head_is", head_is)
    monkeypatch.setattr(move.chunks_repo, "count_revision", count)

    async def arrived(org: str, ns: str, uri: str, *, revision: int, upto_version: int) -> Any:
        return state["arrived"]

    monkeypatch.setattr(move.chunks_repo, "first_written_s", arrived)
    monkeypatch.setattr(move.datasets_repo, "forget_published", forget)
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
    store["erasing"] = [True]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["copied"] == [] and store["deleted"] == []


async def test_an_erasure_registered_while_it_copied_takes_the_copy_back_before_the_delete(
    store: dict[str, Any],
) -> None:
    store["erasing"] = [False, True]
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
    store["erasing"] = [False, True]
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
        raise RuntimeError("the delete failed after its tombstone")

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


async def _reads_of_erasing(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, list[dict[str, Any]]]
) -> tuple[bool, list[tuple[str, str, int]]]:
    """`move._erasing` over a ledger answering `rows` by namespace; what it read."""
    reads: list[tuple[str, str, int]] = []

    async def reaching(org: str, ns: str, user: str, since: int, margin: int) -> Any:
        reads.append((ns, user, since))
        return rows.get(ns, [])

    monkeypatch.setattr(move.ledger_reads, "unfinished_reaching", reaching)
    reached = await move._erasing(_doc("src"), STARTED - 3600, "dst", STARTED)
    return reached, reads


async def test_erasures_are_read_where_it_is_since_it_arrived_and_where_it_goes_since_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A moved document keeps its ingest time; the source side is read from when its text
    # arrived there, so an erasure that finished before then is not one that reaches it.
    reached, reads = await _reads_of_erasing(monkeypatch, {})
    assert reached is False
    assert reads == [("src", "u1", STARTED - 3600), ("dst", "u1", STARTED)]


@pytest.mark.parametrize(
    ("rows", "reached"),
    [
        ({"src": [_erasure("namespace", "src")]}, True),
        ({"dst": [_erasure("user_id", "u1")]}, True),
        ({"src": [_erasure("namespace_list", '["src"]')]}, True),
        # A list the ledger answers that names neither place does not reach it.
        ({"src": [_erasure("namespace_list", '["other"]')]}, False),
    ],
)
async def test_which_erasures_reach_a_document(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, list[dict[str, Any]]], reached: bool
) -> None:
    assert (await _reads_of_erasing(monkeypatch, rows))[0] is reached


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


async def test_a_copy_there_with_its_tables_gone_is_copied_again(store: dict[str, Any]) -> None:
    # A take-back removes every table; a cut-off row insert that landed after it has none.
    store["dst"][URI] = _doc("dst", version=20)
    table = {"dataset_id": "t", "schema": "[]", "schema_state": "draft", "clarifications": "[]"}
    store["tables"]["src"] = [{**table, "row_count": 2}]
    assert _answers(await _run()) == [(URI, True, "")]
    assert store["copied"] == [URI] and store["deleted"] == [URI]


async def test_an_erasure_registered_before_the_claims_leaves_an_earlier_copy_and_the_source(
    store: dict[str, Any],
) -> None:
    # An earlier move's whole copy is there; an erasure of the target registers after the
    # document's first read. Deleting the source now could leave it in neither place.
    store["dst"][URI] = _doc("dst", version=20)
    store["erasing"] = [False, True]  # clear before the claims, reached under them
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["deleted"] == [] and store["taken_back"] == [] and store["copied"] == []


async def test_a_table_cleanup_past_the_claims_cut_off_still_counts_the_move(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def cut_off_claim(org: str, ns: str, uri: str, *, wait_s: float = 10):
        # As `leases.holding`: the block is cut off at its share of the TTL.
        try:
            async with asyncio.timeout(0.05):
                yield True
        except TimeoutError as e:
            raise ClaimExpired("cut off") from e

    monkeypatch.setattr(move.ingest_run, "claim_source", cut_off_claim)
    store["forget_wait_s"] = 0.2  # longer than the claims last
    out = await _run()
    assert _answers(out) == [(URI, True, "")]
    assert store["deleted"] == [URI] and store["forgotten"] == [URI]
    assert store["audited"][0]["subject"]["source_uris"] == [URI]


async def test_a_failed_table_cleanup_after_the_tombstone_counts_the_move(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed(org: str, ns: str, through: dict[str, int]) -> None:
        raise RuntimeError("the datasets delete failed")

    monkeypatch.setattr(move.datasets_repo, "forget_published", failed)
    assert _answers(await _run()) == [(URI, True, "")]
    assert store["audited"][0]["subject"]["source_uris"] == [URI]


async def test_a_retry_after_a_delete_cut_off_between_its_retirement_and_tombstone(
    store: dict[str, Any],
) -> None:
    # The first move copied the document whole, then its delete retired the source's
    # chunks and was cut off before the tombstone: the source row is live with none.
    store["dst"][URI] = _doc("dst", version=20)
    store["chunks"] = {"src": 0, "dst": 3}
    assert _answers(await _run()) == [(URI, True, "")]
    # Only the delete is left: the copy is not written again over what it serves.
    assert store["copied"] == [] and store["deleted"] == [URI]


async def test_a_copy_is_never_written_over_one_that_serves_more(
    real_copy: dict[str, Any],
) -> None:
    # Its tables are gone, but it serves more chunks than the source now does: writing
    # over it would serve less, so it answers as another document, and nothing is written.
    real_copy["dst"][URI] = _doc("dst", version=20)
    real_copy["chunks"] = {"src": 0, "dst": 3}
    table = {"dataset_id": "t", "schema": "[]", "schema_state": "draft", "clarifications": "[]"}
    real_copy["tables"]["src"] = [{**table, "row_count": 2}]
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert "copying" not in real_copy["events"] and real_copy["deleted"] == []
    assert real_copy["originals"] == []


async def test_an_erasure_registered_before_the_original_is_stored_stores_none(
    store: dict[str, Any],
) -> None:
    store["erasing"] = [True]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["originals"] == [] and store["copied"] == []


async def test_an_erased_move_deletes_no_original(store: dict[str, Any]) -> None:
    # Keyed by its bytes, an original stored for the copy may be the one a concurrent
    # write of the same bytes, its row still to land, needs: it is left to the sweep.
    store["erasing"] = [False, True]
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["originals"] == [URI] and store["blobs_deleted"] is False


async def test_a_copy_that_comes_to_serve_more_under_the_claims_answers_exists(
    real_copy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read as incomplete before the claims; by the time they are held it serves more.
    real_copy["dst"][URI] = _doc("dst", version=20)
    real_copy["chunks"] = {"src": 3, "dst": 0}

    def rewritten() -> None:
        real_copy["chunks"]["dst"] = 5

    real_copy["on_claim"] = rewritten
    assert _answers(await _run()) == [(URI, False, "exists")]
    assert "copying" not in real_copy["events"] and real_copy["deleted"] == []


async def test_a_cancel_after_one_source_moved_still_audits_it(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    uris = ["doc://1", "doc://2"]
    store["src"] = {u: _doc("src", u) for u in uris}
    gate = asyncio.Event()

    async def copy(doc: DocumentRow, dst: str, there: Any, originals: Any) -> move._Copy:
        if doc.source_uri == "doc://2":
            await gate.wait()  # still copying when the request is cancelled
        return move._Copy(doc, dst, 99, [], ["m"])

    monkeypatch.setattr(move, "_copy", copy)
    task = asyncio.create_task(_run(uris=uris))
    for _ in range(100):
        await asyncio.sleep(0)
    assert store["deleted"] == ["doc://1"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = store["audited"]
    assert row["subject"]["source_uris"] == ["doc://1"]
    assert store["forgotten"] == ["doc://1"]


async def test_a_namespace_given_as_a_dash_or_blank_is_refused(store: dict[str, Any]) -> None:
    for name in ("-", "  "):
        with pytest.raises(ValidationError):
            await _run(to=name)


async def test_a_cancel_during_the_table_cleanup_still_audits_the_move(
    store: dict[str, Any],
) -> None:
    store["forget_wait_s"] = 0.2
    task = asyncio.create_task(_run())
    for _ in range(100):
        await asyncio.sleep(0)
    assert store["deleted"] == [URI] and store["forgotten"] == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The cleanup ran to its end, and the move is audited.
    assert store["forgotten"] == [URI]
    assert store["audited"][0]["subject"]["source_uris"] == [URI]


async def test_a_cancel_while_the_claims_are_given_up_still_audits_the_move(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def cancelled_on_release(org: str, ns: str, uri: str, *, wait_s: float = 10):
        try:
            yield True
        finally:
            if ns == "src":  # the request's cancel lands as this claim is given up
                raise asyncio.CancelledError

    monkeypatch.setattr(move.ingest_run, "claim_source", cancelled_on_release)
    with pytest.raises(asyncio.CancelledError):
        await _run()
    assert store["deleted"] == [URI] and store["forgotten"] == [URI]
    assert store["audited"][0]["subject"]["source_uris"] == [URI]


async def test_me_from_an_end_user_moves_into_their_own_collection(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from server.core.principals import personal_name

    mine = personal_name(ORG, "user:u1")
    store[mine] = {}
    monkeypatch.setattr(move, "current_auth", lambda: SimpleNamespace(subject="user:u1"))
    out = await _run(to="@me")
    assert out.to_namespace == mine
    assert _answers(out) == [(URI, True, "")]
    assert store["audited"][0]["namespace"] == mine


async def test_a_cancel_while_the_tombstone_is_in_flight_counts_the_move(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()

    async def slow_delete(doc: DocumentRow) -> None:
        await gate.wait()  # the tombstone insert, still to answer
        store["deleted"].append(doc.source_uri)

    monkeypatch.setattr(move, "_delete_source", slow_delete)
    task = asyncio.create_task(_run())
    for _ in range(100):
        await asyncio.sleep(0)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()  # the delete is waited for
    gate.set()  # and lands
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store["deleted"] == [URI] and store["forgotten"] == [URI]
    assert store["audited"][0]["subject"]["source_uris"] == [URI]


async def test_a_claim_cut_off_while_the_tombstone_is_in_flight_answers_moved(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def cut_off_claim(org: str, ns: str, uri: str, *, wait_s: float = 10):
        try:
            async with asyncio.timeout(0.05):
                yield True
        except TimeoutError as e:
            raise ClaimExpired("cut off") from e

    async def slow_delete(doc: DocumentRow) -> None:
        await asyncio.sleep(0.2)  # past the claims' cut-off
        store["deleted"].append(doc.source_uri)

    monkeypatch.setattr(move.ingest_run, "claim_source", cut_off_claim)
    monkeypatch.setattr(move, "_delete_source", slow_delete)
    assert _answers(await _run()) == [(URI, True, "")]
    assert store["forgotten"] == [URI]
    assert store["audited"][0]["subject"]["source_uris"] == [URI]


async def test_a_delete_refused_under_postgres_counts_no_move(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(doc: DocumentRow) -> None:
        raise DocumentErased()

    monkeypatch.setattr(move, "_delete_source", refused)
    assert _answers(await _run()) == [(URI, False, "erased")]
    assert store["taken_back"] == [URI] and store["audited"] == [] and store["forgotten"] == []
