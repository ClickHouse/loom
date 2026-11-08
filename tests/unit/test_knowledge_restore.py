"""knowledge.restore brings back what a delete retired, and nothing an erasure reached."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from server.core import shielded
from server.core.errors import BackendUnavailableError, PreconditionFailedError
from server.erasure.app.ledger_reads import LEDGER_CLOCK_MARGIN_S
from server.knowledge.api.models_knowledge import KnowledgeRestoreInput
from server.knowledge.app import restore
from server.knowledge.store.documents import DocumentRow

# The clock the handler reads (patched in `_World`): written three days before it,
# deleted one day before it, inside the restore window.
_NOW = datetime(2030, 6, 1, 12, 0, 0)
_WRITTEN = _NOW - timedelta(days=3)
_AT = _NOW - timedelta(days=1)


def _doc(**over: Any) -> DocumentRow:
    fields: dict[str, Any] = {
        "org": "acme",
        "namespace": "team",
        "source_uri": "doc://a",
        "doc_id": "d1",
        "content": "text",
        "version": 7,
        "superseded_at": _AT,
        "ingested_at": _WRITTEN,
        "revision": 3,
        "user_id": "u1",
        "blob_path": "sha",
        "blob_size": 4,
        "blob_content_type": "text/plain",
        "original_filename": "a.txt",
    }
    return DocumentRow(**{**fields, **over})


# A namespace list that purges, as the saga reads it: from its scope.
_PURGE = [
    {
        "scope_type": "namespace_list",
        "scope_value": json.dumps({"namespaces": ["team"], "purge": True}),
        "purge": True,
    }
]


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, doc: DocumentRow | None) -> None:
        self.reads = [doc]
        self.inserted: list[dict[str, Any]] = []
        self.unretired: list[tuple[list[int], datetime]] = []
        self.retired: list[int] = [41]
        self.live: list[int] = []
        self.ledger_reads = 0
        self.ledger_rows: list[dict[str, Any]] = [
            {"scope_type": "namespace", "scope_value": "team", "purge": False}
        ]
        self.ledger_args: tuple[str, int, int] | None = None
        self.erasure_after: int | None = None
        self.retired_again: list[tuple[int, datetime]] = []
        self.deleted_rows: list[int] = []
        # Raised by the first insert, the restored row's, once.
        self.insert_error: BaseException | None = None
        self.erased_chunks = False
        self.erasure = False
        self.original: object | None = object()
        self.held = True
        w = self

        async def get_one(*_a: Any, **_k: Any) -> DocumentRow | None:
            return w.reads[0] if len(w.reads) == 1 else w.reads.pop(0)

        async def insert_one(row: dict[str, Any]) -> None:
            if w.insert_error is not None:
                error, w.insert_error = w.insert_error, None
                raise error
            w.inserted.append(row)

        async def retired_by_delete(*_a: Any) -> list[int]:
            return w.retired

        async def live_versions(*_a: Any) -> list[int]:
            return w.live

        async def erased_chunks(*_a: Any) -> bool:
            return w.erased_chunks

        async def unfinished_reaching(
            org: str, _ns: str, user_id: str | None, since_s: int, margin_s: int
        ) -> list[dict[str, Any]]:
            await asyncio.sleep(0)  # a real read yields to the loop, where a cancellation lands
            w.ledger_reads += 1
            w.ledger_args = (org, since_s, margin_s)
            late = w.erasure_after is not None and w.ledger_reads > w.erasure_after
            rows = w.ledger_rows if w.erasure or late else []
            # As the statement does: a user scope only of `user_id`, unless it is None.
            return [
                r
                for r in rows
                if r["scope_type"] != "user_id" or user_id is None or r["scope_value"] == user_id
            ]

        async def mark_doc_superseded(*_a: Any, only_version: int, when: datetime) -> None:
            w.retired_again.append((only_version, when))

        async def delete_row(_o: str, _n: str, _u: str, version: int) -> None:
            w.deleted_rows.append(version)

        async def unretire(_o: str, _n: str, _u: str, vs: list[int], at: datetime) -> None:
            w.unretired.append((vs, at))

        async def locate(*_a: Any) -> object | None:
            return w.original

        async def count_active(*_a: Any) -> int:
            # Live when this call unretired them, or as a cut-off restore left them.
            return 5 if w.unretired or (w.live and not w.retired_again) else 0

        @contextlib.asynccontextmanager
        async def claim(*_a: Any) -> AsyncIterator[bool]:
            yield w.held

        monkeypatch.setattr(restore.documents_repo, "get_one", get_one)
        monkeypatch.setattr(restore.documents_repo, "insert_one", insert_one)
        monkeypatch.setattr(restore.restore_repo, "retired_by_delete", retired_by_delete)
        monkeypatch.setattr(restore.restore_repo, "erased_chunks", erased_chunks)
        monkeypatch.setattr(restore.restore_repo, "live_versions", live_versions)
        monkeypatch.setattr(restore.ledger_reads, "unfinished_reaching", unfinished_reaching)
        monkeypatch.setattr(restore, "naive_utc_now", lambda: _NOW)
        monkeypatch.setattr(restore.restore_repo, "unretire", unretire)
        monkeypatch.setattr(restore.blobs, "locate", locate)
        monkeypatch.setattr(restore.chunks_repo, "count_active_for_doc", count_active)
        monkeypatch.setattr(restore.chunks_repo, "mark_doc_superseded", mark_doc_superseded)
        monkeypatch.setattr(restore.restore_repo, "delete_row", delete_row)
        monkeypatch.setattr("server.knowledge.app.ingest_run.claim_source", claim)


def _restore(**over: Any) -> Any:
    body = {"org": "acme", "namespace": "team", "source_uri": "doc://a", **over}
    return asyncio.run(restore.handle_knowledge_restore(KnowledgeRestoreInput(**body)))


def test_a_deleted_document_and_its_chunks_come_back(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, _doc())
    out = _restore()
    assert (out.restored, out.chunks_restored, out.original) == (True, 5, "kept")
    assert (out.doc_id, out.revision) == ("d1", 3)
    assert w.unretired == [([41], _AT)]
    (row,) = w.inserted
    assert row["superseded_at"] is None and row["version"] > 7
    assert (row["doc_id"], row["revision"], row["blob_path"]) == ("d1", 3, "sha")


def test_a_document_never_written_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, None)
    out = _restore()
    assert (out.restored, out.reason) == (False, "not_found")
    assert not w.inserted and not w.unretired


def test_a_live_document_is_named_and_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, _doc(superseded_at=None, doc_id="d2", revision=4))
    out = _restore()
    assert (out.restored, out.reason, out.doc_id, out.revision) == (False, "live", "d2", 4)
    assert not w.inserted


def test_another_revision_than_the_precondition_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World(monkeypatch, _doc())
    out = _restore(if_revision=2)
    assert (out.restored, out.reason, out.revision) == (False, "revision_mismatch", 3)
    assert not w.inserted and not w.unretired
    assert _restore(if_revision=3).restored


@pytest.mark.parametrize("by", ["chunks", "request"])
def test_what_an_erasure_reached_stays_retired(monkeypatch: pytest.MonkeyPatch, by: str) -> None:
    w = _World(monkeypatch, _doc())
    if by == "chunks":
        w.erased_chunks = True
    else:
        w.erasure = True
    out = _restore()
    assert (out.restored, out.reason) == (False, "not_restorable")
    assert not w.inserted and not w.unretired


def test_a_swept_original_is_reported_and_not_named(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, _doc())
    w.original = None
    out = _restore()
    assert (out.restored, out.original) == (True, "gone")
    (row,) = w.inserted
    assert (row["blob_path"], row["blob_size"], row["original_filename"]) == ("", 0, "")


def test_a_document_whose_original_an_erasure_erased_is_not_restorable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World(monkeypatch, _doc(blob_erased_at=_AT))
    assert _restore().reason == "not_restorable"
    assert not w.inserted and not w.unretired


def test_a_restore_that_failed_after_its_chunks_is_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World(monkeypatch, _doc())
    w.retired, w.live = [], [41]
    out = _restore()
    assert (out.restored, out.chunks_restored) == (True, 5)
    assert not w.unretired and len(w.inserted) == 1


def test_what_no_delete_retired_is_not_restorable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chunks of the revision listed at the row's time: an erasure that superseded the
    row in place, a document with no chunks, or a retirement that was not a delete."""
    w = _World(monkeypatch, _doc())
    w.retired = []
    out = _restore()
    assert (out.restored, out.reason) == (False, "not_restorable")
    assert not w.inserted and not w.unretired


