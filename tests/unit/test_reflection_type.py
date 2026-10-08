"""Reflections are hypotheses, not facts: embedded, but out
of unfiltered recall; written once per run by run.complete(reflection=...)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from server.memory.domain.memory_types import (
    HIDDEN_FROM_UNFILTERED_SEARCH,
    NON_SEARCHABLE_TYPES,
    excluded_from_unfiltered_search,
    is_searchable_type,
)
from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery
from server.tasks.api.models_run import RunCompleteInput


def test_reflection_is_embedded_but_hidden_by_default() -> None:
    assert "reflection" in HIDDEN_FROM_UNFILTERED_SEARCH
    assert is_searchable_type("reflection") is True  # gets an embedding row
    assert "semantic" not in HIDDEN_FROM_UNFILTERED_SEARCH
    assert set(excluded_from_unfiltered_search()) == set(NON_SEARCHABLE_TYPES) | {"reflection"}
    assert excluded_from_unfiltered_search() == sorted(excluded_from_unfiltered_search())


def _q(**over: Any) -> SearchQuery:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "scope": "private",
        "agent": "*",
        "embedding_model": "fake",
        "query_vec": [0.1, 0.2],
        "top_k": 5,
    }
    base.update(over)
    return SearchQuery(**base)


def test_unfiltered_search_excludes_reflections_and_non_searchable_types() -> None:
    where, params = _build_where(_q())
    assert any("e.memory_type NOT IN {non_searchable:Array(String)}" in w for w in where)
    assert "reflection" in params["non_searchable"] and "episodic" in params["non_searchable"]
    assert not any("e.memory_type = {type:String}" in w for w in where)


def test_explicit_reflection_filter_reaches_them() -> None:
    where, params = _build_where(_q(memory_type="reflection"))
    assert any("e.memory_type = {type:String}" in w for w in where)
    assert params["type"] == "reflection"
    assert "non_searchable" not in params


T0 = datetime(2026, 9, 11, 12, 0, 0)


class _FakeCH:
    def __init__(self, run_row: dict[str, Any]) -> None:
        self.one = [run_row, {"n": 0}]
        self.all: list[list[dict[str, Any]]] = [[]]
        self.inserted: list[Any] = []

    async def query_one(self, sql, params=None):
        return self.one.pop(0) if self.one else None

    async def query_all(self, sql, params=None):
        return self.all.pop(0) if self.all else []

    async def insert(self, table, rows, settings=None):
        self.inserted.append(rows)


def _run_row() -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "claude",
        "user_id": "u1",
        "session_id": "s1",
        "run_id": "r1",
        "goal": "fix CI",
        "user_request": "",
        "expected_outcome": "",
        "environment": "",
        "model": "",
        "consumer": "",
        "status": "active",
        "started_at": T0,
        "completed_at": None,
        "duration_ms": 0,
        "final_response": "",
        "confidence": 0.0,
        "memories_used": [],
        "memories_written": [],
        "metadata": "{}",
    }


@pytest.fixture
def complete_env(monkeypatch):
    from server.memory.app import write as write_mod
    from server.tasks.app import run as h

    writes: list[Any] = []

    async def fake_set(set_input):
        writes.append(set_input)
        return None

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    fake = _FakeCH(_run_row())
    with patch.object(h, "get_client", return_value=fake):
        yield fake, writes, h


def test_complete_writes_one_reflection_memory_under_the_run(complete_env) -> None:
    fake, writes, h = complete_env
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o",
                namespace="n",
                run_id="r1",
                status="completed",
                reflection={
                    "what_worked": ["pinning the runner image"],
                    "what_failed": [" retrying the flaky job ", ""],
                    "lessons": ["x" * 700],
                    "evidence": ["ci_runner_log"],  # A lesson names its evidence
                    "confidence": 0.6,
                },
            )
        )
    )
    assert out.reflection_key == "reflection:r1"
    assert len(writes) == 1
    w = writes[0]
    assert w.memory_type == "reflection" and w.memory_key == "reflection:r1"
    assert (w.agent, w.user_id, w.session_id, w.run_id) == ("claude", "u1", "s1", "r1")
    assert w.value["what_worked"] == ["pinning the runner image"]
    assert w.value["what_failed"] == ["retrying the flaky job"]  # trimmed, empties dropped
    assert len(w.value["lessons"][0]) == 500  # capped
    assert w.value["goal"] == "fix CI" and w.value["status"] == "completed"
    assert "reflection" in w.tags and "run:r1" in w.tags and w.confidence == 0.6
    assert w.source_trace == "run:r1"


def test_empty_or_missing_reflection_writes_nothing(complete_env) -> None:
    fake, writes, h = complete_env
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o",
                namespace="n",
                run_id="r1",
                status="failed",
                reflection={"what_worked": [], "lessons": [" "]},
            )
        )
    )
    assert out.reflection_key == "" and writes == []


def test_reflection_write_failure_does_not_fail_the_close(monkeypatch) -> None:
    from server.memory.app import write as write_mod
    from server.tasks.app import run as h

    async def boom(set_input):
        raise RuntimeError("blacklist rejected")

    monkeypatch.setattr(write_mod, "handle_memory_set", boom)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    fake = _FakeCH(_run_row())
    with patch.object(h, "get_client", return_value=fake):
        out = asyncio.run(
            h.handle_run_complete(
                RunCompleteInput(
                    org="o",
                    namespace="n",
                    run_id="r1",
                    status="completed",
                    reflection={
                        "lessons": ["keep the image pinned"],
                        "evidence": ["runner_image_pin"],
                    },
                )
            )
        )
    assert out.status == "completed" and out.reflection_key == ""
    assert fake.inserted, "the terminal run row is still written"
