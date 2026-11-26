"""Server semantics for the Rust SDK's recording identity tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("selector", ["author", "runner"])
@pytest.mark.asyncio
async def test_procedure_record_run_loads_the_selected_author_tuple(
    monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    from server.core.errors import NotFoundError
    from server.memory.api.models_procedure import ProcedureRecordRunInput
    from server.memory.app import handlers, procedure, procedure_ledger

    async def get(input):
        # The same key has one author-owned row; the runner has no such row.
        found = (input.agent, input.user_id, input.session_id) == (
            "author",
            "author-user",
            "saved-session",
        )
        return SimpleNamespace(
            value={"steps": ["ship"], "revision": 7} if found else None, content_sha256="hash"
        )

    record = AsyncMock()
    monkeypatch.setattr(handlers, "handle_memory_get", get)
    monkeypatch.setattr(procedure_ledger, "record", record)
    monkeypatch.setattr(procedure_ledger, "count_since", AsyncMock(return_value=(1, 1)))
    input = ProcedureRecordRunInput(
        org="org",
        namespace="ns",
        memory_key="procedure_shared",
        agent=selector,
        user_id=f"{selector}-user",
        session_id="saved-session" if selector == "author" else "new-session",
    )
    if selector == "runner":
        with pytest.raises(NotFoundError):
            await procedure.handle_procedure_record_run(input)
        record.assert_not_awaited()
    else:
        output = await procedure.handle_procedure_record_run(input)
        assert output.run_stats == {"total": 1, "ok": 1, "unfolded": 1}
        assert record.await_args.kwargs["revision"] == 7
        assert record.await_args.kwargs["agent"] == "author"


@pytest.mark.parametrize("op", ["create", "append"])
@pytest.mark.parametrize("run", [None, "run-42"])
@pytest.mark.asyncio
async def test_conversation_rows_take_run_identity_from_the_body(
    monkeypatch: pytest.MonkeyPatch, op: str, run: str | None
) -> None:
    from server.knowledge.api.models_conversations import (
        ConversationAppendInput,
        ConversationCreateInput,
    )
    from server.knowledge.app import conversations
    from server.knowledge.store import conversations as store

    written = AsyncMock()
    monkeypatch.setattr(store, "insert_thread", written)
    monkeypatch.setattr(store, "append", written)
    monkeypatch.setattr(store, "namespace_logging_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(store, "message_exists", AsyncMock(return_value=False))
    body = dict(
        org="org", namespace="ns", agent="runner", user_id="runner-user", thread_id="thread"
    )
    if run is not None:
        body["run_id"] = run
    if op == "create":
        await conversations.handle_conversation_create(ConversationCreateInput(**body))
        row = written.await_args.args[0]
        assert row["run_id"] == (run or "-")
    else:
        await conversations.handle_conversation_append(
            ConversationAppendInput(**body, role="tool", content="message")
        )
        row = written.await_args.args[0]
        assert row.run_id == (run or "-")