def test_a_document_without_an_original_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, _doc(blob_path="", blob_size=0))
    out = _restore()
    assert (out.restored, out.original) == (True, "none")
    assert len(w.inserted) == 1


def test_a_document_that_changed_under_the_claim_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World(monkeypatch, _doc())
    w.reads = [_doc(), _doc(version=8)]
    with pytest.raises(PreconditionFailedError):
        _restore()
    assert not w.inserted and not w.unretired


def test_a_held_source_is_refused_as_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, _doc())
    w.held = False
    with pytest.raises(BackendUnavailableError):
        _restore()
    assert not w.inserted


def test_a_delete_past_the_window_is_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    old = _NOW - restore.RESTORE_WINDOW - timedelta(days=1)
    w = _World(monkeypatch, _doc(superseded_at=old))
    assert _restore().reason == "expired"
    assert not w.inserted and not w.unretired


def test_the_window_stays_inside_the_erasure_ledger_ttl() -> None:
    schema = (Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS loom.erasure_requests") :]
    ttl = re.search(r"TTL requested_at \+ toIntervalDay\((\d+)\)", table)
    assert ttl is not None
    assert restore.RESTORE_WINDOW.days < int(ttl.group(1))


def test_an_erasure_registered_during_the_restore_takes_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its row written back retired (never deleted: a merge may have folded the delete's
    row in), its ingestions listed again at the delete's own time."""
    w = _World(monkeypatch, _doc())
    w.erasure_after = 1  # the second ledger read, after the row lands, finds it
    assert _restore().reason == "not_restorable"
    restored, retired = w.inserted
    assert not w.deleted_rows and retired["superseded_at"] == _AT
    assert w.retired_again == [(41, _AT)]


@pytest.mark.parametrize(
    "purge", [_PURGE, [{"scope_type": "user_id", "scope_value": "u1", "purge": True}]]
)
def test_a_purge_registered_during_the_restore_deletes_its_row(
    monkeypatch: pytest.MonkeyPatch, purge: list[dict[str, Any]]
) -> None:
    """A purge, of the namespace's list or of the writer, must find no copy of the
    content: the restore's own row goes."""
    w = _World(monkeypatch, _doc())
    w.ledger_rows = purge
    w.erasure_after = 1
    assert _restore().reason == "not_restorable"
    (row,) = w.inserted
    assert w.deleted_rows == [row["version"]] and w.retired_again == [(41, _AT)]


def test_a_namespace_erasure_is_never_a_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its saga only supersedes in place, whatever the row's `purge` column says: the row
    goes back retired, not deleted."""
    w = _World(monkeypatch, _doc())
    w.ledger_rows = [{"scope_type": "namespace", "scope_value": "team", "purge": True}]
    w.erasure_after = 1
    assert _restore().reason == "not_restorable"
    assert not w.deleted_rows and w.inserted[-1]["superseded_at"] == _AT


@pytest.mark.parametrize(("user", "blocked"), [("u1", True), ("someone-else", False)])
def test_a_user_erasure_covers_a_restore_only_of_what_that_user_wrote(
    monkeypatch: pytest.MonkeyPatch, user: str, blocked: bool
) -> None:
    w = _World(monkeypatch, _doc(user_id="u1"))
    w.ledger_rows = [{"scope_type": "user_id", "scope_value": user, "purge": False}]
    w.erasure = True
    assert (_restore().reason == "not_restorable") is blocked


def test_any_user_erasure_covers_a_cut_off_restores_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the delete's signature nothing says whose the live chunks are."""
    w = _World(monkeypatch, _doc(user_id="u1"))
    w.retired, w.live = [], [41]
    w.ledger_rows = [{"scope_type": "user_id", "scope_value": "someone-else", "purge": False}]
    w.erasure = True
    assert _restore().reason == "not_restorable"


@pytest.mark.parametrize("error", [RuntimeError("ClickHouse went away"), asyncio.CancelledError()])
def test_a_failed_or_cancelled_write_takes_the_restore_back(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    w = _World(monkeypatch, _doc())
    w.insert_error = error
    with pytest.raises(type(error)):
        _restore()
    # Written back retired, never deleted: a merge may have folded the delete's row in.
    (retired,) = w.inserted
    assert w.unretired and not w.deleted_rows and w.retired_again == [(41, _AT)]
    assert retired["superseded_at"] == _AT


def test_live_leftovers_an_erasure_covers_are_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _World(monkeypatch, _doc())
    w.retired, w.live, w.erasure = [], [41], True
    assert _restore().reason == "not_restorable"
    assert not w.inserted and w.retired_again == [(41, _AT)]  # at the delete's time


def test_the_restored_row_wins_over_a_delete_from_a_clock_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ahead = time.time_ns() + 10**12  # the deleting replica's clock runs 1000 s ahead
    w = _World(monkeypatch, _doc(version=ahead))
    assert _restore().restored
    assert w.inserted[0]["version"] > ahead


def test_a_swept_artifact_is_not_named_by_the_restored_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World(monkeypatch, _doc(artifact_sha256="art"))

    async def locate(_org: str, _ns: str, sha: str) -> object | None:
        return None if sha == "art" else object()

    monkeypatch.setattr(restore.blobs, "locate", locate)
    assert _restore().original == "kept"
    assert w.inserted[0]["artifact_sha256"] == ""


def test_the_ledger_is_read_from_the_documents_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not from its delete: an erasure that ran between the write and the delete counts."""
    w = _World(monkeypatch, _doc())
    _restore()
    assert w.ledger_args == (
        "acme",
        int(_WRITTEN.replace(tzinfo=UTC).timestamp()),
        LEDGER_CLOCK_MARGIN_S,
    )


def test_a_bucket_failure_before_the_writes_leaves_the_delete_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _World(monkeypatch, _doc())

    async def locate(*_a: Any) -> object | None:
        raise RuntimeError("bucket down")

    monkeypatch.setattr(restore.blobs, "locate", locate)
    with pytest.raises(RuntimeError):
        _restore()
    assert not w.unretired and not w.inserted and not w.retired_again


def _failing_second_read(monkeypatch: pytest.MonkeyPatch, undo_read: Any) -> None:
    """The second ledger read fails; the undo's read answers `undo_read` (None: it fails)."""
    script: list[Any] = [[], RuntimeError("ledger down"), undo_read]

    async def reads(_org: str, _ns: str, user_id: str | None, *_a: Any) -> list[dict[str, Any]]:
        step = script.pop(0)
        if step is None or isinstance(step, Exception):
            raise step or RuntimeError("ledger down")
        mine = [r for r in step if r["scope_type"] != "user_id" or r["scope_value"] == user_id]
        return step if user_id is None else mine

    monkeypatch.setattr(restore.ledger_reads, "unfinished_reaching", reads)


def test_a_failed_second_read_under_a_purge_deletes_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The undo reads the ledger again and finds a purge: it must find no copy left."""
    w = _World(monkeypatch, _doc())
    _failing_second_read(monkeypatch, _PURGE)
    with pytest.raises(RuntimeError):
        _restore()
    (row,) = w.inserted
    assert w.deleted_rows == [row["version"]] and w.retired_again == [(41, _AT)]


def test_an_unreadable_ledger_writes_the_row_back_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never deleted: a merge may have folded the delete's row into it, at any moment."""
    w = _World(monkeypatch, _doc())
    _failing_second_read(monkeypatch, None)
    with pytest.raises(RuntimeError):
        _restore()
    restored, retired = w.inserted
    assert not w.deleted_rows and retired["superseded_at"] == _AT
    assert retired["version"] > restored["version"] and w.retired_again == [(41, _AT)]


def test_the_undo_leaves_a_later_write_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A text write landed a later revision while the restore ran (text writes take no
    claim): the undo retires only its own ingestions, never that write's row."""
    later = _doc(version=time.time_ns() + 10**12, revision=4, superseded_at=None)
    w = _World(monkeypatch, _doc())
    w.reads = [_doc(), _doc(), later]  # the first read, the claim's, the undo's
    w.erasure_after = 1
    assert _restore().reason == "not_restorable"
    (restored,) = w.inserted  # no row written back over the later revision
    assert restored["revision"] == 3 and w.retired_again == [(41, _AT)]


def test_the_undo_writes_nothing_back_when_no_row_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    """A purge took every row of the source before the undo read it (its ledger read
    failed): writing the deleted row back would restore erased content."""
    w = _World(monkeypatch, _doc())
    w.reads = [_doc(), _doc(), None]  # the first read, the claim's, the undo's
    _failing_second_read(monkeypatch, None)
    with pytest.raises(RuntimeError):
        _restore()
    (restored,) = w.inserted
    assert restored["superseded_at"] is None and w.retired_again == [(41, _AT)]


def test_another_users_purge_does_not_delete_a_failed_restores_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The undo reads the ledger as the restore did: a purge of a user who did not write
    the revision does not reach it."""
    w = _World(monkeypatch, _doc(user_id="u1"))
    _failing_second_read(
        monkeypatch, [{"scope_type": "user_id", "scope_value": "someone-else", "purge": True}]
    )
    with pytest.raises(RuntimeError):
        _restore()
    assert not w.deleted_rows and len(w.inserted) == 2


def test_the_undo_keeps_an_erasures_stamps_on_the_restored_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-purge erasure stamped the restored row in place before the undo ran."""
    stamped = _doc(version=time.time_ns() + 10**12, superseded_at=_NOW, blob_erased_at=_NOW)
    w = _World(monkeypatch, _doc())
    w.reads = [_doc(), _doc(), stamped]  # the first read, the claim's, the undo's
    w.erasure_after = 1
    assert _restore().reason == "not_restorable"
    _restored, retired = w.inserted
    assert retired["blob_erased_at"] == _NOW and retired["superseded_at"] == _NOW
    assert retired["version"] > stamped.version


@pytest.mark.parametrize("erasure", [True, False])
def test_a_cancelled_request_takes_its_restore_back_under_the_claim(
    monkeypatch: pytest.MonkeyPatch, erasure: bool
) -> None:
    """An anyio cancel scope, as an HTTP request's, cancels every later await: the
    restore is taken back, an erasure found or not (nobody hears of it and no audit row
    records it), and the source's claim is given up only after the undo."""
    w = _World(monkeypatch, _doc())
    if erasure:
        w.erasure_after = 1
    record = restore.documents_repo.insert_one
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def claim(*_a: Any) -> AsyncIterator[bool]:
        try:
            yield True
        finally:
            events.append("released")

    monkeypatch.setattr("server.knowledge.app.ingest_run.claim_source", claim)

    async def main() -> None:
        async with anyio.create_task_group() as tg:

            async def insert_then_cancel(row: dict[str, Any]) -> None:
                await record(row)
                events.append("insert")
                if len(w.inserted) == 1:
                    tg.cancel_scope.cancel()
                    await anyio.lowlevel.checkpoint()

            monkeypatch.setattr(restore.documents_repo, "insert_one", insert_then_cancel)
            body = KnowledgeRestoreInput(org="acme", namespace="team", source_uri="doc://a")
            tg.start_soon(restore.handle_knowledge_restore, body)
        await shielded.drain()

    asyncio.run(main())
    restored, retired = w.inserted
    assert w.retired_again == [(41, _AT)] and retired["superseded_at"] == _AT
    assert retired["version"] > restored["version"]
    assert events == ["insert", "insert", "released"]


def test_a_request_cancelled_before_the_writes_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled at the bucket read, the last read before the writes."""
    w = _World(monkeypatch, _doc())
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def claim(*_a: Any) -> AsyncIterator[bool]:
        try:
            yield True
        finally:
            events.append("released")

    monkeypatch.setattr("server.knowledge.app.ingest_run.claim_source", claim)

    async def main() -> None:
        async with anyio.create_task_group() as tg:

            async def locate_then_cancel(*_a: Any) -> object:
                tg.cancel_scope.cancel()
                await anyio.lowlevel.checkpoint()
                return w.original

            monkeypatch.setattr(restore.blobs, "locate", locate_then_cancel)
            body = KnowledgeRestoreInput(org="acme", namespace="team", source_uri="doc://a")
            tg.start_soon(restore.handle_knowledge_restore, body)
        await shielded.drain()

    asyncio.run(main())
    assert not (w.inserted or w.unretired or w.retired_again or w.deleted_rows)
    assert events == ["released"]
