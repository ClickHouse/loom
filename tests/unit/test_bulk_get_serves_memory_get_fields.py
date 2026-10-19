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
from server.memory.store.rows import MemoryRow, _hydrate
from server.observability import audit


def _row(key: str, **over: Any) -> MemoryRow:
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
    rows = [_row("plain"), _row("secret", sensitivity="high", entities=["vault"])]

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
        seen["actor"] = kw["actor"]

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
    assert served["actor"] == "a"  # the token's principal; with no token, the request's agent


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


@pytest.mark.asyncio
async def test_list_withholds_a_high_rows_value(served) -> None:
    # Readable by key only: the entry says the value was cut, and a reader asks memory.get.
    out = await query._handle_memory_list(MemoryListInput(org="o", namespace="n", agent="a"))
    secret = next(e for e in out.entries if e.memory_key == "secret")
    assert secret.value is None and secret.value_truncated is True
    assert secret.entities == [] and secret.content_sha256 == ""
    plain = next(e for e in out.entries if e.memory_key == "plain")
    assert plain.value == "v"


@pytest.mark.asyncio
async def test_bulk_get_audits_the_reader_named_by_the_token(served) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.domain.trust import actor_for

    set_current_auth(
        AuthInfo(subject="ci-mirror", org="o", namespaces=("n",), roles=(), is_service_account=True)
    )
    try:
        await query._handle_memory_bulk_get(
            MemoryBulkGetInput(org="o", namespace="n", agent="*", memory_keys=["secret"])
        )
    finally:
        set_current_auth(None)
    assert served["actor"] == actor_for("ci-mirror", is_service_account=True)


@pytest.mark.asyncio
async def test_the_score_list_withholds_a_high_rows_value(monkeypatch) -> None:
    def row(key: str, sensitivity: str) -> dict[str, Any]:
        return {
            "memory_key": key,
            "memory_type": "semantic",
            "confidence": 1.0,
            "version": 1,
            "created_at": "2026-09-19 00:00:00",
            "value": '"plain words"' if sensitivity == "" else '"the door code"',
            "value_truncated": 0,
            "sensitivity": sensitivity,
            "tags": [],
            "entities": ["vault"] if sensitivity == "high" else [],
        }

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [row("plain", ""), row("secret", "high")]

        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"n": 2}

    monkeypatch.setattr(query, "get_client", lambda: _CH())
    out = await query._handle_memory_list(
        MemoryListInput(org="o", namespace="n", agent="a", sort_by="score")
    )
    by_key = {e.memory_key: e for e in out.entries}
    assert by_key["secret"].value is None and by_key["secret"].value_truncated is True
    assert by_key["secret"].entities == []
    assert by_key["plain"].value == "plain words" and by_key["plain"].value_truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "high_elsewhere", "audited"),
    [
        # a sweep labelled v2: v1 holds the same value unlabelled
        (["", "high"], set(), [2, 1]),
        # this page holds only plain versions, a later one is labelled
        (["", ""], {"k"}, [2, 1]),
        (["", ""], set(), []),
    ],
)
async def test_history_of_a_high_key_audits_every_version_it_serves(
    monkeypatch, labels: list[str], high_elsewhere: set[str], audited: list[int]
) -> None:
    from server.memory.api.models_temporal import MemoryHistoryInput
    from server.memory.app import curation
    from server.memory.store.history import HistoryVersion

    def version(n: int, **over: Any) -> HistoryVersion:
        base: dict[str, Any] = {
            "memory_id": "m1",
            "version": n,
            "value": '"v"',
            "confidence": 1.0,
            "created_at": "2026-09-19 00:00:00",
            "source_trace": "",
            "redaction_reason": "",
            "tags": [],
        }
        return HistoryVersion.from_row({**base, **over})

    async def _history(*_a: Any, **_k: Any) -> list[HistoryVersion]:
        return [version(2, sensitivity=labels[1]), version(1, sensitivity=labels[0])]

    async def _high_keys(_org: str, _ns: str, keys: Any) -> set[str]:
        return high_elsewhere & set(keys)

    audits: list[list[int]] = []

    async def _emit_many(event: str, **kw: Any) -> None:
        audits.append([s["version"] for s in kw["subjects"]])

    monkeypatch.setattr(memory_repo, "list_history", _history)
    monkeypatch.setattr(memory_repo, "high_keys", _high_keys)
    monkeypatch.setattr(audit, "emit_many", _emit_many)
    out = await curation.handle_memory_history(
        MemoryHistoryInput(org="o", namespace="n", agent="a", memory_key="k")
    )
    assert len(out.rows) == 2 and audits == [audited]
