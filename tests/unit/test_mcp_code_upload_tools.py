"""`mcp_code_upload_tools`: the compiler-tier upload ops over MCP.

MCP hides `code.scip_ingest` and `code.scip_status` with the rest of the ingest
surface. A deployment whose uploader reaches Loom through MCP only turns the setting
on: both ops are then callable by tool name and through `loom_op`, `all` lists them
where it lists code tools, and no curated roster does. On, an upload is accepted only
for a namespace that has a repository entry.

The calls go through the MCP endpoint with no token and the real handlers, against a
store that keeps the parts in memory.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.transport.mcp as mcp
import server.transport.mcp_call as mcp_call
from server.code.app import ingest
from server.operations.capabilities import capability_snapshot
from server.operations.registry import OPERATIONS_BY_NAME
from server.operations.toolsets import _MCP_HIDDEN_OPS, MCP_TOOLSETS
from server.settings import settings
from server.transport import mcp_op, post_rows
from server.transport.mcp_tools import _tools_descriptor

UPLOAD_OPS = {"code.scip_ingest", "code.scip_status"}
UPLOAD_TOOLS = {"loom_code_scip_ingest", "loom_code_scip_status"}
BATCH = "00000000-0000-4000-8000-000000000001"
# `code-widgets` has a repository entry; `notes` is a namespace the caller writes memory to.
PART = {
    "org": "acme",
    "namespace": "code-widgets",
    "batch_id": BATCH,
    "seq": 0,
    "total_parts": 2,
    "payload": "{}",
}
STATUS = {"org": "acme", "namespace": "code-widgets", "batch_id": BATCH}


class _Store:
    def __init__(self) -> None:
        self.parts: dict[tuple[str, int], str] = {}
        self.batch: dict[str, Any] | None = None

    async def insert_scip_part(
        self, org: str, ns: str, batch_id: str, seq: int, payload: str, payload_hash: str
    ) -> None:
        self.parts[(ns, seq)] = payload

    async def get_scip_batch(self, org: str, ns: str, batch_id: str) -> dict[str, Any] | None:
        return self.batch

    async def upsert_scip_batch(self, org: str, ns: str, batch_id: str, **kw: Any) -> None:
        self.batch = {**kw, "created_at": "t0"}

    async def scip_parts_received(self, org: str, ns: str, batch_id: str) -> int:
        return len(self.parts)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"org": "acme", "namespace": "code-widgets", "repo": "acme/widgets"}]',
    )
    fake = _Store()
    monkeypatch.setattr(ingest, "CodeRepo", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "acme", "namespace": "notes", "agent": "uploader"}

    async def _no_rows(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    monkeypatch.setattr(post_rows, "post_handler_rows", _no_rows)  # the audit row needs a store
    app = FastAPI()
    mcp.attach(app)
    return TestClient(app)


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return client.post("/mcp", json=req).json()["result"]


def _listed(toolset: str) -> set[str]:
    return {t["name"] for t in asyncio.run(_tools_descriptor(toolset=toolset))} & UPLOAD_TOOLS


def test_off_mcp_neither_lists_nor_calls_the_upload_ops(
    client: TestClient, store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.mcp_code_upload_tools is False  # the default
    monkeypatch.setattr(settings, "mcp_code_tools", True)  # the widest listings
    for toolset in MCP_TOOLSETS:
        assert not _listed(toolset), toolset
    for answer in (
        _call(client, "loom_code_scip_ingest", PART),
        _call(client, "loom_op", {"op": "code.scip_ingest", "arguments": PART}),
        _call(client, "loom_code_scip_status", STATUS),
        _call(client, "loom_op", {"op": "code.scip_status", "arguments": STATUS}),
    ):
        assert answer["isError"] and answer["structuredContent"]["code"] == "OP_NOT_ON_MCP"
    assert mcp_op.catalog("scip")["ops"] == []
    assert store.parts == {}


def test_on_both_ops_are_callable_by_tool_name_and_through_loom_op(
    client: TestClient, store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "mcp_code_upload_tools", True)
    first = _call(client, "loom_code_scip_ingest", PART)
    assert first["isError"] is False and first["structuredContent"]["complete"] is False
    last = _call(client, "loom_op", {"op": "code.scip_ingest", "arguments": {**PART, "seq": 1}})
    assert last["structuredContent"] == {
        "batch_id": BATCH,
        "parts_received": 2,
        "total_parts": 2,
        "complete": True,
    }
    assert set(store.parts) == {("code-widgets", 0), ("code-widgets", 1)}
    for answer in (
        _call(client, "loom_code_scip_status", STATUS),
        _call(client, "loom_op", {"op": "code.scip_status", "arguments": STATUS}),
    ):
        assert answer["isError"] is False and answer["structuredContent"]["status"] == "pending"
    assert {row["op"] for row in mcp_op.catalog("scip")["ops"]} == UPLOAD_OPS


def test_on_every_other_hidden_op_stays_off_mcp(
    client: TestClient, store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setting serves the two upload ops and no other hidden op: `code.refresh`, hidden
    beside them, still answers as an op kept off MCP."""
    monkeypatch.setattr(settings, "mcp_code_upload_tools", True)
    assert {name for name in _MCP_HIDDEN_OPS if not mcp_op.is_hidden(name)} == UPLOAD_OPS
    refresh = {"org": "acme", "namespace": "code-widgets"}
    for answer in (
        _call(client, "loom_code_refresh", refresh),
        _call(client, "loom_op", {"op": "code.refresh", "arguments": refresh}),
    ):
        assert answer["isError"] and answer["structuredContent"]["code"] == "OP_NOT_ON_MCP"


