"""Opt-in model code tools keep application scope and whole evidence under a budget."""

from __future__ import annotations

import json

import httpx
import pytest
from loom import Agent
from loom.errors import LoomError
from loom.tools import tool_definitions


def harness(data: dict, *, status: int = 200) -> tuple[Agent, list]:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=data, headers={"X-Request-Id": "code-result-123"})

    agent = Agent("acme", "memory-home", "coder", api_key="secret", mentor_brief=False)
    agent.transport._client = httpx.AsyncClient(
        base_url="http://example.invalid", transport=httpx.MockTransport(handle)
    )
    return agent, calls


async def test_code_tool_is_opt_in_and_formats_without_changing_default_memory_tools():
    agent, calls = harness({})
    assert len(agent.tools().definitions()) == len(tool_definitions()) == 6
    with pytest.raises(LoomError, match="requires code_namespace"):
        agent.tools(only=["loom_code_lookup"])
    enabled = agent.tools(code_namespace="code-repo", only=["loom_code_lookup"])
    for fmt, schema_key in (("generic", "parameters"), ("anthropic", "input_schema")):
        (spec,) = enabled.definitions(fmt)
        assert spec["name"] == "loom_code_lookup"
        assert spec[schema_key]["required"] == ["kind", "target"]
        assert spec[schema_key]["additionalProperties"] is False
    (spec,) = enabled.definitions("openai")
    assert spec["function"]["name"] == "loom_code_lookup"
    assert len(agent.tools(code_namespace="code-repo").definitions()) == 7
    assert agent.tools(code_namespace="code-repo").definitions()[0]["name"] == "loom_code_lookup"
    for options in (
        {"code_namespace": " "},
        {"code_namespace": "code-repo", "max_result_chars": 255},
    ):
        with pytest.raises(LoomError):
            agent.tools(**options)
    assert calls == []
    await agent.close()


@pytest.mark.parametrize("target", ["Foo::~Foo", "Foo::operator()", "src/Foo.cpp:123"])
async def test_code_callers_accepts_cpp_names_and_path_line_at_three_hops(target):
    agent, calls = harness({"edges": [], "epistemic": "lower_bound"})
    await agent.tools(code_namespace="code-repo").execute(
        "loom_code_lookup", {"kind": "callers", "target": target, "depth": 3}
    )
    assert calls[0][1]["name"] == target and calls[0][1]["depth"] == 3
    await agent.close()


async def test_code_lookup_preserves_scope_on_authorization_refusal_without_fallback():
    agent, calls = harness({"detail": {"code": "FORBIDDEN", "message": "denied"}}, status=403)
    with pytest.raises(LoomError):
        await agent.tools(code_namespace="code-repo").execute(
            "loom_code_lookup", {"kind": "symbol", "target": "pkg.fn"}
        )
    assert len(calls) == 1 and calls[0][1]["namespace"] == "code-repo"
    assert agent.namespace == "memory-home"
    await agent.close()


