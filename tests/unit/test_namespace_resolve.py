"""A code/tracker tool's `namespace` that names no namespace.

`namespace: "clickhouse"` on loom_tracker_duplicate_check must say the
namespace does not exist, not answer the self-signup claim text for an
index that does not exist (which sends the model off to `gh`).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.transport import namespace_resolve as nr

KNOWN = {
    "code-clickhouse": "ClickHouse/ClickHouse",
    "code-clickhouse-26-8": "ClickHouse/ClickHouse",
    "code-example-private": "ClickHouse/example-private",
    "code-loom": "ClickHouse/loom",
    "qa-clickhouse": "ClickHouse/ClickHouse",
}


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("clickhouse", "code-clickhouse"),
        ("ClickHouse/ClickHouse", "code-clickhouse"),
        ("clickhouse/clickhouse", "code-clickhouse"),
        ("loom", "code-loom"),
        ("code-loom", "code-loom"),
        ("example-private", "code-example-private"),
        ("nonsense", ""),
    ],
)
def test_a_repository_name_resolves_to_its_code_namespace(name: str, want: str) -> None:
    got = nr.match(
        name, KNOWN, default_branch={"code-clickhouse", "code-loom", "code-example-private"}
    )
    assert got == want


def test_the_default_branch_wins_over_a_release_branch() -> None:
    known = {
        "code-clickhouse-26-8": "ClickHouse/ClickHouse",
        "code-clickhouse": "ClickHouse/ClickHouse",
    }
    assert nr.match("clickhouse", known, default_branch={"code-clickhouse"}) == "code-clickhouse"


def test_suggestions_prefer_substrings_then_close_names() -> None:
    readable = ["code-clickhouse", "qa-clickhouse", "code-loom"]
    assert nr.suggest("clickhous", readable)[:2] == ["code-clickhouse", "qa-clickhouse"]


def test_the_unknown_result_never_carries_the_claim_text() -> None:
    body = nr.unknown_result("clickhouse", ["code-clickhouse"])["structuredContent"]
    assert body["code"] == nr.UNKNOWN_NAMESPACE and body["did_you_mean"] == ["code-clickhouse"]
    assert "claim" not in body["error"] and "self-signup" not in body["error"]


def test_a_restricted_namespace_is_never_suggested_to_an_outsider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repos = [
        SimpleNamespace(
            org="clickhouse",
            namespace="code-clickhouse",
            classification="standard",
            repo="ClickHouse/ClickHouse",
            git_branch="master",
        ),
        SimpleNamespace(
            org="clickhouse",
            namespace="code-secret",
            classification="restricted",
            repo="ClickHouse/secret",
            git_branch="main",
        ),
    ]
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: repos)
    monkeypatch.setattr("server.tracker.domain.namespaces.configured_namespaces", lambda: {})
    got = nr.suggestable("clickhouse", lambda org, ns: False)
    assert got == ["code-clickhouse"]
    assert "code-secret" in nr.suggestable("clickhouse", lambda org, ns: ns == "code-secret")


# ---- the MCP gate ------------------------------------------------------------------


class _Auth:
    org = "clickhouse"
    subject = "sa:agent-x"

    def __init__(self, readable: set[str]) -> None:
        self.readable = readable

    def can_access(self, org: str, ns: str) -> bool:
        return ns in self.readable


@pytest.fixture
def _known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nr, "known_namespaces", lambda org: dict(KNOWN))
    monkeypatch.setattr(nr, "default_branches", lambda org: {"code-clickhouse", "code-loom"})
    monkeypatch.setattr(nr, "suggestable", lambda org, can: ["code-clickhouse", "qa-clickhouse"])

    async def may_read(auth: Any, org: str, ns: str) -> bool:
        return bool(auth.can_access(org, ns))  # the gate, reduced to the claim

    monkeypatch.setattr("server.transport.mcp_auth._may_read", may_read)


def test_the_gate_resolves_a_repository_name(_known: None) -> None:
    from server.transport import mcp_auth

    async def one_request() -> tuple[Any, Any]:
        # the gate and the tool call run in one task: the note reaches the result
        got = await mcp_auth._plane_namespace(
            "loom_tracker_duplicate_check", _Auth({"code-clickhouse"}), "clickhouse"
        )
        return got, nr.RESOLVED.get()

    got, note = asyncio.run(one_request())
    assert got == "code-clickhouse" and note == {"from": "clickhouse", "to": "code-clickhouse"}


def test_the_gate_names_an_unknown_namespace_instead_of_refusing_access(_known: None) -> None:
    from server.transport import mcp_auth

    got = asyncio.run(
        mcp_auth._plane_namespace("loom_tracker_duplicate_check", _Auth(set()), "nosuch")
    )
    assert isinstance(got, dict) and got["structuredContent"]["code"] == nr.UNKNOWN_NAMESPACE


def test_an_existing_namespace_keeps_the_real_refusal(_known: None) -> None:
    from server.transport import mcp_auth

    got = asyncio.run(
        mcp_auth._plane_namespace("loom_code_search", _Auth(set()), "code-example-private")
    )
    assert got is None, "the namespace exists: the caller refuses with the claim text as before"


def test_a_resolution_the_caller_cannot_read_is_refused_on_the_real_namespace(_known: None) -> None:
    from server.transport import mcp_auth

    got = asyncio.run(mcp_auth._plane_namespace("loom_code_search", _Auth(set()), "clickhouse"))
    assert isinstance(got, dict) and got["structuredContent"]["namespace"] == "code-clickhouse"


def test_a_memory_tool_is_untouched(_known: None) -> None:
    from server.transport import mcp_auth

    assert (
        asyncio.run(mcp_auth._plane_namespace("loom_memory_get", _Auth(set()), "clickhouse"))
        is None
    )


def test_a_batch_entry_does_not_inherit_the_previous_entrys_resolution(_known: None) -> None:
    """A JSON-RPC batch runs its entries in one task: the note one call's
    resolution left must not reach the next call's result."""
    from server.transport import mcp_auth

    def call(ns: str) -> dict[str, Any]:
        args = {"namespace": ns, "query": "q"}
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "loom_tracker_duplicate_check", "arguments": args},
        }

    async def batch() -> list[Any]:
        auth = _Auth({"code-clickhouse", "code-loom"})
        notes = []
        for req in (call("clickhouse"), call("code-loom")):
            assert await mcp_auth._screen_tool_call(req, auth) is None
            notes.append(nr.RESOLVED.get())
        return notes

    first, second = asyncio.run(batch())
    assert first == {"from": "clickhouse", "to": "code-clickhouse"}
    assert second is None
