"""Private outcomes do not seed recall credit; permitted activity still keeps runs alive."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.memory.app import activity_trivia_cleanup, entity_communities, recall_credit
from server.observability.events import AgentEvent
from server.tasks.store import runs
from tests.integration._event_fixtures import insert_event_rows
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CHClient]:
    connection = CHClient()
    monkeypatch.setattr(recall_credit, "_CLOSER_CACHE", {})
    try:
        await connection.connect()
        yield connection
    finally:
        await connection.disconnect()


def event(org: str, *, marked: bool, success: bool, **changes: Any) -> dict[str, Any]:
    return {
        **AgentEvent(
            ts=datetime.now(UTC),
            org=org,
            namespace="notes",
            agent="reader",
            user_id="reader",
            session_id="session",
            operation="memory.outcome",
            request_id=uuid4().hex,
            latency_ms=13 if success else 997,
            success=success,
            subject="reader",
            consumer="client",
            kind="request",
            memory_key="answer",
            mirror_marked=marked,
        ).to_row(),
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_historical_closer_cache_retains_only_ordinary_positive_evidence(
    client: CHClient, success: bool
) -> None:
    org = unique_org("closer-history")
    await insert_event_rows(
        client,
        "loom.agent_events",
        [
            event(org, marked=True, success=success),
            event(org, marked=False, success=True, consumer="ordinary"),
        ],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    assert not await recall_credit.consumer_closed_loop(org, "client", ch=client)
    # The same SQL must produce a positive ordinary result; a caught SQL error is no proof.
    assert await recall_credit.consumer_closed_loop(org, "ordinary", ch=client)
    assert recall_credit._CLOSER_CACHE[(org, "client")][0] is False
    assert not await recall_credit.consumer_closed_loop(org, "client", ch=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_marked_participation_keeps_only_its_own_subject_run_busy(
    client: CHClient, success: bool
) -> None:
    org = unique_org("marked-busy")
    run = "mcp-" + uuid4().hex + uuid4().hex[:8]
    await insert_event_rows(
        client,
        "loom.agent_events",
        [event(org, marked=True, success=success, run_id=run)],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    assert await runs.busy_lately(client, org, "notes", run, "reader", 3600)
    assert not await runs.busy_lately(client, org, "notes", run, "other", 3600)
    assert not await runs.busy_lately(
        client, unique_org("foreign-busy"), "notes", run, "reader", 3600
    )


@pytest.mark.asyncio
async def test_maintenance_preserves_named_participation_and_excludes_private_worker_namespaces(
    client: CHClient,
) -> None:
    org = unique_org("marked-maintenance")
    mirror = "acl-" + uuid4().hex[:24]
    await insert_event_rows(
        client,
        "loom.agent_events",
        [
            event(org, marked=True, success=False, operation="memory.get"),
            event(org, marked=False, success=True, operation="memory.get", namespace=mirror),
        ],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    hits = [{"memory_key": "answer"}]
    assert await activity_trivia_cleanup._read_by_name(client, org, "notes", hits, 1) == {"answer"}
    assert await activity_trivia_cleanup._read_by_name(client, org, mirror, hits, 1) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_graph_worker_selects_ordinary_success_but_no_private_success_world(
    client: CHClient, success: bool
) -> None:
    org = unique_org("marked-graph")
    foreign = unique_org("foreign-graph")
    await client.insert(
        "loom.namespaces",
        [
            {"org": owner, "namespace": ns, "owner_email": "", "active": True}
            for owner, ns in [(org, "ordinary"), (org, "private"), (foreign, "foreign")]
        ],
        async_insert_settings(),
    )
    await insert_event_rows(
        client,
        "loom.agent_events",
        [
            event(
                org, marked=False, success=True, operation="facts.traverse", namespace="ordinary"
            ),
            event(
                org, marked=True, success=success, operation="facts.traverse", namespace="private"
            ),
            event(
                foreign, marked=False, success=True, operation="facts.traverse", namespace="foreign"
            ),
        ],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    cfg = entity_communities.EntityCommunitiesConfig(org_filter=org, walked_days=1)
    assert await entity_communities._walked_namespaces(client, cfg) == [(org, "ordinary")]
