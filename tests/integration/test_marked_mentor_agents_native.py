"""Historical private outcomes cannot change later mentor facts or connected-agent spend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.insights.app import mentor as mentor_app
from server.insights.store import agents, mentor
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


async def worlds(client: CHClient) -> tuple[str, str, datetime]:
    left, right = unique_org("mentor-left"), unique_org("mentor-right")
    ts = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=60)
    rows: list[dict[str, Any]] = []
    for org, outcome in [(left, True), (right, False)]:
        ordinary = AgentEvent(
            ts=ts,
            org=org,
            namespace="notes",
            agent="reader",
            user_id="reader",
            session_id="session",
            operation="memory.get",
            request_id=uuid4().hex,
            latency_ms=7,
            success=True,
            subject="reader",
            kind="request",
            memory_key="answer-001",
            upstream_cost_usd=0.25,
            args={"out_found": "true", "out_hint_code": "ordinary"},
        ).to_row()
        rows.append(ordinary)
        for operation in ["memory.get", "memory.set", "memory.search"]:
            rows.append(
                {
                    **ordinary,
                    "request_id": uuid4().hex,
                    "operation": operation,
                    "mirror_marked": 1,
                    "success": outcome,
                    "error_code": "PRIVATE_LEFT" if outcome else "PRIVATE_RIGHT",
                    "model": "private-left" if outcome else "private-right",
                    "upstream_cost_usd": 13 if outcome else 997,
                    "latency_ms": 37 if outcome else 999999,
                    "args": {
                        "out_hint_code": "private-left" if outcome else "private-right",
                        "out_found": "true" if outcome else "false",
                        "out_missing_n": "13" if outcome else "997",
                        "out_duplicate_of": "private" if outcome else "",
                        "out_abstain": "true" if outcome else "false",
                        "value": "text/999999" if outcome else "text/1",
                        "tags": "test" if outcome else "",
                        "out_noop": "false" if outcome else "true",
                    },
                }
            )
        rows.append({**ordinary, "namespace": "hidden", "ts": ts - timedelta(days=1)})
    await insert_event_rows(
        client,
        "loom.agent_events",
        rows,
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    return left, right, ts


def profile(client: CHClient, org: str) -> mentor.ProfileReads:
    return mentor.ProfileReads(
        org,
        "notes",
        "reader",
        3600,
        ops={
            "sets": mentor_app.SETS,
            "gets": mentor_app.GETS,
            "searches": mentor_app.SEARCHES,
            "packs": mentor_app.PACKS,
        },
        limits={
            "ttl": 3600,
            "large": 100,
            "huge": 1000,
            "advisory": 3,
            "clip": 4,
            "episodic_chars": 5,
        },
        ch=client,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "op_counts",
        "named_sessions",
        "shown_codes",
        "session_openings",
        "outcomes",
        "key_reads",
        "hot_keys",
        "top_id_prefix",
        "namespace_ledger",
        "subjects",
    ],
)
async def test_profile_facts_ignore_protected_historical_values(
    client: CHClient, method: str
) -> None:
    left, right, _ = await worlds(client)
    first = await getattr(profile(client, left), method)()
    second = await getattr(profile(client, right), method)()
    assert first == second
    if method == "shown_codes":
        assert first == ["ordinary"]
    elif method == "key_reads":
        assert first == {"keys_asked": 1, "keys_missed": 0}
    elif method == "op_counts":
        assert first["memory.get"] == 2


@pytest.mark.asyncio
async def test_brief_evidence_keeps_attempts_and_ordinary_positive_evidence(
    client: CHClient,
) -> None:
    left, right, _ = await worlds(client)
    kwargs = {"searches": mentor_app.SEARCHES, "gets": mentor_app.GETS, "ch": client}
    first = await mentor.call_evidence(left, "notes", "reader", 3600, **kwargs)
    second = await mentor.call_evidence(right, "notes", "reader", 3600, **kwargs)
    assert first == second
    assert first["attempts"] == 4 and first["successes"] == 1
    assert first["committed_writes"] == 0 and first["returned_hits"] == 1
    assert await mentor.hints_shown(left, "notes", "reader", 3600, cap=10, ch=client) == (
        await mentor.hints_shown(right, "notes", "reader", 3600, cap=10, ch=client)
    )
    first_calls = await mentor.last_calls(
        left, "notes", "reader", 3600, sessions=["session"], ch=client
    )
    second_calls = await mentor.last_calls(
        right, "notes", "reader", 3600, sessions=["session"], ch=client
    )
    assert sorted(first_calls, key=lambda r: r["operation"]) == sorted(
        second_calls, key=lambda r: r["operation"]
    )


@pytest.mark.asyncio
async def test_connected_agents_keep_participation_but_no_private_spend_or_hidden_first_use(
    client: CHClient,
) -> None:
    left, right, ts = await worlds(client)
    outputs = []
    for org in [left, right]:
        where, params = agents.connected_where(
            org, 1, namespace="notes", scope_where="", scope_params={}
        )
        rows = await agents.connected_rows(where, params, lookback_days=7, ch=client)
        assert len(rows) == 1
        assert rows[0]["ops"] == 4 and float(rows[0]["cost_usd"]) == 0.25
        assert rows[0]["first_seen"].replace(tzinfo=UTC) == ts
        assert await agents.new_agent_count(where, params, lookback_days=7, ch=client) == 1
        outputs.append(rows)
    assert outputs[0] == outputs[1]
