"""The analytics export's statements on a live ClickHouse: one UTC day of the
ledger summed per operation, the namespace snapshot, and Parquet output.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from server.core.ch import CHClient, get_client
from server.insights.store.analytics_export import NAMESPACES_SQL, USAGE_SQL, parquet
from tests.integration._resources import unique_org

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

DAY = date(2026, 1, 2)


@pytest.fixture
async def ch() -> AsyncIterator[CHClient]:
    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def _event(org: str, ts: str, **kw: Any) -> dict[str, Any]:
    row = {
        "ts": datetime.fromisoformat(ts),
        "org": org,
        "namespace": "ns",
        "agent": "a",
        "user_id": "u1",
        "session_id": "s1",
        "operation": "memory.set",
        "request_id": ts,
        "latency_ms": 10,
        "success": True,
        "transport": "http",
        "status_class": "ok",
        "budget_ms": 100,
        "input_tokens": 0,
        "upstream_cost_usd": Decimal(0),
    }
    return {**row, **kw}


async def test_usage_sums_one_utc_day_per_operation(ch: CHClient) -> None:
    org = unique_org("analytics")
    await ch.insert(
        "loom.agent_events",
        [
            _event(org, "2026-01-02 00:00:00", upstream_cost_usd=Decimal("0.25"), input_tokens=7),
            _event(
                org,
                "2026-01-02 12:00:00",
                user_id="u2",
                latency_ms=300,
                success=False,
                status_class="server",
            ),
            _event(org, "2026-01-02 23:59:59.999", user_id="-", session_id="-"),
            _event(org, "2026-01-01 23:59:59.999"),
            _event(org, "2026-01-03 00:00:00"),
            _event(org, "2026-01-02 08:00:00", operation="memory.get", transport="mcp"),
        ],
    )
    rows = await ch.query_all(
        f"SELECT * FROM ({USAGE_SQL}) WHERE org = {{org:String}}", {"day": DAY, "org": org}
    )
    by_op = {r["operation"]: r for r in rows}
    assert sorted(by_op) == ["memory.get", "memory.set"]
    s = by_op["memory.set"]
    assert (s["ops"], s["errors"], s["server_errors"], s["slow"]) == (3, 1, 1, 1)
    assert (s["users"], s["sessions"]) == (2, 1)
    assert (s["input_tokens"], s["cost_usd"]) == (7, Decimal("0.25"))
    assert (s["namespace"], s["agent"], s["transport"], s["day"]) == ("ns", "a", "http", DAY)
    assert by_op["memory.get"]["ops"] == 1


async def test_namespaces_lists_registered_and_unregistered_with_live_counts(
    ch: CHClient,
) -> None:
    org = unique_org("analytics")
    await ch.insert(
        "loom.namespaces",
        [
            {"org": org, "namespace": "kb", "owner_email": "x@example.com", "kind": "knowledge"},
            {"org": org, "namespace": "mem", "owner_email": "x@example.com", "kind": "memory"},
        ],
    )
    base = {"agent": "a", "user_id": "-", "session_id": "-", "memory_type": "fact", "version": 1}
    await ch.insert(
        "loom.memory",
        [
            {**base, "org": org, "namespace": "mem", "memory_key": "k1", "value": "v"},
            {**base, "org": org, "namespace": "mem", "memory_key": "k2", "value": "v"},
            {**base, "org": org, "namespace": "loose", "memory_key": "k1", "value": "v"},
        ],
    )
    rows = await ch.query_all(
        f"SELECT * FROM ({NAMESPACES_SQL}) WHERE org = {{org:String}}", {"day": DAY, "org": org}
    )
    got = {r["namespace"]: r for r in rows}
    assert sorted(got) == ["kb", "loose", "mem"]
    assert (got["kb"]["registered"], got["kb"]["kind"], got["kb"]["live_memories"]) == (
        True,
        "knowledge",
        0,
    )
    assert got["kb"]["last_memory_at"] is None
    assert (got["mem"]["live_memories"], got["mem"]["kind"]) == (2, "memory")
    loose = got["loose"]
    assert (loose["registered"], loose["kind"], loose["created_at"]) == (False, None, None)
    assert loose["live_memories"] == 1


@pytest.mark.parametrize("entity", ["usage", "namespaces"])
async def test_each_entity_is_a_parquet_file(ch: CHClient, entity: str) -> None:
    body = await parquet(entity, DAY, ch=ch)
    assert body[:4] == b"PAR1" and body[-4:] == b"PAR1"
