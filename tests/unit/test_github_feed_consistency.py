"""Record publications read acknowledged state before changing visibility or progress."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core import ch
from server.tracker.app import github_feed as app
from server.tracker.store import github_feed as store

NOW = datetime(2025, 1, 2, microsecond=123000, tzinfo=UTC)
NOW_MS = int(NOW.timestamp()) * 1000 + NOW.microsecond // 1000
OWNER = "00000000-0000-0000-0000-000000000001"
TARGET = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
async def replicas(monkeypatch):
    state = SimpleNamespace(session={}, lag=False, latest=None, stale=None, writes=[])

    async def query(sql, parameters=None, settings=None, **kwargs):
        effective = state.session | (settings or {})
        consistent = effective.get("select_sequential_consistency") == 1 or (
            "select_sequential_consistency=1" in "".join(sql.split())
        )
        fresh = not state.lag or consistent
        if "FROM feed_owner" in sql:
            row = dict(org="team", owner_uuid=OWNER, schema_version=1)
        elif "FROM feed_repositories" in sql:
            row = dict(state.latest if fresh else state.stale)
        elif "AS now_ms" in sql:
            row = dict(now_ms=NOW_MS, prior_ms=NOW_MS + (7 if fresh else 2))
        else:
            raise AssertionError("unrouted record query: " + sql)
        return SimpleNamespace(column_names=list(row), result_rows=[list(row.values())], summary={})

    async def connect(**kwargs):
        state.session = dict(kwargs["settings"])
        return SimpleNamespace(server_tz=None, query=query, close=AsyncMock())

    async def direct(name, call, **kwargs):
        return await call()

    async def insert(table, rows, settings=None):
        state.writes.extend((table, dict(row)) for row in rows)
        if table == "feed_repositories":
            state.latest = dict(rows[-1])

    @asynccontextmanager
    async def fence(name, **kwargs):
        yield lambda value: None

    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", connect)
    monkeypatch.setattr(ch, "guard_pending_schema", AsyncMock())
    monkeypatch.setattr(app, "fenced", fence)
    monkeypatch.setattr(app, "resealed", lambda *args: None)
    client = ch.ExternalCHClient(
        host="record.example",
        port=8443,
        secure=True,
        database="team_record",
        username="record_writer",
        password="fixture-secret",
    )
    monkeypatch.setattr(client, "_with_retry", direct)
    monkeypatch.setattr(client, "insert", insert)
    await client.connect()
    state.client = client
    try:
        yield state
    finally:
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("lag", [False, True])
async def test_old_public_observation_cannot_replace_acknowledged_private_revision(replicas, lag):
    state = replicas
    state.lag = lag
    state.stale = app.identity("team") | dict(
        repository="widgets/repo",
        repository_id=7,
        owner_uuid=OWNER,
        token_enc="sealed-secret",
        is_private=0,
        available=1,
        visible_until=NOW + timedelta(seconds=900),
        cursors="{}",
        max_pages=5,
        revision=uuid.uuid4(),
    )
    state.latest = state.stale | {"is_private": 1, "revision": uuid.uuid4()}
    private_revision = state.latest["revision"]
    published = await app._publish(
        state.client,
        dict(state.stale),
        OWNER,
        TARGET,
        "items",
        None,
        {},
        app._Observation(7, False, NOW),
    )
    assert published is False
    assert state.latest["is_private"] == 1 and state.latest["revision"] == private_revision
    assert not state.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("lag", [False, True])
async def test_new_repository_publication_exceeds_acknowledged_watermark(replicas, lag):
    replicas.lag = lag
    await store.put_items(
        replicas.client,
        "feed_comments",
        [{"comment_id": 1}],
        app.identity("team"),
        "widgets/second",
    )
    assert replicas.writes[0][1]["version"] == NOW + timedelta(milliseconds=8)


@pytest.mark.asyncio
async def test_reconnected_writer_preserves_acknowledged_publication_floor(replicas):
    previous = replicas.client.client
    await replicas.client._reconnect(replicas.client.generation)
    assert replicas.client.client is not previous
    previous.close.assert_awaited_once()
    replicas.lag = True
    await store.put_items(
        replicas.client,
        "feed_comments",
        [{"comment_id": 1}],
        app.identity("team"),
        "widgets/second",
    )
    assert replicas.writes[0][1]["version"] == NOW + timedelta(milliseconds=8)
