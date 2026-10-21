"""memory.extraction_retry_mine: one more replay of the caller's own failed batches.

- Only a `failed` batch the calling principal queued in the namespace, inside the
  token's read scope, goes back to `pending`, every column carried, with one attempt
  left under `extraction_queue_max_attempts`: a failed replay marks it `failed` again.
- It reads the batches' states first and the full rows of the failed ones only, with
  sequential consistency.
- While the org's model calls are switched off nothing is put back (`model_calls_off`):
  the batch could only wait, and the switched-off drop would record it dropped again.
- A batch still waiting or done is left alone (`not_failed`); any other id is `unknown`.
- It is a writer op: callable by name and through `loom_op`, not on a read-only
  connection.

The reads run on clickhouse-local against schema.sql where the binary is installed.
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
from tests.unit.switch_state import switch_off, switch_unread
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


class _WritingLocalCH(_LocalCH):
    """Reads run on clickhouse-local; inserts are recorded, not run."""

    def __init__(self, *setup: str) -> None:
        super().__init__(*setup)
        self.reads: list[str] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.reads.append(sql)
        return await super().query_all(sql, params)

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.append((table, rows))


class _StubCH:
    """Answers every read with `rows`; records what is inserted."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.reads.append((sql, dict(params or {})))
        return [dict(r) for r in self.rows]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.extend(rows)


def _failed_row(queue_id: str = "q1", attempts: int = 3) -> dict[str, Any]:
    return {c: "" for c in q.COLUMNS} | {
        "org": "o",
        "namespace": "n",
        "queue_id": queue_id,
        "status": "failed",
        "attempts": attempts,
        "last_error": "ExtractionError: boom",
        "version": 1,
    }


def _requeue(ch: Any, ids: list[str], *, max_attempts: int = 3, **kw: Any) -> q.Requeued:
    return asyncio.run(
        q.requeue_own_failed(
            org="o",
            namespace="n",
            queue_ids=ids,
            enqueued_by="process:sa:a",
            scope=kw.pop("scope", WHOLE_NAMESPACE),
            max_attempts=max_attempts,
            ch=ch,
        )
    )


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
        _queued("q7", by="process:sa:a", org="o2", status="failed"),
    )
    asked = ["q1", "q2", "q3", "q4", "q5", "q6", "q7"]

    one_user = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert _requeue(ch, asked, scope=one_user) == (["q1"], ["q2", "q3"], [])
    # The states first, then the full rows of the failed ones only.
    assert ch.reads[0].split("FROM")[0].split() == ["SELECT", "queue_id,", "status"]
    assert "payload" in ch.reads[1].split("FROM")[0]
    table, rows = ch.inserted[0]
    assert table == q.TABLE and [r["queue_id"] for r in rows] == ["q1"]
    row = rows[0]
    assert (row["status"], row["attempts"], row["version"] > 1) == ("pending", 2, True)
    assert row["lease_until"] == q._EPOCH and set(row) == set(q.COLUMNS)
    assert "process:sa:a" in row["payload"]  # the stored request rides along unchanged

    ch.inserted.clear()
    assert _requeue(ch, asked) == (["q1", "q6"], ["q2", "q3"], [])
    assert sorted(r["queue_id"] for r in ch.inserted[0][1]) == ["q1", "q6"]


def test_the_full_rows_are_read_for_the_failed_ids_only() -> None:
    ch = _StubCH([_failed_row("q1")])
    _requeue(ch, ["q1", "q2"])
    (first, first_params), (second, second_params) = ch.reads
    assert first_params["qids"] == ["q1", "q2"] and second_params["qids"] == ["q1"]
    assert "payload" not in first.split("FROM")[0] and "payload" in second.split("FROM")[0]


@pytest.mark.parametrize(("max_attempts", "left"), [(0, 0), (1, 0), (3, 2), (8, 7)])
def test_a_requeued_batch_has_one_attempt_left(max_attempts: int, left: int) -> None:
    ch = _StubCH([_failed_row()])
    _requeue(ch, ["q1"], max_attempts=max_attempts)
    assert [r["attempts"] for r in ch.inserted] == [left]


