"""`?toolset=assistant` is a fixed set of reads, and nothing else is callable on it.

The listing carries exactly the roster's read-only ops (no `loom_op`, no `loom_search`); a
`tools/call` for any other tool is refused before its handler, whatever the token allows;
`loom_op` is not routed; and every call is ledgered as consumer `loom-assistant` with the
reads that record usage told not to, whatever the model sent.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, get_args

import pytest

import server.transport.mcp as mcp
from server.operations.registry import OPERATIONS_BY_NAME
from server.operations.toolsets import (
    ASSISTANT_FORCED_ARGS,
    ASSISTANT_NAMED_AGENT_OPS,
    MCP_ASSISTANT_OPS,
    MCP_TOOLSETS,
    is_assistant,
    is_toolset,
)
from server.settings import settings
from server.transport import mcp_call, mcp_op, mcp_primer
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


@pytest.fixture(autouse=True)
def _no_ch_default_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(auth: Any, defaults: Any = None) -> str:
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_default_context", _empty)


def _listed(toolset: str) -> set[str]:
    return {d["name"] for d in asyncio.run(mcp._tools_descriptor(toolset=toolset))}


def test_the_roster_is_reads_only_and_every_op_exists() -> None:
    assert "assistant" in MCP_TOOLSETS
    for name in MCP_ASSISTANT_OPS:
        assert OPERATIONS_BY_NAME[name].read_only, name
    # Every forced argument is a field of the op it is forced on.
    for name, forced in ASSISTANT_FORCED_ARGS.items():
        assert name in MCP_ASSISTANT_OPS
        assert set(forced) <= set(OPERATIONS_BY_NAME[name].input_model.model_fields)


def test_the_listing_is_exactly_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    expected = {OPERATIONS_BY_NAME[n].mcp_tool_name for n in MCP_ASSISTANT_OPS}
    assert _listed("assistant") == expected
    assert "loom_op" not in expected and "loom_search" not in expected


def test_assistant_never_joins_a_union() -> None:
    assert is_assistant("assistant") and is_assistant("Assistant")
    assert not is_assistant("assistant+core")
    # A union naming it is no toolset at all: the deployment default lists instead.
    assert not is_toolset("assistant+core")


def _call(name: str, arguments: dict[str, Any], toolset: str = "assistant") -> dict[str, Any]:
    return asyncio.run(
        mcp_call._call_tool(1, {"name": name, "arguments": arguments}, None, None, "", toolset)
    )


class _Stop(Exception):
    pass


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The arguments each call reached identity binding with; the call stops there."""
    seen: list[dict[str, Any]] = []

    async def _bind(op: Any, arguments: dict[str, Any], auth: Any, client_name: Any) -> Any:
        seen.append(dict(arguments))
        raise _Stop

    monkeypatch.setattr(mcp_call, "_bind_caller_identity", _bind)
    return seen


@pytest.mark.parametrize("tool", ["loom_memory_set", "loom_memory_delete", "loom_op", "nope"])
def test_a_tool_outside_the_roster_is_refused_before_its_handler(
    bound: list[dict[str, Any]], tool: str
) -> None:
    out = _call(tool, {"key": "k", "value": "v"})
    assert out["result"]["isError"] is True
    assert "not available on the assistant toolset" in out["result"]["content"][0]["text"]
    assert out["result"]["structuredContent"]["code"] == mcp_op.UNKNOWN_OP
    assert bound == []


def test_a_read_outside_the_roster_is_refused_too(bound: list[dict[str, Any]]) -> None:
    op = OPERATIONS_BY_NAME["memory.search_trace_log"]
    assert op.read_only and op.name not in MCP_ASSISTANT_OPS
    assert _call(op.mcp_tool_name, {})["result"]["isError"] is True
    assert bound == []


