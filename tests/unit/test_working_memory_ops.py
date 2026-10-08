"""Unit tests for working_memory.* handlers."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_working_memory import (
    DEFAULT_CHAR_LIMIT,
    WorkingMemoryAppendInput,
    WorkingMemoryDeleteInput,
    WorkingMemoryRenderInput,
    WorkingMemoryReplaceInput,
    WorkingMemorySetInput,
)
from server.memory.app import working_memory as wm_handlers


class StubCH:
    def __init__(self) -> None:
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table: str, rows: list[dict[str, Any]]):
        self.insert_calls.append((table, rows))


def _patch_ch(monkeypatch: pytest.MonkeyPatch, ch: StubCH) -> None:
    monkeypatch.setattr(wm_handlers, "get_client", lambda: ch)


def _set_input(**overrides: Any) -> WorkingMemorySetInput:
    data = {
        "org": "o",
        "namespace": "n",
        "agent": "agent-a",
        "user_id": "u",
        "session_id": "s",
        "run_id": "r",
        "block_key": "scratchpad",
        "content": "current task state",
    }
    data.update(overrides)
    return WorkingMemorySetInput(**data)


@pytest.mark.asyncio
async def test_working_memory_set_inserts_table_row_identity(monkeypatch) -> None:
    ch = StubCH()
    _patch_ch(monkeypatch, ch)

    out = await wm_handlers.handle_working_memory_set(_set_input(char_limit=512))

    assert out.block_key == "scratchpad"
    assert out.char_limit == 512
    table, rows = ch.insert_calls[0]
    row = rows[0]
    assert table == "loom.working_memory"
    assert row["namespace"] == "n"
    assert row["user_id"] == "u"
    assert row["run_id"] == "r"
    assert row["content"] == "current task state"
    assert "ns" not in row
    assert "uid" not in row


@pytest.mark.asyncio
async def test_working_memory_append_preserves_existing_char_limit(monkeypatch) -> None:
    ch = StubCH()
    ch.query_one_returns.append(
        {
            "block_key": "scratchpad",
            "content": "one",
            "char_limit": 64,
            "version": 1,
            "updated_at": None,
        }
    )
    _patch_ch(monkeypatch, ch)

    out = await wm_handlers.handle_working_memory_append(
        WorkingMemoryAppendInput(
            org="o",
            namespace="n",
            agent="agent-a",
            user_id="u",
            session_id="s",
            run_id="r",
            block_key="scratchpad",
            addition="two",
        )
    )

    assert out.value_length == len("one\ntwo")
    # The pressure number rides every mutation: 7 of 64 chars used.
    assert out.fill_ratio == round(7 / 64, 3)
    row = ch.insert_calls[0][1][0]
    assert row["content"] == "one\ntwo"
    assert row["char_limit"] == 64


@pytest.mark.asyncio
async def test_working_memory_get_reports_fill_ratio(monkeypatch) -> None:
    """A caller sees the block filling before the 422 at char_limit."""
    ch = StubCH()
    ch.query_one_returns.append(
        {
            "block_key": "scratchpad",
            "content": "x" * 3000,
            "char_limit": 4096,
            "version": 1,
            "updated_at": None,
        }
    )
    _patch_ch(monkeypatch, ch)
    from server.memory.api.models_working_memory import WorkingMemoryGetInput

    out = await wm_handlers.handle_working_memory_get(
        WorkingMemoryGetInput(
            org="o",
            namespace="n",
            agent="agent-a",
            user_id="u",
            session_id="s",
            run_id="r",
            block_key="scratchpad",
        )
    )
    assert out.found and out.fill_ratio == round(3000 / 4096, 3)
    assert wm_handlers.fill_ratio(10, 0) == 0.0


@pytest.mark.asyncio
async def test_working_memory_append_missing_block_creates_it(monkeypatch) -> None:
    ch = StubCH()
    ch.query_one_returns.append(None)
    _patch_ch(monkeypatch, ch)

    await wm_handlers.handle_working_memory_append(
        WorkingMemoryAppendInput(
            org="o",
            namespace="n",
            agent="agent-a",
            user_id="u",
            session_id="s",
            run_id="r",
            addition="first",
        )
    )

    row = ch.insert_calls[0][1][0]
    assert row["block_key"] == "scratchpad"
    assert row["content"] == "first"
    assert row["char_limit"] == DEFAULT_CHAR_LIMIT


@pytest.mark.asyncio
async def test_working_memory_replace_missing_substring_returns_422(monkeypatch) -> None:
    ch = StubCH()
    ch.query_one_returns.append(
        {
            "block_key": "scratchpad",
            "content": "seed",
            "char_limit": DEFAULT_CHAR_LIMIT,
            "version": 1,
            "updated_at": None,
        }
    )
    _patch_ch(monkeypatch, ch)

    with pytest.raises(HTTPException) as excinfo:
        await wm_handlers.handle_working_memory_replace(
            WorkingMemoryReplaceInput(
                org="o",
                namespace="n",
                agent="agent-a",
                user_id="u",
                session_id="s",
                old="absent",
                new="new",
            )
        )

    assert excinfo.value.status_code == 422
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_working_memory_render_filters_and_escapes_xml(monkeypatch) -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "block_key": 'plan"',
                "content": "A & B < C",
                "char_limit": DEFAULT_CHAR_LIMIT,
                "version": 1,
                "updated_at": None,
            }
        ]
    )
    _patch_ch(monkeypatch, ch)

    out = await wm_handlers.handle_working_memory_render(
        WorkingMemoryRenderInput(
            org="o",
            namespace="n",
            agent="agent-a",
            user_id="u",
            session_id="s",
            block_keys=["plan"],
        )
    )

    sql, params = ch.query_all_calls[0]
    assert "block_key IN {keys:Array(String)}" in sql
    assert params["keys"] == ["plan"]
    assert 'block="plan&quot;"' in out.rendered
    assert "A &amp; B &lt; C" in out.rendered
    assert out.block_count == 1
    assert out.total_chars == len("A & B < C")


@pytest.mark.asyncio
async def test_working_memory_delete_writes_tombstone_row(monkeypatch) -> None:
    ch = StubCH()
    ch.query_one_returns.append(
        {
            "block_key": "scratchpad",
            "content": "done",
            "char_limit": DEFAULT_CHAR_LIMIT,
            "version": 1,
            "updated_at": None,
        }
    )
    _patch_ch(monkeypatch, ch)

    out = await wm_handlers.handle_working_memory_delete(
        WorkingMemoryDeleteInput(
            org="o",
            namespace="n",
            agent="agent-a",
            user_id="u",
            session_id="s",
        )
    )

    assert out.deleted is True
    row = ch.insert_calls[0][1][0]
    assert row["tombstoned_at"] is not None
    assert row["content"] == "done"


def test_working_memory_input_allows_full_server_cap() -> None:
    inp = _set_input(content="x" * 5000, char_limit=5000)
    assert len(inp.content) == 5000
    assert inp.char_limit == 5000
