"""`memory.move` renames a key and leaves the old key readable.

The destination is written through the real `memory.set` path (stubbed here,
its input asserted); the SOURCE row gains a version with `status='duplicate'`
and `duplicate_of` = the destination's memory_id, every other column carried
forward; the search mirror is re-emitted with the same status; the source's
chunks are tombstoned. After the move `memory.get(from_key)` answers the old
value with `canonical_key == to_key`, `follow: true` serves the new row with
`resolved_from == from_key`, list hides the old row by default and history
reads the append-only mirror the redirect version lands in. CH-free.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

import server.memory.app.handlers as mem_handlers
import server.memory.app.move as mv
from server.core.hashing import sha256_value_json
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.api.models_crud import MemoryGetInput, MemorySetOutput
from server.memory.api.models_move import MemoryMoveInput
from server.memory.app import write as write_mod
from server.memory.store import (
    EMBEDDING_PERSISTED_COLS,
    MEMORY_PERSISTED_COLS,
    MemoryRow,
)
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import _MCP_ATOMIC_OPS, _MCP_CODING_OPS, _MCP_CORE_OPS, op_toolsets

_NOW = datetime(2026, 9, 18, 9, 0, 0)
_FROM = "alert_3f9c1a2b"
_TO = "replica_lag_after_merge_storm"
_SRC_ID = "11111111-1111-1111-1111-111111111111"
_DEST_ID = "22222222-2222-2222-2222-222222222222"
_OLD_VALUE = {"alert": "ReplicaLag", "host": "ch-3", "minutes": 42}
_NEW_VALUE = "Replica lag follows a merge storm on the shared tier; lower the merge pool."


def _source(**over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": _SRC_ID,
        "org": "test",
        "namespace": "ops-alerts",
        "agent": "sre",
        "user_id": "-",
        "session_id": "-",
        "memory_key": _FROM,
        "memory_type": "semantic",
        "value": _OLD_VALUE,
        "confidence": 0.9,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=3),
        "updated_at": _NOW - timedelta(days=1),
        "expires_at": None,
        "valid_at": datetime(2026, 9, 15),
        "run_id": "run-7",
        "source_trace": "observed://alertmanager",
        "tags": ["alert", "replica"],
        "entities": ["ch-3"],
        "temporal_anchor": datetime(2026, 9, 15, 8),
        "temporal_confidence": 0.7,
        "reinforcement_count": 3,
        "last_reinforced_at": _NOW - timedelta(days=1),
        "source_resource_id": "res-1",
        "actor": "process:sre-bot",
        "source_class": "observed",
        "sensitivity": "low",
        "files": ["deploy/ch.yaml"],
        "concepts": ["merge_storm"],
        "verified_by": "human:owner",
        "verified_at": _NOW - timedelta(days=2),
        "verified_sha256": "abc",
        "grounding": "cited",
        "evidence": "row 1000",
        "on_expire": "demote",
        "expire_reason": "seasonal",
        "char_limit": 4000,
    }
    base.update(over)
    return MemoryRow(**base)


_MIRROR_ROW: dict[str, Any] = {
    **EMBEDDING_PERSISTED_COLS,
    "org": "test",
    "namespace": "ops-alerts",
    "agent": "sre",
    "user_id": "-",
    "session_id": "-",
    "memory_key": _FROM,
    "content_excerpt": "ReplicaLag ch-3 42 (#alert_3f9c1a2b)",
    "embedding_model": "fake-test",
    "embedding_dim": 3,
    "embedding": [0.1, 0.2, 0.3],
    "created_at": _NOW - timedelta(days=3),
    "last_reinforced_at": _NOW - timedelta(days=1),
    "version": 100,
    "tags": ["alert", "replica"],
    "entities": ["ch-3"],
    "status": "active",
    "confidence": 0.9,
    "source_resource_id": "res-1",
    "source_class": "observed",
    "verified": 1,
    "files": ["deploy/ch.yaml"],
    "concepts": ["merge_storm"],
}
_OTHER_AGENT_MIRROR = {**_MIRROR_ROW, "agent": "other-agent"}

_CHUNK_ROW: dict[str, Any] = {
    "agent": "sre",
    "session_id": f"memory.set:{_FROM}",
    "run_id": "-",
    "chunk_index": 0,
    "content_sha256": "c" * 64,
    "content": "Replica lag …",
    "embedding": [0.1, 0.1, 0.1],
    "embedding_model": "fake-test",
    "embedding_dim": 3,
    "parent_memory_key": _FROM,
    "line_start": 1,
    "line_end": 4,
}


class _CH:
    """Records inserts; `query_all` answers routed by a SQL needle."""

    def __init__(self, routes: list[tuple[str, list[dict[str, Any]]]] | None = None) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_all_sqls: list[str] = []
        self.routes = routes or []
        self.fail_inserts_into: tuple[str, ...] = ()

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table in self.fail_inserts_into:
            raise RuntimeError(f"insert into {table} unavailable")
        self.inserts.append((table, [dict(r) for r in rows], settings or {}))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.query_all_sqls.append(sql)
        for needle, result in self.routes:
            if needle in sql:
                return [dict(r) for r in result]
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.inserts if t == table for r in rows]


def _ch_with_mirror_and_chunks() -> _CH:
    return _CH(
        routes=[
            ("FROM loom.memory_embeddings FINAL", [_MIRROR_ROW, _OTHER_AGENT_MIRROR]),
            ("FROM loom.memory_chunks FINAL", [_CHUNK_ROW]),
        ]
    )


class _Harness:
    """The move's collaborators: the source read, memory.set and memory.delete
    stubbed and recorded, a fake ClickHouse under the redirect write."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        source: MemoryRow | None,
        ch: _CH | None = None,
        set_out: MemorySetOutput | None = None,
    ) -> None:
        self.ch = ch or _ch_with_mirror_and_chunks()
        self.set_inputs: list[Any] = []
        self.delete_inputs: list[Any] = []
        self.get_by_key_calls: list[tuple[memory_repo.Identity, str]] = []
        self.set_out = set_out or MemorySetOutput(
            version=777,
            memory_id=_DEST_ID,
            content_sha256="d" * 64,
            memory_key=_TO,
            advisories=["base64-encoded"],
        )
        by_key: dict[str, MemoryRow | None] = {_FROM: source}

        async def _get_by_key(identity: Any, key: str, **_: Any) -> MemoryRow | None:
            self.get_by_key_calls.append((identity, key))
            return by_key.get(key)

        async def _set(inp: Any) -> MemorySetOutput:
            self.set_inputs.append(inp)
            return self.set_out

        async def _delete(inp: Any) -> Any:
            self.delete_inputs.append(inp)
            return object()

        self.by_key = by_key
        monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
        monkeypatch.setattr(write_mod, "handle_memory_set", _set)
        monkeypatch.setattr(
            "server.memory.app.write.handle_memory_set", write_mod.handle_memory_set
        )
        monkeypatch.setattr(mem_handlers, "handle_memory_delete", _delete)
        monkeypatch.setattr(mv, "get_client", lambda: self.ch)


