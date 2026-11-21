"""Atomic episodic appends keep every occurrence or refuse the whole request."""

from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from server.core import idempotency, pg_lease
from server.core.errors import NotFoundError, ValidationError
from server.memory import store as memory_repo
from server.memory.api.models_bulk import MemoryAppendBatchInput
from server.memory.app import blacklist_guard, bulk_rows, bulk_set
from server.memory.store import identity_key, pg_memory, pg_publish
from server.settings import settings
from tests.unit.ch_fake import RecordingCH
from tests.unit.clock_fake import ManualClock
from tests.unit.memory_rows import memory_head


def _input(*pairs: tuple[str, Any]) -> MemoryAppendBatchInput:
    return MemoryAppendBatchInput(
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="u",
        session_id="s",
        entries=[{"memory_key": key, "value": value} for key, value in pairs],
    )


def _head(*, tombstoned: bool = False) -> Any:
    return memory_head(
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="u",
        session_id="s",
        memory_key="second",
        memory_type="episodic",
        value=json.dumps({"role": "user", "content": "old"}),
        memory_id="11111111-1111-1111-1111-111111111111",
        version=1,
        tombstoned_at=datetime(2025, 1, 2, tzinfo=UTC) if tombstoned else None,
    )


class _Authority:
    """Storage seams; preparation, verdicts and commit admission remain real.

    The commit stand-in applies the handler's locked-head callback to the whole
    batch before recording any row. It does not qualify Postgres transactions.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ch = RecordingCH()
        self.lookup: dict[Any, Any] = {}
        self.locked_heads: dict[Any, dict[str, Any]] = {}
        self.rows: dict[Any, dict[str, Any]] = {}
        self.commits: list[pg_memory.Committed] = []
        self.commit_attempts = 0
        self.publication_error: BaseException | None = None
        self.lookup_error: BaseException | None = None
        self.checked_values: list[str] = []
        self.refused_value: str | None = None
        self.clock = ManualClock(t=100.0)
        monkeypatch.setattr(settings, "storage_authority", "postgres")
        monkeypatch.setattr(bulk_rows.time, "monotonic", self.clock.monotonic)
        monkeypatch.setattr(bulk_set, "get_client", lambda: self.ch)
        monkeypatch.setattr(bulk_rows, "writer_actor", lambda: "process:append-test")
        self.embedder = Mock(side_effect=AssertionError("an episodic append used an embedder"))
        monkeypatch.setattr(bulk_set, "get_embedder", self.embedder)
        monkeypatch.setattr(
            bulk_set,
            "_bulk_identity_dupes",
            AsyncMock(side_effect=AssertionError("identity dedup")),
        )
        monkeypatch.setattr(
            bulk_set,
            "_bulk_resolve_entities",
            AsyncMock(side_effect=AssertionError("entity lookup")),
        )
        monkeypatch.setattr(pg_memory, "capture", AsyncMock(return_value={}))
        monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", self.read_heads)
        monkeypatch.setattr(blacklist_guard, "find_match", self.find_match)
        monkeypatch.setattr(blacklist_guard, "note_hit", AsyncMock())
        monkeypatch.setattr(bulk_set, "_bulk_mark_same_key", AsyncMock())
        monkeypatch.setattr(bulk_set, "_bulk_intra_batch_pairs", AsyncMock(return_value=[]))
        monkeypatch.setattr(bulk_set, "_bulk_follow_ups", AsyncMock())
        monkeypatch.setattr(bulk_set, "_fence_and_announce", AsyncMock())
        monkeypatch.setattr(bulk_set.after_write, "index_late_rows", Mock())
        monkeypatch.setattr(pg_publish, "commit", self.commit)
        monkeypatch.setattr(pg_publish, "publish", self.publish)

    async def read_heads(self, keys: Any, **_: Any) -> dict[Any, Any]:
        if self.lookup_error is not None:
            raise self.lookup_error
        return {key: self.lookup[key] for key in keys if key in self.lookup}

    async def find_match(self, *, value: str, **_: Any) -> Any:
        self.checked_values.append(value)
        if value == self.refused_value:
            return blacklist_guard.BlacklistPattern(
                "pattern", "test", "personal", "blocked", "substring", "namespace", ""
            )
        return None

    async def commit(
        self,
        rows: list[dict[str, Any]],
        *,
        targets: list[int],
        admission: pg_memory.Admission,
        idempotent: bool,
    ) -> pg_memory.Committed:
        self.commit_attempts += 1
        assert idempotent
        assert admission.recheck is not None
        for row in rows:
            key = identity_key(row)
            admission.recheck(key, self.locked_heads.get(key))
        stored = copy.deepcopy(rows)
        committed = pg_memory.Committed(
            rows=stored,
            audits=[],
            seqs=[(row["org"], row["namespace"], i + 1) for i, row in enumerate(stored)],
            targets=targets,
        )
        self.commits.append(committed)
        self.rows.update({identity_key(row): copy.deepcopy(row) for row in stored})
        return committed

    async def publish(self, committed: pg_memory.Committed, **_: Any) -> None:
        assert committed is self.commits[-1]
        if self.publication_error is not None:
            raise self.publication_error


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch) -> _Authority:
    return _Authority(monkeypatch)


@pytest.mark.asyncio
async def test_unsupported_authority_refuses_before_preparation_or_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    prepare = AsyncMock(side_effect=AssertionError("append preparation started"))
    client = Mock(side_effect=AssertionError("append acquired a client"))
    embedder = Mock(side_effect=AssertionError("append acquired an embedder"))
    monkeypatch.setattr(bulk_set, "handle_memory_bulk_set", prepare)
    monkeypatch.setattr(bulk_set, "get_client", client)
    monkeypatch.setattr(bulk_set, "get_embedder", embedder)

    with pytest.raises(ValidationError, match="Postgres"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello")))

    prepare.assert_not_awaited()
    client.assert_not_called()
    embedder.assert_not_called()


@pytest.mark.asyncio
async def test_equal_values_under_fresh_keys_remain_two_complete_occurrences(
    authority: _Authority,
) -> None:
    value = {"role": "user", "content": "hello", "metadata": {"sequence": [1, 2]}}
    result = await bulk_set.handle_memory_append_batch(_input(("first", value), ("second", value)))

    assert len(authority.commits) == 1
    committed = authority.commits[0]
    assert [r.memory_key for r in result.written] == ["first", "second"]
    assert all(r.version > 0 for r in result.written)
    assert len({r.memory_id for r in result.written}) == 2
    assert len(authority.rows) == 2
    for row, written in zip(committed.rows, result.written, strict=True):
        assert authority.rows[identity_key(row)] == row
        assert json.loads(row["value"]) == value
        assert json.loads(row["value_json"]) == value
        assert row["memory_type"] == "episodic"
        assert (row["org"], row["namespace"], row["agent"], row["user_id"], row["session_id"]) == (
            "test",
            "personal",
            "cursor",
            "u",
            "s",
        )
        assert row["version"] == written.version
        assert row["memory_id"] == written.memory_id
        assert row["source_class"] == "asserted"
        assert row["actor"] == "process:append-test"
        assert row["tombstoned_at"] is None
    assert committed.targets == [pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW] * 2
    assert authority.ch.calls == []
    authority.embedder.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_second_entry_never_commits_the_valid_first_entry(
    authority: _Authority,
) -> None:
    authority.refused_value = json.dumps("blocked")

    with pytest.raises(ValidationError, match="refused an entry"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello"), ("second", "blocked")))

    assert authority.checked_values == [json.dumps("hello"), json.dumps("blocked")]
    assert authority.commit_attempts == 0
    assert authority.rows == {}
    assert authority.ch.calls == []


@pytest.mark.asyncio
async def test_duplicate_keys_are_refused_before_preparing_either_occurrence(
    authority: _Authority,
) -> None:
    with pytest.raises(ValidationError, match="distinct keys"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello"), ("first", "again")))

    assert authority.checked_values == []
    assert authority.commit_attempts == 0
    assert authority.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("tombstoned", [False, True])
async def test_existing_live_or_deleted_head_refuses_the_entire_append(
    authority: _Authority, tombstoned: bool
) -> None:
    head = _head(tombstoned=tombstoned)
    key = ("test", "personal", "cursor", "u", "s", "second")
    authority.lookup[key] = head

    with pytest.raises(ValidationError, match="must not already exist"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello"), ("second", "again")))

    assert authority.commit_attempts == 0
    assert authority.rows == {}
    assert authority.lookup[key] is head


@pytest.mark.asyncio
async def test_unverifiable_freshness_never_becomes_a_first_write(authority: _Authority) -> None:
    authority.lookup_error = RuntimeError("head read unavailable")

    with pytest.raises(ValidationError, match="verify fresh keys"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello")))

    assert authority.commit_attempts == 0
    assert authority.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("tombstoned", [False, True])
async def test_a_head_appearing_at_locked_admission_is_not_overwritten(
    authority: _Authority, tombstoned: bool
) -> None:
    key = ("test", "personal", "cursor", "u", "s", "second")
    raced = {"version": 2, "tombstoned_at": "deleted" if tombstoned else None}
    authority.locked_heads[key] = raced

    with pytest.raises(ValidationError, match="must not already exist"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello"), ("second", "again")))

    assert authority.commit_attempts == 1
    assert authority.commits == []
    assert authority.rows == {}
    assert authority.locked_heads[key] is raced


@pytest.mark.asyncio
async def test_a_committed_append_with_pending_publication_is_not_reported_as_rollback(
    authority: _Authority,
) -> None:
    authority.publication_error = pg_lease.PublicationPending()

    with pytest.raises(pg_lease.PublicationPending) as caught:
        await bulk_set.handle_memory_append_batch(_input(("first", "hello"), ("second", "again")))

    assert caught.value.extra["committed"] is True
    assert authority.commit_attempts == 1
    assert len(authority.commits) == 1
    assert [row["memory_key"] for row in authority.rows.values()] == ["first", "second"]


@pytest.mark.asyncio
async def test_losing_pg_authority_before_insert_does_not_fall_back_to_clickhouse(
    authority: _Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert = bulk_set._bulk_insert

    async def lose_authority(ch: Any, state: bulk_rows._BulkWriteState) -> None:
        monkeypatch.setattr(settings, "storage_authority", "clickhouse")
        await insert(ch, state)

    monkeypatch.setattr(bulk_set, "_bulk_insert", lose_authority)

    with pytest.raises(ValidationError, match="Postgres"):
        await bulk_set.handle_memory_append_batch(_input(("first", "hello")))

    assert authority.commit_attempts == 0
    assert authority.rows == {}
    assert authority.ch.calls == []


def test_append_commit_can_bind_its_idempotency_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = ("test", "request-key", 7)
    monkeypatch.setattr(idempotency, "_bound", ContextVar("append_test_claim", default=claim))
    operation = ContextVar("append_test_operation", default="memory.append_batch")
    monkeypatch.setattr(idempotency, "_bound_op", operation)

    assert idempotency.bound_key() == claim
    operation.set("profile.set")
    assert idempotency.bound_key() is None


@pytest.mark.asyncio
async def test_committed_replay_preserves_both_occurrence_receipts() -> None:
    revisions = [
        {
            "namespace": "personal",
            "seq": i,
            "memory_key": key,
            "memory_id": f"id-{i}",
            "version": 10 + i,
            "content_sha256": "a" * 64,
        }
        for i, key in enumerate(("first", "second"), start=1)
    ]
    conn = Mock(fetch=AsyncMock(return_value=revisions))
    hit = await idempotency._committed_hit(
        conn,
        {
            "operation": "memory.append_batch",
            "revisions": [["test", "personal", 1], ["test", "personal", 2]],
        },
    )

    assert hit.status_code == 200
    assert hit.body == {
        "written": [
            {"memory_key": "first", "version": 11, "memory_id": "id-1"},
            {"memory_key": "second", "version": 12, "memory_id": "id-2"},
        ],
        "idempotent_replay": "committed",
    }
    assert hit.headers == {"X-Loom-Idempotent-Replay": "committed"}


@pytest.mark.asyncio
async def test_committed_replay_does_not_expose_an_erased_occurrence() -> None:
    conn = Mock(fetch=AsyncMock(return_value=[]))

    with pytest.raises(NotFoundError, match="erased"):
        await idempotency._committed_hit(
            conn, {"operation": "memory.append_batch", "revisions": [["test", "personal", 1]]}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["derived", "external", "imported"])
async def test_append_preserves_a_declared_provenance_downgrade(
    authority: _Authority, source: str
) -> None:
    input = _input(("first", "hello"))
    input.entries[0].source_class = source
    input.entries[0].source_trace = "native-history"
    await bulk_set.handle_memory_append_batch(input)
    row = next(iter(authority.rows.values()))
    assert row["source_class"] == source
    assert row["source_trace"] == "native-history"
    assert row["actor"] == "process:append-test"


@pytest.mark.asyncio
async def test_an_invalid_second_source_declaration_aborts_the_whole_batch(
    authority: _Authority,
) -> None:
    input = _input(("first", "hello"), ("second", "again"))
    input.entries[1].source_class = "asserted"
    with pytest.raises(ValidationError):
        await bulk_set.handle_memory_append_batch(input)
    assert authority.rows == {} and authority.commit_attempts == 0
