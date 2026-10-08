"""Unit tests for conversation.update and conversation.delete.

Boundaries we stub:
  - conversation_repo.fetch_thread       (live row lookup)
  - conversation_repo.insert_thread      (update: RMT-collapsing rewrite)
  - conversation_repo.tombstone_thread   (delete: bulk soft-delete)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

# ============================================================
# conversation.update
# ============================================================


@pytest.mark.asyncio
async def test_update_title_merges_with_existing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationUpdateInput
    from server.knowledge.app.conversations import handle_conversation_update
    from server.knowledge.store import conversations as conversation_repo

    existing = {
        "thread_id": "t-1",
        "title": "Untitled",
        "metadata": '{"owner":"alice"}',
        "run_id": "run-7",
        "user_id": "alice",
        "agent": "reviewer",
        "created_at": datetime(2026, 5, 18, 10, 0, 0),
        "updated_at": datetime(2026, 5, 18, 10, 0, 0),
    }
    captured: dict[str, Any] = {}

    async def fake_fetch(*a, **kw):
        return existing

    async def fake_insert(row, *, ch=None):
        captured.update(row)

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch)
    monkeypatch.setattr(conversation_repo, "insert_thread", fake_insert)

    out = await handle_conversation_update(
        ConversationUpdateInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            title="Q1 incident response",
        )
    )
    assert out.updated is True
    assert out.thread_id == "t-1"
    # Title replaced; existing metadata preserved (not overwritten because
    # the caller only passed title).
    assert captured["title"] == "Q1 incident response"
    assert captured["metadata"] == '{"owner":"alice"}'
    assert captured["run_id"] == "run-7"
    assert captured["user_id"] == "alice"
    # created_at carried over from existing.
    assert captured["created_at"] == existing["created_at"]


@pytest.mark.asyncio
async def test_update_metadata_replaces_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationUpdateInput
    from server.knowledge.app.conversations import handle_conversation_update
    from server.knowledge.store import conversations as conversation_repo

    existing = {
        "thread_id": "t-1",
        "title": "Existing title",
        "metadata": '{"owner":"alice"}',
        "run_id": "-",
        "user_id": "-",
        "agent": "reviewer",
        "created_at": datetime(2026, 5, 18, 10, 0, 0),
        "updated_at": datetime(2026, 5, 18, 10, 0, 0),
    }
    captured: dict[str, Any] = {}

    async def fake_fetch(*a, **kw):
        return existing

    async def fake_insert(row, *, ch=None):
        captured.update(row)

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch)
    monkeypatch.setattr(conversation_repo, "insert_thread", fake_insert)

    out = await handle_conversation_update(
        ConversationUpdateInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            metadata={"priority": "high"},
        )
    )
    assert out.updated is True
    # Metadata replaced; title preserved.
    assert '"priority"' in captured["metadata"]
    assert captured["title"] == "Existing title"


@pytest.mark.asyncio
async def test_update_thread_missing_returns_updated_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationUpdateInput
    from server.knowledge.app.conversations import handle_conversation_update
    from server.knowledge.store import conversations as conversation_repo

    async def fake_fetch(*a, **kw):
        return None

    insert_called = {"v": False}

    async def fake_insert(row, *, ch=None):
        insert_called["v"] = True

    monkeypatch.setattr(conversation_repo, "fetch_thread", fake_fetch)
    monkeypatch.setattr(conversation_repo, "insert_thread", fake_insert)

    out = await handle_conversation_update(
        ConversationUpdateInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="ghost",
            title="x",
        )
    )
    assert out.updated is False
    # Soft-fail: don't materialize a new thread on update of missing.
    assert insert_called["v"] is False


@pytest.mark.asyncio
async def test_update_with_no_fields_raises_400() -> None:
    from server.knowledge.api.models_conversations import ConversationUpdateInput
    from server.knowledge.app.conversations import handle_conversation_update

    with pytest.raises(HTTPException) as exc:
        await handle_conversation_update(
            ConversationUpdateInput(
                org="clickhouse",
                namespace="finance",
                agent="reviewer",
                thread_id="t-1",  # nothing else set
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "NO_FIELDS_PROVIDED"


# ============================================================
# conversation.delete
# ============================================================


@pytest.mark.asyncio
async def test_delete_tombstones_three_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_conversations import ConversationDeleteInput
    from server.knowledge.app.conversations import handle_conversation_delete
    from server.knowledge.store import conversations as conversation_repo

    captured: dict[str, Any] = {}

    async def fake_tombstone(org, ns, agent, thread_id, *, when, ch=None):
        captured["thread_id"] = thread_id
        captured["when"] = when
        return {
            "loom.conversations": 42,
            "loom.conversation_threads": 1,
            "loom.conversation_embeddings": 42,
        }

    monkeypatch.setattr(conversation_repo, "tombstone_thread", fake_tombstone)

    out = await handle_conversation_delete(
        ConversationDeleteInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            thread_id="t-1",
            confirm_thread_id="t-1",
        )
    )
    assert out.thread_id == "t-1"
    assert out.total == 85
    assert out.rows_affected["loom.conversations"] == 42
    assert out.rows_affected["loom.conversation_threads"] == 1
    assert out.rows_affected["loom.conversation_embeddings"] == 42
    assert captured["thread_id"] == "t-1"


@pytest.mark.asyncio
async def test_delete_confirm_mismatch_raises_400() -> None:
    from server.knowledge.api.models_conversations import ConversationDeleteInput
    from server.knowledge.app.conversations import handle_conversation_delete

    with pytest.raises(HTTPException) as exc:
        await handle_conversation_delete(
            ConversationDeleteInput(
                org="clickhouse",
                namespace="finance",
                agent="reviewer",
                thread_id="t-1",
                confirm_thread_id="t-2",  # mismatch
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_THREAD_ID_MISMATCH"


# ============================================================
# Op registration
# ============================================================


@pytest.mark.asyncio
async def test_update_op_registered_with_writer_role() -> None:
    from server.knowledge.api.ops_conversations import OPERATIONS
    from server.operations.base import Role

    op = next(o for o in OPERATIONS if o.name == "conversation.update")
    assert op.required_role == Role.WRITER
    assert op.idempotent is True
    assert op.audit is False


@pytest.mark.asyncio
async def test_delete_op_registered_with_admin_role_and_audit() -> None:
    """Destructive op: ADMIN-only and audit=True.

    Single-thread soft-delete is bounded enough not to need the full
    erasure saga, but it still mutates state — audit=True ensures the
    event lands in loom.audit_log under the hash chain."""
    from server.knowledge.api.ops_conversations import OPERATIONS
    from server.operations.base import Role

    op = next(o for o in OPERATIONS if o.name == "conversation.delete")
    assert op.required_role == Role.ADMIN
    assert op.audit is True
