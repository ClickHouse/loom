"""Caller-bound extraction status over HTTP, MCP and production SQL."""

from __future__ import annotations

import asyncio
import contextvars
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import server.transport.mcp as mcp
import server.transport.mcp_call as mcp_call
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.api.models_bulk import MAX_OWN_BATCH_IDS, MemoryExtractionStatusMineInput
from server.memory.app import extraction_queue as q
from server.settings import settings
from server.transport import mcp_op
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

OP = "memory.extraction_status_mine"
TOOL = "loom_memory_extraction_status_mine"


class _RecordingCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.calls.append((sql, dict(params or {})))
        return []


def _row(queue_id: str, status: str = "pending", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "queue_id": queue_id,
        "agent": "a",
        "user_id": "u1",
        "session_id": "-",
        "status": status,
        "attempts": 0,
        "message_count": 2,
        "content_chars": 40,
        "written": 0,
        "last_error": "",
        "enqueued_at": None,
        "updated_at": None,
    }
    return {**base, **kw}


def _auth(subject: str = "sa:chat-agent", **claims: Any) -> Any:
    from server.auth.jwt import AuthInfo

    return AuthInfo(
        subject=subject,
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        **claims,
    )


def _in_context(auth: Any, fn: Any) -> Any:
    """Run `fn()` with `auth` bound and its scope resolved, as the transport does."""
    from server.auth.context import set_current_auth
    from server.identity.domain.read_scope import bind_request_read_filters, resolve_read_filters

    def go() -> Any:
        set_current_auth(auth)
        bind_request_read_filters(
            auth, resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth)
        )
        return fn()

    return contextvars.Context().run(go)


def test_the_read_binds_the_namespace_the_ids_and_the_enqueuer() -> None:
    ch = _RecordingCH()
    asyncio.run(
        q.list_own_status(
            org="o",
            namespace="n",
            queue_ids=["q1", "q2"],
            enqueued_by="process:sa:chat-agent",
            scope=WHOLE_NAMESPACE,
            ch=ch,
        )
    )
    sql, params = ch.calls[0]
    assert params == {
        "org": "o",
        "ns": "n",
        "qids": ["q1", "q2"],
        "by": "process:sa:chat-agent",
        "lim": 2,
    }
    prewhere, where = sql.split("PREWHERE", 1)[1].split("WHERE if(enqueued_by", 1)
    assert "queue_id IN {qids:Array(String)}" in prewhere and "namespace = {ns:String}" in prewhere
    assert ", enqueued_by, JSONExtractString(payload, 'enqueued_by')) = {by:String}" in where
    # The transcript is the caller's own and never part of the answer.
    assert "payload" not in sql.split("FROM", 1)[0]


def test_a_token_bounded_to_one_user_reads_only_that_users_batches() -> None:
    ch = _RecordingCH()
    scope = ReadFilters(scope="own_session", user_id="u1", session_id="s1")
    asyncio.run(
        q.list_own_status(
            org="o", namespace="n", queue_ids=["q1"], enqueued_by="process:sa:b", scope=scope, ch=ch
        )
    )
    sql, params = ch.calls[0]
    prewhere = sql.split("PREWHERE", 1)[1].split("WHERE if(enqueued_by", 1)[0]
    assert "user_id = {scope_uid:String}" in prewhere and params["scope_uid"] == "u1"
    assert "session_id = {scope_sid:String}" in prewhere and params["scope_sid"] == "s1"


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)