def _inp(**kw: Any) -> MemoryMoveInput:
    base: dict[str, Any] = {
        "org": "test",
        "namespace": "ops-alerts",
        "agent": "sre",
        "from_key": _FROM,
        "to_key": _TO,
    }
    base.update(kw)
    return MemoryMoveInput(**base)


def _move(inp: MemoryMoveInput) -> Any:
    return asyncio.run(mv.handle_memory_move(inp))


def _refusal(inp: MemoryMoveInput) -> HTTPException:
    with pytest.raises(HTTPException) as ei:
        _move(inp)
    return ei.value


def _redirect_row(ch: _CH) -> dict[str, Any]:
    rows = ch.rows_for("loom.memory")
    assert len(rows) == 1, rows
    return rows[0]


# ---------------------------------------------------------------- the move


def test_plain_move_writes_the_destination_through_memory_set_with_the_sources_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source())
    out = _move(_inp())

    assert len(h.set_inputs) == 1
    s = h.set_inputs[0]
    assert (s.memory_key, s.value, s.memory_type) == (_TO, _OLD_VALUE, "semantic")
    assert (s.tags, s.entities, s.confidence) == (["alert", "replica"], ["ch-3"], 0.9)
    assert (s.valid_at, s.source_resource_id) == (datetime(2026, 9, 15), "res-1")
    assert s.source_trace == "observed://alertmanager"
    # the rename stays with the row's writer
    assert (s.org, s.namespace, s.agent, s.user_id, s.session_id) == (
        "test",
        "ops-alerts",
        "sre",
        "-",
        "-",
    )
    # `observed` is server-decided: not declared, so memory.set stamps it
    assert s.source_class is None
    assert s.classify is False and s.dry_run is False

    assert out.from_key == _FROM and out.to_key == _TO
    assert out.redirected is True and out.dry_run is False
    assert (out.memory_id, out.version, out.content_sha256) == (_DEST_ID, 777, "d" * 64)
    assert out.advisories == ["base64-encoded"]  # memory.set's, passed through

    red = _redirect_row(h.ch)
    assert red["status"] == "duplicate" and red["duplicate_of"] == _DEST_ID
    assert red["memory_key"] == _FROM and red["memory_id"] == _SRC_ID
    assert red["version"] > 100
    assert json.loads(red["value"]) == _OLD_VALUE  # the source's value is never rewritten


