"""memory.import_evidence refuses blacklisted text the way memory.bulk_set does: the
matching chunk or digest is dropped (counted in `skipped`, its pattern's hit noted) and the
rest of the batch is written."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

SECRET = "sk-live-canary"


class _Embedder:
    model_name = "fake-embed"

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class _CH:
    def __init__(self) -> None:
        self.inserts: dict[str, list[dict[str, Any]]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.setdefault(table, []).extend(rows)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[_CH, list[str]]:
    from server import embedding
    from server.memory.app import blacklist_guard as blacklist
    from server.memory.app import evidence_transfer as mod

    ch = _CH()
    hits: list[str] = []

    async def _find_match(*, org: str, namespace: str, value: str) -> Any:
        if SECRET in value:
            return SimpleNamespace(pattern_id="p-1", scope="namespace", reason="secret")
        if "too long to check" in value:  # what the regex budget answers past it
            return SimpleNamespace(pattern_id="", scope="namespace", reason="too long")
        return None

    async def _note_hit(pattern_id: str) -> None:
        hits.append(pattern_id)

    monkeypatch.setattr(mod, "get_client", lambda: ch)
    monkeypatch.setattr(embedding, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(blacklist, "find_match", _find_match)
    monkeypatch.setattr(blacklist, "note_hit", _note_hit)
    return ch, hits


@pytest.mark.asyncio
async def test_blacklisted_chunks_and_digests_are_dropped_not_written(wired) -> None:
    from server.memory.api.models_evidence_transfer import (
        EvidenceChunk,
        EvidenceDigest,
        MemoryImportEvidenceInput,
    )
    from server.memory.app.chunk_links import SOURCES_TABLE
    from server.memory.app.evidence_transfer import handle_memory_import_evidence

    ch, hits = wired
    out = await handle_memory_import_evidence(
        MemoryImportEvidenceInput(
            org="acme",
            namespace="ns",
            agent="a",
            chunks=[
                EvidenceChunk(
                    session_id="s1",
                    chunk_index=0,
                    content="the parade was fun",
                    memory_keys=["user_parade"],
                ),
                EvidenceChunk(
                    session_id="s1",
                    chunk_index=1,
                    content=f"my key is {SECRET}",
                    memory_keys=["user_key"],
                ),
            ],
            digests=[
                EvidenceDigest(session_id="s1", summary_text="a parade happened"),
                EvidenceDigest(session_id="s2", summary_text=f"key {SECRET} shared"),
                # key_facts are stored on the row too, so they are checked as well.
                EvidenceDigest(session_id="s3", summary_text="ok", key_facts=[SECRET]),
            ],
        )
    )

    stored = [r["content"] for r in ch.inserts.get("loom.memory_chunks", [])]
    stored += [r["summary_text"] for r in ch.inserts.get("loom.session_summaries", [])]
    assert stored == ["the parade was fun", "a parade happened"]
    assert (out.chunks_written, out.digests_written, out.skipped) == (1, 1, 3)
    # The refused chunk's evidence link is not written either.
    assert "user_key" not in repr(ch.inserts.get(SOURCES_TABLE, []))
    assert hits == ["p-1", "p-1", "p-1"]


@pytest.mark.asyncio
async def test_a_chunk_the_blacklist_could_not_check_is_counted_apart(wired) -> None:
    """Past the request's regex budget a chunk is skipped, and `unchecked` says so: the
    same chunk in a smaller request is checked."""
    from server.memory.api.models_evidence_transfer import EvidenceChunk, MemoryImportEvidenceInput
    from server.memory.app.evidence_transfer import handle_memory_import_evidence

    out = await handle_memory_import_evidence(
        MemoryImportEvidenceInput(
            org="acme",
            namespace="ns",
            agent="a",
            chunks=[
                EvidenceChunk(session_id="s1", chunk_index=0, content="too long to check"),
                EvidenceChunk(session_id="s1", chunk_index=1, content=f"my key is {SECRET}"),
            ],
        )
    )
    assert (out.chunks_written, out.skipped, out.unchecked) == (0, 2, 1)
