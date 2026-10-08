"""`loom backfill-chunks` writes the chunks memory.set would write.

Chunks stored under `backfill:<key>` would be invisible to a later memory.set /
memory.delete of the key, which looks for `memory.set:<key>` and so would never
tombstone them, and their hashes would never match a live write's.
Hermetic: the client and embedder are faked.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

import server.core.ch as ch_mod
import server.embedding as embedding_mod
from server.cli.ops import _cmd_backfill_chunks
from server.memory.app import set_chunks as sc
from server.settings import settings

pytestmark = pytest.mark.usefixtures("stub_model_command")

_VALUE = {"note": "\n\n".join(f"Paragraph {i}: " + "word " * 60 for i in range(30))}


class _Embedder:
    model_name = "fake-batch"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1, 0.2, 0.3]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeCH:
    def __init__(self, live_chunks: list[dict[str, Any]] | None = None) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.live = live_chunks or []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query_all(self, sql: str, *a: Any, **_: Any) -> list[dict[str, Any]]:
        if "loom.memory_chunks" in sql:
            return list(self.live)
        return [
            {
                "memory_key": "report",
                "agent": "sre",
                "run_id": "-",
                "memory_type": "semantic",
                "value": json.dumps(_VALUE),
                "created_at": None,
            }
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


@pytest.fixture(autouse=True)
def _chunking_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_set_chunk_max_chunks", 48, raising=False)


def _run(monkeypatch: pytest.MonkeyPatch, fake: _FakeCH, *, dry_run: bool = False) -> _Embedder:
    emb = _Embedder()
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    monkeypatch.setattr(embedding_mod, "get_embedder", lambda *a, **k: emb)
    rc = _cmd_backfill_chunks(argparse.Namespace(org="acme", namespace="ns", dry_run=dry_run))
    assert rc == 0
    return emb


def _expected() -> list[tuple[str, int, int]]:
    text = sc.human_text(_VALUE, "semantic")
    return [c for c in sc.chunk_text_with_truncation(text, max_chunks=48)[0]]


def test_backfilled_chunks_match_the_memory_set_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH()
    _run(monkeypatch, fake)
    rows = [r for t, rs in fake.inserts if t == "loom.memory_chunks" for r in rs]
    expected = _expected()
    assert rows and len(rows) == len(expected)
    for r, (chunk, _, _) in zip(rows, expected, strict=True):
        assert r["session_id"] == "memory.set:report"
        assert r["parent_memory_key"] == "report" and r["agent"] == "sre"
        assert r["content"] == chunk
    # The human text (indented JSON), not the stored compact JSON.
    assert '{\n "note"' in rows[0]["content"]


def test_a_key_whose_chunks_are_current_is_not_re_embedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    live = [
        {"chunk_index": i, "content_sha256": hashlib.sha256(c.encode()).hexdigest()}
        for i, (c, _, _) in enumerate(_expected())
    ]
    fake = _FakeCH(live_chunks=live)
    emb = _run(monkeypatch, fake)
    assert fake.inserts == [] and emb.calls == 0


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH()
    emb = _run(monkeypatch, fake, dry_run=True)
    assert fake.inserts == [] and emb.calls == 0
