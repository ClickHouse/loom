"""The memory webhook delivery statements, run on clickhouse-local against the real
tables: which events are due and in what turns, which `mark_dead` settles (with
either `join_use_nulls`), what an admin's deliveries read shows, and which keys
still have history."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.memory.store import webhooks as store
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

# The tables' 3-day TTL is on the wall clock: rows older than it never land.
NOW = naive_utc_now().replace(microsecond=0)
SINCE = NOW - timedelta(days=3)


def _ts(dt: datetime) -> str:
    return dt.strftime("'%Y-%m-%d %H:%M:%S.%f'")[:-4] + "'"


def _event(webhook: str, event_id: str, minutes_ago: int, namespace: str = "chat") -> str:
    return (
        "INSERT INTO loom.memory_webhook_outbox (org, namespace, webhook_id, event_id, "
        "event_type, agent, user_id, session_id, memory_key, memory_id, memory_version, actor, "
        f"request_id, erasure_id, occurred_at) VALUES ('acme', '{namespace}', '{webhook}', "
        f"'{event_id}', 'memory.written', 'bot', 'u1', '-', 'k/{event_id}', 'm', 1, 'x', '', '', "
        f"{_ts(NOW - timedelta(minutes=minutes_ago))})"
    )


def _attempt(
    event_id: str, status: str, version: int, *, due_in: int = 0, attempts: int = 1
) -> str:
    return (
        "INSERT INTO loom.memory_webhook_attempts (event_id, status, attempts, next_attempt_at, "
        f"last_status, last_error, recorded_at, version) VALUES ('{event_id}', '{status}', "
        f"{attempts}, {_ts(NOW + timedelta(seconds=due_in))}, 500, 'e', {_ts(NOW)}, {version})"
    )


class _LocalCH:
    """Runs each query on clickhouse-local after the setup statements; an
    `execute` is kept, to run before the read that checks it."""

    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(self.setup, sql, params)

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        self.executed.append((sql, dict(params or {})))

    def after_execute(self, read: str) -> list[dict[str, Any]]:
        (sql, params), *_ = self.executed
        return run(self.setup, f"{sql};\n{read}", params)


def _tables(*rows: str, nulls: bool = False) -> _LocalCH:
    return _LocalCH(
        *(["SET join_use_nulls = 1"] if nulls else []),
        ddl("memory_webhook_outbox"),
        ddl("memory_webhook_attempts"),
        *rows,
    )


HOOKS = [("acme", "chat", "wa"), ("acme", "chat", "wb")]


@pytest.mark.asyncio
async def test_due_events_are_the_unsettled_due_ones_in_turns() -> None:
    ch = _tables(
        # wa: four events, of which two are due
        _event("wa", "a1", 50),
        _event("wa", "a2", 40),
        _event("wa", "a3", 30),
        _event("wa", "a4", 20),
        _attempt("a1", "pending", 1, due_in=-5, attempts=2),  # due again
        _attempt("a2", "leased", 2, due_in=60),  # leased now
        _attempt("a3", "pending", 3),
        _attempt("a3", "delivered", 4),  # its newest attempt settles it
        # wb: two events, neither tried
        _event("wb", "b1", 10),
        _event("wb", "b2", 5),
        # a webhook the pass cannot send to
        _event("wc", "c1", 60),
    )
    got = await store.due_events(NOW, since=SINCE, hooks=HOOKS, per_webhook=5, limit=10, ch=ch)
    # turn 1 of each webhook (oldest first), then turn 2
    assert [(r["event_id"], r["attempts"], r["last_status"], r["last_error"]) for r in got] == [
        ("a1", 2, 500, "e"),
        ("b1", 0, 0, ""),
        ("a4", 0, 0, ""),
        ("b2", 0, 0, ""),
    ]
    one_each = await store.due_events(NOW, since=SINCE, hooks=HOOKS, per_webhook=1, limit=10, ch=ch)
    assert [r["event_id"] for r in one_each] == ["a1", "b1"]
    capped = await store.due_events(NOW, since=SINCE, hooks=HOOKS, per_webhook=5, limit=3, ch=ch)
    assert [r["event_id"] for r in capped] == ["a1", "b1", "a4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("nulls", [False, True], ids=["join_use_nulls=0", "join_use_nulls=1"])
async def test_mark_dead_settles_only_the_unsettled_events_of_its_webhooks(nulls: bool) -> None:
    ch = _tables(
        _event("wa", "a1", 50),  # never tried
        _event("wa", "a2", 40),
        _attempt("a2", "pending", 1),
        _event("wa", "a3", 30),
        _attempt("a3", "delivered", 2),
        _event("wb", "b1", 10),  # another webhook
        nulls=nulls,
    )
    await store.mark_dead(
        NOW, since=SINCE, hooks=[HOOKS[0]], reason="the webhook was removed", version=99, ch=ch
    )
    rows = ch.after_execute(
        "SELECT event_id, status, attempts, last_error FROM loom.memory_webhook_attempts "
        "WHERE version = 99 ORDER BY event_id"
    )
    assert [(r["event_id"], r["status"], r["attempts"], r["last_error"]) for r in rows] == [
        ("a1", "dead", 0, "the webhook was removed"),
        ("a2", "dead", 1, "the webhook was removed"),
    ]


@pytest.mark.asyncio
async def test_mark_dead_with_a_deadline_settles_only_the_older_events() -> None:
    ch = _tables(_event("wa", "old", 61 * 60), _event("wa", "new", 5))
    await store.mark_dead(
        NOW,
        since=SINCE,
        hooks=[HOOKS[0]],
        reason="no usable secret",
        version=99,
        before=NOW - timedelta(hours=60),
        ch=ch,
    )
    rows = ch.after_execute("SELECT event_id FROM loom.memory_webhook_attempts WHERE version = 99")
    assert [r["event_id"] for r in rows] == ["old"]


@pytest.mark.asyncio
@pytest.mark.parametrize("nulls", [False, True], ids=["join_use_nulls=0", "join_use_nulls=1"])
async def test_deliveries_show_the_namespaces_events_and_their_newest_state(nulls: bool) -> None:
    ch = _tables(
        _event("wa", "untried", 30),
        _event("wa", "leased", 20),
        _attempt("leased", "leased", 1, due_in=60),
        _event("wa", "done", 10),
        _attempt("done", "pending", 2),
        _attempt("done", "delivered", 3, attempts=2),
        _event("wx", "elsewhere", 5, namespace="other"),
        nulls=nulls,
    )

    async def read(**kw: Any) -> list[tuple[str, str, int]]:
        rows = await store.namespace_deliveries(
            "acme", "chat", since=SINCE, limit=10, ch=ch, **({"webhook_id": "", "status": ""} | kw)
        )
        return [(r["event_id"], r["status"], int(r["attempts"])) for r in rows]

    assert await read() == [
        ("done", "delivered", 2),
        ("leased", "pending", 1),
        ("untried", "pending", 0),
    ]
    assert await read(status="pending") == [("leased", "pending", 1), ("untried", "pending", 0)]
    assert await read(webhook_id="wx") == []


@pytest.mark.asyncio
async def test_an_event_announced_twice_is_due_and_listed_once() -> None:
    # A delete_version check that runs twice writes the same event id twice.
    ch = _tables(_event("wa", "twice", 10), _event("wa", "twice", 10))
    due = await store.due_events(NOW, since=SINCE, hooks=HOOKS, per_webhook=5, limit=10, ch=ch)
    assert [r["event_id"] for r in due] == ["twice"]
    listed = await store.namespace_deliveries(
        "acme", "chat", since=SINCE, webhook_id="", status="", limit=10, ch=ch
    )
    assert [r["event_id"] for r in listed] == ["twice"]


@pytest.mark.asyncio
async def test_keys_with_history_are_the_keys_memory_versions_holds() -> None:
    ch = _LocalCH(
        ddl("memory_versions"),
        "INSERT INTO loom.memory_versions (org, namespace, agent, user_id, session_id, "
        "memory_id, memory_key, version) VALUES ('acme', 'chat', 'bot', 'u1', '-', "
        "'00000000-0000-0000-0000-000000000001', 'kept', 1)",
    )
    kept = ("acme", "chat", "bot", "u1", "-", "kept")
    erased = ("acme", "chat", "bot", "u1", "-", "erased")
    assert await store.keys_with_history([kept, erased], ch=ch) == {kept}
