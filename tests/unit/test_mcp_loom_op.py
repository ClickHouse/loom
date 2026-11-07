"""`loom_op` reaches an op the roster leaves unlisted.

Claude Code's MCP client calls only tools it was listed, so ops the docs call
"callable by name" (tracker.flaky, task.pull, …) need a listed way in.
`loom_op {op, arguments}` is rewritten
into the target's own `tools/call` before authorization, so it runs the same
path a listed tool runs."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import server.transport.mcp as mcp
import server.transport.mcp_call as mcp_call
from server.auth.jwt import mint_token
from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.settings import settings
from server.transport import mcp_op
from server.transport.mcp_tools import _tools_descriptor


def _call(op: str, arguments: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"op": op, **extra}
    if arguments is not None:
        args["arguments"] = arguments
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": args},
    }


def _client(monkeypatch: pytest.MonkeyPatch, *, authed: bool) -> TestClient:
    if authed:
        monkeypatch.setattr(settings, "auth_required", True)
        monkeypatch.setattr(settings, "identity_provider", "")
        monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    else:
        monkeypatch.setattr(settings, "identity_provider", "single-user")
    app = FastAPI()
    mcp.attach(app)
    return TestClient(app)


@pytest.mark.parametrize("toolset", ["core", "coding", "tracker", "tasks", "atomic", "readonly"])
def test_every_roster_lists_loom_op(toolset: str) -> None:
    import asyncio

    names = {t["name"] for t in asyncio.run(_tools_descriptor(toolset=toolset))}
    assert "loom_op" in names, toolset


def test_help_lists_matching_ops_name_matches_first_and_never_a_hidden_one() -> None:
    cat = mcp_op.catalog("task")
    assert cat["ops"][0]["op"].startswith("task.")
    assert mcp_op.catalog("tracker flaky")["ops"][0]["op"] == "tracker.flaky"
    # several ops in one ask: any word matches, best-covered names first
    first = [r["op"] for r in mcp_op.catalog("tracker similar flaky test_signal")["ops"][:3]]
    assert set(first) == {"tracker.similar", "tracker.flaky", "tracker.test_signal"}
    listed = {r["op"] for r in mcp_op.catalog("", limit=1000)["ops"]}
    assert listed and not (listed & _MCP_HIDDEN_OPS)
    flaky = next(r for r in mcp_op.catalog("tracker.flaky")["ops"])
    assert flaky["required"] == ["test_name"] and flaky["purpose"]


def test_loom_op_runs_the_target_through_the_listed_tool_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    tool = "loom_tracker_flaky"
    op = mcp_call.TOOLS_BY_NAME[tool]

    async def _handler(inp: Any) -> Any:
        seen.append(inp)
        return op.output_model(test_name=inp.test_name, verdict="no_flaky_signal")

    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, tool, dataclasses.replace(op, handler=_handler))

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    r = _client(monkeypatch, authed=False).post(
        "/mcp", json=_call("tracker.flaky", {"test_name": "02346_text_index"})
    )
    body = r.json()["result"]
    assert not body.get("isError"), body
    assert body["structuredContent"]["verdict"] == "no_flaky_signal"
    # identity defaults were filled exactly as for a listed call
    assert seen and seen[0].test_name == "02346_text_index" and seen[0].namespace == "n"


def test_a_hidden_op_is_refused_as_a_direct_call_is(monkeypatch: pytest.MonkeyPatch) -> None:
    hidden = sorted(_MCP_HIDDEN_OPS)[0]
    body = _client(monkeypatch, authed=False).post("/mcp", json=_call(hidden)).json()["result"]
    # still refused, with why and what to call instead
    assert body["isError"] and body["structuredContent"]["code"] == "OP_NOT_ON_MCP"
    direct = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": f"loom_{hidden.replace('.', '_')}", "arguments": {}},
    }
    body = _client(monkeypatch, authed=False).post("/mcp", json=direct).json()["result"]
    assert body["isError"] and "agents cannot call it over MCP" in body["content"][0]["text"]


def test_an_unknown_op_names_the_nearest_ones() -> None:
    _, early = mcp_op.route(_call("tracker.flakey"), toolset="coding", auth=None)
    assert early is not None
    sc = early["result"]["structuredContent"]
    assert sc["code"] == "UNKNOWN_OP" and "tracker.flaky" in sc["did_you_mean"]


def test_a_readonly_connection_routes_reads_only() -> None:
    _, early = mcp_op.route(_call("task.create", {"title": "x"}), toolset="readonly", auth=None)
    assert early is not None and "read-only" in early["result"]["structuredContent"]["error"]
    rewritten, early = mcp_op.route(
        _call("tracker.flaky", {"test_name": "t"}), toolset="readonly", auth=None
    )
    assert early is None and rewritten["params"]["name"] == "loom_tracker_flaky"


def test_the_namespace_check_sees_the_inner_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch-level namespace gate reads the TARGET's arguments: a
    namespace the token cannot reach is refused before any handler runs."""
    from server.auth import authz

    async def _no_grant(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    tok = mint_token(
        subject="lo-agent@dev",
        org="dev",
        namespaces=["alpha"],
        roles=["writer"],
        ttl_seconds=300,
        is_service_account=True,
    )
    r = _client(monkeypatch, authed=True).post(
        "/mcp",
        json=_call("tracker.flaky", {"test_name": "t", "namespace": "beta"}),
        headers={"Authorization": f"Bearer {tok}"},
    )
    sc = r.json()["result"]["structuredContent"]
    # "beta" names no namespace at all — said so, never the claim text
    assert sc["code"] == "UNKNOWN_NAMESPACE" and sc["namespace"] == "beta"
    from server.transport import namespace_resolve as nr

    monkeypatch.setattr(nr, "known_namespaces", lambda org: {"beta": "acme/beta"})
    r = _client(monkeypatch, authed=True).post(
        "/mcp",
        json=_call("tracker.flaky", {"test_name": "t", "namespace": "beta"}),
        headers={"Authorization": f"Bearer {tok}"},
    )
    sc = r.json()["result"]["structuredContent"]
    # a refusal on a namespace that exists is the role refusal, NO_REPO_ACCESS its reason
    assert sc["code"] == "AUTH_INSUFFICIENT" and sc["reason"] == "NO_REPO_ACCESS"
    assert "beta" in json.dumps(sc)


def test_the_description_stays_small() -> None:
    import json

    assert len(json.dumps(mcp_op.DESCRIPTOR)) // 4 <= 220
