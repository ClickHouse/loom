"""memory.bulk_get answers a row the way memory.get does: its sensitivity, usage counters
and lock, and an audit row for each 'high' row it serves by key. memory.list serves the
usage counters too."""

from __future__ import annotations

from typing import Any

import pytest

import server.memory.store as memory_repo
from server.memory.api.models_bulk import MemoryBulkGetInput
from server.memory.api.models_crud import MemoryListInput
from server.memory.app import query
from server.memory.store.rows import _hydrate
from server.observability import audit


def _row(key: str, **over: Any) -> Any:
    return _hydrate(
        {
            "memory_id": f"00000000-0000-0000-0000-00000000000{len(key)}",
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": key,
            "memory_type": "semantic",
            "value": '"v"',
            "confidence": 1.0,
            "version": 7,
            "created_at": "2026-09-19 00:00:00",
            "status": "active",
            "pinned": False,
            "updated_at": None,
            "expires_at": None,
            "run_id": "-",
            "source_trace": "",
            "tags": [],
            "entities": [],
            **over,
        }
    )


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"audits": [], "overlaid": 0}
    rows = [_row("plain"), _row("secret", sensitivity="high")]

    async def _rows(*_a: Any, **_k: Any) -> list[Any]:
        return rows

    async def _overlay(rs: list[Any], **_k: Any) -> list[Any]:
        seen["overlaid"] += 1
        return [r.model_copy(update={"recall_count": 4}) for r in rs]

    async def _none(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    async def _version(*_a: Any, **_k: Any) -> int:
        return 1

    async def _count(*_a: Any, **_k: Any) -> int:
        return 2

    async def _emit_many(event: str, **kw: Any) -> None:
        seen["audits"].append((event, [s["memory_key"] for s in kw["subjects"]]))

    monkeypatch.setattr(memory_repo, "get_many_by_keys", _rows)
    monkeypatch.setattr(memory_repo, "list_by_namespace", _rows)
    monkeypatch.setattr(memory_repo, "overlay_usage_totals_many", _overlay)
    monkeypatch.setattr(query, "_canonical_keys_for_rows", _none)
    monkeypatch.setattr(query, "shown_successors", _none)
    monkeypatch.setattr(audit, "emit_many", _emit_many)
    monkeypatch.setattr(memory_repo, "namespace_version", _version)
    monkeypatch.setattr(memory_repo, "count_by_namespace", _count)
    return seen


@pytest.mark.asyncio
async def test_bulk_get_serves_sensitivity_and_usage_and_audits_high_rows(served) -> None:
    out = await query._handle_memory_bulk_get(
        MemoryBulkGetInput(org="o", namespace="n", agent="a", memory_keys=["plain", "secret"])
    )
    assert out.results["secret"].sensitivity == "high"
    assert out.results["plain"].sensitivity == ""
    assert out.results["plain"].recall_count == 4
    assert served["audits"] == [("memory.sensitive_read", ["secret"])]  # one insert per call


@pytest.mark.asyncio
async def test_bulk_get_reads_usage_only_when_the_projection_serves_it(served) -> None:
    await query._handle_memory_bulk_get(
        MemoryBulkGetInput(
            org="o", namespace="n", agent="a", memory_keys=["plain"], fields=["sensitivity"]
        )
    )
    assert served["overlaid"] == 0


@pytest.mark.asyncio
async def test_list_serves_the_usage_counters(served) -> None:
    out = await query._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", fields=["recall_count"])
    )
    assert [e.recall_count for e in out.entries] == [4, 4]


@pytest.mark.asyncio
async def test_list_reads_usage_only_when_the_projection_serves_it(served) -> None:
    out = await query._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", fields=["sensitivity"])
    )
    assert served["overlaid"] == 0
    assert [e.sensitivity for e in out.entries] == ["", "high"]


@pytest.mark.asyncio
async def test_emit_many_writes_rows_that_verify_like_emit_rows(monkeypatch) -> None:
    inserts: list[list[dict[str, Any]]] = []

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            inserts.append(rows)

    monkeypatch.setattr(audit, "get_client", lambda: _CH())
    subjects = [{"memory_key": "a", "memory_id": "1"}, {"memory_key": "b", "memory_id": "2"}]
    await audit.emit_many(
        "memory.sensitive_read", org="o", namespace="n", actor="x", subjects=subjects
    )
    await audit.emit(
        "memory.sensitive_read", org="o", namespace="n", actor="x", subject=subjects[0]
    )
    many, one = inserts
    assert len(many) == 2 and all(audit.row_matches_hash(r) for r in many + one)
