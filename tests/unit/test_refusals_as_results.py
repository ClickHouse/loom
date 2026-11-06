"""A Loom refusal is a tool result; an owner-only refusal
names the user, never a route; a mentor line needs no skill."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from fastapi import HTTPException

from server.core.errors import (
    AuthInsufficientError,
    BackendTimeoutError,
    NotFoundError,
    OverloadedError,
    SchemaPendingError,
)
from server.transport import mcp_call, mcp_outcome
from server.transport.mcp_refusals import refusal_body, refusal_text

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_a_4xx_refusal_keeps_its_structured_body() -> None:
    e = HTTPException(
        423,
        detail={
            "code": "LOCKED",
            "message": "memory 'k' is locked read_only",
            "memory_key": "k",
            "lock_mode": "read_only",
        },
    )
    body = refusal_body(e)
    assert body == {
        "code": "LOCKED",
        "message": "memory 'k' is locked read_only",
        "memory_key": "k",
        "lock_mode": "read_only",
        "status": 423,
    }


def test_typed_errors_string_details_and_retry_after() -> None:
    assert refusal_body(NotFoundError("no such task"))["code"] == "RESOURCE_NOT_FOUND"
    assert refusal_body(AuthInsufficientError("no grant"))["status"] == 403
    b = refusal_body(HTTPException(409, detail="task is 'triage'"))
    assert b == {"message": "task is 'triage'", "code": "CONFLICT", "status": 409}
    r = refusal_body(
        HTTPException(
            429,
            detail={"code": "RATE_LIMITED", "message": "slow down"},
            headers={"Retry-After": "7"},
        )
    )
    assert r["retry_after_s"] == 7


@pytest.mark.parametrize(
    "e",
    [
        HTTPException(401, detail="unauthenticated"),
        HTTPException(500, detail="boom"),
        HTTPException(503, detail="unavailable"),
        ValueError("a bug"),
        KeyError("key"),
    ],
)
def test_auth_failures_server_errors_and_bugs_are_not_refusals(e: BaseException) -> None:
    assert refusal_body(e) is None


def test_the_text_line_carries_the_next_step() -> None:
    assert refusal_text(
        {
            "code": "TASK_NOT_CLAIMABLE",
            "message": "task is 'triage'",
            "next": "the owner accepts it via task.accept",
        }
    ) == ("TASK_NOT_CLAIMABLE: task is 'triage' Next: the owner accepts it via task.accept")


def _fail(monkeypatch: pytest.MonkeyPatch, e: BaseException) -> tuple[dict[str, Any], list[dict]]:
    events: list[dict] = []
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: events.append(k))
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: events.append(k))

    class _Op:
        name = "memory.delete"

    out = mcp_outcome._handler_failed(
        e,
        9,
        "loom_memory_delete",
        _Op(),
        {"memory_key": "k"},
        None,
        None,
        "rid",
        None,
        rl_ns="ns",
        elapsed_ms=3,
    )
    return out, events


def test_a_refusal_is_an_iserror_result_recorded_under_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, events = _fail(
        monkeypatch, HTTPException(423, detail={"code": "LOCKED", "message": "locked"})
    )
    res = out["result"]
    assert "error" not in out and res["isError"] is True
    assert (
        res["structuredContent"]["code"] == "LOCKED" and res["structuredContent"]["status"] == 423
    )
    assert res["content"][0]["text"] == "LOCKED: locked"
    assert events[0]["error_code"] == "LOCKED" and events[0]["success"] is False


def test_a_bug_stays_a_json_rpc_error_recorded_as_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    out, events = _fail(monkeypatch, ValueError("oops"))
    assert out["error"]["message"] == "tool execution failed: internal error; request_id=rid"
    assert events[0]["error_code"] == "INTERNAL_ERROR"


def test_owner_only_refusals_name_the_user_never_a_route() -> None:
    from server.memory.domain import lock as L
    from server.transport.mcp_op import hidden_refusal

    class Row:
        memory_key, memory_id, lock_reason, locked_by, lock_until = "k", "id", "", "", None

    lock_msg = L.refusal(Row(), "delete", op="memory.delete", mode="read_only").detail["message"]
    unlock = hidden_refusal("memory.unlock")
    other = hidden_refusal("memory.dedup_list")
    assert unlock is not None and other is not None
    for text in (lock_msg, unlock["instead"], unlock["error"], other["instead"]):
        low = text.lower()
        assert "sdk" not in low and "http" not in low and "/v1" not in low, text
    assert "dashboard" in lock_msg and "credential" in lock_msg
    assert "only the user" in unlock["instead"] and "credential" in unlock["instead"]


def test_the_mentor_line_does_not_send_the_agent_to_the_skill() -> None:
    from server.insights.app import mentor, mentor_rules

    class P:
        hints = [
            mentor_rules.Hint(
                code="facts_graph_unused",
                severity="info",
                text="walk the graph",
                ops=("facts.traverse",),
                evidence={"rows": 3},
            )
        ]

    line = mentor.render_pack_section(P())  # type: ignore[arg-type]
    assert line and "skill" not in line and "mentor.brief" in line


def test_the_mentor_skill_fires_on_an_unknown_plane_not_on_every_session() -> None:
    for plugin in ("claude-code-plugin", "codex-plugin"):
        text = (ROOT / "integrations" / plugin / "skills" / "loom-mentor" / "SKILL.md").read_text()
        head = text.split("---", 2)[1]
        assert "namespace you have not used before" not in head
        assert "needs no skill" in head


@pytest.mark.parametrize(
    ("e", "code", "extra"),
    [
        (
            OverloadedError("every connection is busy", retry_after_s=2),
            "OVERLOADED",
            {"retry_after_s": 2},
        ),
        (
            BackendTimeoutError("statement exceeded 30 s", statement_class="read", timeout_s=30.0),
            "BACKEND_TIMEOUT",
            {"statement_class": "read", "timeout_s": 30.0},
        ),
        (
            SchemaPendingError("loom.t is pending", table="t", migration="0001"),
            "SCHEMA_PENDING",
            {"table": "t", "migration": "0001"},
        ),
    ],
)
def test_a_named_5xx_keeps_its_code_and_hint(
    monkeypatch: pytest.MonkeyPatch, e: BaseException, code: str, extra: dict[str, Any]
) -> None:
    """A 5xx Loom raised on purpose stays a JSON-RPC error, but one that names
    its code and carries the body HTTP answers with, and the ledger records
    that code rather than INTERNAL_ERROR."""
    out, events = _fail(monkeypatch, e)
    err = out["error"]
    assert err["message"].startswith(f"tool execution failed: {code}: ")
    assert err["message"].endswith("; request_id=rid")
    assert err["data"]["code"] == code and err["data"]["status"] in (503, 504)
    assert extra.items() <= err["data"].items()
    assert err["data"]["request_id"] == "rid" and err["data"]["tool"] == "loom_memory_delete"
    assert events[0]["error_code"] == code


def test_http_and_mcp_carry_the_same_detail_for_a_loom_error() -> None:
    """HTTP's 5xx body and MCP's error data are both `to_detail()`."""
    import asyncio

    from fastapi import HTTPException as _HTTPException
    from pydantic import BaseModel

    from server.operations.base import Operation, Role
    from server.transport import http as http_mod

    class _In(BaseModel):
        pass

    e = OverloadedError("busy", retry_after_s=3)

    async def _raise(_inp: Any) -> _In:
        raise e

    op = Operation(
        name="test.busy",
        input_model=_In,
        output_model=_In,
        handler=_raise,
        idempotent=True,
        required_role=Role.VIEWER,
    )
    mp = pytest.MonkeyPatch()
    mp.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    mp.setattr(http_mod, "_record_failure", lambda *a, **k: None)
    try:
        with pytest.raises(_HTTPException) as got:
            asyncio.run(
                http_mod._run_handler(
                    op, _In(), http_mod._HttpCall(request_id="r", identity={"namespace": "n"})
                )
            )
    finally:
        mp.undo()
    assert got.value.status_code == 503
    assert (
        got.value.detail
        == e.to_detail()
        == {"code": "OVERLOADED", "message": "busy", "retry_after_s": 3}
    )


def test_a_role_refusal_names_the_role_the_catalogue_names() -> None:
    """`required_role` on MCP's role refusal is the role's name, as
    ops.capabilities and the action schema spell it — not the IntEnum's
    ordinal ("30"), which tells a model nothing."""
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["memory.clear"]
    out = mcp_call._forbidden_result(op, PermissionError("no"), None, "o", "n")
    assert out["isError"] is True
    assert out["structuredContent"]["code"] == "AUTH_INSUFFICIENT"
    assert out["structuredContent"]["required_role"] == op.required_role.name == "ADMIN"