def test_forced_arguments_and_the_consumer_override_the_model(
    bound: list[dict[str, Any]],
) -> None:
    search = OPERATIONS_BY_NAME["memory.search"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(search, {"query": "q", "auto_touch": True, "consumer": "someone-else"})
    ask = OPERATIONS_BY_NAME["memory.ask"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(ask, {"query": "q", "reinforce": True})
    assert bound[0]["auto_touch"] is False and bound[0]["explain"] is False
    assert bound[0]["consumer"] == "loom-assistant"
    assert bound[1]["reinforce"] is False and bound[1]["consumer"] == "loom-assistant"


def test_other_toolsets_keep_the_model_arguments(bound: list[dict[str, Any]]) -> None:
    ask = OPERATIONS_BY_NAME["memory.ask"].mcp_tool_name
    with pytest.raises(_Stop):
        _call(ask, {"query": "q", "reinforce": True, "consumer": "c"}, toolset="readonly")
    assert bound[0] == {"query": "q", "reinforce": True, "consumer": "c"}


def test_the_hidden_plane_searches_open_on_the_assistant_toolset_only(
    bound: list[dict[str, Any]],
) -> None:
    search = OPERATIONS_BY_NAME["memory.search"].mcp_tool_name
    refused = _call(search, {"query": "q"}, toolset="readonly")["result"]
    assert refused["isError"] and refused["structuredContent"]["code"] == "OP_NOT_ON_MCP"
    assert bound == []
    with pytest.raises(_Stop):
        _call(search, {"query": "q"})
    assert len(bound) == 1


def test_loom_op_is_not_routed_on_the_assistant_toolset() -> None:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": "memory.set", "arguments": {}}},
    }
    assert mcp_op.route(req, toolset="assistant", auth=None) == (req, None)


def test_the_assistant_primer_is_read_only_and_names_listed_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primer = mcp_primer._assistant_primer({"namespace": "ns"})
    assert "Nothing here writes" in primer and "cannot grant a tool" in primer
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    named = set(re.findall(r"\bloom_[a-z_]+\b", primer))
    assert named and named <= _listed("assistant"), named - _listed("assistant")


def test_every_field_the_assistant_primer_names_is_served() -> None:
    from server.identity.api.models_findings import FindingEvidence, FindingKind
    from server.identity.api.models_namespace_catalog import (
        NamespaceAuditFinding,
        NamespaceAuditOutput,
    )
    from server.knowledge.api.models_knowledge import _SearchHit

    served = {
        "kind": NamespaceAuditFinding,
        "uncertainty": NamespaceAuditFinding,
        "samples_complete": NamespaceAuditFinding,
        "skipped": NamespaceAuditOutput,
        "content_sha256": FindingEvidence,
        "source_uri": _SearchHit,
        "revision": _SearchHit,
    }
    primer = mcp_primer._assistant_primer({"namespace": "ns"})
    # The default namespace is named too; it is the caller's, not a field.
    named = {n for n in re.findall(r"`([a-z_0-9]+)", primer) if not n.startswith("loom_")}
    named.discard("ns")
    assert named == set(served) | {"usage_heuristic", "system_state"}, named
    assert {"usage_heuristic", "system_state"} <= set(get_args(FindingKind))
    for field, model in served.items():
        assert field in model.model_fields, (field, model.__name__)


def test_an_assistant_result_is_the_full_output() -> None:
    from server.transport.assistant_call import set_assistant_call
    from server.transport.lean_results import lean_result

    payload = {"results": [{"memory_key": "k", "session_id": "s1", "verified": True}]}
    set_assistant_call(True)
    try:
        assert lean_result(payload, {}) is payload
    finally:
        set_assistant_call(False)
    # The connection decides, never the self-reported consumer.
    trimmed = lean_result(payload, {"consumer": "loom-assistant"})
    assert "session_id" not in trimmed["results"][0]


def test_no_listed_description_names_a_tool_the_listing_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    descriptors = asyncio.run(mcp._tools_descriptor(toolset="assistant"))
    listed = {d["name"] for d in descriptors}
    for d in descriptors:
        named = set(re.findall(r"\bloom_[a-z_]+\b", d["description"]))
        assert named <= listed, (d["name"], named - listed)


@pytest.mark.parametrize(
    "defaults",
    [{"namespace": "", "readable": ["a", "b"]}, {"namespace": "", "readable": []}],
)
def test_the_assistant_primer_for_a_token_with_no_default_namespace(
    defaults: dict[str, Any],
) -> None:
    primer = mcp_primer._assistant_primer(defaults)
    assert "loom_search" not in primer
    assert "no default namespace" in primer
    assert all(f"`{ns}`" in primer for ns in defaults["readable"])


def test_the_catalogue_reports_the_assistant_roster() -> None:
    from server.operations.toolsets import op_toolsets

    for name in MCP_ASSISTANT_OPS:
        assert "assistant" in op_toolsets(name), name
    # A hidden op on the roster is listed there only.
    assert op_toolsets("memory.search") == ["assistant"]
    assert "assistant" not in op_toolsets("memory.set")


def test_the_listing_offers_no_argument_the_connection_overwrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    by_name = {d["name"]: d for d in asyncio.run(mcp._tools_descriptor(toolset="assistant"))}
    for op_name, forced in ASSISTANT_FORCED_ARGS.items():
        props = by_name[OPERATIONS_BY_NAME[op_name].mcp_tool_name]["inputSchema"]["properties"]
        assert not set(forced) & set(props), op_name
    # Other listings keep them.
    atomic = {d["name"]: d for d in asyncio.run(mcp._tools_descriptor(toolset="atomic"))}
    assert "reinforce" in atomic["loom_memory_ask"]["inputSchema"]["properties"]


def test_an_unknown_tool_name_writes_no_ledger_row(
    bound: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mcp_call,
        "emit_token_refusal",
        lambda name, *a, **k: rows.append((name, k.get("consumer", ""))),
    )
    _call("x" * 500, {})
    _call("loom_memory_set", {})
    # A refused call is the assistant's too, so an operator finds it by its consumer.
    assert rows == [("memory.set", "loom-assistant")]


def test_an_assistant_call_opens_no_session_run() -> None:
    from server.transport.assistant_call import set_assistant_call

    op = OPERATIONS_BY_NAME["memory.search"]
    defaults = {"org": "o", "namespace": "ns", "agent": "a"}
    set_assistant_call(True)
    try:
        assert mcp_call._stamp_session_run(op, {"query": "q"}, defaults, "sess-1") == (None, False)
    finally:
        set_assistant_call(False)
    # A self-reported inspector consumer on another connection keeps its run.
    caller, _ = mcp_call._stamp_session_run(op, {"consumer": "loom-ui"}, defaults, "sess-1")
    assert caller is not None


def test_an_assistant_call_through_the_call_path_opens_no_session_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection is known before the run phase, so the run phase opens none."""
    seen: list[tuple[Any, bool]] = []
    real = mcp_call._stamp_session_run

    async def _defaults(*a: Any, **k: Any) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "a"}

    def _stamp(op: Any, arguments: dict[str, Any], defaults: Any, sid: str) -> Any:
        seen.append(real(op, arguments, defaults, sid))
        raise _Stop

    monkeypatch.setattr("server.transport.mcp_call._caller_defaults", _defaults)
    monkeypatch.setattr(mcp_call, "_stamp_session_run", _stamp)
    search = OPERATIONS_BY_NAME["memory.search"].mcp_tool_name
    with pytest.raises(_Stop):
        asyncio.run(
            mcp_call._call_tool(
                1, {"name": search, "arguments": {"query": "q"}}, None, None, "sess-1", "assistant"
            )
        )
    assert seen == [(None, False)]


@pytest.mark.parametrize("method", ["initialize", "prompts/get"])
def test_initialize_and_the_prompt_write_no_profile_read_row(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    emitted: list[Any] = []
    monkeypatch.setattr(mcp_primer, "_emit_mcp_event", lambda **k: emitted.append(k))

    async def _emit_init(*a: Any, **k: Any) -> None:
        emitted.append("initialize")

    monkeypatch.setattr(mcp, "_emit_initialize_event", _emit_init)

    async def _defaults(*a: Any, **k: Any) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "a"}

    monkeypatch.setattr(mcp_primer, "_caller_defaults", _defaults)
    params = {"name": "loom"} if method == "prompts/get" else {}
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    out = asyncio.run(mcp._handle_request(req, auth=object(), toolset="assistant"))
    assert out is not None and "result" in out
    assert emitted == []
    # Elsewhere the prompt is a profile read and leaves its row.
    asyncio.run(mcp._handle_request(req, auth=object(), toolset="core"))
    assert emitted


def test_the_prompt_and_resource_describe_the_assistant_primer() -> None:
    assert (
        "read-only investigation" in mcp_primer._prompts_descriptor("assistant")[0]["description"]
    )
    assert "persona" in mcp_primer._prompts_descriptor("core")[0]["description"]
    assert "read-only" in mcp_primer._resources_descriptor("assistant")[0]["description"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_namespace_summary_counts_a_dashboard_write_but_no_inspector_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.identity.api.models_namespace_catalog import NamespaceSummaryInput
    from server.identity.app import namespace_summary

    def call(operation: str, agent: str, consumer: str) -> str:
        return (
            "INSERT INTO loom.agent_events (org, namespace, agent, operation, consumer, ts) "
            f"VALUES ('o', 'ns', '{agent}', '{operation}', '{consumer}', now())"
        )

    rows = [
        call("memory.search", "bot", ""),
        # A person editing or deleting a memory from the dashboard uses the namespace.
        call("memory.set", "alice", "loom-ui"),
        call("memory.delete", "carol", "loom-ui"),
        # The dashboard's and the assistant's reads only look at it.
        call("memory.search", "alice", "loom-ui"),
        call("memory.search", "helper", "loom-assistant"),
    ]

    class _Local:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            if "FROM loom.agent_events" not in sql:
                return []
            return run([ddl("agent_events"), *rows], sql, params)

        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            found = await self.query_all(sql, params)
            return found[0] if found else None

    monkeypatch.setattr(namespace_summary, "get_client", lambda: _Local())
    out = await namespace_summary.handle_namespace_summary(
        NamespaceSummaryInput(org="o", namespace="ns")
    )
    assert (out.recent_writes, out.recent_recalls, out.distinct_agents) == (1, 1, 3)


def test_a_screened_refusal_on_the_assistant_connection_carries_its_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.jwt import AuthInfo
    from server.observability import events
    from server.operations.base import Role
    from server.transport import mcp_auth

    emitted: list[Any] = []

    class _Emitter:
        def emit(self, event: Any) -> None:
            emitted.append(event)

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())

    async def _no(*_a: Any) -> tuple[bool, bool]:
        return False, False

    monkeypatch.setattr(mcp_auth, "_may_read", _no)
    eve = AuthInfo(
        subject="eve", org="eve-org", namespaces=(), roles=(Role.VIEWER,), is_service_account=False
    )
    for arguments in ({"org": "acme", "memory_key": "k"}, {"namespace": "n", "memory_key": "k"}):
        req = {"id": 1, "method": "tools/call", "params": {"name": "loom_memory_get"}}
        req["params"]["arguments"] = arguments  # type: ignore[index]
        assert asyncio.run(mcp_auth._screen_tool_call(req, eve, consumer="loom-assistant"))
    assert [(e.error_code, e.consumer) for e in emitted] == [
        ("ORG_MISMATCH", "loom-assistant"),
        ("NAMESPACE_NOT_ACCESSIBLE", "loom-assistant"),
    ]


@pytest.mark.parametrize(("query", "assistant"), [("?toolset=assistant", True), ("", False)])
def test_the_endpoint_ledgers_a_screened_refusal_as_the_connections(
    monkeypatch: pytest.MonkeyPatch, query: str, assistant: bool
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.auth.jwt import mint_token
    from server.observability import events

    emitted: list[Any] = []

    class _Emitter:
        def emit(self, event: Any) -> None:
            emitted.append(event)

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    app = FastAPI()
    mcp.attach(app)
    tok = mint_token(
        subject="viewer@dev", org="dev", namespaces=["alpha"], roles=["viewer"], ttl_seconds=300
    )
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_memory_get", "arguments": {"org": "other", "memory_key": "k"}},
    }
    r = TestClient(app).post(f"/mcp{query}", json=call, headers={"Authorization": f"Bearer {tok}"})
    assert r.json()["result"]["structuredContent"]["code"] == "ORG_MISMATCH"
    (row,) = [e for e in emitted if e.error_code == "ORG_MISMATCH"]
    # On the assistant connection the refusal is its own; elsewhere the token's class.
    assert (row.consumer == "loom-assistant") is assistant


def _model_fields(model: Any, out: set[str], seen: set[Any]) -> set[str]:
    """Every field name of `model` and of the models nested in its fields."""
    from pydantic import BaseModel

    for arg in get_args(model):
        _model_fields(arg, out, seen)
    if isinstance(model, type) and issubclass(model, BaseModel) and model not in seen:
        seen.add(model)
        for name, field in model.model_fields.items():
            out.add(name)
            _model_fields(field.annotation, out, seen)
    return out


def test_every_field_an_assistant_description_names_is_served() -> None:
    from server.transport.mcp_descriptions import ASSISTANT_TOOL_DESCRIPTIONS
    from server.transport.mcp_tools import TOOLS_BY_NAME

    for name, text in ASSISTANT_TOOL_DESCRIPTIONS.items():
        # The op's own fields, and those of a tool the text sends the model to.
        ops = [OPERATIONS_BY_NAME[name]]
        ops += [TOOLS_BY_NAME[t] for t in re.findall(r"loom_[a-z_]+", text) if t in TOOLS_BY_NAME]
        served: set[str] = set()
        for op in ops:
            _model_fields(op.input_model, served, set())
            _model_fields(op.output_model, served, set())
        named = set(re.findall(r"`([a-z_0-9]+)", text)) - {"true", "false"}
        assert named <= served, (name, named - served)


@pytest.mark.parametrize(
    ("op_name", "on_assistant", "agent"),
    [
        # The assistant's own client holds no rows: an ask reads across agents and a
        # brief is about the namespace's top writer.
        ("memory.ask", True, "*"),
        ("mentor.brief", True, ""),
        # Elsewhere the client's agent fills in, as before.
        ("memory.ask", False, "claude-code"),
        ("mentor.brief", False, "claude-code"),
    ],
)
def test_an_assistant_read_naming_no_agent_keeps_the_ops_default(
    monkeypatch: pytest.MonkeyPatch, op_name: str, on_assistant: bool, agent: str
) -> None:
    from server.transport.assistant_call import set_assistant_call

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "claude-code", "user_id": "-"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    arguments: dict[str, Any] = {"query": "when is the deploy window?"}
    set_assistant_call(on_assistant)
    try:
        asyncio.run(
            mcp_call._bind_caller_identity(OPERATIONS_BY_NAME[op_name], arguments, None, None)
        )
    finally:
        set_assistant_call(False)
    assert arguments["agent"] == agent
    # An agent the model names is kept on any connection.
    named: dict[str, Any] = {"query": "q", "agent": "writer"}
    set_assistant_call(True)
    try:
        asyncio.run(mcp_call._bind_caller_identity(OPERATIONS_BY_NAME[op_name], named, None, None))
    finally:
        set_assistant_call(False)
    assert named["agent"] == "writer"


@pytest.mark.parametrize("op_name", ["memory.aggregate", "memory.provenance"])
def test_an_assistant_read_of_one_writers_rows_needs_the_writer_named(
    monkeypatch: pytest.MonkeyPatch, op_name: str
) -> None:
    from pydantic import ValidationError

    from server.transport.assistant_call import set_assistant_call

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "claude-code", "user_id": "-"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    op = OPERATIONS_BY_NAME[op_name]
    arguments: dict[str, Any] = {"memory_key": "k", "agg_func": "count"}
    set_assistant_call(True)
    try:
        asyncio.run(mcp_call._bind_caller_identity(op, arguments, None, None))
    finally:
        set_assistant_call(False)
    # Refused for the missing writer, not answered empty under the client's own agent.
    with pytest.raises(ValidationError, match="agent"):
        op.input_model(**arguments)


def test_the_summary_keeps_a_dashboard_change_to_a_memory_and_no_read() -> None:
    from server.identity.app.namespace_summary import _memory_changes

    changes = set(_memory_changes())
    assert {"memory.set", "memory.delete", "memory.pin", "memory.update"} <= changes
    # An admin's read, and anything outside memory (the dashboard's page-view vitals).
    assert not {"memory.dedup_list", "memory.exposure_summary", "ui.vitals"} & changes


def test_every_argument_an_assistant_description_asks_for_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport.mcp_descriptions import ASSISTANT_TOOL_DESCRIPTIONS

    monkeypatch.setattr(settings, "mcp_code_tools", True)
    listed = {d["name"]: d for d in asyncio.run(mcp._tools_descriptor(toolset="assistant"))}
    for name, text in ASSISTANT_TOOL_DESCRIPTIONS.items():
        op = OPERATIONS_BY_NAME[name]
        if op.mcp_tool_name not in listed:
            continue
        props = listed[op.mcp_tool_name]["inputSchema"]["properties"]
        inputs = set(re.findall(r"`([a-z_0-9]+)", text)) & set(op.input_model.model_fields)
        # A strict client sends only listed properties: an argument the text asks for
        # (the writer's `agent` on an aggregate) must be one it can send.
        assert inputs <= set(props), (name, inputs - set(props))
    provenance = listed["loom_memory_provenance"]["inputSchema"]["properties"]
    # The writer's whole identity, so a row written in a session or for a user is found.
    assert {"agent", "user_id", "session_id"} <= set(provenance)
    # A read that is refused without the writer lists it as required.
    for name in ASSISTANT_NAMED_AGENT_OPS:
        schema = listed[OPERATIONS_BY_NAME[name].mcp_tool_name]["inputSchema"]
        assert "agent" in schema["required"], name
    for tool in ("loom_code_search", "loom_namespace_inventory"):
        props = set(listed[tool]["inputSchema"]["properties"])
        assert not {"agent", "user_id", "session_id"} & props, tool


def test_every_roster_read_of_one_writer_is_named() -> None:
    """A roster read whose `agent` is required and that neither reads across agents nor
    falls back by itself must be one the assistant refuses without a writer."""
    for name in MCP_ASSISTANT_OPS:
        field = OPERATIONS_BY_NAME[name].input_model.model_fields.get("agent")
        exact = field is not None and field.is_required()
        exempt = name in mcp_call._MCP_CROSS_AGENT_READS or name == "memory.get"
        assert (exact and not exempt) == (name in ASSISTANT_NAMED_AGENT_OPS), name


@pytest.mark.parametrize(
    ("assistant", "claimed", "namespace"),
    [
        (True, ["acme-handbook"], "acme-handbook"),  # the answer token's one namespace
        (True, [], "personal-alice"),  # a token naming none keeps the home pick
        (False, ["acme-handbook"], "personal-alice"),  # every other toolset: unchanged
    ],
)
def test_an_assistant_call_reads_the_namespace_its_token_names(
    monkeypatch: pytest.MonkeyPatch, assistant: bool, claimed: list[str], namespace: str
) -> None:
    # The home pick prefers a namespace the token may write: its subject's personal one,
    # never where a viewer token minted for one namespace reads.
    from server.transport import mcp_identity

    async def _home(*_: Any) -> tuple[str, list[str]]:
        return "personal-alice", []

    async def _claims(*_: Any) -> list[str]:
        return claimed

    monkeypatch.setattr(mcp_identity, "caller_org", lambda _: "acme")
    monkeypatch.setattr(mcp_identity, "default_and_readable", _home)
    monkeypatch.setattr(mcp_identity, "readable_named_claims", _claims)
    defaults = asyncio.run(mcp_identity._caller_defaults(object(), assistant=assistant))
    assert defaults is not None and defaults["namespace"] == namespace


def test_an_assistant_call_reads_its_first_claim_the_gate_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Through the call's own connection (no `assistant=`), with the real claim filter: a
    # claim the gate refuses is skipped, and a token never reads past what it names.
    from server.auth import authz
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.transport import mcp_identity
    from server.transport.assistant_call import set_assistant_call

    async def _home(*_: Any) -> tuple[str, list[str]]:
        return "personal-alice", []

    async def _admits(self: Any, namespace: str, role: Role) -> bool:
        return namespace != "revoked-ns"

    monkeypatch.setattr(mcp_identity, "default_and_readable", _home)
    monkeypatch.setattr(authz.CachedGate, "admits", _admits)
    auth = AuthInfo(
        subject="alice",
        org="acme",
        namespaces=("revoked-ns", "acme-handbook"),
        roles=(Role.VIEWER,),
        is_service_account=True,
    )

    async def _run() -> dict[str, Any] | None:
        set_assistant_call(True)
        return await mcp_identity._caller_defaults(auth)

    defaults = asyncio.run(_run())
    assert defaults is not None and defaults["namespace"] == "acme-handbook"
