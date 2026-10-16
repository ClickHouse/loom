"""code.scip_ingest: one part of a chunked SCIP delta upload. Size, count and
sequence bounds refuse before any write; a part lands with its sha256; the
batch row is created pending, refreshed by a re-upload, and never reset once
applied or failed; completion counts distinct parts received."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
from pydantic import ValidationError as InputValidationError

from server.code.api.models_ingest import CodeScipIngestInput
from server.code.app import ingest as h
from server.core.errors import ValidationError
from server.settings import settings

BATCH = str(uuid.UUID(int=7))
SHA = "a" * 40


class _Repo:
    def __init__(self, batch: dict[str, Any] | None = None) -> None:
        self.parts: dict[int, tuple[str, str]] = {}
        self.batch = batch
        self.upserts: list[dict[str, Any]] = []

    async def insert_scip_part(
        self, org: str, ns: str, batch_id: str, seq: int, payload: str, payload_hash: str
    ) -> None:
        self.parts[seq] = (payload, payload_hash)

    async def get_scip_batch(self, org: str, ns: str, batch_id: str) -> dict[str, Any] | None:
        return self.batch

    async def upsert_scip_batch(self, org: str, ns: str, batch_id: str, **kw: Any) -> None:
        self.upserts.append(kw)
        self.batch = {**kw, "created_at": kw["created_at"] or "t0"}

    async def scip_parts_received(self, org: str, ns: str, batch_id: str) -> int:
        return len(self.parts)


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> _Repo:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    fake = _Repo()
    monkeypatch.setattr(h, "CodeRepo", lambda: fake)
    return fake


def _ingest(**kw: Any) -> Any:
    args: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "batch_id": BATCH,
        "seq": 0,
        "total_parts": 2,
        "commit_sha": SHA,
        "payload": '{"part": 0}',
        **kw,
    }
    return asyncio.run(h.handle_code_scip_ingest(CodeScipIngestInput(**args)))


def test_parts_land_with_their_hash_and_the_batch_completes_on_the_last(repo: _Repo) -> None:
    first = _ingest(seq=0, payload='{"part": 0}')
    assert (first.parts_received, first.total_parts, first.complete) == (1, 2, False)
    assert repo.parts[0] == ('{"part": 0}', hashlib.sha256(b'{"part": 0}').hexdigest())
    (created,) = repo.upserts
    assert created["status"] == "pending" and created["commit_sha"] == SHA
    assert created["created_at"] is None  # a new batch: the repository stamps it

    last = _ingest(seq=1, payload='{"part": 1}')
    assert (last.parts_received, last.complete) == (2, True)
    # the refresh of a pending batch keeps its creation time
    assert repo.upserts[-1]["created_at"] == "t0"


def test_a_re_uploaded_part_counts_once(repo: _Repo) -> None:
    _ingest(seq=0)
    again = _ingest(seq=0)
    assert again.parts_received == 1 and again.complete is False


@pytest.mark.parametrize("status", ["applied", "failed"])
def test_a_late_part_never_resets_a_settled_batch_to_pending(repo: _Repo, status: str) -> None:
    repo.batch = {"status": status, "created_at": "t0", "total_parts": 2}
    _ingest(seq=1)
    assert repo.upserts == []
    assert repo.batch["status"] == status


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"seq": 2, "total_parts": 2}, "seq 2 outside"),
        ({"total_parts": 513}, "exceeds max 512"),
    ],
)
def test_out_of_range_parts_are_refused_before_any_write(
    repo: _Repo, override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _ingest(**override)
    assert repo.parts == {} and repo.upserts == []


def test_an_oversized_part_is_refused_before_any_write(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "code_scip_max_part_bytes", 8)
    with pytest.raises(ValidationError, match="max 8"):
        _ingest(payload="é" * 5)  # 10 bytes in UTF-8, 5 characters
    assert repo.parts == {}


@pytest.mark.parametrize("sha", ["--output=/tmp/x", "abc", "g" * 40])
def test_a_commit_that_is_not_a_sha_is_refused_at_the_input(sha: str) -> None:
    with pytest.raises(InputValidationError):
        CodeScipIngestInput(batch_id=BATCH, seq=0, total_parts=1, payload="{}", commit_sha=sha)


@pytest.mark.parametrize("batch_id", ["not-a-uuid-at-all", "g" * 32, "12345678"])
def test_a_batch_id_that_is_not_a_uuid_is_refused_at_the_input(batch_id: str) -> None:
    # the staging tables key on a UUID column: a bad id is the caller's 422, not a failed insert
    with pytest.raises(InputValidationError, match="must be a UUID"):
        CodeScipIngestInput(batch_id=batch_id, seq=0, total_parts=1, payload="{}")


def test_the_input_schema_says_the_batch_id_is_a_uuid() -> None:
    schema = CodeScipIngestInput.model_json_schema()
    assert schema["properties"]["batch_id"]["format"] == "uuid"


@pytest.mark.parametrize("spelling", ["hex", "dashed"])
def test_both_uuid_spellings_are_accepted_as_sent(repo: _Repo, spelling: str) -> None:
    # scip_to_delta.py sends uuid4().hex, without dashes
    batch_id = uuid.UUID(int=9).hex if spelling == "hex" else str(uuid.UUID(int=9))
    out = _ingest(batch_id=batch_id, total_parts=1)
    assert out.complete is True and out.batch_id == batch_id


# -- an upload a pass hands off is fenced by that pass's entry lease -------------


@pytest.fixture
def lock_table(monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.unit.claims_fake import FakeLockTable

    ch = FakeLockTable()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    return ch


_LEASE = "scip_entry:o/n"


def test_a_part_from_a_pass_that_lost_its_entry_lease_is_refused(
    repo: _Repo, lock_table: Any
) -> None:
    """Another host took the entry over: the old pass's upload must not complete a batch."""
    lock_table.seed(_LEASE, "other-host:7:zz")
    with pytest.raises(h.EntryLeaseNotHeld):
        _ingest(entry_lease_holder="this-host:1:aa")
    assert repo.parts == {} and repo.upserts == []
    lock_table.keys.clear()  # released: the pass is over, its upload no longer fenced in
    with pytest.raises(h.EntryLeaseNotHeld):
        _ingest(entry_lease_holder="this-host:1:aa")
    assert repo.parts == {}


def test_a_part_from_the_holder_lands(repo: _Repo, lock_table: Any) -> None:
    lock_table.seed(_LEASE, "this-host:1:aa")
    assert _ingest(entry_lease_holder="this-host:1:aa").parts_received == 1
    lock_table.seed(_LEASE, "this-host:1:aa", stale=True)  # expired: takeable by another host
    with pytest.raises(h.EntryLeaseNotHeld):
        _ingest(seq=1, entry_lease_holder="this-host:1:aa")
    assert set(repo.parts) == {0}


def test_an_unfenced_upload_reads_no_lease(repo: _Repo, lock_table: Any) -> None:
    """CI pushes name no holder: no lease is read and the part lands."""
    lock_table.seed(_LEASE, "other-host:7:zz")
    assert _ingest().parts_received == 1
    assert lock_table.sql == []
