"""Reads and revisions that `Agent.call` reaches read the identity sentinel it stamps
("-" for user_id, session_id and run_id when a call names none) as unset, not as
the rows written with no user, session or run."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.knowledge.api.models_conversations import (
    ConversationListInput,
    ConversationSearchInput,
    ConversationUpdateInput,
)
from server.knowledge.app import conversations as conv
from server.knowledge.store import conversations as conversation_repo
from server.memory.api.models_temporal import MemoryDigestsInput
from server.tasks.api.models_run import RunGradeInput, RunListInput
from server.tasks.store import runs as runs_store

_ID = {"org": "o", "namespace": "ns"}


async def test_conversation_list_reads_run_sentinel_as_no_run_filter(monkeypatch):
    seen: dict[str, Any] = {}

    async def list_thread(*_a: Any, run_id: Any = None, **_k: Any) -> list[Any]:
        seen["run_id"] = run_id
        return []

    monkeypatch.setattr(conversation_repo, "list_thread", list_thread)
    await conv.handle_conversation_list(
        ConversationListInput(**_ID, agent="w", thread_id="t", run_id="-")
    )
    assert seen == {"run_id": None}


async def test_conversation_search_reads_the_sentinels_as_unset(monkeypatch):
    seen: dict[str, Any] = {}

    class _NoEmbedder:
        model_name = "m"

        async def embed(self, _text: str) -> list[float]:
            raise RuntimeError("no embedder")

    async def search_substring(*_a: Any, run_id: Any = None, user_id: Any = None, **_k: Any):
        seen.update(run_id=run_id, user_id=user_id)
        return []

    monkeypatch.setattr(conv, "get_embedder", lambda: _NoEmbedder())
    monkeypatch.setattr(conversation_repo, "search_substring", search_substring)
    await conv.handle_conversation_search(
        ConversationSearchInput(**_ID, agent="w", query="q", run_id="-", user_id="-")
    )
    assert seen == {"run_id": None, "user_id": None}


async def test_saved_message_search_keeps_its_owner_exact(monkeypatch):
    """The saved-message search shares the search path, but its user_id is the owner."""
    from server.knowledge.api.models_conversations import ConversationSearchSavedMessagesInput

    seen: dict[str, Any] = {}

    class _NoEmbedder:
        model_name = "m"

        async def embed(self, _text: str) -> list[float]:
            raise RuntimeError("no embedder")

    async def search_substring(*_a: Any, user_id: Any = None, **_k: Any):
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr(conv, "get_embedder", lambda: _NoEmbedder())
    monkeypatch.setattr(conversation_repo, "search_substring", search_substring)
    await conv.handle_conversation_search_saved_messages(
        ConversationSearchSavedMessagesInput(
            **_ID, agent="w", query="q", user_id="-", search_mode="semantic"
        )
    )
    assert seen == {"user_id": "-"}


@pytest.mark.parametrize(("sent", "stored"), [("-", "run-7"), (None, "run-7"), ("", "-")])
async def test_conversation_update_keeps_or_clears_the_threads_run(monkeypatch, sent, stored):
    """None and the stamped "-" keep the thread's run; "" detaches it."""
    written: list[dict[str, Any]] = []

    async def fetch_thread(*_a: Any) -> dict[str, Any]:
        return {"title": "old", "metadata": "{}", "run_id": "run-7", "created_at": None}

    async def insert_thread(row: dict[str, Any]) -> None:
        written.append(row)

    monkeypatch.setattr(conversation_repo, "fetch_thread", fetch_thread)
    monkeypatch.setattr(conversation_repo, "insert_thread", insert_thread)
    await conv.handle_conversation_update(
        ConversationUpdateInput(**_ID, agent="w", thread_id="t", title="new", run_id=sent)
    )
    assert [(r["title"], r["run_id"]) for r in written] == [("new", stored)]


async def test_conversation_update_with_only_the_sentinel_changes_nothing():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        await conv.handle_conversation_update(
            ConversationUpdateInput(**_ID, agent="w", thread_id="t", run_id="-")
        )
    assert e.value.status_code == 400


async def test_memory_digests_reads_the_session_sentinel_as_every_session(monkeypatch):
    from server.memory.app.curation import handle_memory_digests

    seen: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, _sql: str, params: Any = None) -> list[Any]:
            seen.append(params)
            return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    await handle_memory_digests(MemoryDigestsInput(**_ID, agent="w", session_id="-"))
    assert "s" not in seen[0]


