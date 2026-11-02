"""Unit tests for memory.bulk_set parity with the point write path."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app.extract import handler as mem_extract
from tests.unit.bulk_stubs import BatchEmbedder, StubCH, no_delete_in_flight, wire_stubs


@pytest.fixture(autouse=True)
def _no_delete_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    no_delete_in_flight(monkeypatch)


_ORIGINAL_CREATED_AT = datetime(2025, 1, 2, 3, 4, 5)


def _bulk_input(entries: list[_BulkSetEntry]) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="u",
        session_id="s",
        entries=entries,
    )


@pytest.mark.asyncio
async def test_bulk_set_embeds_key_text_in_the_same_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = StubCH()
    embedder = BatchEmbedder()
    captured: list[dict[str, Any]] = []
    wire_stubs(monkeypatch, ch, embedder, captured)

    out = await mem_extract.handle_memory_bulk_set(
        _bulk_input(
            [
                _BulkSetEntry(memory_key="deploy_window", value="Tue/Thu only."),
                _BulkSetEntry(memory_key="user_dog_name", value="Dog is Biscuit."),
            ]
        )
    )
    assert [r.memory_key for r in out.written] == ["deploy_window", "user_dog_name"]
    assert out.skipped_pinned == []

    assert len(embedder.batches) == 1
    batch = embedder.batches[0]
    assert len(batch) == 4
    assert batch[2:] == ["deploy window", "user dog name"]

    assert [r["memory_key_embedding"] for r in captured] == [
        [3.0, 3.0, 3.0],
        [4.0, 4.0, 4.0],
    ]
    assert all(r["memory_key_embedding_dim"] == 3 for r in captured)
    assert [r["embedding"] for r in captured] == [
        [1.0, 1.0, 1.0],
        [2.0, 2.0, 2.0],
    ]


@pytest.mark.asyncio
async def test_bulk_set_skips_entries_targeting_pinned_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = StubCH(
        lookup_rows=[
            {
                "memory_key": "user_favorite_color",
                "memory_id": "11111111-1111-1111-1111-111111111111",
                "content_sha256": "0" * 64,
                "pinned": 1,
                "created_at": _ORIGINAL_CREATED_AT,
            }
        ]
    )
    embedder = BatchEmbedder()
    captured: list[dict[str, Any]] = []
    wire_stubs(monkeypatch, ch, embedder, captured)

    out = await mem_extract.handle_memory_bulk_set(
        _bulk_input(
            [
                _BulkSetEntry(memory_key="user_favorite_color", value="Favorite color is green."),
                _BulkSetEntry(memory_key="user_dog_name", value="Dog is Biscuit."),
            ]
        )
    )
    assert out.skipped_pinned == ["user_favorite_color"]
    by_key = {r.memory_key: r.version for r in out.written}
    assert by_key["user_favorite_color"] == 0
    assert by_key["user_dog_name"] > 0

    mem_rows = ch.rows_for("loom.memory")
    assert [r["memory_key"] for r in mem_rows] == ["user_dog_name"]
    assert [r["memory_key"] for r in captured] == ["user_dog_name"]


@pytest.mark.asyncio
async def test_bulk_set_byte_identical_pinned_reingest_is_unchanged_not_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = "Favorite color is blue."
    ch = StubCH(
        lookup_rows=[
            {
                "memory_key": "user_favorite_color",
                "memory_id": "11111111-1111-1111-1111-111111111111",
                "content_sha256": sha256_value(value),
                "pinned": 1,
                "created_at": _ORIGINAL_CREATED_AT,
            }
        ]
    )
    embedder = BatchEmbedder()
    captured: list[dict[str, Any]] = []
    wire_stubs(monkeypatch, ch, embedder, captured)

    out = await mem_extract.handle_memory_bulk_set(
        _bulk_input([_BulkSetEntry(memory_key="user_favorite_color", value=value)])
    )
    assert out.skipped_pinned == []
    assert [r.version for r in out.written] == [0]
    assert ch.rows_for("loom.memory") == []


@pytest.mark.asyncio
async def test_bulk_set_overwrite_carries_created_at_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = StubCH(
        lookup_rows=[
            {
                "memory_key": "user_dog_name",
                "memory_id": "22222222-2222-2222-2222-222222222222",
                "content_sha256": "0" * 64,
                "pinned": 0,
                "created_at": _ORIGINAL_CREATED_AT,
            }
        ]
    )
    embedder = BatchEmbedder()
    captured: list[dict[str, Any]] = []
    wire_stubs(monkeypatch, ch, embedder, captured)

    await mem_extract.handle_memory_bulk_set(
        _bulk_input([_BulkSetEntry(memory_key="user_dog_name", value="Dog is Bowie.")])
    )
    mem_rows = ch.rows_for("loom.memory")
    assert len(mem_rows) == 1
    assert mem_rows[0]["created_at"] == _ORIGINAL_CREATED_AT
    assert mem_rows[0]["memory_id"] == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_first", [True, False])
async def test_bulk_set_mixed_existing_and_new_keys_share_one_column_set(
    monkeypatch: pytest.MonkeyPatch,
    existing_first: bool,
) -> None:
    """A batch mixing existing-key and new-key entries must produce rows with
    identical key sets, in either ordering."""
    ch = StubCH(
        lookup_rows=[
            {
                "memory_key": "user_favorite_color",
                "memory_id": "33333333-3333-3333-3333-333333333333",
                "content_sha256": "0" * 64,
                "pinned": 0,
                "created_at": _ORIGINAL_CREATED_AT,
            }
        ]
    )
    embedder = BatchEmbedder()
    captured: list[dict[str, Any]] = []
    wire_stubs(monkeypatch, ch, embedder, captured)

    entries = [
        _BulkSetEntry(memory_key="user_favorite_color", value="Favorite color is green."),
        _BulkSetEntry(memory_key="user_dog_name", value="Dog is Biscuit."),
    ]
    if not existing_first:
        entries.reverse()

    await mem_extract.handle_memory_bulk_set(_bulk_input(entries))

    mem_rows = ch.rows_for("loom.memory")
    assert len(mem_rows) == 2
    columns = list(mem_rows[0].keys())
    assert all(set(r.keys()) == set(columns) for r in mem_rows)
    _ = [[r[c] for c in columns] for r in mem_rows]

    by_key = {r["memory_key"]: r for r in mem_rows}
    existing = by_key["user_favorite_color"]
    assert existing["memory_id"] == "33333333-3333-3333-3333-333333333333"
    assert existing["created_at"] == _ORIGINAL_CREATED_AT
    fresh = by_key["user_dog_name"]
    assert fresh["memory_id"] and fresh["memory_id"] != existing["memory_id"]
    assert fresh["created_at"] != _ORIGINAL_CREATED_AT


@pytest.mark.asyncio
async def test_bulk_set_revision_carries_the_live_rows_usage_and_resets_its_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bulk revision is built by `build_revision_row` from the live row the
    batched lookup read: recall / evidence counts and lineage survive, the
    old value's verdict and attestation do not; a first write in the same
    batch takes the DDL defaults, and both rows carry one full column set."""
    ch = StubCH(
        lookup_rows=[
            {
                "memory_key": "user_favorite_color",
                "memory_id": "44444444-4444-4444-4444-444444444444",
                "content_sha256": "0" * 64,
                "created_at": _ORIGINAL_CREATED_AT,
                "recall_count": 7,
                "evidence_count": 5,
                "last_recalled_at": _ORIGINAL_CREATED_AT,
                "cross_promoted_from_namespace": "team",
                "status": "superseded",
                "superseded_by": "other",
                "verified_by": "human:bob",
                "verified_sha256": "0" * 64,
            }
        ]
    )
    wire_stubs(monkeypatch, ch, BatchEmbedder(), [])
    await mem_extract.handle_memory_bulk_set(
        _bulk_input(
            [
                _BulkSetEntry(memory_key="user_favorite_color", value="Favorite color is teal."),
                _BulkSetEntry(memory_key="user_dog_name", value="Dog is Biscuit."),
            ]
        )
    )
    by_key = {r["memory_key"]: r for r in ch.rows_for("loom.memory")}
    rev, first = by_key["user_favorite_color"], by_key["user_dog_name"]
    from server.memory.store import MEMORY_PERSISTED_COLS

    assert set(rev) == set(first) == set(MEMORY_PERSISTED_COLS)
    assert (rev["recall_count"], rev["evidence_count"]) == (7, 5)
    assert rev["last_recalled_at"] == _ORIGINAL_CREATED_AT
    assert rev["cross_promoted_from_namespace"] == "team"
    assert rev["status"] == "active" and rev["superseded_by"] == ""
    # the attestation rides the revision: over the old bytes, so it reads stale
    assert (rev["verified_by"], rev["verified_sha256"]) == ("human:bob", "0" * 64)
    assert (first["recall_count"], first["evidence_count"], first["verified_by"]) == (0, 1, "")


