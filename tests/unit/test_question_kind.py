"""A prose question's kind — responsibility vs incident — is
deterministic and gates the card and vocabulary legs."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.domain.index.question_kind import kind_allowed, question_kind
from server.settings import settings


def test_responsibility_questions() -> None:
    for q in (
        "where is the memory limit for a query enforced",
        "how does keeper persist its replication log to disk",
        "How does the server ensure sorting properties are enforced during distributed query planning?",
        "which component decides to spill aggregation state to disk",
        "why does the server crash when the mark cache is full",  # a question about a symptom is a question
    ):
        assert question_kind(q) == "responsibility", q


def test_incident_titles_and_other() -> None:
    for q in (
        "Received signal -1 (STID: 2103-2cc2)",
        "Fix performance issue in reading of iceberg tables",
        "Backport #95164 to 25.10: Fix crash in sequenceMatch",
        "Issue with sequenceMatchEvents with time gates",
        "Bump libcotp from v3.1.1 to v4.2",
        "Dictionary permanent detach missing check",
    ):
        assert question_kind(q) == "incident", q
    assert question_kind("sync private") == "incident"  # a change title
    # a topic phrase is how a coding agent searches: a responsibility question without
    # its interrogative
    assert question_kind("mark cache sizing") == "responsibility"
    assert question_kind("replicated insert deduplication block id") == "responsibility"
    assert question_kind("MemoryTracker") == "other"  # not prose


def test_kind_allowed_parses_lists_and_any() -> None:
    assert kind_allowed("responsibility", "responsibility")
    assert not kind_allowed("incident", "responsibility")
    assert kind_allowed("incident", "responsibility, incident") and kind_allowed("other", "any")


_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        self.calls.append("lexical")
        return [{"chunk_id": "a", "lex_score": 40.0, "path": "src/a.cpp", **_UNIT}]

    async def vector_search_chunks(self, *a, **k):
        return [{"chunk_id": "c", "distance": 0.3, "path": "src/c.cpp", **_UNIT}]

    async def vector_search_cards(self, *a, **k):
        self.calls.append("cards")
        return [{"path": "src/Common/MemoryTracker.h", "distance": 0.4}]

    async def symbol_names_for_paths(self, *a, **k):
        return [{"name": "MemoryTracker", "kind": "class", "path": "src/Common/MemoryTracker.h"}]

    async def chunks_for_paths(self, *a, **k):
        self.calls.append("chunks_for_paths")
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(query: str) -> tuple[Any, _Repo]:
    repo = _Repo()
    with patch.multiple(
        settings,
        code_fusion_signals=False,
        code_search_card_leg=True,
        code_search_abstain_card_below=0.0,
        code_search_concept_expand_model="cards:5",
    ):
        res = asyncio.run(
            cs.search(repo, "o", "code-clickhouse", query=query, top_k=5, embedder=_Embedder())
        )
    return res, repo


def test_incident_titles_skip_both_voting_legs_and_responsibility_questions_run_them() -> None:
    res, repo = _run("Fix crash in sequenceMatch with time gates")
    assert res.retrieval_work["question_kind"] == "incident"
    assert "cards" not in repo.calls and repo.calls.count("lexical") == 1
    res, repo = _run("where is the memory limit for a query enforced")
    assert res.retrieval_work["question_kind"] == "responsibility"
    assert "cards" in repo.calls and repo.calls.count("lexical") == 2
