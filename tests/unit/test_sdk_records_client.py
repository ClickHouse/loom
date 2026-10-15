"""SDK typed-record client:
`agent.memory.record.*` and `agent.memory.schema.*` post the wire fields of
the `memory.record.*` / `memory_schema.*` ops, the field shorthand
normalises to the server's `{value, state, evidence_id}` shape, and
`SyncAgent` blocks on the nested clients too."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from loom.agent import Agent
from loom.records import MemoryRecordClient, MemorySchemaClient, normalize_record_fields
from loom.sync import SyncAgent


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reply: dict[str, Any] = {"ok": True}

    async def post(
        self, op_path: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        return self.reply


class _Agent:
    org = "acme"
    namespace = "shop"
    agent = "bot"

    def __init__(self) -> None:
        self.transport = _Transport()


def test_field_shorthand_normalises_to_the_wire_shape() -> None:
    out = normalize_record_fields(
        {
            "tier": "team",
            "seats": 12,
            "trial": False,
            "region": None,
            "owner": {"value": "ana", "evidence_id": "e-1"},
            "notes": {"state": "rejected"},
            "blob": {"value": {"nested": 1}},
        }
    )
    assert out["tier"] == {"value": "team"} and out["seats"] == {"value": 12}
    assert out["trial"] == {"value": False}
    assert out["region"] == {"state": "unknown"}
    assert out["owner"] == {"value": "ana", "evidence_id": "e-1"}
    assert out["notes"] == {"state": "rejected"}
    assert out["blob"] == {"value": {"nested": 1}}  # a dict with other keys is a value


@pytest.mark.asyncio
async def test_write_posts_identity_schema_and_normalised_fields() -> None:
    ag = _Agent()
    c = MemoryRecordClient(ag)  # type: ignore[arg-type]
    await c.write(
        "customer",
        {"account_id": "acc-7", "seats": None},
        memory_key="customer:acc-7",
        memory_id="m-1",
        user_id="u1",
        valid_at=datetime(2026, 9, 18, 8, 0, tzinfo=UTC),
    )
    op, body = ag.transport.calls[0]
    assert op == "/v1/memory.record.write"
    assert body == {
        "org": "acme",
        "namespace": "shop",
        "schema_name": "customer",
        "memory_key": "customer:acc-7",
        "agent": "bot",
        "user_id": "u1",
        "session_id": "-",
        "fields": {"account_id": {"value": "acc-7"}, "seats": {"state": "unknown"}},
        "memory_id": "m-1",
        "valid_at": "2026-09-18T08:00:00+00:00",
    }
    await c.write("customer", {"account_id": "x"}, agent_id="other", namespace="crm")
    body = ag.transport.calls[1][1]
    assert body["agent"] == "other" and body["namespace"] == "crm"
    assert "memory_id" not in body and "valid_at" not in body and body["memory_key"] == ""


@pytest.mark.asyncio
async def test_query_history_conflicts_wire_fields() -> None:
    ag = _Agent()
    c = MemoryRecordClient(ag)  # type: ignore[arg-type]
    await c.query(
        "customer",
        filters={"account_id": "acc-7"},
        predicates=[{"field": "seats", "op": "gte", "number": 10}],
        valid_as_of="2026-09-01T00:00:00Z",
        limit=5,
    )
    op, body = ag.transport.calls[0]
    assert op == "/v1/memory.record.query"
    assert body == {
        "org": "acme",
        "namespace": "shop",
        "schema_name": "customer",
        "limit": 5,
        "filters": {"account_id": "acc-7"},
        "predicates": [{"field": "seats", "op": "gte", "number": 10}],
        "valid_as_of": "2026-09-01T00:00:00Z",
    }
    await c.query("customer")
    assert ag.transport.calls[1][1] == {
        "org": "acme",
        "namespace": "shop",
        "schema_name": "customer",
        "limit": 100,
    }
    ag.transport.reply = {"rows": [{"record_id": "r1"}, {"record_id": "r0"}]}
    rows = await c.history("r1", limit=2)
    assert rows == [{"record_id": "r1"}, {"record_id": "r0"}]
    assert ag.transport.calls[2] == (
        "/v1/memory.record.history",
        {"org": "acme", "namespace": "shop", "record_id": "r1", "limit": 2},
    )
    ag.transport.reply = {"rows": [], "truncated": False}
    out = await c.conflicts(schema_name="customer", open_only=False)
    assert out == {"rows": [], "truncated": False}
    assert ag.transport.calls[3] == (
        "/v1/memory.record.conflicts",
        {
            "org": "acme",
            "namespace": "shop",
            "schema_name": "customer",
            "memory_key": "",
            "open_only": False,
            "limit": 100,
        },
    )


@pytest.mark.asyncio
async def test_schema_register_and_list() -> None:
    ag = _Agent()
    c = MemorySchemaClient(ag)  # type: ignore[arg-type]
    await c.register(
        "customer",
        [{"name": "account_id", "identity": True}, {"name": "tier", "enum_values": ["a"]}],
        description="one row per account",
    )
    op, body = ag.transport.calls[0]
    assert op == "/v1/memory_schema.register"
    assert body == {
        "org": "acme",
        "namespace": "shop",
        "name": "customer",
        "description": "one row per account",
        "fields": [
            {"name": "account_id", "identity": True},
            {"name": "tier", "enum_values": ["a"]},
        ],
        "created_by": "bot",
        "status": "active",
    }
    ag.transport.reply = {"schemas": [{"name": "customer"}]}
    assert await c.list(include_proposed=True) == [{"name": "customer"}]
    assert ag.transport.calls[1] == (
        "/v1/memory_schema.list",
        {"org": "acme", "namespace": "shop", "include_proposed": True, "limit": 100},
    )


@pytest.mark.asyncio
async def test_agent_memory_carries_record_and_schema_clients() -> None:
    a = Agent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x")
    try:
        assert isinstance(a.memory.record, MemoryRecordClient)
        assert isinstance(a.memory.schema, MemorySchemaClient)
        assert a.memory.record._loom_nested_client is True
    finally:
        await a.close()


def test_sync_agent_blocks_on_the_nested_record_client() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"path": req.url.path, "echo": json.loads(req.content)},
            headers={"X-Request-Id": "r1"},
        )

    with SyncAgent(org="o", namespace="n", agent="bot", api_key="k", base_url="http://x") as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )
        out = sa.memory.record.write("customer", {"account_id": "acc-7"})
        assert out["path"] == "/v1/memory.record.write"
        assert out["echo"]["fields"] == {"account_id": {"value": "acc-7"}}
        assert sa.memory.record is sa.memory.record  # one proxy per nested client
        schemas = sa.memory.schema.list()
        assert schemas == []  # the echo has no `schemas` key