def _vec_state(entries: list[tuple[str, int]], *, lookup_ok: bool = True) -> Any:
    """A bulk write state of (sensitivity, version) rows on key `k`, each with an
    embedding row unless it is high (as the row builder does)."""
    from server.memory.app.bulk_rows import _BulkWriteState

    state = _BulkWriteState(existing_lookup_ok=lookup_ok)
    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    for sensitivity, version in entries:
        row = {**ident, "memory_key": "k", "memory_type": "semantic"}
        state.memory_rows.append({**row, "sensitivity": sensitivity, "version": version})
        if sensitivity != "high":
            state.embedding_rows.append({**row, "version": version, "content_excerpt": "plain"})
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entries", "lookup_ok", "retired"),
    [
        ([("", 1), ("high", 2)], True, True),  # the high row wins: no vector at all
        ([("high", 1), ("", 2)], True, False),  # the plain row wins: its vector lands
        ([("high", 1)], True, False),  # a new key, never embedded: nothing to read
        ([("high", 1)], False, True),  # the lookup failed: retire by identity anyway
    ],
)
async def test_bulk_insert_decides_a_keys_vectors_by_the_row_that_wins(
    monkeypatch: pytest.MonkeyPatch, entries: Any, lookup_ok: bool, retired: bool
) -> None:
    import server.memory.store as memory_repo

    inserted: list[dict[str, Any]] = []
    retire_calls: list[Any] = []

    async def _noop(rows: Any, *, ch: Any = None) -> None:
        return None

    async def _emb(rows: list[dict[str, Any]], *, ch: Any = None) -> None:
        inserted.extend(rows)

    async def _retire(items: Any, *, ch: Any = None) -> int:
        retire_calls.extend(items)
        return 0

    monkeypatch.setattr(memory_repo, "insert_memory_versions", _noop)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _emb)
    fences: list[Any] = []

    async def _fence(items: Any, *, model: str, ch: Any = None) -> int:
        fences.extend(items)
        return len(items)

    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)

    async def _no_chunks(**_k: Any) -> int:
        return 0

    from server.memory.app import high_fence

    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _no_chunks)
    await _bulk_set_mod._bulk_insert(object(), _vec_state(entries, lookup_ok=lookup_ok))
    # a key whose winning row is high is fenced at that row's version, vector or not
    winner = max(entries, key=lambda e: e[1])
    if winner[0] == "high":
        assert fences == [(("o", "n", "a", "u", "s", "k"), winner[1])]
    else:
        assert fences == []
    if retired:
        assert inserted == []
        assert retire_calls == [(("o", "n", "a", "u", "s", "k"), max(v for _, v in entries))]
    else:
        assert retire_calls == []
        assert len(inserted) == sum(1 for s, _ in entries if s != "high")


