"""Two task.create calls with one idempotency key that both miss the lookup,
on live ClickHouse: the second insert carries the same dedup token, so
ClickHouse drops it, and the second call answers with the task that landed.
The coverage-watch ready event's token works the same way.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from server.tasks.api import models_task as wire
from server.tasks.app import inboxes, transitions
from server.tasks.store import repo

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    repo.reset_lease_backend()
    yield c
    repo.reset_lease_backend()
    await c.disconnect()


@pytest.mark.asyncio
async def test_a_create_that_raced_on_its_key_answers_with_the_stored_task(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = f"itest-dedup-{uuid.uuid4().hex[:8]}"
    ident = {"org": org, "namespace": "private", "agent": "admin"}
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            **ident, inbox="bot", owner_agent="bot", accept_disclosure=["internal"]
        )
    )
    body = {
        "org": org,
        "namespace": "public",
        "agent": "poster",
        "inbox": "bot",
        "kind": "chore",
        "title": "one task",
        "disclosure": "internal",
        "idempotency_key": "k1",
    }
    first = await transitions.handle_task_create(wire.TaskCreateInput(**body))

    # The second call looked the key up before the first one landed.
    lookup = repo.fetch_task_by_idempotency_key
    calls = []

    async def miss_first_lookup(*args: Any) -> Any:
        calls.append(args)
        return None if len(calls) == 1 else await lookup(*args)

    monkeypatch.setattr(repo, "fetch_task_by_idempotency_key", miss_first_lookup)
    second = await transitions.handle_task_create(wire.TaskCreateInput(**body, brief={"n": 2}))

    assert first.created and not second.created
    assert second.task.task_id == first.task.task_id
    rows = await ch.query_all(
        "SELECT task_id FROM loom.tasks FINAL WHERE org = {org:String}", params={"org": org}
    )
    assert [str(r["task_id"]) for r in rows] == [first.task.task_id]
    events = await ch.query_all(
        "SELECT task_id FROM loom.task_events WHERE org = {org:String} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [str(e["task_id"]) for e in events] == [first.task.task_id]


@pytest.mark.asyncio
async def test_a_second_ready_event_with_the_same_id_is_dropped(ch: Any) -> None:
    from server.code.store import coverage_watches as wrepo

    org, task_id = f"itest-ready-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())

    def event(seq: int) -> dict[str, Any]:
        return {
            "org": org,
            "inbox": "bot",
            "namespace": "ns",
            "task_id": task_id,
            "seq": seq,
            "event": wrepo.READY_EVENT,
            "from_status": "",
            "to_status": "",
            "actor": "process:test",
            "agent": "",
            "from_subject": "",
            "fence": 0,
            "version": seq,
            "reason": "",
            "patch": "{}",
            "note": "",
            "ts": datetime.now(UTC).replace(tzinfo=None),
        }

    await wrepo.insert_ready_event(event(100))
    await wrepo.insert_ready_event(event(200))
    assert await wrepo.ready_event_seq(org, "bot", task_id) == 100


@pytest.mark.asyncio
async def test_a_create_after_its_task_was_erased_lands_a_new_task(ch: Any) -> None:
    # ClickHouse keeps an insert's dedup token after DELETE, so the second
    # create's insert is dropped although no task holds the key any more.
    from server.erasure.store.steps_ledgers import delete_tasks_by_namespace

    org = f"itest-erased-{uuid.uuid4().hex[:8]}"
    ident = {"org": org, "namespace": "private", "agent": "admin"}
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            **ident, inbox="bot", owner_agent="bot", accept_disclosure=["internal"]
        )
    )
    body = {
        "org": org,
        "namespace": "public",
        "agent": "poster",
        "inbox": "bot",
        "kind": "chore",
        "title": "one task",
        "disclosure": "internal",
        "idempotency_key": "k1",
    }
    first = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    await delete_tasks_by_namespace(org, first.task.namespace)

    second = await transitions.handle_task_create(wire.TaskCreateInput(**body))

    assert second.created
    assert second.task.task_id != first.task.task_id
    rows = await ch.query_all(
        "SELECT task_id FROM loom.tasks FINAL WHERE org = {org:String}", params={"org": org}
    )
    assert [str(r["task_id"]) for r in rows] == [second.task.task_id]