def test_a_declarable_source_class_is_carried_to_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source(source_class="imported"))
    _move(_inp())
    assert h.set_inputs[0].source_class == "imported"


def test_value_and_shape_overrides_reach_the_new_row_and_the_redirect_still_points_at_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source())
    out = _move(
        _inp(
            value=_NEW_VALUE,
            memory_type="episodic",
            tags=["claim"],
            entities=["shared-tier"],
            valid_at=datetime(2026, 9, 18),
            source_resource_id="res-2",
        )
    )
    s = h.set_inputs[0]
    assert s.value == _NEW_VALUE and s.memory_type == "episodic"
    assert s.tags == ["claim"] and s.entities == ["shared-tier"]
    assert s.valid_at == datetime(2026, 9, 18) and s.source_resource_id == "res-2"
    assert out.memory_type == "episodic"

    red = _redirect_row(h.ch)
    assert red["status"] == "duplicate" and red["duplicate_of"] == _DEST_ID
    assert json.loads(red["value"]) == _OLD_VALUE  # the old shape stays under the old key
    assert red["memory_type"] == "semantic" and red["tags"] == ["alert", "replica"]


def test_keep_redirect_false_takes_the_delete_path_and_writes_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source())
    out = _move(_inp(keep_redirect=False))
    assert len(h.set_inputs) == 1 and h.set_inputs[0].memory_key == _TO
    assert len(h.delete_inputs) == 1
    d = h.delete_inputs[0]
    assert (d.memory_key, d.agent, d.user_id, d.session_id) == (_FROM, "sre", "-", "-")
    assert out.redirected is False and out.memory_id == _DEST_ID
    assert h.ch.inserts == []  # the delete path (stubbed) owns the source's rows


def test_a_merge_that_wrote_nothing_still_redirects_to_the_live_destination_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(
        monkeypatch,
        source=_source(),
        set_out=MemorySetOutput(version=0, memory_id="", noop=True, conflict_verdict="merge"),
    )
    h.by_key[_TO] = _source(memory_id=_DEST_ID, memory_key=_TO, value=_NEW_VALUE)
    out = _move(_inp())
    assert out.memory_id == _DEST_ID
    assert _redirect_row(h.ch)["duplicate_of"] == _DEST_ID
    assert [k for _, k in h.get_by_key_calls] == [_FROM, _TO]


# ------------------------------------------------------------- the refusals


def test_missing_or_tombstoned_source_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, source=None)
    e = _refusal(_inp())
    assert e.status_code == 404 and e.detail["code"] == "NOT_FOUND"
    assert h.set_inputs == [] and h.ch.inserts == []

    h = _Harness(monkeypatch, source=_source(tombstoned_at=_NOW))
    e = _refusal(_inp())
    assert e.status_code == 404 and h.set_inputs == []


def test_same_key_is_400_before_any_read(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, source=_source())
    e = _refusal(_inp(to_key=_FROM))
    assert e.status_code == 400 and e.detail["code"] == "SAME_KEY"
    assert h.get_by_key_calls == [] and h.set_inputs == []


def test_an_already_redirected_source_is_409_naming_its_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source(status="duplicate", duplicate_of=_DEST_ID))
    e = _refusal(_inp())
    assert e.status_code == 409 and e.detail["code"] == "ALREADY_REDIRECTED"
    assert e.detail["duplicate_of"] == _DEST_ID
    assert h.set_inputs == [] and h.ch.inserts == []


def test_hash_precondition_is_409_on_mismatch_and_passes_on_the_current_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source())
    e = _refusal(_inp(if_match_sha256="0" * 64))
    assert e.status_code == 409 and e.detail["code"] == "PRECONDITION_FAILED"
    assert e.detail["current_sha256"] == sha256_value_json(json.dumps(_OLD_VALUE))
    assert h.set_inputs == []

    out = _move(_inp(if_match_sha256=sha256_value_json(json.dumps(_OLD_VALUE))))
    assert out.redirected is True and len(h.set_inputs) == 1