async def test_run_list_reads_the_sentinels_as_unset(monkeypatch):
    from server.tasks.app import run as h

    seen: list[dict[str, Any]] = []

    async def list_runs(_ch: Any, _o: str, _n: str, *, params: dict[str, Any], **_k: Any):
        seen.append(params)
        return []

    async def count_runs(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(h, "get_client", lambda: None)
    monkeypatch.setattr(runs_store, "list_runs", list_runs)
    monkeypatch.setattr(runs_store, "count_runs", count_runs)
    await h.handle_run_list(RunListInput(**_ID, agent="*", session_id="-"))
    await h.handle_run_list(RunListInput(**_ID, agent="w", session_id="s1"))
    assert seen == [{}, {"agent": "w", "sid": "s1"}]


@pytest.mark.parametrize(
    ("run_id", "session_id", "target"),
    [("r1", "-", ("r1", "")), ("-", "s1", ("", "s1"))],
)
def test_run_grade_reads_the_sentinel_as_the_unnamed_target(run_id, session_id, target):
    g = RunGradeInput(**_ID, run_id=run_id, session_id=session_id, grade="pass")
    assert (g.run_id, g.session_id) == target


def test_run_grade_still_needs_one_target():
    with pytest.raises(ValidationError):
        RunGradeInput(**_ID, run_id="-", session_id="-", grade="pass")


async def test_cost_summary_reads_the_user_sentinel_as_every_user(monkeypatch):
    from datetime import date

    from server.insights.api.models_cost import CostSummaryInput
    from server.insights.app import cost as cost_app
    from server.insights.store import cost as cost_store

    seen: list[tuple[dict[str, Any], bool]] = []

    async def summary_rows(_where: str, params: dict[str, Any], *, scan: bool, **_k: Any):
        seen.append((params, scan))
        return []

    async def ask_cache_totals(*_a: Any, **_k: Any) -> tuple[int, float]:
        return 0, 0.0

    monkeypatch.setattr(cost_app, "get_client", lambda: None)
    monkeypatch.setattr(cost_store, "summary_rows", summary_rows)
    monkeypatch.setattr(cost_store, "ask_cache_totals", ask_cache_totals)
    day = date(2026, 1, 1)
    await cost_app.handle_cost_summary(
        CostSummaryInput(**_ID, from_date=day, to_date=day, user_id="-")
    )
    params, scan = seen[0]
    assert "-" not in params.values()
    assert scan is False


async def test_evidence_export_reads_the_session_sentinel_as_every_session(monkeypatch):
    from server.memory.api.models_evidence_transfer import MemoryExportEvidenceInput
    from server.memory.app import evidence_transfer

    seen: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, _sql: str, params: Any = None) -> list[Any]:
            seen.append(params)
            return []

    monkeypatch.setattr(evidence_transfer, "get_client", lambda: _CH())
    await evidence_transfer.handle_memory_export_evidence(
        MemoryExportEvidenceInput(**_ID, kind="digests", session_id="-")
    )
    assert seen and "sid" not in seen[0]


async def test_search_trace_log_reads_the_run_sentinel_as_every_run(monkeypatch):
    from server.memory.api.models_search import SearchTraceLogInput
    from server.memory.app.search import trace

    seen: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, _sql: str, params: Any = None) -> list[Any]:
            seen.append(params)
            return []

    monkeypatch.setattr(trace, "get_client", lambda: _CH())
    await trace.handle_memory_search_trace_log(SearchTraceLogInput(**_ID, run_id="-"))
    assert seen and "rid" not in seen[0]


async def test_an_outcome_for_the_run_sentinel_derives_no_keys(monkeypatch):
    """A stamped run_id "-" names no run: it must not credit every search made outside one."""
    from server.memory.api.models_temporal import MemoryOutcomeRunInput
    from server.memory.app import ops

    queries: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[Any]:
            queries.append(sql)
            return [{"memory_key": "k1"}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    out = await ops.handle_memory_outcome_run(
        MemoryOutcomeRunInput(**_ID, outcome="negative", run_id="-")
    )
    assert (out.applied, out.keys_matched, queries) == (0, 0, [])
