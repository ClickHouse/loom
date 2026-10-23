"""Retries update stored queue rows without copying their transcripts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.api.models_bulk import MemoryExtractionRetryMineInput
from server.memory.app import extraction_queue as q
from server.settings import settings
from server.transport import mcp_op
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.switch_state import switch_off, switch_unread
from tests.unit.test_extraction_status_mine import (
    _auth,
    _client,
    _defaults,
    _in_context,
    _loom_op,
    _queued,
)

OP = "memory.extraction_retry_mine"


class _StubCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.reads.append((sql, dict(params or {})))
        if "system.mutations" in sql:
            return []
        return [dict(r) for r in self.rows]

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.commands.append((sql, dict(params or {})))

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.extend(rows)


class _MutationCH(_StubCH):
    def __init__(self, path: Path, *setup: str) -> None:
        super().__init__([])
        self.path = path
        run([ddl("memory_extraction_queue"), *setup], "SELECT 1", path=path)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.reads.append((sql, dict(params or {})))
        return run([], sql, params, path=self.path)

    def apply(self, sql: str, params: dict[str, Any] | None = None) -> None:
        # clickhouse-local must finish the mutation before its process exits.
        run(
            [sql.replace("mutations_sync = 0", "mutations_sync = 2")],
            "SELECT 1",
            params,
            path=self.path,
        )

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        await super().execute(sql, params)
        self.apply(sql, params)

    def stored(self) -> list[dict[str, Any]]:
        return run(
            [], "SELECT * FROM loom.memory_extraction_queue FINAL ORDER BY queue_id", path=self.path
        )


def _failed_row(queue_id: str = "q1", attempts: int = 3) -> dict[str, Any]:
    return {c: "" for c in q.COLUMNS} | {
        "org": "o",
        "namespace": "n",
        "queue_id": queue_id,
        "status": "failed",
        "attempts": attempts,
        "version": 1,
        "last_error": "ExtractionError: boom",
        "past_last_day": 0,
    }


def _requeue(
    ch: Any, ids: list[str], *, max_attempts: int = 3, scope: ReadFilters = WHOLE_NAMESPACE
) -> q.Requeued:
    return asyncio.run(
        q.requeue_own_failed(
            org="o",
            namespace="n",
            queue_ids=ids,
            enqueued_by="process:sa:a",
            scope=scope,
            max_attempts=max_attempts,
            ch=ch,
        )
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_retry_changes_only_the_principals_failed_rows_in_scope(tmp_path: Path) -> None:
    ch = _MutationCH(
        tmp_path / "ch",
        _queued("q1", by="process:sa:a", status="failed"),
        _queued("q2", by="process:sa:a"),
        _queued("q3", by="process:sa:a", status="done"),
        _queued("q4", by="process:sa:b", status="failed"),
        _queued("q5", by="process:sa:a", namespace="n2", status="failed"),
        _queued("q6", by="process:sa:a", user_id="u2", status="failed"),
        _queued("q7", by="process:sa:a", org="o2", status="failed"),
    )
    before = {r["queue_id"]: r for r in ch.stored()}
    scope = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert _requeue(ch, list(before), scope=scope) == (["q1"], ["q2", "q3"], [], [])
    after = {r["queue_id"]: r for r in ch.stored()}
    for qid, old in before.items():
        if qid != "q1":
            assert after[qid] == old
    row = after["q1"]
    assert (row["status"], row["attempts"]) == ("pending", 2)
    changed = {"status", "attempts", "lease_until", "updated_at"}
    assert {k: v for k, v in row.items() if k not in changed} == {
        k: v for k, v in before["q1"].items() if k not in changed
    }
    assert not ch.inserted


def test_retry_reads_states_and_submits_without_waiting_for_a_mutation() -> None:
    ch = _StubCH([_failed_row("q1")])
    assert _requeue(ch, ["q1", "q2"]).requeued == ["q1"]
    assert len(ch.reads) == 2 and "payload" not in ch.reads[0][0].split("FROM")[0]
    assert "system.mutations" in ch.reads[1][0]
    sql, params = ch.commands[0]
    assert "mutations_sync = 0" in sql and params["batches"] == [("", "", "", "q1", 1)]
    assert "version =" not in sql and "payload =" not in sql and not ch.inserted


@pytest.mark.parametrize(("max_attempts", "left"), [(0, 0), (1, 0), (3, 2), (8, 7)])
def test_a_retry_leaves_one_attempt(max_attempts: int, left: int) -> None:
    ch = _StubCH([_failed_row()])
    _requeue(ch, ["q1"], max_attempts=max_attempts)
    assert ch.commands[0][1]["attempts"] == left


@pytest.mark.parametrize("max_attempts", [1, 3, 8])
def test_a_failed_replay_ends_failed_again(max_attempts: int) -> None:
    ch = _StubCH([_failed_row(attempts=max_attempts)])
    _requeue(ch, ["q1"], max_attempts=max_attempts)
    row = _failed_row() | {"status": "pending", "attempts": ch.commands[0][1]["attempts"]}
    status = asyncio.run(q.mark_error(row, error="again", max_attempts=max_attempts, ch=ch))
    assert status == "failed" and ch.inserted[-1]["attempts"] == max_attempts


def test_waiting_and_finished_batches_submit_no_retry() -> None:
    ch = _StubCH([{"queue_id": "q1", "status": "held"}, {"queue_id": "q2", "status": "done"}])
    assert _requeue(ch, ["q1", "q2", "q9"]) == ([], ["q1", "q2"], [], [])
    assert not ch.commands


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_batch_past_its_last_day_is_expired_not_requeued(tmp_path: Path) -> None:
    # enqueued_at is in the sort key: the old batch is written queued on its last day.
    old = _queued("old", by="process:sa:a", status="failed").replace(
        "enqueued_by) VALUES", "enqueued_by, enqueued_at) VALUES"
    )
    old = f"{old[:-1]}, now64(3) - toIntervalDay({q.TTL_DAYS - 1}))"
    ch = _MutationCH(tmp_path / "ch", old, _queued("new", by="process:sa:a", status="failed"))
    assert _requeue(ch, ["old", "new"]) == (["new"], [], [], ["old"])
    assert [p["batches"] for _, p in ch.commands] == [[("a", "u1", "s1", "new", 1)]]
    assert asyncio.run(q.drop_expiring(ch=ch)) == 0


def test_switched_off_orgs_leave_failed_batches_unchanged() -> None:
    ch = _StubCH([_failed_row()])
    switch_off("o")
    assert _requeue(ch, ["q1"]) == ([], [], ["q1"], [])
    assert not ch.commands
    switch_off()
    assert _requeue(ch, ["q1"]).requeued == ["q1"]


def test_an_unread_switch_submits_no_retry() -> None:
    from server.llm.customer import SwitchUnread

    ch = _StubCH([_failed_row()])
    switch_unread()
    with pytest.raises(SwitchUnread):
        _requeue(ch, ["q1"])
    assert not ch.commands


def test_the_handler_requeues_for_the_callers_principal_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.extract.handler import handle_memory_extraction_retry_mine

    seen: dict[str, Any] = {}

    async def _requeue_own(**kw: Any) -> q.Requeued:
        seen.update(kw)
        return q.Requeued(["q2"], ["q1"], ["q3"], ["q4"])

    monkeypatch.setattr(q, "requeue_own_failed", _requeue_own)
    monkeypatch.setattr(settings, "extraction_queue_max_attempts", 5)
    inp = MemoryExtractionRetryMineInput(
        org="o", namespace="n", queue_ids=["q1", "q2", "q1", "q3", "q4", "q9"]
    )
    out = _in_context(
        _auth(read_user_id="u1"), lambda: asyncio.run(handle_memory_extraction_retry_mine(inp))
    )
    assert seen["queue_ids"] == ["q1", "q2", "q3", "q4", "q9"]
    assert (seen["enqueued_by"], seen["max_attempts"]) == ("process:sa:chat-agent", 5)
    assert seen["scope"] == ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert (out.requeued, out.not_failed, out.model_calls_off, out.expired, out.unknown) == (
        ["q2"],
        ["q1"],
        ["q3"],
        ["q4"],
        ["q9"],
    )


def test_a_token_naming_no_principal_requeues_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.extract.handler import handle_memory_extraction_retry_mine

    async def _never(**kw: Any) -> q.Requeued:
        raise AssertionError("no read for a token that names no principal")

    monkeypatch.setattr(q, "requeue_own_failed", _never)
    inp = MemoryExtractionRetryMineInput(org="o", namespace="n", queue_ids=["q1"])
    out = _in_context(
        _auth(subject=""), lambda: asyncio.run(handle_memory_extraction_retry_mine(inp))
    )
    assert (out.requeued, out.unknown) == ([], ["q1"])


def test_it_is_a_scoped_writer_op_rate_limited_with_memory_writes() -> None:
    from server.core.ratelimit import op_class_for
    from server.identity.domain.read_scope import SCOPED_OPS
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import _MCP_HIDDEN_OPS, op_toolsets
    from server.transport.mcp_tools import _tool_annotations

    op = OPERATIONS_BY_NAME[OP]
    assert op.required_role == Role.WRITER and not op.read_only and op.idempotent
    assert _tool_annotations(op) == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    assert OP in SCOPED_OPS and OP not in _MCP_HIDDEN_OPS and op_toolsets(OP) == []
    assert op_class_for(OP) == op_class_for("memory.set") == "memory"


def test_loom_op_runs_it_but_not_on_a_readonly_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _requeue_own(**kw: Any) -> q.Requeued:
        return q.Requeued(list(kw["queue_ids"]), [], [], [])

    monkeypatch.setattr(q, "requeue_own_failed", _requeue_own)
    _defaults(monkeypatch)
    body = _client(monkeypatch).post("/mcp", json=_loom_op(OP, {"queue_ids": ["q1"]})).json()
    assert not body["result"].get("isError"), body
    assert body["result"]["structuredContent"]["requeued"] == ["q1"]
    _, early = mcp_op.route(_loom_op(OP, {"queue_ids": ["q1"]}), toolset="readonly", auth=None)
    assert early is not None and "read-only" in early["result"]["structuredContent"]["error"]