def test_a_read_only_lock_refuses_the_move_and_a_no_delete_lock_permits_a_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source(lock_mode="read_only", locked_by="human:owner"))
    e = _refusal(_inp())
    assert e.status_code == 423 and h.set_inputs == []

    h = _Harness(monkeypatch, source=_source(lock_mode="no_delete", locked_by="human:owner"))
    assert _move(_inp()).redirected is True
    e = _refusal(_inp(keep_redirect=False))
    assert e.status_code == 423 and h.delete_inputs == []


# ---------------------------------------------------------------- dry run


def test_dry_run_writes_nothing_and_previews_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source())
    long_value = "x" * 900
    out = _move(_inp(dry_run=True, value=long_value, memory_type="episodic"))
    assert out.dry_run is True and out.redirected is False
    assert (out.memory_id, out.version, out.content_sha256) == ("", 0, "")
    assert out.memory_type == "episodic" and out.value_preview == "x" * 200
    assert h.set_inputs == [] and h.delete_inputs == [] and h.ch.inserts == []

    out = _move(_inp(dry_run=True))
    assert out.value_preview == json.dumps(_OLD_VALUE)[:200]


def test_a_dry_run_never_previews_a_high_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # A high value is readable by key only, through an audited read.
    _Harness(monkeypatch, source=_source(sensitivity="high"))
    out = _move(_inp(dry_run=True))
    assert out.dry_run is True and out.value_preview == "" and out.sensitivity == "high"


# ------------------------------------------------ every column carried forward


def test_redirect_version_carries_every_memory_column_of_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    h = _Harness(monkeypatch, source=source)
    _move(_inp())
    red = _redirect_row(h.ch)
    assert set(red) == set(MEMORY_PERSISTED_COLS)
    expected = memory_repo.build_memory_version_row(source)
    changed = {"version", "status", "duplicate_of", "updated_at"}
    for col in MEMORY_PERSISTED_COLS:
        if col in changed:
            continue
        assert red[col] == expected[col], col
    assert red["status"] == "duplicate" and red["duplicate_of"] == _DEST_ID
    assert red["updated_at"] > source.updated_at
    # the typed write: committed, not async
    table, _, settings = h.ch.inserts[0]
    assert table == "loom.memory" and settings.get("async_insert") == 0


def test_mirror_rows_of_the_source_identity_are_restamped_duplicate_with_every_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, source=_source())
    _move(_inp())
    emb = h.ch.rows_for("loom.memory_embeddings")
    assert len(emb) == 1, emb  # the other agent's row under the same key is not touched
    row = emb[0]
    assert set(row) >= set(EMBEDDING_PERSISTED_COLS)
    assert row["status"] == "duplicate" and row["agent"] == "sre"
    assert row["version"] > _MIRROR_ROW["version"]
    for col in EMBEDDING_PERSISTED_COLS:
        if col in ("status", "version", "content_excerpt_lemma"):
            continue
        assert row[col] == _MIRROR_ROW[col], col
    assert any("FROM loom.memory_embeddings FINAL" in s for s in h.ch.query_all_sqls)


def test_source_chunks_are_tombstoned(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, source=_source())
    _move(_inp())
    chunks = h.ch.rows_for("loom.memory_chunks")
    assert len(chunks) == 1
    c = chunks[0]
    assert c["parent_memory_key"] == _FROM and c["tombstoned_at"] is not None
    assert c["session_id"] == f"memory.set:{_FROM}" and c["chunk_index"] == 0


def test_mirror_and_chunk_failures_leave_the_redirect_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ch_with_mirror_and_chunks()
    ch.fail_inserts_into = ("loom.memory_embeddings", "loom.memory_chunks")
    h = _Harness(monkeypatch, source=_source(), ch=ch)
    out = _move(_inp())
    assert out.redirected is True
    assert _redirect_row(h.ch)["status"] == "duplicate"


# ------------------------------------------- what the readers do afterwards


def _redirected_source_row(monkeypatch: pytest.MonkeyPatch) -> MemoryRow:
    """The source row as `memory.get` reads it after the move: built from the
    redirect version the handler inserted."""
    h = _Harness(monkeypatch, source=_source())
    _move(_inp())
    red = _redirect_row(h.ch)
    return MemoryRow.from_snapshot(red, value=_OLD_VALUE, pinned=bool(red["pinned"]))