@pytest.mark.parametrize("max_attempts", [1, 3, 8])
def test_a_requeue_buys_exactly_one_more_replay(max_attempts: int) -> None:
    """The worker's own transition after the replay: a failure marks it failed again."""
    ch = _StubCH([_failed_row(attempts=max_attempts)])
    _requeue(ch, ["q1"], max_attempts=max_attempts)
    (requeued,) = ch.inserted
    status = asyncio.run(
        q.mark_error(requeued, error="ExtractionError: again", max_attempts=max_attempts, ch=ch)
    )
    assert status == "failed" and ch.inserted[-1]["attempts"] == max_attempts


def test_a_batch_put_back_or_gone_between_the_reads_is_not_requeued_again() -> None:
    """The second read decides: a batch another retry already put back is `not_failed`,
    one no longer found (erased, or past the TTL) is in no list, so the handler answers
    it `unknown`."""

    class _CH(_StubCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            self.reads.append((sql, dict(params or {})))
            if len(self.reads) == 1:  # the states: both failed
                return [_failed_row("q1"), _failed_row("q2")]
            return [_failed_row("q1") | {"status": "pending"}]  # q1 put back, q2 gone

    ch = _CH([])
    assert _requeue(ch, ["q1", "q2"]) == ([], ["q1"], []) and ch.inserted == []


def test_nothing_is_written_when_no_asked_batch_failed() -> None:
    ch = _StubCH([{"queue_id": "q1", "status": "pending"}])
    assert _requeue(ch, ["q1", "q2"]) == ([], ["q1"], []) and ch.inserted == []
    assert len(ch.reads) == 1  # no full-row read either


def test_a_switched_off_orgs_failed_batches_stay_failed() -> None:
    """A replay could only wait, and the switched-off drop would record it dropped again
    on its next pass; once the org is back on, a retry buys the one replay."""
    ch = _StubCH([_failed_row("q1"), {"queue_id": "q2", "status": "done"}])
    switch_off("o")
    assert _requeue(ch, ["q1", "q2"]) == ([], ["q2"], ["q1"])
    assert ch.inserted == [] and len(ch.reads) == 1
    switch_off()
    assert _requeue(ch, ["q1"]).requeued == ["q1"]


def test_an_unread_switch_requeues_nothing() -> None:
    from server.llm.customer import SwitchUnread

    ch = _StubCH([_failed_row()])
    switch_unread()
    with pytest.raises(SwitchUnread):
        _requeue(ch, ["q1"])
    assert ch.inserted == []


def test_the_handler_requeues_for_the_callers_principal_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.extract.handler import handle_memory_extraction_retry_mine

    seen: dict[str, Any] = {}

    async def _requeue_own(**kw: Any) -> q.Requeued:
        seen.update(kw)
        return q.Requeued(["q2"], ["q1"], ["q3"])

    monkeypatch.setattr(q, "requeue_own_failed", _requeue_own)
    monkeypatch.setattr(settings, "extraction_queue_max_attempts", 5)
    inp = MemoryExtractionRetryMineInput(
        org="o", namespace="n", queue_ids=["q1", "q2", "q1", "q3", "q9"]
    )
    out = _in_context(
        _auth(read_user_id="u1"), lambda: asyncio.run(handle_memory_extraction_retry_mine(inp))
    )
    assert seen["queue_ids"] == ["q1", "q2", "q3", "q9"]
    assert (seen["enqueued_by"], seen["max_attempts"]) == ("process:sa:chat-agent", 5)
    assert seen["scope"] == ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert (out.requeued, out.not_failed, out.model_calls_off, out.unknown) == (
        ["q2"],
        ["q1"],
        ["q3"],
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
        return q.Requeued(list(kw["queue_ids"]), [], [])

    monkeypatch.setattr(q, "requeue_own_failed", _requeue_own)
    _defaults(monkeypatch)
    body = _client(monkeypatch).post("/mcp", json=_loom_op(OP, {"queue_ids": ["q1"]})).json()
    assert not body["result"].get("isError"), body
    assert body["result"]["structuredContent"]["requeued"] == ["q1"]
    _, early = mcp_op.route(_loom_op(OP, {"queue_ids": ["q1"]}), toolset="readonly", auth=None)
    assert early is not None and "read-only" in early["result"]["structuredContent"]["error"]
