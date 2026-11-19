"""Exports and activity readers mask marked outcomes before selecting their results."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.erasure.store import exports
from server.insights.store import ops
from server.knowledge.store import documents
from server.memory.store import inspect
from server.observability.events import AgentEvent
from tests.integration._event_fixtures import insert_event_rows
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[CHClient]:
    connection = CHClient()
    try:
        await connection.connect()
        yield connection
    finally:
        await connection.disconnect()


async def insert(client: CHClient, *, success: bool) -> str:
    org = unique_org("event-reader")
    ordinary = AgentEvent(
        ts=datetime.now(UTC),
        org=org,
        namespace="notes",
        agent="reader",
        user_id="reader",
        session_id="session",
        operation="memory.get",
        request_id=uuid4().hex,
        latency_ms=7,
        success=True,
        upstream_cost_usd=0.25,
        input_tokens=3,
        output_tokens=2,
        bytes_written=7,
        kind="request",
        args={"out_found": "true"},
    ).to_row()
    marked = {
        **ordinary,
        "request_id": uuid4().hex,
        "mirror_marked": 1,
        "success": success,
        "latency_ms": 13 if success else 999999,
        "input_tokens": 13 if success else 997,
        "upstream_cost_usd": 13 if success else 997,
        "bytes_written": 13 if success else 997,
        "error_code": "PRIVATE_LEFT" if success else "PRIVATE_RIGHT",
        "args": {"out_found": "true" if success else "false", "source_uri": "private://file"},
    }
    await insert_event_rows(
        client,
        "loom.agent_events",
        [
            ordinary,
            marked,
            {**ordinary, "operation": "knowledge.get", "args": {"source_uri": "ordinary://file"}},
            {**marked, "operation": "knowledge.get"},
            {**ordinary, "org": unique_org("event-reader-foreign")},
            {**ordinary, "namespace": "hidden"},
        ],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    return org


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_export_keeps_ordinary_rows_and_neutralizes_all_selected_protected_fields(
    client: CHClient, success: bool
) -> None:
    org = await insert(client, success=success)
    rows = await exports.export_agent_events(org, "namespace", "notes", limit=20, ch=client)
    assert len(rows) == 4
    ordinary = [r for r in rows if r["success"]]
    marked = [r for r in rows if not r["success"]]
    assert len(ordinary) == len(marked) == 2
    assert all(float(r["upstream_cost_usd"]) == 0.25 and r["latency_ms"] == 7 for r in ordinary)
    for row in marked:
        assert row["latency_ms"] == row["input_tokens"] == row["output_tokens"] == 0
        assert row["embedding_tokens"] == row["bytes_written"] == 0
        assert float(row["upstream_cost_usd"]) == 0
        assert row["args"] == {} and row["error_code"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_subject_activity_counts_participation_and_only_ordinary_success(
    client: CHClient, success: bool
) -> None:
    org = await insert(client, success=success)
    rows = await inspect.subject_calls(
        org, "notes", "user", "reader", window_seconds=3600, skip_consumers=[], ch=client
    )
    assert {r["operation"] for r in rows} == {"memory.get", "knowledge.get"}
    assert all(r["calls"] == 2 and r["succeeded"] == 1 for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_document_read_counts_do_not_select_private_result_addresses(
    client: CHClient, success: bool
) -> None:
    org = await insert(client, success=success)
    rows = await documents.agent_opens(
        org, "notes", days=1, skip_consumers=[], source_uris=[], ch=client
    )
    assert len(rows) == 1 and rows[0]["source_uri"] == "ordinary://file"
    assert rows[0]["reads"] == 1 and rows[0]["agents"] == 1
    assert (
        await documents.agent_opens(
            org, "notes", days=1, skip_consumers=[], source_uris=["private://file"], ch=client
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_successful_operation_mix_keeps_only_real_ordinary_successes(
    client: CHClient, success: bool
) -> None:
    org = await insert(client, success=success)
    rows = await ops.memory_op_counts(org, "notes", 1, ch=client)
    assert rows == [{"operation": "memory.get", "c": 1, "agents": ["reader"]}]