def test_on_all_lists_them_where_it_lists_code_tools_and_no_curated_roster_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_code_upload_tools", True)
    monkeypatch.setattr(settings, "mcp_code_tools", True)
    assert _listed("all") == UPLOAD_TOOLS
    # `readonly` is `all` without the writes, and `loom_op` routes reads only there.
    assert _listed("readonly") == {"loom_code_scip_status"}
    ingest_op = OPERATIONS_BY_NAME["code.scip_ingest"]
    status_op = OPERATIONS_BY_NAME["code.scip_status"]
    assert not mcp_op.reachable(ingest_op, "readonly") and mcp_op.reachable(status_op, "readonly")
    for toolset in ("core", "coding", "atomic", "tasks", "tracker", "assistant"):
        assert not _listed(toolset), toolset
    # `all` lists a code op only with the code tools; without them both stay callable by name.
    monkeypatch.setattr(settings, "mcp_code_tools", False)
    assert not _listed("all") and mcp_op.reachable(ingest_op, "all")


def test_on_an_upload_for_a_namespace_without_a_repository_entry_is_refused(
    client: TestClient, store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "mcp_code_upload_tools", True)
    for answer in (
        _call(client, "loom_code_scip_ingest", {**PART, "namespace": "notes"}),
        _call(
            client,
            "loom_op",
            {"op": "code.scip_ingest", "arguments": {**PART, "namespace": "notes"}},
        ),
    ):
        body = answer["structuredContent"]
        assert answer["isError"] and body["code"] == "RESOURCE_NOT_FOUND"
        assert "acme/notes is not a repository index" in body["message"]
        assert "LOOM_MCP_CODE_UPLOAD_TOOLS" in body["message"]
    assert store.parts == {} and store.batch is None


def test_the_catalogue_describes_the_default_whatever_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ops.capabilities` and the generated reference are built from the catalogue, so they
    must not vary with a deployment's setting."""
    monkeypatch.setattr(settings, "mcp_code_upload_tools", True)
    ops = capability_snapshot()["ops"]
    for name in UPLOAD_OPS:
        assert (ops[name]["mcp_tier"], ops[name]["toolsets"]) == ("hidden", [])