def _queued(
    queue_id: str,
    *,
    by: str | None,
    org: str = "o",
    namespace: str = "n",
    user_id: str = "u1",
    status: str = "pending",
    version: int = 1,
    last_error: str = "",
    column_by: str = "",
) -> str:
    payload = json.dumps({"messages": [], **({"enqueued_by": by} if by is not None else {})})
    return (
        "INSERT INTO loom.memory_extraction_queue (org, namespace, agent, user_id, session_id, "
        "queue_id, payload, message_count, content_chars, status, version, last_error, enqueued_by) VALUES "
        f"('{org}', '{namespace}', 'a', '{user_id}', 's1', '{queue_id}', '{payload}', 2, 40, "
        f"'{status}', {version}, '{last_error}', '{column_by}')"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_only_the_enqueuers_batches_in_the_namespace_and_scope_answer() -> None:
    """On the production table: the newest version of each batch the principal queued
    in the org, namespace and scope, and nothing another principal, org, namespace or
    user holds."""
    ch = _LocalCH(
        ddl("memory_extraction_queue"),
        _queued("q1", by="process:sa:a"),
        _queued("q1", by="process:sa:a", status="done", version=2),
        _queued("q2", by="process:sa:b"),
        _queued("q3", by="process:sa:a", namespace="n2"),
        _queued("q4", by="process:sa:a", user_id="u2", status="failed"),
        _queued("q5", by=None),  # queued by a dev-mode server: no principal stored
        _queued("q6", by="process:sa:a", org="o2"),  # the same subject in another org
    )
    asked = ["q1", "q2", "q3", "q4", "q5", "q6"]

    def ids(
        namespace: str, by: str, scope: ReadFilters = WHOLE_NAMESPACE, org: str = "o"
    ) -> list[tuple[str, str]]:
        rows = asyncio.run(
            q.list_own_status(
                org=org, namespace=namespace, queue_ids=asked, enqueued_by=by, scope=scope, ch=ch
            )
        )
        return sorted((r["queue_id"], r["status"]) for r in rows)

    assert ids("n", "process:sa:a") == [("q1", "done"), ("q4", "failed")]
    one_user = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert ids("n", "process:sa:a", one_user) == [("q1", "done")]
    assert ids("n", "process:sa:b") == [("q2", "pending")]
    assert ids("n2", "process:sa:a") == [("q3", "pending")]
    assert ids("n", "") == [("q5", "pending")]
    assert ids("n", "process:sa:c") == []
    assert ids("n", "process:sa:a", org="o2") == [("q6", "pending")]


def test_the_read_sees_writes_another_replica_just_made() -> None:
    """Sequentially consistent: a deferral or a requeue may have landed a moment ago on
    another replica, and the retry must not put back a stale `failed`."""
    from server.core.ch import sequential_reads_active

    seen: list[bool] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            seen.append(sequential_reads_active())
            return []

    asyncio.run(
        q.list_own_status(
            org="o",
            namespace="n",
            queue_ids=["q1"],
            enqueued_by="process:sa:a",
            scope=WHOLE_NAMESPACE,
            ch=_CH(),
        )
    )
    assert seen == [True] and not sequential_reads_active()


def test_the_handler_asks_for_the_callers_principal_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.extract.handler import handle_memory_extraction_status_mine

    seen: dict[str, Any] = {}

    async def _own(**kw: Any) -> list[dict[str, Any]]:
        seen.update(kw)
        return [
            _row("q3", "failed", last_error="ExtractionError: x"),
            _row("q1", "held"),
            _row("q2", "done", written=3),
        ]

    monkeypatch.setattr(q, "list_own_status", _own)
    inp = MemoryExtractionStatusMineInput(
        org="o", namespace="n", queue_ids=["q1", "q2", "q1", "q3", "q9"]
    )
    out = _in_context(
        _auth(read_user_id="u1"), lambda: asyncio.run(handle_memory_extraction_status_mine(inp))
    )
    assert seen["queue_ids"] == ["q1", "q2", "q3", "q9"]  # a repeated id is asked once
    assert seen["enqueued_by"] == "process:sa:chat-agent"
    assert seen["scope"] == ReadFilters(scope="own_user", user_id="u1", session_id=None)
    assert (seen["org"], seen["namespace"]) == ("o", "n")
    assert [b.queue_id for b in out.batches] == ["q3", "q1", "q2"]
    assert out.unknown == ["q9"]
    # Not extracted yet: any state before an outcome; failed: the batches that ended so.
    assert (out.pending, out.failed) == (1, 1)


def test_a_token_naming_no_principal_matches_no_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not the batches queued without one: those are a dev-mode server's, read with no
    auth at all."""
    from server.memory.app.extract.handler import handle_memory_extraction_status_mine

    async def _never(**kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("no read for a token that names no principal")

    monkeypatch.setattr(q, "list_own_status", _never)
    inp = MemoryExtractionStatusMineInput(org="o", namespace="n", queue_ids=["q1", "q2"])
    out = _in_context(
        _auth(subject=""), lambda: asyncio.run(handle_memory_extraction_status_mine(inp))
    )
    assert (out.batches, out.unknown) == ([], ["q1", "q2"])


@pytest.mark.parametrize(
    "claims",
    [{"read_scope": "own_session", "read_user_id": "u1"}, {"read_scope": "own_user"}],
    ids=["own_session", "own_user_unpinned"],
)
def test_a_scope_the_body_cannot_complete_is_refused_in_words_that_fit(
    claims: dict[str, str],
) -> None:
    """The op takes no user_id or session_id, so the refusal does not ask for one in the
    body."""
    from server.core.errors import IdentityIncompleteError
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    op = OPERATIONS_BY_NAME[OP]
    inp = MemoryExtractionStatusMineInput(org="o", namespace="n", queue_ids=["q1"])
    with pytest.raises(IdentityIncompleteError, match="takes no user_id or session_id") as e:
        contextvars.Context().run(gates.read_scope, op, _auth(**claims), inp)
    assert "request body" not in str(e.value)


@pytest.mark.parametrize(
    "ids",
    [[], [f"q{i}" for i in range(MAX_OWN_BATCH_IDS + 1)], ["x" * 65], [""]],
    ids=["none", "past_the_page", "id_too_long", "blank_id"],
)
def test_the_page_is_bounded(ids: list[str]) -> None:
    with pytest.raises(ValidationError):
        MemoryExtractionStatusMineInput(org="o", namespace="n", queue_ids=ids)
    MemoryExtractionStatusMineInput(
        org="o", namespace="n", queue_ids=[f"q{i}" for i in range(MAX_OWN_BATCH_IDS)]
    )


def test_it_is_a_scoped_read_only_viewer_op_rate_limited_with_memory_reads() -> None:
    from server.core.ratelimit import op_class_for
    from server.identity.domain.read_scope import SCOPED_OPS
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import _MCP_HIDDEN_OPS, op_toolsets
    from server.transport.mcp_tools import _tool_annotations

    op = OPERATIONS_BY_NAME[OP]
    assert op.read_only and op.required_role == Role.VIEWER and not op.audit
    assert _tool_annotations(op)["readOnlyHint"] is True
    assert OP in SCOPED_OPS and OP not in _MCP_HIDDEN_OPS
    assert op_toolsets(OP) == []  # callable by name, listed on no roster
    assert op_class_for(OP) == op_class_for("memory.list") == "memory"
    assert op.budget_p99_ms > 0
    assert "memory.extraction_status" in _MCP_HIDDEN_OPS


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    mcp.attach(app)
    return TestClient(app)


def _loom_op(op: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": op, "arguments": arguments}},
    }


def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _caller_defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "chat-agent"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _caller_defaults)


def test_loom_op_runs_it_through_the_listed_tool_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def _own(**kw: Any) -> list[dict[str, Any]]:
        seen.append(kw)
        return [_row("q1")]

    monkeypatch.setattr(q, "list_own_status", _own)
    _defaults(monkeypatch)
    client = _client(monkeypatch)
    body = client.post("/mcp", json=_loom_op(OP, {"queue_ids": ["q1", "q2"]})).json()["result"]
    assert not body.get("isError"), body
    sc = body["structuredContent"]
    assert [b["queue_id"] for b in sc["batches"]] == ["q1"] and sc["unknown"] == ["q2"]
    assert (sc["pending"], sc["failed"]) == (1, 0)
    # The connection's identity defaults filled the namespace, as for a listed tool.
    assert (seen[0]["org"], seen[0]["namespace"]) == ("o", "n")
    direct = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"queue_ids": ["q1"]}},
    }
    body = client.post("/mcp", json=direct).json()["result"]
    assert not body.get("isError"), body
    assert body["structuredContent"]["batches"][0]["queue_id"] == "q1"


def test_a_namespace_the_token_cannot_reach_is_refused_before_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz
    from server.auth.jwt import mint_token
    from server.transport import namespace_resolve as nr

    read: list[Any] = []

    async def _own(**kw: Any) -> list[dict[str, Any]]:
        read.append(kw)
        return []

    async def _no_grant(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(q, "list_own_status", _own)
    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    monkeypatch.setattr(nr, "known_namespaces", lambda org: {"beta": "dev/beta"})
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    tok = mint_token(
        subject="sa:chat-agent",
        org="dev",
        namespaces=["alpha"],
        roles=["writer"],
        ttl_seconds=300,
        is_service_account=True,
    )
    app = FastAPI()
    mcp.attach(app)
    r = TestClient(app).post(
        "/mcp",
        json=_loom_op(OP, {"namespace": "beta", "queue_ids": ["q1"]}),
        headers={"Authorization": f"Bearer {tok}"},
    )
    body = r.json()["result"]
    assert body["isError"] and body["structuredContent"]["code"] == "NAMESPACE_NOT_ACCESSIBLE"
    assert read == []


def test_a_readonly_connection_routes_it() -> None:
    rewritten, early = mcp_op.route(
        _loom_op(OP, {"queue_ids": ["q1"]}), toolset="readonly", auth=None
    )
    assert early is None and rewritten["params"]["name"] == TOOL


def test_help_finds_it_and_the_hidden_op_names_it_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = [r["op"] for r in mcp_op.catalog("extraction status")["ops"]]
    assert OP in found and "memory.extraction_status" not in found
    row = next(r for r in mcp_op.catalog(OP)["ops"] if r["op"] == OP)
    assert row["required"] == ["queue_ids"]
    body = (
        _client(monkeypatch)
        .post("/mcp", json=_loom_op("memory.extraction_status", {}))
        .json()["result"]
    )
    sc = body["structuredContent"]
    assert body["isError"] and sc["code"] == "OP_NOT_ON_MCP"
    assert OP in sc["instead"]


def test_the_mcp_tool_runs_the_registered_handler() -> None:
    """A guard for the MCP tests above: they stub the queue read, not the handler."""
    from server.memory.app.extract.handler import handle_memory_extraction_status_mine

    assert mcp_call.TOOLS_BY_NAME[TOOL].handler is handle_memory_extraction_status_mine


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_held_batches_report_the_same_state_as_namespace_status() -> None:
    ch = _LocalCH(
        ddl("memory_extraction_queue"),
        _queued("held", by="process:sa:a", status="running", last_error="held: budget"),
        _queued("running", by="process:sa:a", status="running"),
    )
    rows = asyncio.run(
        q.list_own_status(
            org="o",
            namespace="n",
            queue_ids=["held", "running"],
            enqueued_by="process:sa:a",
            scope=WHOLE_NAMESPACE,
            ch=ch,
        )
    )
    assert {r["queue_id"]: r["status"] for r in rows} == {"held": "held", "running": "running"}


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_owner_column_and_legacy_payload_rows_are_both_readable() -> None:
    ch = _LocalCH(
        ddl("memory_extraction_queue"),
        _queued("column", by=None, column_by="process:sa:a"),
        _queued("legacy", by="process:sa:a"),
    )
    rows = asyncio.run(
        q.list_own_status(
            org="o",
            namespace="n",
            queue_ids=["column", "legacy"],
            enqueued_by="process:sa:a",
            scope=WHOLE_NAMESPACE,
            ch=ch,
        )
    )
    assert sorted(r["queue_id"] for r in rows) == ["column", "legacy"]
