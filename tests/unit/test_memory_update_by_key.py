"""memory.update: a key-addressed update.

The key resolves namespace-wide unless narrowed by identity; one live row
takes the update_by_id write path, none answers not_found, several answer
409 AMBIGUOUS_KEY with the candidates; the hash precondition passes through.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_crud import MemoryUpdateInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import update as update_mod
from server.memory.store import MemoryRow

_NOW = datetime.now(UTC).replace(tzinfo=None)
_ID_A = "22222222-2222-2222-2222-222222222222"
_ID_B = "33333333-3333-3333-3333-333333333333"


def _row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": _ID_A,
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "deploy_day",
        "memory_type": "semantic",
        "value": "Deploys happen on Tuesday",
        "confidence": 0.8,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=30),
        "expires_at": None,
        "entities": [],
    }
    base.update(overrides)
    return MemoryRow(**base)


class _StubCH:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = candidates
        self.inserts: list[Any] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, dict(params or {})))
        return list(self.candidates)

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


def _cand(memory_id: str, agent: str = "cursor", version: int = 100) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "agent": agent,
        "user_id": "-",
        "session_id": "-",
        "version": version,
        "status": "active",
    }


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(candidates: list[dict[str, Any]], row: MemoryRow | None) -> _StubCH:
        ch = _StubCH(candidates)
        monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
        monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
        monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
        monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

        async def _get_by_id(memory_id: str, **_: Any) -> MemoryRow | None:
            return row if row is not None and memory_id == row.memory_id else None

        monkeypatch.setattr("server.memory.store.get_by_id", _get_by_id)

        # The conditional write's key lock, held in process memory.
        from tests.unit.claims_fake import memory_claims

        memory_claims(monkeypatch)

        async def _enforce(**_: Any) -> None:
            return None

        monkeypatch.setattr("server.memory.app.blacklist_guard.enforce_on_insert", _enforce)

        # The shared write path checks the new value for
        # conflicts and runs same-key supersession; keep both inert here.
        from server.memory.app import conflicts as cf
        from server.memory.app import key_supersession as ks
        from server.memory.domain.conflicts import ConflictResult

        async def _clear(**_: Any) -> ConflictResult:
            return ConflictResult(verdict="clear", peers=[])

        async def _zero(**_: Any) -> int:
            return 0

        monkeypatch.setattr(cf, "check_on_commit", _clear)
        monkeypatch.setattr(ks, "supersede_same_key_rows", _zero)
        return ch

    return _wire


@pytest.mark.asyncio
async def test_one_live_row_is_updated_through_the_shared_write_path(wired) -> None:
    ch = wired([_cand(_ID_A)], _row())
    out = await update_mod.handle_memory_update(
        MemoryUpdateInput(
            org="test",
            namespace="personal",
            memory_key="deploy_day",
            value="Deploys happen on Thursday",
            confidence=0.95,
            reason="user corrected it",
        )
    )
    assert out.updated and out.memory_id == _ID_A and out.memory_key == "deploy_day"
    assert out.new_version > 100 and out.content_sha256 and out.reason == ""
    table, rows = ch.inserts[0]
    assert table == "loom.memory"
    written = rows[0]
    assert written["value"] == '"Deploys happen on Thursday"'
    assert written["memory_id"] == _ID_A  # identity preserved, version bumped
    assert written["confidence"] == 0.95
    # The resolver asked namespace-wide: no identity predicate without a caller value.
    sql, params = ch.queries[0]
    assert "memory_key = {key:String}" in sql and "agent =" not in sql
    assert params == {"org": "test", "ns": "personal", "key": "deploy_day"}
    assert "status IN ('active', 'duplicate', 'conflicted')" in sql


@pytest.mark.asyncio
async def test_no_live_row_answers_not_found_without_writing(wired) -> None:
    ch = wired([], None)
    out = await update_mod.handle_memory_update(
        MemoryUpdateInput(org="test", namespace="personal", memory_key="deploy_day", value="x")
    )
    assert out.updated is False and out.reason == "not_found" and out.memory_key == "deploy_day"
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_several_live_rows_answer_409_with_the_candidates(wired) -> None:
    ch = wired([_cand(_ID_B, agent="claude-code", version=200), _cand(_ID_A)], _row())
    with pytest.raises(HTTPException) as e:
        await update_mod.handle_memory_update(
            MemoryUpdateInput(org="test", namespace="personal", memory_key="deploy_day", value="x")
        )
    assert e.value.status_code == 409
    d = e.value.detail
    assert d["code"] == "AMBIGUOUS_KEY" and d["memory_key"] == "deploy_day"
    assert [c["memory_id"] for c in d["candidates"]] == [_ID_B, _ID_A]
    assert d["candidates"][0]["agent"] == "claude-code"
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_identity_fields_narrow_the_resolution(wired) -> None:
    ch = wired([_cand(_ID_A)], _row())
    out = await update_mod.handle_memory_update(
        MemoryUpdateInput(
            org="test",
            namespace="personal",
            memory_key="deploy_day",
            value="x",
            agent="cursor",
            session_id="s-1",
        )
    )
    assert out.updated
    sql, params = ch.queries[0]
    assert "agent = {agent:String}" in sql and "session_id = {session_id:String}" in sql
    assert "user_id =" not in sql  # "" = any
    assert params["agent"] == "cursor" and params["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_hash_precondition_passes_through_to_the_write_path(wired) -> None:
    ch = wired([_cand(_ID_A)], _row())
    with pytest.raises(HTTPException) as e:
        await update_mod.handle_memory_update(
            MemoryUpdateInput(
                org="test",
                namespace="personal",
                memory_key="deploy_day",
                value="x",
                if_match_sha256="0" * 64,
            )
        )
    assert e.value.status_code == 409 and e.value.detail["code"] == "PRECONDITION_FAILED"
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_update_by_id_still_writes_through_the_shared_helper(wired) -> None:
    from server.memory.api.models_crud import MemoryUpdateByIdInput

    ch = wired([], _row())
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org="test", memory_id=_ID_A, value="edited")
    )
    assert out.updated and ch.inserts[0][1][0]["value"] == '"edited"'
    assert ch.inserts[0][1][0]["confidence"] == 0.8  # no confidence field → kept


def test_op_is_registered_writer_and_in_the_core_toolset() -> None:
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS
    from server.operations.toolsets import _MCP_CORE_OPS
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = next(o for o in ALL_OPERATIONS if o.name == "memory.update")
    # The handler writes the audit row (the test below); a transport row would
    # carry the value and fire on not_found too.
    assert op.required_role == Role.WRITER and not op.audit and not op.idempotent
    assert "memory.update" in _MCP_CORE_OPS
    assert "changed or got more specific" in TOOL_DESCRIPTIONS["memory.update"]


@pytest.mark.asyncio
async def test_an_update_that_writes_a_credential_is_labelled_high_and_leaves_search(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory.set never embeds a 'high' row (the missing embedding row is what keeps a
    credential out of every search leg); a revision that puts one into a plain row gets
    the same label, no embedding row, and the key's old embedding rows tombstoned."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", True)
    ch = wired([_cand(_ID_A)], _row())
    dropped: list[tuple[str, int]] = []

    async def _retire(items, *, ch=None):
        dropped.extend((key[5], version) for key, version in items)
        return 1

    from server.memory.store import embeddings as embeddings_store

    monkeypatch.setattr(embeddings_store, "retire_vectors_for_keys", _retire)
    out = await update_mod.handle_memory_update(
        MemoryUpdateInput(
            org="test",
            namespace="personal",
            memory_key="deploy_day",
            value="deploy key sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWX0123",
        )
    )
    assert out.updated
    [written] = [r for t, rows in ch.inserts if t == "loom.memory" for r in rows]
    assert written["sensitivity"] == "high" and "sensitivity:high" in written["tags"]
    # the only embeddings row is the fence: no vector, no text, tombstoned at the new
    # row's version, so an older write's vector that lands late stays under it
    [fence] = [r for t, rows in ch.inserts if t == "loom.memory_embeddings" for r in rows]
    assert fence["embedding_dim"] == 0 and fence["content_excerpt"] == ""
    assert fence["tombstoned_at"] is not None and fence["version"] == written["version"]
    # at the new row's version, so a concurrent embedded write's newer vector survives
    assert dropped == [("deploy_day", written["version"])]


@pytest.mark.asyncio
async def test_update_announces_its_row_when_supersession_fails(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version row is in before the supersession runs: a failure there answers an
    error, and the memory.revised event is already in the outbox."""
    wired([_cand(_ID_A)], _row())
    announced: list[Any] = []

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    async def _supersede(**_: Any) -> int:
        raise RuntimeError("supersession insert failed")

    from server.memory.app import key_supersession as ks

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    monkeypatch.setattr(ks, "supersede_same_key_rows", _supersede)
    with pytest.raises(RuntimeError, match="supersession"):
        await update_mod.handle_memory_update(
            MemoryUpdateInput(
                org="test",
                namespace="personal",
                memory_key="deploy_day",
                value="Deploys happen on Thursday",
            )
        )
    [(event, row)] = announced
    assert event == "memory.revised" and row["memory_id"] == _ID_A


