"""The list answers what the page asked.

`memory.list` gains `preview_chars` (40–2,048; hidden from the MCP listing)
and its entries gain `value_truncated`, `content_sha256` and `char_limit`,
so a page can ask for the 400 characters its cards show and know when a
row holds more. The Memories page sends `fields` and `preview_chars`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pydantic
import pytest

from server.knowledge.api.models_conversations import ConversationThreadsInput
from server.memory import store as memory_repo
from server.memory.api import models_crud as memory_crud
from server.memory.app import query as _memory_query
from server.operations import read_projection
from server.tasks.api.models_run import RunListInput
from server.transport import mcp_schemas

_ROOT = Path(__file__).resolve().parents[2]


def _row(value: str, *, truncated: bool = False) -> memory_repo.MemoryRow:
    return memory_repo.MemoryRow(
        memory_id="00000000-0000-0000-0000-000000000001",
        org="acme",
        namespace="team",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value=value,
        value_truncated=truncated,
        confidence=1.0,
        version=1,
        pinned=False,
        expires_at=None,
        created_at="2026-09-19 00:00:00",
        updated_at="2026-09-19 00:00:00",
    )


def test_preview_chars_is_bounded_and_hidden_from_the_mcp_listing() -> None:
    ident = {"org": "acme", "namespace": "team", "agent": "*"}
    assert memory_crud.MemoryListInput(**ident, preview_chars=400).preview_chars == 400
    assert memory_crud.MemoryListInput(**ident).preview_chars == 2048
    with pytest.raises(pydantic.ValidationError):
        memory_crud.MemoryListInput(**ident, preview_chars=10)
    with pytest.raises(pydantic.ValidationError):
        memory_crud.MemoryListInput(**ident, preview_chars=4096)
    assert "preview_chars" in mcp_schemas._HIDDEN_OP_PROPS["memory.list"]


def test_memory_list_takes_an_offset_past_100k() -> None:
    """Exports and the LangGraph store page a whole namespace by offset, so memory.list has no cap."""
    ident = {"org": "acme", "namespace": "team", "agent": "*"}
    assert memory_crud.MemoryListInput(**ident, offset=250_000).offset == 250_000


def test_run_list_and_threads_refuse_an_offset_past_100k() -> None:
    for model in (RunListInput, ConversationThreadsInput):
        assert model(org="acme", namespace="team", offset=100_000).offset == 100_000
        with pytest.raises(pydantic.ValidationError):
            model(org="acme", namespace="team", offset=100_001)


def test_a_row_longer_than_the_preview_sent_is_marked_truncated() -> None:
    value, cut = _memory_query._preview_for_list(_row("x" * 500), 400)
    assert cut is True and len(value) == 401 and value.endswith("…")
    value, cut = _memory_query._preview_for_list(_row("short"), 400)
    assert cut is False and value == "short"
    # A stored preview the repository already cut at 2,048 stays truncated
    # whatever the caller asked for.
    value, cut = _memory_query._preview_for_list(_row("y" * 100, truncated=True), 400)
    assert cut is True and value == "y" * 100
    # A JSON object over the cap comes back as its text, cut.
    obj = json.dumps({"k": "v" * 600})
    value, cut = _memory_query._preview_for_list(_row(obj), 400)
    assert cut is True and isinstance(value, str) and len(value) == 401


def test_the_projection_accepts_the_three_new_entry_fields() -> None:
    keep = read_projection.resolve(
        memory_crud._MemoryListEntry,
        ["value", "value_truncated", "content_sha256", "char_limit"],
        always=read_projection.LIST_ENTRY_ALWAYS,
    )
    assert (
        keep is not None
        and {"memory_key", "value_truncated", "content_sha256", "char_limit"} <= keep
    )
    entry = memory_crud._MemoryListEntry(
        memory_key="k",
        memory_type="semantic",
        confidence=1.0,
        version=1,
        created_at="2026-09-19 00:00:00",
        updated_at="2026-09-19 00:00:00",
        value="v",
        value_truncated=True,
        content_sha256="ab" * 32,
        char_limit=None,
        entities=[],
    )
    entry._projection = keep
    dumped = entry.model_dump()
    assert dumped["value_truncated"] is True and "entities" not in dumped and "tags" not in dumped


def test_the_memories_page_asks_for_the_fields_it_renders() -> None:
    reads = (_ROOT / "frontend/src/views/memoryListReads.ts").read_text()
    card = (_ROOT / "frontend/src/components/MemoryCard.tsx").read_text()
    # The list asks for what the card shows: two lines at a 1400 px window.
    assert "const MEMORIES_PREVIEW_CHARS = CARD_VALUE_CHARS + 1;" in reads
    assert "export const CARD_VALUE_CHARS = 320;" in card
    fields_src = reads[
        reads.index("MEMORIES_LIST_FIELDS = [") : reads.index(
            "];", reads.index("MEMORIES_LIST_FIELDS = [")
        )
    ]
    fields = set(json.loads("[" + fields_src.split("[", 1)[1].rstrip().rstrip(",") + "]"))
    assert "entities" not in fields and {"memory_key", "value", "tags", "verification"} <= fields
    assert len(fields) == 12, "cut 54: the twelve fields the page reads"
    known = set(memory_crud._MemoryListEntry.model_fields)
    assert fields <= known, sorted(fields - known)
    assert re.search(r"fields: MEMORIES_LIST_FIELDS,\s*previewChars: MEMORIES_PREVIEW_CHARS", reads)
    api = (_ROOT / "frontend/src/api.ts").read_text()
    assert "preview_chars: opts.previewChars" in api and "fields: opts.fields" in api
