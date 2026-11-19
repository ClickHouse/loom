"""Persisted marked outcomes leave assistant steps and citation evidence unknown."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from server.assistant.domain import support as rule
from server.assistant.domain.steps import steps_of
from server.assistant.domain.support import CallRow
from server.assistant.store import ledger
from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.memory.app import exposure_ledger
from server.observability.consumer import ASSISTANT_CONSUMER
from server.observability.events import AgentEvent
from server.operations.toolsets import ASSISTANT_CITABLE_OPS, ASSISTANT_NO_NAMESPACE_DATA_OPS
from tests.integration._event_fixtures import insert_event_rows
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest_asyncio.fixture
async def events() -> AsyncIterator[CHClient]:
    client = CHClient()
    try:
        await client.connect()
        yield client
    finally:
        await client.disconnect()


def event(
    org: str, run: str, *, marked: bool, success: bool, hits: str, digest: str, ts: datetime
) -> dict[str, Any]:
    return AgentEvent(
        ts=ts,
        org=org,
        namespace="notes",
        agent="reader",
        user_id="reader",
        session_id="session",
        operation="insights.query",
        request_id=run,
        latency_ms=17,
        success=success,
        subject="reader",
        run_id=run,
        kind="request",
        consumer=ASSISTANT_CONSUMER,
        args={"out_found": "true", "out_hits": hits, "out_sql_digest": digest},
        model="private-model" if marked else "ordinary-model",
        input_tokens=13,
        output_tokens=7,
        mirror_marked=marked,
    ).to_row()


async def read(client: CHClient, mode: str, org: str, run: str, ts: datetime) -> Any:
    if mode == "digests":
        return await ledger.query_digests(org, "notes", run, "reader", ch=client)
    if mode == "live":
        return await ledger.live_rows(
            org, run, "reader", namespace="notes", started=ts.timestamp(), ch=client
        )
    return await ledger.run_rows(org, run, "reader", ch=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "live", "digests"])
async def test_marked_outcomes_keep_one_public_support_result(events: CHClient, mode: str) -> None:
    org = unique_org("marked-assistant")
    ts = datetime.now(UTC)
    left = event(org, "left", marked=True, success=True, hits="31", digest="left", ts=ts)
    right = event(org, "right", marked=True, success=False, hits="89", digest="right", ts=ts)
    right.update(model="different-private-model", input_tokens=997, latency_ms=12345)
    await insert_event_rows(
        events,
        "loom.agent_events",
        [left, right],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    expected = None if mode == "digests" else [CallRow("insights.query", False, protected=True)]
    assert await read(events, mode, org, "left", ts) == expected
    assert await read(events, mode, org, "right", ts) == expected
    if mode != "digests":
        assert steps_of(expected) == ([], False)
        assert (
            rule.support_of(
                [],
                text="An answer.",
                calls=rule.derive(expected),
                citable=ASSISTANT_CITABLE_OPS,
                no_data=ASSISTANT_NO_NAMESPACE_DATA_OPS,
            )
            == rule.UNCHECKED
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "live", "digests"])
async def test_ordinary_support_and_sql_digest_remain_visible(events: CHClient, mode: str) -> None:
    org = unique_org("ordinary-assistant")
    ts = datetime.now(UTC)
    row = event(org, "ordinary", marked=False, success=True, hits="7", digest="ordinary", ts=ts)
    await insert_event_rows(
        events,
        "loom.agent_events",
        [row],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    expected = {"ordinary"} if mode == "digests" else [CallRow("insights.query", True, "true", "7")]
    assert await read(events, mode, org, "ordinary", ts) == expected


@pytest.mark.asyncio
async def test_assistant_support_keeps_org_and_subject_scope(events: CHClient) -> None:
    org = unique_org("assistant-scope")
    foreign = unique_org("assistant-foreign")
    ts = datetime.now(UTC)
    own = event(org, "shared", marked=False, success=True, hits="7", digest="own", ts=ts)
    other_subject = {**own, "subject": "other", "request_id": "other"}
    other_org = {**own, "org": foreign, "request_id": "foreign"}
    await insert_event_rows(
        events,
        "loom.agent_events",
        [own, other_subject, other_org],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    assert await ledger.run_rows(org, "shared", "reader", ch=events) == [
        CallRow("insights.query", True, "true", "7")
    ]
    assert await ledger.query_digests(org, "notes", "shared", "reader", ch=events) == {"own"}


@pytest.mark.asyncio
async def test_key_evidence_keeps_namespace_org_actor_and_run_scope(events: CHClient) -> None:
    org = unique_org("assistant-key-scope")
    foreign = unique_org("assistant-key-foreign")
    ts = datetime.now(UTC)
    scopes = [
        ("notes", org, "reader", "answer"),
        ("other", org, "reader", "answer"),
        ("notes", foreign, "reader", "answer"),
        ("notes", org, "other", "answer"),
        ("notes", org, "reader", "other"),
    ]
    exposures = []
    protected = []
    for index, (namespace, owner, actor, run) in enumerate(scopes):
        exposures.append(
            exposure_ledger.build_row(
                operation="memory.search",
                org=owner,
                namespace=namespace,
                agent="reader",
                user_id="reader",
                session_id="session",
                run_id=run,
                keys=[f"key/{index}"],
                auth=SimpleNamespace(subject=actor),
            )
        )
        if index:
            row = event(owner, run, marked=True, success=True, hits="97", digest="private", ts=ts)
            row.update(namespace=namespace, subject=actor, operation="memory.search")
            protected.append(row)
    await events.insert("loom.memory_exposures", exposures, async_insert_settings())
    await insert_event_rows(
        events,
        "loom.agent_events",
        protected,
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    # Foreign participation cannot hide this scope's ordinary evidence or verify its keys.
    assert await ledger.returned_keys(
        org, "notes", "answer", "reader", [f"key/{index}" for index in range(5)], ch=events
    ) == {"key/0"}
