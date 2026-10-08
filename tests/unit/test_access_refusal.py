"""A code/tracker refusal says who, which index, how to get
in, and that retrying on another connection will not help."""

from __future__ import annotations

from types import SimpleNamespace

import server.transport.mcp_auth as mcp_auth
import server.transport.mcp_call as mcp_call
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import access_refusal as ar


def test_a_self_signup_token_is_told_to_claim_or_mint() -> None:
    body = ar.refusal("sa:agent-ubuntu-box-claude-c-2cfdb3", "clickhouse", "code-loom")
    assert body["code"] == "AUTH_INSUFFICIENT" and body["reason"] == "NO_REPO_ACCESS"
    assert body["retry"] is False and body["namespace"] == "code-loom"
    assert "clickhouse/code-loom" in body["error"] and "do not retry" in body["error"]
    assert "claim-*.txt" in body["error"] and "loom mint-token --save" in body["error"]


def test_a_person_is_told_to_request_the_grant() -> None:
    body = ar.refusal("avery@example.com", "clickhouse", "code-loom")
    assert "/me → Discover" in body["error"] and "claim" not in body["error"]


def test_role_check_refusal_on_a_code_op_is_actionable() -> None:
    auth = SimpleNamespace(subject="sa:agent-x", org="clickhouse")
    out = mcp_call._forbidden_result(
        OPERATIONS_BY_NAME["code.search"],
        PermissionError("cannot access"),
        auth,
        "clickhouse",
        "code-loom",
    )
    sc = out["structuredContent"]
    assert out["isError"] and (sc["code"], sc["reason"]) == ("AUTH_INSUFFICIENT", "NO_REPO_ACCESS")
    mem = mcp_call._forbidden_result(
        OPERATIONS_BY_NAME["memory.get"], PermissionError("nope"), auth, "clickhouse", "jdoe"
    )
    assert mem["structuredContent"]["code"] == "AUTH_INSUFFICIENT"
    assert "reason" not in mem["structuredContent"]


def test_not_accessible_on_a_code_tool_is_actionable_memory_unchanged() -> None:
    auth = SimpleNamespace(subject="avery@example.com", org="clickhouse")
    code = mcp_auth._not_accessible_result("loom_code_grep", auth, "code-loom")
    assert code["structuredContent"]["reason"] == "NO_REPO_ACCESS"
    assert code["structuredContent"]["code"] == "AUTH_INSUFFICIENT"
    mem = mcp_auth._not_accessible_result("loom_memory_get", auth, "other")
    assert mem["structuredContent"]["code"] == "NAMESPACE_NOT_ACCESSIBLE"