@pytest.mark.asyncio
async def test_a_batch_at_another_width_is_refused_before_it_embeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused before the embed call it would pay for; a batch with nothing to
    embed carries no vector and is written."""
    from server.core.errors import CodedWidthMismatchError
    from server.core.vectors import codes_state

    ch = StubCH()
    embedder = BatchEmbedder()
    captured: list[dict[str, Any]] = []
    wire_stubs(monkeypatch, ch, embedder, captured)
    monkeypatch.setattr(codes_state._state, "widths", {"loom.memory_embeddings.embedding": 8})
    with pytest.raises(CodedWidthMismatchError):
        await mem_extract.handle_memory_bulk_set(
            _bulk_input([_BulkSetEntry(memory_key="rv/fact/a", value="the sky is blue")])
        )
    assert embedder.batches == [] and ch.insert_calls == [] and captured == []
    out = await mem_extract.handle_memory_bulk_set(
        _bulk_input([_BulkSetEntry(memory_key="rv/ep/a", memory_type="episodic", value="ran")])
    )
    assert [r.memory_key for r in out.written] == ["rv/ep/a"] and captured == []


@pytest.mark.asyncio
async def test_bulk_set_announces_its_rows_when_the_embedding_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory rows land while the embedding insert fails: the batch answers an
    error, and its events are already in the outbox (a retry of the same values
    writes nothing and would announce nothing)."""
    ch = StubCH()
    wire_stubs(monkeypatch, ch, BatchEmbedder(), [])
    announced: list[Any] = []

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    async def _fail(*_: Any, **__: Any) -> None:
        raise RuntimeError("insert into loom.memory_embeddings unavailable")

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    monkeypatch.setattr("server.memory.store.insert_embedding_rows", _fail)
    with pytest.raises(RuntimeError, match="memory_embeddings"):
        await mem_extract.handle_memory_bulk_set(
            _bulk_input(
                [
                    _BulkSetEntry(memory_key="deploy_window", value="Tue/Thu only."),
                    _BulkSetEntry(memory_key="user_dog_name", value="Dog is Biscuit."),
                ]
            )
        )
    assert len(ch.rows_for("loom.memory")) == 2
    assert [(event, row["memory_key"]) for event, row in announced] == [
        ("memory.written", "deploy_window"),
        ("memory.written", "user_dog_name"),
    ]
