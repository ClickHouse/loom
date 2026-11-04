"""A code/tracker refusal says who, which index, how to get
in, and that retrying on another connection will not help."""

from __future__ import annotations

from types import SimpleNamespace

import server.transport.mcp_auth as mcp_auth
import server.transport.mcp_call as mcp_call
from server.auth.jwt import AuthInfo
from server.operations.base import Role
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
    assert "dashboard's Spaces page" in body["error"] and "claim" not in body["error"]


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


def test_a_refused_collection_points_at_collection_list_and_a_namespace_does_not() -> None:
    auth = AuthInfo(
        subject="avery@example.com",
        org="clickhouse",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    col = mcp_auth._not_accessible_result("loom_memory_get", auth, "col-research")
    assert "collection.list" in col["structuredContent"]["error"]
    assert "Spaces" not in col["structuredContent"]["error"], "a collection is never joined"
    assert col["content"][0]["text"] == col["structuredContent"]["error"]
    other = mcp_auth._not_accessible_result("loom_memory_get", auth, "other")
    assert "collection.list" not in other["structuredContent"]["error"]


def test_an_assistant_connection_is_pointed_at_no_tool_it_cannot_call() -> None:
    auth = AuthInfo(
        subject="avery@example.com",
        org="clickhouse",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    col = mcp_auth._not_accessible_result(
        "loom_knowledge_search", auth, "col-research", assistant=True
    )
    text = col["structuredContent"]["error"]
    assert "admin" in text
    assert "loom_op" not in text and "loom_search" not in text
    plain = mcp_auth._not_accessible_result("loom_knowledge_search", auth, "other", assistant=True)
    assert "Spaces page" in plain["structuredContent"]["error"]
    assert "loom_search" not in plain["structuredContent"]["error"]


def test_someone_elses_personal_collection_gets_no_advice() -> None:
    auth = AuthInfo(
        subject="avery@example.com",
        org="clickhouse",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    personal = mcp_auth._not_accessible_result("loom_memory_get", auth, "col-me-0123456789abcdef")
    text = personal["structuredContent"]["error"]
    # A personal collection takes no grant, so the refusal names none.
    assert "grant" not in text and "Spaces" not in text and "collection.list" not in text


def test_a_scoped_token_refused_a_collection_is_told_its_claim_must_name_it() -> None:
    def scoped(ns_roles: tuple[tuple[str, str], ...]) -> AuthInfo:
        return AuthInfo(
            subject="avery@example.com",
            org="clickhouse",
            namespaces=("n",),
            roles=(Role.WRITER,),
            is_service_account=False,
            ns_roles=ns_roles,
        )

    # Both needs, read from the token alone: a claim that names it, and the grant a new
    # token is capped to.
    capped = mcp_auth._not_accessible_result(
        "loom_memory_get", scoped((("n", "writer"),)), "col-research"
    )
    text = capped["structuredContent"]["error"]
    assert text == (
        "namespace 'col-research' is not in your accessible list: this token's claim must name"
        " it, and the token's owner must hold a grant of at least viewer on it or a collection"
        " above it."
    )
    # Its claim already names the collection: the grant is what is missing.
    named = mcp_auth._not_accessible_result(
        "loom_memory_get", scoped((("col-research", "viewer"),)), "col-research"
    )
    assert "claim must" not in named["structuredContent"]["error"]
    assert "grant on it" in named["structuredContent"]["error"]


def test_a_delegated_token_refused_a_collection_is_told_its_creator_needs_a_grant() -> None:
    bot = AuthInfo(
        subject="sa:bot",
        org="clickhouse",
        namespaces=("col-research",),
        roles=(Role.WRITER,),
        is_service_account=True,
        creator="alice",
    )
    text = mcp_auth._not_accessible_result("loom_memory_get", bot, "col-research")[
        "structuredContent"
    ]["error"]
    assert "This token's creator must hold a grant on it too." in text
    assert "must mint" not in text


def test_an_unverified_collection_refusal_asks_for_no_grant() -> None:
    """The check itself failed: the agent may hold a grant, so it is not sent to ask."""
    auth = AuthInfo(
        subject="avery@example.com",
        org="clickhouse",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    text = mcp_auth._not_accessible_result(
        "loom_memory_get", auth, "col-research", unverified=True
    )["structuredContent"]["error"]
    assert text == "namespace 'col-research' is not in your accessible list."


def test_a_claim_capped_delegated_token_is_told_its_creator_needs_a_grant_too() -> None:
    """The MCP screen says what the gate says: the claim, the grant and the creator's."""
    bot = AuthInfo(
        subject="sa:bot",
        org="clickhouse",
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
        creator="alice",
    )
    text = mcp_auth._not_accessible_result("loom_memory_get", bot, "col-research")[
        "structuredContent"
    ]["error"]
    assert "this token's claim must name it" in text
    assert text.endswith("This token's creator must hold a grant on it too.")