@pytest.mark.asyncio
@pytest.mark.parametrize(("wrote", "shape"), [(True, "webhook"), (False, "")])
async def test_an_update_is_held_to_the_webhook_budget_only_when_it_wrote_outbox_rows(
    wired, monkeypatch: pytest.MonkeyPatch, wrote: bool, shape: str
) -> None:
    from server.observability.budget_shape import current_budget_shape, note_budget_shape

    wired([_cand(_ID_A)], _row())

    async def _announce(changes: Any) -> bool:
        return wrote

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    note_budget_shape("")
    await update_mod.handle_memory_update(
        MemoryUpdateInput(
            org="test",
            namespace="personal",
            memory_key="deploy_day",
            value="Deploys happen on Thursday",
        )
    )
    assert current_budget_shape() == shape


@pytest.mark.asyncio
@pytest.mark.parametrize(("wrote", "shape"), [(True, "webhook"), (False, "")])
async def test_an_update_by_id_is_held_to_the_webhook_budget_only_when_it_wrote_outbox_rows(
    wired, monkeypatch: pytest.MonkeyPatch, wrote: bool, shape: str
) -> None:
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.observability.budget_shape import current_budget_shape, note_budget_shape

    wired([], _row())

    async def _announce(changes: Any) -> bool:
        return wrote

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    note_budget_shape("")
    await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org="test", memory_id=_ID_A, value="edited")
    )
    assert current_budget_shape() == shape


