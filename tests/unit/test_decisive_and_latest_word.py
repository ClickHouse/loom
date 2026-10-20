"""The decisive chunks of a bug thread and the latest
word on an identifier get their own reads; the supersession note rides along."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.ask import identifiers, question_shapes
from server.memory.app.ask.identifiers import _HELD_MARGIN


def test_question_shapes() -> None:
    assert question_shapes.is_decisive_question(
        "What do we know about the amd_tsan flaky check timeout?"
    )
    assert question_shapes.is_decisive_question(
        "the parallel replicas coordinator LOGICAL_ERROR and its fix"
    )
    assert not question_shapes.is_decisive_question("What are @SamRivera's review expectations?")
    assert question_shapes.is_latest_word_question(
        "Is the stress job task a known creator-ledger gap?"
    )
    assert question_shapes.is_latest_word_question("How should brightlark's issues be classified?")
    assert question_shapes.is_latest_word_question(
        "What is the standing disposition for PR #107585, and is there a pending design decision?"
    )
    assert question_shapes.is_latest_word_question(
        "Is there a known defect where a Distributed table declared with a different type misprunes?"
    )
    # set / directive questions are not supersession questions
    assert not question_shapes.is_latest_word_question("What are @SamRivera's review expectations?")
    assert not question_shapes.is_latest_word_question(
        "How should the inbox cron handle a new directive?"
    )
    assert not question_shapes.is_latest_word_question("What do we know about PR 117723?")


class _Repo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def read_your_writes_enabled(self) -> bool:
        return False

    async def find_by_identifier(self, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "fn": "facts",
                **{k: v for k, v in kw.items() if k in ("limit", "order", "identifiers")},
            }
        )
        tag = kw.get("order", "cosine")
        return [
            {
                "memory_key": f"user_{tag}_{kw['limit']}",
                "content_excerpt": "f",
                "valid_at": "2026-09-01",
            }
        ]

    async def find_chunks_by_identifier(self, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append(
            {"fn": "chunks", **{k: v for k, v in kw.items() if k in ("limit", "order", "markers")}}
        )
        tag = "m" if kw.get("markers") else kw.get("order", "cosine")
        return [
            {
                "memory_key": f"chunk:s:{tag}_{kw['limit']}",
                "content_excerpt": "c",
                "created_at": "2026-09-02",
            }
        ]


def _run(
    monkeypatch: pytest.MonkeyPatch, query: str, *, decisive: int, recent: int, chunks: int = 0
):
    repo = _Repo()
    import server.memory.store as mr

    monkeypatch.setattr(mr, "find_by_identifier", repo.find_by_identifier)
    monkeypatch.setattr(mr, "find_chunks_by_identifier", repo.find_chunks_by_identifier)
    monkeypatch.setattr(mr, "read_your_writes_enabled", repo.read_your_writes_enabled)
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_decisive_chunks", decisive)
    monkeypatch.setattr(settings, "memory_ask_identifier_inject_recent", recent)
    monkeypatch.setattr(settings, "memory_ask_identifier_inject_chunks", chunks)
    monkeypatch.setattr(settings, "memory_ask_identifier_inject_set_facts", 0)

    class _Emb:
        async def embed(self, q: str) -> list[float]:
            return [0.1, 0.2]

    import server.embedding as emb

    monkeypatch.setattr(emb, "get_embedder", lambda: _Emb())
    si = SimpleNamespace(
        query=query, org="o", namespace="n", scope="private", agent="a", user_id=None
    )
    out = asyncio.run(identifiers._inject_identifier_memories(si, ["117831"], []))
    return repo, out


def test_decisive_read_fires_only_for_bug_questions_and_only_when_on(monkeypatch) -> None:
    repo, out = _run(
        monkeypatch,
        "What do we know about the amd_tsan flaky timeout on PR 117831?",
        decisive=6,
        recent=0,
    )
    marker_calls = [c for c in repo.calls if c["fn"] == "chunks" and c.get("markers")]
    # A chunk lookup is asked for spare rows, for the chunks held back as credentials.
    assert len(marker_calls) == 1 and marker_calls[0]["limit"] == 6 + _HELD_MARGIN
    assert "root cause" in marker_calls[0]["markers"]
    assert any(k.memory_key == f"chunk:s:m_{6 + _HELD_MARGIN}" for k in out)
    repo, _ = _run(monkeypatch, "What do we know about PR 117831?", decisive=6, recent=0)
    assert not [c for c in repo.calls if c.get("markers")]
    repo, _ = _run(
        monkeypatch,
        "What do we know about the amd_tsan flaky timeout on PR 117831?",
        decisive=0,
        recent=0,
    )
    assert not [c for c in repo.calls if c.get("markers")]


def test_latest_word_read_is_by_recency_for_disposition_questions(monkeypatch) -> None:
    repo, out = _run(monkeypatch, "Is PR 117831 a known creator-ledger gap?", decisive=0, recent=4)
    rec = [c for c in repo.calls if c.get("order") == "recency"]
    assert {c["fn"] for c in rec} == {"facts", "chunks"}
    assert {c["fn"]: c["limit"] for c in rec} == {"facts": 4, "chunks": 4 + _HELD_MARGIN}
    keys = {k.memory_key for k in out}
    assert "user_recency_4" in keys and f"chunk:s:recency_{4 + _HELD_MARGIN}" in keys
    repo, _ = _run(monkeypatch, "What do we know about PR 117831?", decisive=0, recent=4)
    assert not [c for c in repo.calls if c.get("order") == "recency"]


def test_knob_defaults() -> None:
    from server.settings import Settings

    assert Settings.model_fields["memory_ask_decisive_chunks"].default == 6  # measured
    assert Settings.model_fields["memory_ask_identifier_inject_recent"].default == 4  # measured


def test_a_named_account_without_its_at_sign_is_an_identifier() -> None:
    ids = identifiers._injectable_identifiers(
        "What do we know about the GitHub account brightlark and how should its issues be classified?"
    )
    assert "brightlark" in ids
    ids = identifiers._injectable_identifiers("Who is the reporter contrib1 on issue 119651?")
    assert "contrib1" in ids and "119651" in ids
    # product words and short words stay out
    assert "clickhouse" not in [
        i.lower()
        for i in identifiers._injectable_identifiers("the ClickHouse user account ClickHouse")
    ]
