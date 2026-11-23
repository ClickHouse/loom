"""Ordinary SDK schemas and explicitly configured result formats over owned ASGI HTTP."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from server.clients import mcp_source as mcp
from server.knowledge.app import mcp_profiles as profiles
from tests.unit.test_mcp_source_protocol import APPROVED, BEARER, ENDPOINT, EXTRA, _Fixture, _source

BODY = "Ordinary tool output.\nSecond line."


class Document(BaseModel):
    """A generated document contract.\nDescriptions are data, not policy."""

    id: str
    title: str
    text: str
    uri: str
    revision: str | None = None
    labels: dict[str, str] | None = None


class SearchResult(BaseModel):
    items: list[Document]
    cursor: str | None = Field(default=None, description="Optional upstream data.\nNot authority.")


@dataclass
class InteropFixture(_Fixture):
    result_format: str = "structured"
    advertised: bool = True
    raw_text: str | None = None
    definitions: dict[str, Tool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind == "resources":
            super().__post_init__()
            return

        def document(identity: str) -> Document:
            return Document(id=identity, title="Selected document", text=BODY, uri=identity)

        async def search(query: str, tenant: str) -> SearchResult:
            self.calls.append(("search_documents", {"query": query, "tenant": tenant}))
            return SearchResult(items=[document(APPROVED), document(EXTRA)])

        async def search_json(query: str, tenant: str) -> str:
            return (await search(query, tenant)).model_dump_json()

        async def read(id: str, tenant: str) -> Document:
            self.calls.append(("read_document", {"id": id, "tenant": tenant}))
            return document(id)

        async def read_text(id: str, tenant: str) -> str:
            result = await read(id, tenant)
            if self.raw_text is not None:
                return self.raw_text
            return result.model_dump_json() if self.result_format == "json_text" else BODY

        structured = self.result_format == "structured"
        search_fn = search if self.result_format != "json_text" else search_json
        self.definitions = {
            "search_documents": Tool.from_function(
                search_fn,
                name="search_documents",
                structured_output=self.result_format != "json_text",
            ),
            "read_document": Tool.from_function(
                read if structured else read_text,
                name="read_document",
                structured_output=structured,
            ),
        }
        self.server = FastMCP(
            "Ordinary schema fixture",
            tools=list(self.definitions.values()),
            stateless_http=True,
            json_response=self.json_response,
            transport_security=TransportSecuritySettings(allowed_hosts=["mcp.example"]),
        )

    def config(self) -> mcp.MCPSourceConfig:
        if self.kind == "resources":
            return super().config()
        mappings = {}
        for operation, argument in (("search", "query"), ("read", "id")):
            tool = self.definitions[
                "search_documents" if operation == "search" else "read_document"
            ]
            mode = (
                self.result_format
                if operation == "read" or self.result_format == "json_text"
                else "structured"
            )
            contract = tool.fn_metadata.output_schema
            if contract is None:
                contract = (
                    {"type": "string"}
                    if mode == "text"
                    else (
                        SearchResult.model_json_schema()
                        if operation == "search"
                        else Document.model_json_schema()
                    )
                )
            mappings[operation] = mcp.MCPToolMapping(
                tool.name,
                copy.deepcopy(tool.parameters),
                copy.deepcopy(contract),
                argument,
                fixed_arguments={"tenant": "selected-tenant"},
                items_path=("items",) if operation == "search" else (),
                revision_path=("revision",) if mode != "text" else None,
                result_format=mode,
                require_output_schema=self.advertised if mode == "structured" else False,
            )
        return mcp.MCPSourceConfig(
            ENDPOINT,
            ENDPOINT,
            approved_tools=tuple(self.definitions),
            approved_resource_ids=(APPROVED,),
            limits=mcp.MCPLimits(timeout_seconds=5, request_seconds=1),
            **mappings,
        )

    def _change_response(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        super()._change_response(request, response)
        if request.get("method") == "tools/list" and not self.advertised:
            for tool in response["result"]["tools"]:
                tool.pop("outputSchema", None)


def test_generated_schema_admission_and_validation() -> None:
    schema = SearchResult.model_json_schema()
    assert "$defs" in schema
    assert "maxItems" not in schema["properties"]["items"]
    mcp._schema(schema)
    mcp._validate(schema, SearchResult(items=[]).model_dump(), 4096)


@pytest.mark.parametrize(
    "mode,advertised",
    [("structured", True), ("structured", False), ("json_text", False), ("text", False)],
)
async def test_generated_formats_share_selected_read_engine(
    monkeypatch: pytest.MonkeyPatch, mode: str, advertised: bool
) -> None:
    fixture = InteropFixture("tools", result_format=mode, advertised=advertised)
    async with _source(fixture, monkeypatch) as source:
        discovery = await source.discover()
        assert discovery.tools == ("read_document", "search_documents")
        page = await source.search("selected")
        assert {row.resource_id for row in page.items} == {APPROVED, EXTRA}
        assert all(row.receipt is None for row in page.items)
        evidence = await source.read(APPROVED)
        assert evidence.resource_id == evidence.uri == APPROVED
        assert evidence.text == BODY
        assert evidence.receipt is not None
        assert evidence.receipt.requested_id == APPROVED
        assert evidence.receipt.operation == "tools/call"
        assert evidence.receipt.identity_verified is (mode != "text")
        assert evidence.receipt.content_sha256 == hashlib.sha256(BODY.encode()).hexdigest()
        assert evidence.receipt.config_sha256 == source.config_sha256
    assert all(args["tenant"] == "selected-tenant" for _, args in fixture.calls)
    assert all(name in {"search_documents", "read_document"} for name, _ in fixture.calls)


async def test_resources_keep_same_receipt_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = InteropFixture("resources")
    async with _source(fixture, monkeypatch) as source:
        evidence = await source.read(APPROVED)
        assert evidence.receipt is not None
        assert evidence.receipt.identity_verified is True
        assert evidence.receipt.requested_id == APPROVED
        assert evidence.receipt.operation == "resources/read"


@pytest.mark.parametrize("mode", ["structured", "json_text", "text"])
async def test_unselected_identity_refuses_before_any_tool_call(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    fixture = InteropFixture("tools", result_format=mode, advertised=mode == "structured")
    async with _source(fixture, monkeypatch) as source:
        with pytest.raises(mcp.MCPSourceError, match="denied"):
            await source.read(EXTRA)
        assert fixture.calls == []


@pytest.mark.parametrize(
    "raw", ['{"id":"one","id":"two"}', '{"id":NaN}', '{"id":Infinity}', 'prefix {"id":"one"}', "[]"]
)
async def test_json_text_has_no_lenient_parser_or_prose_fallback(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    fixture = InteropFixture("tools", result_format="json_text", advertised=False, raw_text=raw)
    async with _source(fixture, monkeypatch) as source:
        with pytest.raises(mcp.MCPSourceError, match="schema"):
            await source.read(APPROVED)
        assert source._owner_task is not None and source._owner_task.done()


def _profile_payload(mapping: mcp.MCPToolMapping) -> str:
    from dataclasses import asdict

    return json.dumps(
        {
            "version": 1,
            "profiles": [
                {
                    "id": "ordinary",
                    "label": "Ordinary source",
                    "auth": "bearer",
                    "allowed_orgs": ["org"],
                    "config": {
                        "endpoint": ENDPOINT,
                        "bearer_audience": ENDPOINT,
                        "approved_tools": [mapping.name],
                        "read": asdict(mapping),
                    },
                }
            ],
        }
    )


def test_catalog_admits_multiline_schema_data_but_not_policy_controls() -> None:
    fixture = InteropFixture("tools", result_format="json_text", advertised=False)
    mapping = fixture.config().read
    assert mapping is not None
    catalog = profiles.MCPProfileCatalog.parse(_profile_payload(mapping))
    profile = catalog.get("org", "ordinary")
    conf = profiles._config(json.loads(profile.config_snapshot)["config"])
    client = mcp.MCPSourceClient(conf, mcp.MCPBearer(BEARER, ENDPOINT), validate_access=_allowed)
    assert client.config.read == mapping
    assert client.config.endpoint == client.config.bearer_audience == ENDPOINT
    bad = replace(mapping, name="read\nother")
    with pytest.raises(profiles.MCPProfileError):
        profiles.MCPProfileCatalog.parse(_profile_payload(bad))


async def _allowed() -> None:
    return None


@pytest.mark.parametrize("additional", [True, False, {"type": "string"}, {}])
def test_ordinary_objects_arrays_and_nullable_types_have_independent_bounds(
    additional: Any,
) -> None:
    schema = {
        "type": "object",
        "additionalProperties": additional,
        "properties": {"rows": {"type": "array", "items": {"type": ["string", "null"]}}},
    }
    mcp._schema(schema)
    mcp._validate(schema, {"rows": ["a", None]}, 4096)
    if additional is not False:
        mcp._validate(schema, {"rows": [], "extra": "data"}, 4096)
    with pytest.raises(mcp.MCPSourceError, match="schema"):
        mcp._validate(schema, {"rows": [7]}, 4096)


def _deep_value() -> Any:
    node: Any = None
    for _ in range(17):
        node = [node]
    return node


@pytest.mark.parametrize(
    "value",
    [
        [0] * 1001,
        "x" * 1_000_001,
        [[0] * 1000 for _ in range(5)],
        {str(i): 0 for i in range(257)},
        _deep_value(),
        1 << 4096,
    ],
    ids=["array-items", "string-bytes", "nodes", "object-members", "depth", "integer-bits"],
)
def test_independent_instance_work_bounds_run_before_validation(
    monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    invoked = []

    def validate(*args: Any, **kw: Any) -> bool:
        invoked.append(True)
        return True

    mcp._schema({})
    monkeypatch.setattr(mcp.Draft202012Validator, "is_valid", validate)
    with pytest.raises(mcp.MCPSourceError, match="limit"):
        mcp._validate({}, value, 4_000_000)
    assert invoked == []


@pytest.mark.parametrize(
    "schema",
    [
        {"$defs": {"Cycle": {"$ref": "#/$defs/Cycle"}}, "type": "object"},
        {"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}, "type": "object"},
        {"$ref": "#/$defs/Missing", "$defs": {}},
        {"type": "string", "items": {"$ref": "https://untrusted.example/schema"}},
        {"type": "object", "additionalProperties": {"$ref": "urn:external"}},
        {"type": "string", "$dynamicRef": "#/$defs/Other"},
        {"type": "object", "$defs": {"A": {"$defs": {"B": {"type": "string"}}}}},
        {"type": "string", "pattern": "(a+)+$"},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"anyOf": [{"type": "null"}, {"type": "string"}, {"type": "integer"}]},
        {"oneOf": [{"type": "null"}, {"type": "string"}]},
        {"type": ["string", "integer"]},
    ],
)
def test_schema_graph_refuses_cycles_remote_refs_and_unbounded_choices(
    schema: dict[str, Any],
) -> None:
    with pytest.raises(mcp.MCPSourceError, match="schema"):
        mcp._schema(schema)


def test_acyclic_shared_definition_fanout_has_an_expanded_work_budget() -> None:
    definitions: dict[str, Any] = {"Leaf": {"type": "string"}}
    previous = "Leaf"
    for i in range(9):
        name = "Level" + str(i)
        definitions[name] = {
            "type": "object",
            "properties": {
                "a": {"$ref": "#/$defs/" + previous},
                "b": {"$ref": "#/$defs/" + previous},
            },
        }
        previous = name
    schema = {"$defs": definitions, "$ref": "#/$defs/" + previous}
    assert len(json.dumps(schema)) < 16_384
    with pytest.raises(mcp.MCPSourceError, match="schema"):
        mcp._schema(schema)


@pytest.mark.parametrize("mode", ["structured", "json_text", "text"])
async def test_generated_input_schema_drift_precedes_execution(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    fixture = InteropFixture("tools", result_format=mode, advertised=mode == "structured")
    async with _source(fixture, monkeypatch) as source:
        await source.discover()
        fixture.drift = True
        with pytest.raises(mcp.MCPSourceError, match="schema"):
            await source.read(APPROVED)
        assert fixture.calls == []
        assert source._owner_task is not None and source._owner_task.done()


@pytest.mark.parametrize("advertised", [True, False])
async def test_output_schema_appearance_and_disappearance_are_drift(
    monkeypatch: pytest.MonkeyPatch, advertised: bool
) -> None:
    fixture = InteropFixture("tools", advertised=advertised)
    async with _source(fixture, monkeypatch) as source:
        await source.discover()
        fixture.advertised = not advertised
        with pytest.raises(mcp.MCPSourceError, match="schema"):
            await source.read(APPROVED)
        assert fixture.calls == []


@pytest.mark.parametrize("mode", ["structured", "json_text"])
async def test_generated_output_must_echo_the_selected_identity(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    other = Document(id=EXTRA, uri=EXTRA, title="Other", text=BODY).model_dump_json()
    fixture = InteropFixture(
        "tools", result_format=mode, advertised=mode == "structured", raw_text=other
    )
    fixture.mismatch = mode == "structured"
    async with _source(fixture, monkeypatch) as source:
        with pytest.raises(mcp.MCPSourceError, match="protocol"):
            await source.read(APPROVED)
        assert fixture.calls == [("read_document", {"id": APPROVED, "tenant": "selected-tenant"})]


def test_format_is_explicit_and_never_falls_back_to_another_representation() -> None:
    from mcp import types

    fixture = InteropFixture("tools")
    mapping = fixture.config().read
    assert mapping is not None
    document = Document(id=APPROVED, uri=APPROVED, title="One", text=BODY).model_dump()
    text = types.TextContent(type="text", text=json.dumps(document))
    with pytest.raises(mcp.MCPSourceError, match="protocol"):
        mcp._tool_value(types.CallToolResult(content=[text]), mapping, mcp.MCPLimits())
    as_text = replace(mapping, result_format="json_text", require_output_schema=False)
    for content, structured in (([], None), ([text, text], None), ([text], document)):
        with pytest.raises(mcp.MCPSourceError, match="protocol"):
            mcp._tool_value(
                types.CallToolResult(content=content, structuredContent=structured),
                as_text,
                mcp.MCPLimits(),
            )


@pytest.mark.parametrize(
    "change",
    [
        {"result_format": "guess"},
        {"require_output_schema": "false"},
        {"result_format": "text", "output_schema": {"type": "string"}},
    ],
)
def test_search_and_catalog_mapping_modes_fail_closed(change: dict[str, Any]) -> None:
    fixture = InteropFixture("tools")
    search = fixture.config().search
    assert search is not None
    mapping = replace(search, **change)
    with pytest.raises(mcp.MCPSourceError, match="config"):
        mcp.validate_mcp_tool_mapping(mapping, search=True)
    payload = json.loads(_profile_payload(mapping))
    payload["profiles"][0]["config"]["search"] = payload["profiles"][0]["config"].pop("read")
    with pytest.raises(profiles.MCPProfileError):
        profiles.MCPProfileCatalog.parse(json.dumps(payload))


def test_generated_schema_defaults_are_not_fixed_arguments_or_permissions() -> None:
    schema = {
        "type": "object",
        "properties": {"tenant": {"type": "string", "default": "remote-scope"}},
    }
    before = {}
    mcp._schema(schema)
    mcp._validate(schema, before, 4096)
    assert before == {}


@pytest.mark.parametrize(
    "arguments",
    [
        {"tenant": "scope\nother"},
        {"input_schema": {"scope": "scope\nother"}},
    ],
)
def test_fixed_policy_data_cannot_use_schema_multiline_permission(
    arguments: dict[str, Any],
) -> None:
    fixture = InteropFixture("tools")
    mapping = fixture.config().read
    assert mapping is not None
    mapping = replace(mapping, fixed_arguments=arguments)
    with pytest.raises(mcp.MCPSourceError):
        mcp.validate_mcp_tool_mapping(mapping)
    with pytest.raises(profiles.MCPProfileError):
        profiles.MCPProfileCatalog.parse(_profile_payload(mapping))


@pytest.mark.parametrize(
    "selector",
    ["id_path", "title_path", "text_path", "uri_path", "revision_path", "next_cursor_path"],
)
def test_plain_text_refuses_custom_structured_selectors(selector: str) -> None:
    fixture = InteropFixture("tools", result_format="text")
    mapping = fixture.config().read
    assert mapping is not None
    mapping = replace(mapping, **{selector: ("claimed-echo",)})
    with pytest.raises(mcp.MCPSourceError, match="config"):
        mcp.validate_mcp_tool_mapping(mapping)


@pytest.mark.parametrize("mode", ["structured", "json_text", "text"])
async def test_official_sdk_sse_response_has_same_explicit_format_engine(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    fixture = InteropFixture("tools", result_format=mode, advertised=True, json_response=False)
    async with _source(fixture, monkeypatch) as source:
        evidence = await source.read(APPROVED)
        assert evidence.text == BODY
        assert evidence.receipt is not None
        assert evidence.receipt.identity_verified is (mode != "text")
        assert evidence.receipt.requested_id == APPROVED


async def test_interop_profile_cannot_change_credential_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = InteropFixture("tools")
    conf = replace(fixture.config(), endpoint="https://other.example/mcp")
    with pytest.raises(mcp.MCPSourceError, match="config"):
        async with _source(fixture, monkeypatch, config=conf):
            raise AssertionError("origin mismatch was admitted")
    assert fixture.requests == fixture.calls == []
