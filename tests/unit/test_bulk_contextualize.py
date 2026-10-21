"""Handler path: contextualize=true prepends a model-written
context to what each entry embeds and to its excerpt; value untouched;
off by default; kill switch honoured; fail-open."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app.extract import handler as mem_extract
from tests.unit.bulk_stubs import BatchEmbedder, StubCH, wire_stubs


class _LLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0) -> Any:
        self.calls.append(user)
        return SimpleNamespace(text="Debra and India, 2020-03-06, photography tips")


def _input(contextualize: bool) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="u",
        session_id="s",
        contextualize=contextualize,
        entries=[
            _BulkSetEntry(memory_key="s1_c0", value="Debra: Just went for my jog earlier."),
            _BulkSetEntry(
                memory_key="s1_c1", value="India: Last week I went to a street art fest."
            ),
        ],
    )


@pytest.mark.asyncio
async def test_contextualize_prefixes_embed_text_and_excerpt_not_value(monkeypatch) -> None:
    ch, embedder, rows = StubCH(), BatchEmbedder(), []
    wire_stubs(monkeypatch, ch, embedder, rows)
    llm = _LLM()
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: llm)
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_bulk_contextualize_allowed", True, raising=False)

    await mem_extract.handle_memory_bulk_set(_input(contextualize=False))
    assert llm.calls == []
    plain_texts = [t for b in embedder.batches for t in b]
    assert not any(t.startswith("Debra and India") for t in plain_texts)

    ch2, embedder2, rows2 = StubCH(), BatchEmbedder(), []
    wire_stubs(monkeypatch, ch2, embedder2, rows2)
    await mem_extract.handle_memory_bulk_set(_input(contextualize=True))
    assert len(llm.calls) == 2 and "<document>" in llm.calls[0]
    value_texts = embedder2.batches[0][:2]
    assert all(t.startswith("Debra and India, 2020-03-06, photography tips\n") for t in value_texts)
    excerpts = [r["content_excerpt"] for r in rows2]
    assert all(e.startswith("Debra and India, 2020-03-06, photography tips\n") for e in excerpts)
    # value stays byte-identical in loom.memory rows
    mem_rows = ch2.rows_for("loom.memory")
    assert mem_rows and all("Debra and India" not in str(r.get("value", "")) for r in mem_rows)


@pytest.mark.asyncio
async def test_kill_switch_and_model_failure_leave_entries_plain(monkeypatch) -> None:
    ch, embedder, rows = StubCH(), BatchEmbedder(), []
    wire_stubs(monkeypatch, ch, embedder, rows)
    from server.settings import settings

    llm = _LLM()
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: llm)
    monkeypatch.setattr(settings, "memory_bulk_contextualize_allowed", False, raising=False)
    await mem_extract.handle_memory_bulk_set(_input(contextualize=True))
    assert llm.calls == []

    class Boom:
        async def complete(self, **kw: Any) -> Any:
            raise RuntimeError("down")

    monkeypatch.setattr(settings, "memory_bulk_contextualize_allowed", True, raising=False)
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: Boom())
    ch3, embedder3, rows3 = StubCH(), BatchEmbedder(), []
    wire_stubs(monkeypatch, ch3, embedder3, rows3)
    out = await mem_extract.handle_memory_bulk_set(_input(contextualize=True))
    assert (
        out is not None and embedder3.batches and len(embedder3.batches[0]) >= 2
    )  # embedded plain
