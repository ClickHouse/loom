"""Two task.create calls with one idempotency key that both miss the lookup,
on live ClickHouse: the second insert carries the same dedup token, so
ClickHouse drops it, and the second call answers with the task that landed.
The coverage-watch ready event's token works the same way.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import asyncio
import os
import time
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

    await wrepo.insert_ready_event(event(100), token=f"{task_id}:1")
    await wrepo.insert_ready_event(event(200), token=f"{task_id}:1")
    assert await wrepo.ready_event_seq(org, "bot", task_id) == 100


async def _erase_tasks(ch: Any, org: str, namespace: str) -> None:
    """What a namespace erasure does to the task tables: record the request
    (the saga writes it before any step), then delete the rows."""
    from server.erasure.store.steps_ledgers import delete_tasks_by_namespace

    row = {
        "id": str(uuid.uuid4()),
        "org": org,
        "scope_type": "namespace",
        "scope_value": namespace,
        "requester": "itest",
        "status": "in_progress",
        "version": time.time_ns(),
    }
    await ch.insert("loom.erasure_requests", [row])
    await delete_tasks_by_namespace(org, namespace)


@pytest.mark.asyncio
async def test_a_create_after_its_task_was_erased_lands_a_new_task(ch: Any) -> None:
    # ClickHouse keeps an insert's dedup token after DELETE, so the second
    # create's insert is dropped although no task holds the key any more.
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
    await _erase_tasks(ch, org, first.task.namespace)

    second = await transitions.handle_task_create(wire.TaskCreateInput(**body))

    assert second.created
    assert second.task.task_id != first.task.task_id
    rows = await ch.query_all(
        "SELECT task_id FROM loom.tasks FINAL WHERE org = {org:String}", params={"org": org}
    )
    assert [str(r["task_id"]) for r in rows] == [second.task.task_id]

    # A second erasure of the same key's task: the next create lands again.
    await _erase_tasks(ch, org, first.task.namespace)
    third = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    assert third.created and third.task.task_id != second.task.task_id


@pytest.mark.asyncio
async def test_racing_creates_after_an_erasure_land_one_task(ch: Any) -> None:
    # Every racer's first insert is dropped (the token outlived the erased
    # task); their second inserts must still dedup one another.
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
    await _erase_tasks(ch, org, first.task.namespace)

    racers = [transitions.handle_task_create(wire.TaskCreateInput(**body)) for _ in range(4)]
    outs = await asyncio.gather(*racers)

    winners = [o for o in outs if o.created]
    assert len(winners) == 1
    assert {o.task.task_id for o in outs} == {winners[0].task.task_id}
    rows = await ch.query_all(
        "SELECT task_id FROM loom.tasks FINAL WHERE org = {org:String} AND is_deleted = 0",
        params={"org": org},
    )
    assert [str(r["task_id"]) for r in rows] == [winners[0].task.task_id]


@pytest.mark.asyncio
async def test_a_new_watch_after_the_namespace_was_erased_posts_its_ready_event(ch: Any) -> None:
    # The ready event's id is fixed per (org, namespace, commit, inbox), and
    # ClickHouse keeps a dedup token after DELETE: the second watch's event
    # must not be dropped as a repeat of the erased one's.
    from server.code.app import watches as svc
    from server.code.store import coverage_watches as wrepo
    from server.erasure.store.mutations import count_then_mutate
    from server.erasure.store.predicates import ORG_NS

    org, ns, sha = f"itest-rewatch-{uuid.uuid4().hex[:8]}", "code", "a" * 40

    async def watch_and_deliver() -> bool:
        watch = svc.new_watch(org, ns, sha, "bot", "poster")
        await wrepo.insert_watch_version(watch)
        emitted, _ = await svc.deliver(watch, {"tiers": []})
        return emitted

    assert await watch_and_deliver()
    await _erase_tasks(ch, org, ns)
    await count_then_mutate(
        "loom.code_coverage_watches",
        count_where=ORG_NS,
        mutation="DELETE",
        mutation_where=ORG_NS,
        params={"org": org, "ns": ns},
        final=False,
        ch=ch,
    )

    assert await watch_and_deliver()
    task_id = wrepo.event_id(org, ns, sha, "bot")
    assert await wrepo.ready_event_seq(org, "bot", task_id) is not None
    await _erase_tasks(ch, org, ns)
    await count_then_mutate(
        "loom.code_coverage_watches",
        count_where=ORG_NS,
        mutation="DELETE",
        mutation_where=ORG_NS,
        params={"org": org, "ns": ns},
        final=False,
        ch=ch,
    )
