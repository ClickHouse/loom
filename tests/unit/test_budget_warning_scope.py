"""Warning payloads keep their user scope inside shared task event streams."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from server.identity.domain.read_scope import ReadFilters, read_filters_bound
from server.tasks.api.models_task import TaskUpdatesInput
from server.tasks.app import common, reads, watch_routes
from server.tasks.store import repo
from tests.unit.principals import acting_as, principal

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class EventSQL:
    """Execute the store's predicates, ordering and limit in an in-memory SQL table."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("ATTACH DATABASE ':memory:' AS loom")
        columns = repo.EVENT_COLS.split(", ")
        self.db.execute(
            "CREATE TABLE loom.task_events ("
            + ", ".join(
                f"{c} {'INTEGER' if c in {'seq', 'version', 'fence'} else 'TEXT'}" for c in columns
            )
            + ")"
        )

    def add(
        self,
        seq: int,
        *,
        user_id: str = "-",
        session_id: str = "-",
        event: str = "budget.warning",
        org: str = "acme",
        inbox: str = "warnings",
        from_subject: str = "",
    ) -> None:
        row = dict.fromkeys(repo.EVENT_COLS.split(", "), "")
        row.update(
            org=org,
            namespace="shop",
            inbox=inbox,
            task_id=f"00000000-0000-0000-0000-{seq:012d}",
            seq=seq,
            event=event,
            fence=0,
            version=seq,
            ts=NOW.isoformat(),
            patch=json.dumps({"user_id": user_id, "spent_24h_usd": float(seq)}),
            user_id=user_id,
            session_id=session_id,
            from_subject=from_subject,
        )
        self.db.execute(
            "INSERT INTO loom.task_events ("
            + ", ".join(row)
            + ") VALUES ("
            + ", ".join("?" for _ in row)
            + ")",
            list(row.values()),
        )

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert "FROM loom.task_events" in sql
        # Only the driver parameter syntax and server-clock/settings syntax differ.
        sql = sql.replace("now64(3) - toIntervalSecond({lag:Int32})", ":read_before")
        sql = re.sub(r"\{(\w+):[^}]+\}", r":\1", sql)
        sql = sql.removesuffix(" SETTINGS select_sequential_consistency = 1")
        rows = self.db.execute(sql, {**params, "read_before": NOW.isoformat()}).fetchall()
        return [dict(row) for row in rows]


@pytest.fixture
def event_sql(monkeypatch: pytest.MonkeyPatch) -> Iterator[EventSQL]:
    ch = EventSQL()
    monkeypatch.setattr(repo, "_default_client", lambda: ch)
    monkeypatch.setattr(
        repo,
        "fetch_inbox_row",
        AsyncMock(return_value={"org": "acme", "inbox": "warnings", "owner_namespace": "shop"}),
    )
    monkeypatch.setattr(common, "_in_scope", AsyncMock(return_value=True))
    yield ch
    ch.db.close()


async def updates(*, since_seq: int = 0, limit: int = 200):
    return await reads.handle_task_updates(
        TaskUpdatesInput(
            org="acme", namespace="shop", inbox="warnings", since_seq=since_seq, limit=limit
        )
    )


@pytest.mark.asyncio
async def test_pinned_updates_hide_other_users_and_namespace_warnings(event_sql: EventSQL):
    event_sql.add(1, user_id="bob")
    event_sql.add(2)
    event_sql.add(3, user_id="alice")
    with acting_as(principal("viewer", read_user_id="alice")):
        out = await updates()
    assert [event.seq for event in out.events] == [3]
    assert out.events[0].patch == {"user_id": "alice", "spent_24h_usd": 3.0}
    assert out.next_seq == 3


@pytest.mark.asyncio
async def test_session_updates_hide_warnings_outside_the_session(event_sql: EventSQL):
    event_sql.add(1, user_id="alice")
    event_sql.add(2)
    event_sql.add(3, user_id="alice", session_id="other")
    event_sql.add(4, user_id="alice", session_id="current")
    auth = principal("viewer", read_scope="own_session", read_user_id="alice")
    with (
        acting_as(auth),
        read_filters_bound(auth, ReadFilters("own_session", "alice", "current")),
    ):
        out = await updates()
    assert [event.seq for event in out.events] == [4]


@pytest.mark.asyncio
async def test_namespace_updates_keep_all_warnings_but_not_other_inboxes_or_orgs(
    event_sql: EventSQL,
):
    event_sql.add(1, user_id="bob")
    event_sql.add(2)
    event_sql.add(3, user_id="alice")
    event_sql.add(4, org="other")
    event_sql.add(5, inbox="other")
    with acting_as(principal("viewer")):
        out = await updates()
    assert [event.seq for event in out.events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_hidden_warnings_do_not_starve_shared_task_cursor_pages(event_sql: EventSQL):
    for seq in range(1, 5):
        event_sql.add(seq, user_id="bob")
    event_sql.add(5, user_id="bob", event="progress")
    event_sql.add(6, user_id="alice")
    with acting_as(principal("viewer", read_user_id="alice")):
        first = await updates(limit=1)
        second = await updates(since_seq=first.next_seq, limit=1)
        end = await updates(since_seq=second.next_seq, limit=1)
    assert [event.seq for event in first.events] == [5]
    assert [event.seq for event in second.events] == [6]
    assert (first.next_seq, second.next_seq, end.next_seq) == (5, 6, 6)
    assert end.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("read_scope", ["own_user", "own_session"])
async def test_incomplete_scope_keeps_shared_tasks_and_excludes_warnings(
    event_sql: EventSQL, read_scope: str
):
    event_sql.add(1, user_id="alice")
    event_sql.add(2, event="progress", user_id="bob")
    with acting_as(principal("viewer", read_scope=read_scope)):
        out = await updates()
    assert [event.seq for event in out.events] == [2]


@pytest.mark.asyncio
async def test_pinned_watch_filters_warning_payloads(
    event_sql: EventSQL, monkeypatch: pytest.MonkeyPatch
):
    event_sql.add(1, user_id="bob")
    event_sql.add(2, user_id="alice")
    event_sql.add(3, event="progress", user_id="bob")
    auth = principal("writer", read_user_id="alice")
    monkeypatch.setattr(watch_routes, "_bearer_auth", AsyncMock(return_value=auth))
    monkeypatch.setattr(
        watch_routes, "_resolve_key", AsyncMock(return_value=("key", "warnings", None))
    )

    async def one_page(*, read, **_):
        for row in await read(0):
            yield watch_routes.frame_event(row)

    monkeypatch.setattr(watch_routes, "event_stream", one_page)
    with acting_as(auth):
        response = await watch_routes.task_watch(
            Request({"type": "http", "headers": []}), org="acme", inbox="warnings"
        )
        frames = [frame async for frame in response.body_iterator]
    payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in frames]
    assert [payload["seq"] for payload in payloads] == [2, 3]