@pytest.mark.asyncio
async def test_update_announces_its_row_when_the_high_fence_fails(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version row is in before the fence: a fence that fails answers an error, and
    the memory.revised event is already in the outbox."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", True)
    ch = wired([_cand(_ID_A)], _row())
    announced: list[Any] = []

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    async def _fence(*_: Any, **__: Any) -> None:
        raise RuntimeError("fence insert failed")

    async def _retire(*_: Any, **__: Any) -> int:
        return 1

    from server.memory.store import embeddings as embeddings_store

    monkeypatch.setattr(embeddings_store, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    monkeypatch.setattr(update_mod, "fence_high", _fence)
    with pytest.raises(RuntimeError, match="fence"):
        await update_mod.handle_memory_update(
            MemoryUpdateInput(
                org="test",
                namespace="personal",
                memory_key="deploy_day",
                value="deploy key sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWX0123",
            )
        )
    assert [t for t, _ in ch.inserts].count("loom.memory") == 1
    [(event, row)] = announced
    assert event == "memory.revised" and row["sensitivity"] == "high"


@pytest.mark.asyncio
async def test_update_fences_a_high_key_when_cancelled_during_the_write(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline or a disconnect that cancels the write while its inserts run still
    writes the high fence: the version row may already be in."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", True)
    wired([_cand(_ID_A)], _row())
    inserting = asyncio.Event()
    fenced: list[Any] = []

    async def _insert(*_: Any, **__: Any) -> None:
        inserting.set()
        await asyncio.Event().wait()

    async def _fence(keys: Any, **_: Any) -> None:
        fenced.extend(keys)

    async def _retire(*_: Any, **__: Any) -> int:
        return 1

    from server.memory.store import embeddings as embeddings_store

    monkeypatch.setattr(embeddings_store, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(update_mod, "insert_memory_versions", _insert)
    monkeypatch.setattr(update_mod, "fence_high", _fence)
    task = asyncio.create_task(
        update_mod.handle_memory_update(
            MemoryUpdateInput(
                org="test",
                namespace="personal",
                memory_key="deploy_day",
                value="deploy key sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWX0123",
            )
        )
    )
    await inserting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fenced) == 1


def _audit_rows(ch: _StubCH) -> list[dict[str, Any]]:
    return [r for table, rows in ch.inserts if table == "loom.audit_log" for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["memory.update", "memory.update_by_id"])
async def test_a_revision_leaves_one_hash_only_audit_row_in_the_rows_namespace(
    wired, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    import json

    from server.memory.api.models_crud import MemoryUpdateByIdInput

    ch = wired([_cand(_ID_A)], _row())
    monkeypatch.setattr("server.observability.audit.get_client", lambda: ch)
    if op == "memory.update":
        out: Any = await update_mod.handle_memory_update(
            MemoryUpdateInput(
                org="test", namespace="personal", memory_key="deploy_day", value="Thursday"
            )
        )
    else:
        out = await update_mod.handle_memory_update_by_id(
            MemoryUpdateByIdInput(org="test", memory_id=_ID_A, value="Thursday")
        )
    assert out.updated
    [row] = _audit_rows(ch)
    assert row["event_type"] == op and row["org"] == "test" and row["namespace"] == "personal"
    subject = json.loads(row["subject"])
    assert subject == {
        "memory_key": "deploy_day",
        "memory_id": _ID_A,
        "content_sha256": out.content_sha256,
        "source_class": "",
        "version": out.new_version,
        "verdict": "clear",
    }
    assert "Thursday" not in row["subject"]


@pytest.mark.asyncio
async def test_an_update_that_finds_no_row_leaves_no_audit_row(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_crud import MemoryUpdateByIdInput

    ch = wired([], None)
    monkeypatch.setattr("server.observability.audit.get_client", lambda: ch)
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org="test", memory_id=_ID_A, value="x")
    )
    assert out.updated is False and _audit_rows(ch) == []