@pytest.mark.parametrize(
    ("kind", "target", "depth", "fields"),
    [
        (
            "symbol",
            "src.flask.blueprints::Blueprint::__init__",
            None,
            {"name": "src.flask.blueprints::Blueprint::__init__", "token_budget": 2500},
        ),
        (
            "search",
            "How are nested blueprints registered?",
            None,
            {"query": "How are nested blueprints registered?", "top_k": 3, "token_budget": 2500},
        ),
        (
            "callers",
            "DB::Context::getSettingsRef",
            2,
            {"name": "DB::Context::getSettingsRef", "depth": 2, "max_nodes": 20},
        ),
        (
            "impact",
            "std::vector<int>::push_back",
            2,
            {"name": "std::vector<int>::push_back", "depth": 2, "per_hop_limit": 20},
        ),
    ],
)
async def test_code_lookup_uses_real_metadata_transport_and_fixed_scope(
    kind, target, depth, fields
):
    data = {
        "ambiguous": True,
        "candidates": ["pkg.Other"],
        "epistemic": "lower_bound",
        "causes": ["treesitter_only_files"],
        "as_of": {"git_head": "abc123"},
    }
    agent, calls = harness(data)
    tools = agent.tools(code_namespace="code-repo", user_id="u", session_id="s")
    args = {"kind": kind, "target": target, **({"depth": depth} if depth else {})}
    result = json.loads(await tools.execute("loom_code_lookup", args))
    assert result == {
        "op": "code." + kind,
        "request_id": "code-result-123",
        "scope": {"org": "acme", "namespace": "code-repo"},
        "data": data,
    }
    path, sent = calls[0]
    assert path == "/v1/code." + kind
    assert sent.items() >= fields.items()
    assert (sent["org"], sent["namespace"], sent["agent"], sent["user_id"]) == (
        "acme",
        "code-repo",
        "coder",
        "u",
    )
    assert sent["include_lessons"] is False
    if kind == "symbol":
        assert (
            sent["include_history"]
            is sent["include_examples"]
            is sent["uses"]
            is sent["uses_brief"]
            is False
        )
    assert agent.namespace == "memory-home"
    await tools.execute("loom_note", {"addition": "keep this in memory"})
    assert calls[-1][1]["namespace"] == "memory-home"
    await agent.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"org": "other"},
        {"namespace": "other"},
        {"op": "memory.set"},
        {"token_budget": 9999},
        {"lesson_namespaces": ["private"]},
    ],
)
async def test_code_lookup_rejects_model_scope_and_operation_injection_before_network(extra):
    agent, calls = harness({})
    with pytest.raises(LoomError, match="unexpected"):
        await agent.tools(code_namespace="code-repo").execute(
            "loom_code_lookup", {"kind": "symbol", "target": "pkg.fn", **extra}
        )
    assert calls == []
    await agent.close()


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "callers", "target": "who calls this function"},
        {"kind": "symbol", "target": "where is this function"},
        {"kind": "impact", "target": "what might break"},
        {"kind": "impact", "target": "pkg.fn", "depth": 3},
        {"kind": "symbol", "target": "pkg.fn", "depth": 2},
        {"kind": "callers", "target": "pkg.fn", "depth": 4},
    ],
)
async def test_code_lookup_rejects_invalid_reference_or_depth_before_network(args):
    agent, calls = harness({})
    with pytest.raises(LoomError):
        await agent.tools(code_namespace="code-repo").execute("loom_code_lookup", args)
    assert calls == []
    await agent.close()


async def test_code_result_budget_preserves_whole_evidence_and_discloses_client_elision():
    unit = {
        "path": "src/code.py",
        "start_line": 5,
        "end_line": 90,
        "body": "def work():\n" + "    pass\n" * 300,
        "elided": False,
        "tier": "treesitter",
    }
    data = {
        "definitions": [unit],
        "ambiguous": False,
        "as_of": {"git_head": "abc123"},
        "epistemic": "lower_bound",
        "causes": ["treesitter_only_files"],
    }
    agent, calls = harness(data)
    tools = agent.tools(code_namespace="code-repo", max_result_chars=900)
    text = await tools.execute("loom_code_lookup", {"kind": "symbol", "target": "pkg.fn"})
    result = json.loads(text)
    assert len(text) <= 900
    assert result["data"]["definitions"][0] == {**unit, "body": "", "elided": True}
    assert result["data"]["causes"] == data["causes"]
    assert result["data"]["as_of"] == data["as_of"]
    assert result["request_id"] == "code-result-123"
    assert result["client_truncation"]["omitted_bodies"] == 1
    assert result["client_truncation"]["insufficient"] is True
    full = json.loads(
        await agent.tools(code_namespace="code-repo", max_result_chars=12000).execute(
            "loom_code_lookup", {"kind": "symbol", "target": "pkg.fn"}
        )
    )
    assert full["data"] == data and "client_truncation" not in full
    assert len(calls) == 2
    await agent.close()


async def test_code_result_budget_drops_whole_rows_or_explicitly_refuses_metadata():
    edge = {
        "path": "src/code.py",
        "line": 9,
        "tier": "treesitter",
        "confidence": 0.8,
        "from_symbol": "p" * 180,
    }
    data = {
        "edges": [edge] * 20,
        "counts_floor": True,
        "epistemic": "lower_bound",
        "causes": ["truncated"],
        "truncated": True,
    }
    agent, _ = harness(data)
    tools = agent.tools(code_namespace="code-repo", max_result_chars=900)
    text = await tools.execute("loom_code_lookup", {"kind": "callers", "target": "pkg.fn"})
    result = json.loads(text)
    assert len(text) <= 900
    assert result["data"]["counts_floor"] is True
    assert all(row == edge for row in result["data"]["edges"])
    assert len(result["data"]["edges"]) + result["client_truncation"]["omitted_rows"]["edges"] == 20
    await agent.close()
    agent, _ = harness({"note": "long uncertainty " * 1000})
    text = await agent.tools(code_namespace="code-repo", max_result_chars=256).execute(
        "loom_code_lookup", {"kind": "symbol", "target": "pkg.fn"}
    )
    result = json.loads(text)
    assert len(text) <= 256 and result["client_truncation"]["insufficient"] is True
    assert result["client_truncation"]["metadata_omitted"] is True
    assert "data" not in result
    await agent.close()
