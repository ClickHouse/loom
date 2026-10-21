"""memory.extraction_retry_mine: one more replay of the caller's own failed batches.

- Only a `failed` batch the calling principal queued in the namespace, inside the
  token's read scope, goes back to `pending`, every column carried, with one attempt
  left under `extraction_queue_max_attempts`: a failed replay marks it `failed` again.
- A batch still waiting or done is left alone (`not_failed`); any other id is `unknown`.
- It is a writer op: callable by name and through `loom_op`, not on a read-only
  connection.

The read runs on clickhouse-local against schema.sql where the binary is installed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.api.models_bulk import MemoryExtractionRetryMineInput
from server.memory.app import extraction_queue as q
from server.settings import settings
from server.transport import mcp_op
from tests.unit.clickhouse_local import CLICKHOUSE, ddl
from tests.unit.test_extraction_status_mine import (
    _auth,
    _client,
    _defaults,
    _in_context,
    _LocalCH,
    _loom_op,
    _queued,
)

OP = "memory.extraction_retry_mine"
TOOL = "loom_memory_extraction_retry_mine"


class _WritingLocalCH(_LocalCH):
    """Reads run on clickhouse-local; inserts are recorded, not run."""

    def __init__(self, *setup: str) -> None:
        super().__init__(*setup)
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.append((table, rows))


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_only_the_principals_failed_batches_go_back_with_one_attempt_left() -> None:
    ch = _WritingLocalCH(
        ddl("memory_extraction_queue"),
        _queued("q1", by="process:sa:a", status="failed"),
        _queued("q2", by="process:sa:a"),
        _queued("q3", by="process:sa:a", status="done"),
        _queued("q4", by="process:sa:b", status="failed"),
        _queued("q5", by="process:sa:a", namespace="n2", status="failed"),
        _queued("q6", by="process:sa:a", user_id="u2", status="failed"),
    )
    asked = ["q1", "q2", "q3", "q4", "q5", "q6"]

    def requeue(scope: ReadFilters) -> tuple[list[str], list[str]]:
        return asyncio.run(
            q.requeue_own_failed(
                org="o",
                namespace="n",
                queue_ids=asked,
                enqueued_by="process:sa:a",
                scope=scope,
                max_attempts=3,
                ch=ch,
            )
        )

    one_user = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert requeue(one_user) == (["q1"], ["q2", "q3"])
    table, rows = ch.inserted[0]
    assert table == q.TABLE and [r["queue_id"] for r in rows] == ["q1"]
    row = rows[0]
    assert (row["status"], row["attempts"], row["version"] > 1) == ("pending", 2, True)
    assert row["lease_until"] == q._EPOCH and set(row) == set(q.COLUMNS)
    assert "process:sa:a" in row["payload"]  # the stored request rides along unchanged

    ch.inserted.clear()
    assert requeue(WHOLE_NAMESPACE) == (["q1", "q6"], ["q2", "q3"])
    assert sorted(r["queue_id"] for r in ch.inserted[0][1]) == ["q1", "q6"]


@pytest.mark.parametrize(("max_attempts", "left"), [(0, 0), (1, 0), (3, 2), (8, 7)])
def test_a_requeued_batch_has_one_attempt_left(max_attempts: int, left: int) -> None:
    class _CH:
        inserted: list[dict[str, Any]] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            return [{c: "" for c in q.COLUMNS} | {"queue_id": "q1", "status": "failed"}]

        async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None):
            self.inserted.extend(rows)

    ch = _CH()
    asyncio.run(
        q.requeue_own_failed(
            org="o",
            namespace="n",
            queue_ids=["q1"],
            enqueued_by="process:sa:a",
            scope=WHOLE_NAMESPACE,
            max_attempts=max_attempts,
            ch=ch,
        )
    )
    assert [r["attempts"] for r in ch.inserted] == [left]


def test_nothing_is_written_when_no_asked_batch_failed() -> None:
    class _CH:
        inserted: list[Any] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            return [{"queue_id": "q1", "status": "pending"}]

        async def insert(self, *a: Any, **k: Any) -> None:
            self.inserted.append(a)

    ch = _CH()
    out = asyncio.run(
        q.requeue_own_failed(
            org="o",
            namespace="n",
            queue_ids=["q1", "q2"],
            enqueued_by="process:sa:a",
            scope=WHOLE_NAMESPACE,
            max_attempts=3,
            ch=ch,
        )
    )
    assert out == ([], ["q1"]) and ch.inserted == []


def test_the_handler_requeues_for_the_callers_principal_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.extract.handler import handle_memory_extraction_retry_mine

    seen: dict[str, Any] = {}

    async def _requeue(**kw: Any) -> tuple[list[str], list[str]]:
        seen.update(kw)
        return ["q2"], ["q1"]

    monkeypatch.setattr(q, "requeue_own_failed", _requeue)
    monkeypatch.setattr(settings, "extraction_queue_max_attempts", 5)
    inp = MemoryExtractionRetryMineInput(org="o", namespace="n", queue_ids=["q1", "q2", "q1", "q9"])
    out = _in_context(
        _auth(read_user_id="u1"), lambda: asyncio.run(handle_memory_extraction_retry_mine(inp))
    )
    assert seen["queue_ids"] == ["q1", "q2", "q9"]
    assert (seen["enqueued_by"], seen["max_attempts"]) == ("process:sa:chat-agent", 5)
    assert seen["scope"] == ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert (out.requeued, out.not_failed, out.unknown) == (["q2"], ["q1"], ["q9"])


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
    async def _requeue(**kw: Any) -> tuple[list[str], list[str]]:
        return list(kw["queue_ids"]), []

    monkeypatch.setattr(q, "requeue_own_failed", _requeue)
    _defaults(monkeypatch)
    body = _client(monkeypatch).post("/mcp", json=_loom_op(OP, {"queue_ids": ["q1"]})).json()
    assert not body["result"].get("isError"), body
    assert body["result"]["structuredContent"]["requeued"] == ["q1"]
    _, early = mcp_op.route(_loom_op(OP, {"queue_ids": ["q1"]}), toolset="readonly", auth=None)
    assert early is not None and "read-only" in early["result"]["structuredContent"]["error"]
