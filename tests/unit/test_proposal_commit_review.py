"""The commit boundary preserves pending review across every ordinary writer."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store
from server.memory.store import write_fence
from server.memory.store.rows import identity_key
from tests.unit.test_memory_verify import _row


@pytest.mark.parametrize("status", ["proposed", "rejected"])
async def test_an_ordinary_insert_cannot_activate_an_unreviewed_head(
    monkeypatch: Any, status: str
) -> None:
    source = _row(status=status, requires_review=True)

    async def heads(*args: Any, **kwargs: Any) -> dict[Any, Any]:
        return {identity_key(source): source}

    class CH:
        def __init__(self) -> None:
            self.rows: list[Any] = []

        async def insert(self, table: str, rows: list[Any], **kwargs: Any) -> None:
            self.rows.extend(rows)

    monkeypatch.setattr(write_fence, "memory_rows_by_identity_keys", heads)
    ch = CH()
    replacement = store.build_memory_version_row(
        source, status="active", version=source.version + 1
    )
    with pytest.raises(PermissionError):
        await store.insert_memory_versions([replacement], ch=ch)
    assert ch.rows == []


def test_a_pending_flag_survives_a_snapshot_into_another_namespace():
    from server.identity.api.models import _SnapshotMemoryRow
    from server.memory.domain.proposal_review import enforce_review

    source = _row(status="superseded", requires_review=True)
    payload = _SnapshotMemoryRow(**source.model_dump()).model_dump()
    restored = store.MemoryRow.from_snapshot(payload, namespace="destination")
    assert restored.requires_review
    replacement = store.build_memory_version_row(
        restored, status="active", version=restored.version + 1
    )
    with pytest.raises(PermissionError):
        enforce_review([replacement], {identity_key(restored): restored})


@pytest.mark.parametrize("status", ["proposed", "rejected", "superseded"])
def test_moving_pending_content_cannot_activate_it_at_a_new_key(status):
    from server.memory.api.models_move import MemoryMoveInput
    from server.memory.app.move import _destination_input

    source = _row(status=status, requires_review=True)
    request = MemoryMoveInput(
        org="o", namespace="ns", agent=source.agent, from_key=source.memory_key, to_key="moved"
    )
    with pytest.raises(PermissionError):
        _destination_input(request, source)


def test_a_fresh_proposal_persists_its_pending_review_state():
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.conflicts import ConflictResult
    from server.memory.app.write import _stamp_lifecycle

    row = store.build_memory_version_row(_row(), requires_review=0)
    _stamp_lifecycle(
        row,
        ConflictResult(verdict="clear", peers=[]),
        MemorySetInput(
            org="o", namespace="ns", agent="a", memory_key="p", value="candidate", proposed=True
        ),
    )
    assert row["status"] == "proposed" and row["requires_review"] == 1


def test_a_rejected_established_fact_can_be_corrected():
    from server.memory.domain.proposal_review import enforce_review

    source = _row(status="rejected", requires_review=False)
    corrected = store.build_memory_version_row(source, status="active", value="corrected")
    enforce_review([corrected], {identity_key(source): source})


@pytest.mark.parametrize("snapshot", [False, True])
def test_a_legacy_rejected_proposal_remains_pending_without_its_flag(snapshot):
    from server.identity.api.models import _SnapshotMemoryRow
    from server.memory.domain.proposal_review import enforce_review, pending_review
    from server.memory.store.rows import _hydrate

    raw = store.build_memory_version_row(_row(status="rejected"))
    raw.pop("requires_review")
    assert pending_review(raw)
    if snapshot:
        raw["cross_promoted_from_memory_id"] = ""
        payload = _SnapshotMemoryRow(**raw).model_dump()
        source = store.MemoryRow.from_snapshot(payload)
    else:
        source = _hydrate(raw)
    assert source.requires_review
    replacement = store.build_memory_version_row(source, status="active")
    with pytest.raises(PermissionError):
        enforce_review([replacement], {identity_key(source): raw})


@pytest.mark.parametrize("status", ["proposed", "rejected"])
def test_native_decoding_preserves_legacy_pending_review(status):
    from server.memory.domain.proposal_review import enforce_review
    from server.memory.store import pg_memory
    from server.memory.store.rows import _hydrate

    raw = store.build_memory_version_row(_row(status=status, requires_review=True))
    raw.pop("requires_review")
    decoded = pg_memory.decode_row(pg_memory.encode_row(raw))
    assert decoded["requires_review"]
    source = _hydrate(decoded)
    replacement = store.build_memory_version_row(source, status="active", requires_review=0)
    with pytest.raises(PermissionError):
        enforce_review([replacement], {identity_key(source): decoded})


@pytest.mark.parametrize("pending", [False, True])
def test_native_sparse_storage_keeps_explicit_review_provenance(pending):
    from server.memory.domain.proposal_review import enforce_review
    from server.memory.store import pg_memory
    from server.memory.store.rows import _hydrate

    source = _row(status="rejected", requires_review=pending)
    raw = pg_memory.encode_row(store.build_memory_version_row(source))
    sparse = pg_memory.sparse_row(raw)
    assert "requires_review" in sparse
    decoded = pg_memory.decode_row(sparse)
    assert bool(decoded["requires_review"]) is pending
    restored = _hydrate(decoded)
    replacement = store.build_memory_version_row(restored, status="active", requires_review=0)
    if pending:
        with pytest.raises(PermissionError):
            enforce_review([replacement], {identity_key(source): decoded})
    else:
        enforce_review([replacement], {identity_key(source): decoded})


@pytest.mark.parametrize("legacy", [False, True])
async def test_native_commit_checks_decoded_review_state_before_publishing(monkeypatch, legacy):
    from contextlib import asynccontextmanager

    from server.core import pg
    from server.memory.store import pg_memory

    source = _row(status="rejected", requires_review=False)
    head = pg_memory.encode_row(store.build_memory_version_row(source))
    if legacy:
        head.pop("requires_review")
    wrote = []

    class Conn:
        async def fetch(self, sql, *args):
            if "loom.memory_current c" in sql:
                return [{**head, "row": head}]
            return [{"org": source.org, "scope": source.namespace, "generation": 5}]

        async def fetchval(self, *args):
            return False

        async def execute(self, sql, *args):
            wrote.append((sql, args))

    @asynccontextmanager
    async def transaction():
        yield Conn()

    monkeypatch.setattr(pg, "transaction", transaction)
    replacement = store.build_memory_version_row(
        source, status="active", requires_review=0, version=source.version + 1
    )
    if legacy:
        with pytest.raises(PermissionError):
            await pg_memory.commit_versions([replacement], [{}])
        assert not any(
            sql in (pg_memory._UPSERT_HEADS, pg_memory._INSERT_REVISIONS) for sql, _ in wrote
        )
    else:
        await pg_memory.commit_versions([replacement], [{}])
        heads = next(args[-1] for sql, args in wrote if sql == pg_memory._UPSERT_HEADS)
        assert heads[0]["requires_review"] == 0


def test_consolidation_cannot_drop_pending_proposal_provenance():
    from server.core.datetimes import naive_utc_now
    from server.memory.api.models_admin import MemoryConsolidateInput
    from server.memory.app.consolidate import (
        _CONSOLIDATION_CANDIDATES_SQL,
        _consolidation_canonical_row,
    )
    from server.memory.domain.proposal_review import enforce_review

    pending = _row(status="active", requires_review=True)
    row = _consolidation_canonical_row(
        MemoryConsolidateInput(org="o", namespace="ns", agent="a"),
        [pending.model_dump()],
        canonical_key="merged",
        canonical_value="derived fact",
        content_sha256="a" * 64,
        version=2,
        now=naive_utc_now(),
        sensitivity="",
    )
    assert row["requires_review"] == 1
    assert "m.requires_review = 0" in _CONSOLIDATION_CANDIDATES_SQL
    with pytest.raises(PermissionError):
        enforce_review([row], {})