def _destination_row() -> MemoryRow:
    return _source(memory_id=_DEST_ID, memory_key=_TO, value=_NEW_VALUE, version=777)


def _wire_readers(monkeypatch: pytest.MonkeyPatch, redirect: MemoryRow) -> None:
    async def _get_by_key(identity: Any, key: str, **_: Any) -> MemoryRow | None:
        return redirect if key == _FROM else None

    async def _get_by_id(memory_id: str, **_: Any) -> MemoryRow | None:
        return _destination_row() if memory_id == _DEST_ID else None

    async def _canonical_keys_for(org: str, ns: str, ids: list[str]) -> dict[str, str]:
        return {_DEST_ID: _TO} if _DEST_ID in ids else {}

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    monkeypatch.setattr(memory_repo, "get_by_id", _get_by_id)
    monkeypatch.setattr(
        "server.memory.app.served_duplicates.canonical_keys_for", _canonical_keys_for
    )


def test_memory_get_on_the_old_key_answers_the_old_value_with_canonical_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect = _redirected_source_row(monkeypatch)
    _wire_readers(monkeypatch, redirect)
    out = asyncio.run(
        mem_handlers._handle_memory_get(
            MemoryGetInput(org="test", namespace="ops-alerts", agent="sre", memory_key=_FROM)
        )
    )
    assert out.found is True and out.value == _OLD_VALUE
    assert out.status == "duplicate" and out.canonical_key == _TO
    assert out.resolved_from is None and out.memory_id == _SRC_ID


def test_memory_get_follow_serves_the_new_row_with_resolved_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect = _redirected_source_row(monkeypatch)
    _wire_readers(monkeypatch, redirect)
    out = asyncio.run(
        mem_handlers._handle_memory_get(
            MemoryGetInput(
                org="test", namespace="ops-alerts", agent="sre", memory_key=_FROM, follow=True
            )
        )
    )
    assert out.found is True and out.value == _NEW_VALUE
    assert out.memory_id == _DEST_ID and out.status == "active"
    assert out.resolved_from == _FROM and out.canonical_key == _TO


def test_memory_list_hides_the_redirect_by_default_and_include_duplicates_shows_it() -> None:
    ident = memory_repo.Identity(org="test", namespace="ops-alerts", agent="sre")
    ch = _CH()
    asyncio.run(
        memory_repo.list_by_namespace(ident, ch=ch, include_duplicates=False, scope=WHOLE_NAMESPACE)
    )
    assert "status != 'duplicate'" in ch.query_all_sqls[-1]
    asyncio.run(
        memory_repo.list_by_namespace(ident, ch=ch, include_duplicates=True, scope=WHOLE_NAMESPACE)
    )
    assert "status != 'duplicate'" not in ch.query_all_sqls[-1]


def test_search_and_list_predicates_hide_a_duplicate_row() -> None:
    from server.memory.domain import temporal_sql

    assert "duplicate" not in temporal_sql.SERVED_STATUSES  # search's legs
    assert "duplicate" in temporal_sql.LIVE_STATUSES  # the write path still sees it


def test_memory_history_reads_the_append_only_mirror_the_redirect_lands_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    ident = memory_repo.Identity(org="test", namespace="ops-alerts", agent="sre")
    asyncio.run(memory_repo.list_history(ident, _FROM, ch=ch, limit=10, scope=WHOLE_NAMESPACE))
    sql = ch.query_all_sqls[-1]
    assert "loom.memory_versions" in sql
    # the redirect is an INSERT into loom.memory; memory_versions_mv copies
    # every insert, status and duplicate_of included
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[2] / "schemas/schema.sql").read_text()
    mv_at = schema.index("loom.memory_versions_mv TO loom.memory_versions")
    body = schema[mv_at : mv_at + 4000]
    assert "status" in body and "duplicate_of" in body


# -------------------------------------------------------------- the registry


def test_the_op_is_writer_audited_not_idempotent_and_listed_under_all_only() -> None:
    op = {o.name: o for o in ALL_OPERATIONS}["memory.move"]
    assert op.required_role is Role.WRITER
    assert op.audit is True and op.idempotent is False
    assert op.maturity == "stable"
    assert op.input_model.__name__ == "MemoryMoveInput"
    assert op.output_model.__name__ == "MemoryMoveOutput"
    assert "memory.move" not in _MCP_CORE_OPS
    assert "memory.move" not in _MCP_CODING_OPS
    assert "memory.move" not in _MCP_ATOMIC_OPS
    assert op_toolsets("memory.move") == ["all"]
