"""task.create calls with one idempotency key, on live ClickHouse: they run
one at a time under a lease on the key, so a racing retry answers with the
task the first one stored, and a create after the task was erased lands a new
one. Each insert also carries the key's dedup token, for a create whose lease
ran out before its insert. The coverage-watch ready event's delivery works the
same way.

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

from server.core.leases import ClaimExpired
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
    # Each insert waits a moment: without the key's lease the second call would
    # look the key up before the first one's task landed.
    insert = repo.insert_task_version

    async def slow_insert(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.1)
        await insert(*args, **kwargs)

    monkeypatch.setattr(repo, "insert_task_version", slow_insert)
    first, second = await asyncio.gather(
        transitions.handle_task_create(wire.TaskCreateInput(**body)),
        _late(1, transitions.handle_task_create, wire.TaskCreateInput(**body, brief={"n": 2})),
    )

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
async def test_a_retry_of_a_create_cut_off_after_its_insert_gets_its_event_and_lease_row(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key's lease timed the first create out after its task row landed and before
    # its created event and lease row: the retry finds the task and must leave it whole.
    org = f"itest-cutoff-{uuid.uuid4().hex[:8]}"
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

    async def cut_off(*_: Any, **__: Any) -> None:
        raise TimeoutError  # what the lease's asyncio.timeout raises inside the body

    with monkeypatch.context() as m:
        m.setattr(repo, "insert_event", cut_off)
        with pytest.raises(ClaimExpired):
            await transitions.handle_task_create(wire.TaskCreateInput(**body))
    retry = await transitions.handle_task_create(wire.TaskCreateInput(**body))

    assert not retry.created
    leases = await ch.query_all(
        "SELECT task_id, fence FROM loom.task_leases WHERE task_id = {tid:String}",
        params={"tid": retry.task.task_id},
    )
    assert [(r["task_id"], int(r["fence"])) for r in leases] == [(retry.task.task_id, 0)]
    # One created event, and a second retry writes no other.
    await transitions.handle_task_create(wire.TaskCreateInput(**body))
    assert await _claimable(ch, org, retry.task.task_id)


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
    # The second create holds the key's lease and finds no task under it. ClickHouse
    # keeps an insert's dedup token after DELETE, so its insert under the key's token
    # is dropped; it reads back no task and lands it again under a fresh token.
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
    # The racers take the key's lease one at a time. The first finds no task; its
    # insert under the key's token is dropped (the token outlived the erased task),
    # so it lands again under a fresh token. The others find that task.
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
    # The ready event's id is fixed per (org, namespace, commit, inbox) and is its
    # insert's dedup token, which ClickHouse keeps after DELETE: the second watch's
    # event is dropped as a repeat of the erased one's, read back missing, and
    # inserted again under a fresh token.
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
    assert await wrepo.ready_event_exists(org, "bot", task_id)
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


async def _late(i: int, call: Any, *args: Any) -> Any:
    """Racer `i` starts half an insert's delay after racer `i - 1`, so each one's
    lookup runs while the other's insert is still on its way."""
    await asyncio.sleep(0.025 * i)
    return await call(*args)


@pytest.mark.asyncio
async def test_racing_creates_store_one(ch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The original task was erased, then two retries race, each insert slow: the
    # lease makes the second wait and find the task the first one stored.
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

    insert = repo.insert_task_version

    async def slow_insert(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.05)  # the other racer runs meanwhile
        await insert(*args, **kwargs)

    monkeypatch.setattr(repo, "insert_task_version", slow_insert)
    outs = await asyncio.gather(
        *(_late(i, transitions.handle_task_create, wire.TaskCreateInput(**body)) for i in range(2))
    )

    assert sorted(o.created for o in outs) == [False, True]
    rows = await ch.query_all(
        "SELECT task_id FROM loom.tasks FINAL WHERE org = {org:String} AND is_deleted = 0",
        params={"org": org},
    )
    assert [str(r["task_id"]) for r in rows] == [o.task.task_id for o in outs if o.created]


@pytest.mark.asyncio
async def test_racing_deliveries_store_one(ch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app import watches as svc
    from server.code.store import coverage_watches as wrepo

    org, ns, sha = f"itest-rewatch-{uuid.uuid4().hex[:8]}", "code", "b" * 40
    watch = svc.new_watch(org, ns, sha, "bot", "poster")
    await wrepo.insert_watch_version(watch)

    insert = wrepo.insert_ready_event

    async def slow_insert(*args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(0.05)  # the other deliverer runs meanwhile
        return await insert(*args, **kwargs)

    monkeypatch.setattr(wrepo, "insert_ready_event", slow_insert)
    outs = await asyncio.gather(*(_late(i, svc.deliver, watch, {"tiers": []}) for i in range(2)))

    assert sorted(emitted for emitted, _ in outs) == [False, True]
    task_id = wrepo.event_id(org, ns, sha, "bot")
    events = await ch.query_all(
        "SELECT seq FROM loom.task_events WHERE org = {org:String} AND task_id = {tid:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "tid": task_id},
    )
    assert len(events) == 1


# ---- the previous build beside this one (rolling upgrade) ----------------------------
#
# The previous build takes no lease. Its create looks the key up, inserts the task, logs
# the created event and writes the lease row; its delivery checks for the event and
# inserts it. It names a dedup token, but its client writes a fresh random one over it
# for every insert, so storage never drops its rows as repeats. Pausing that build
# mid-request takes hooks it does not have, so these tests make its exact inserts
# (a random token each) beside this build's call. Neither build keeps the other's row
# out: a race gives two tasks, as two replicas of the previous build already can.

_OLD_SETTINGS = {"async_insert": 0, "wait_for_async_insert": 1}


def _old_token() -> dict[str, Any]:
    """The previous build's insert settings: its client's random per-insert token."""
    return {**_OLD_SETTINGS, "insert_deduplication_token": uuid.uuid4().hex}


async def _registered(prefix: str) -> tuple[str, dict[str, Any]]:
    org = f"{prefix}-{uuid.uuid4().hex[:8]}"
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
    return org, body


async def _old_build_create(ch: Any, org: str, namespace: str) -> str:
    """The previous build's create once its lookup found no task: the task row, its
    created event and its lease row. Returns the task id it answered."""
    from server.tasks.app.common import _new_version

    task_id, now = str(uuid.uuid4()), datetime.now(UTC).replace(tzinfo=None)
    task = {c: None for c in repo.TASK_FIELDS}
    task.update(
        org=org,
        inbox="bot",
        namespace=namespace,
        task_id=task_id,
        kind="chore",
        priority=2,
        disclosure="internal",
        status="todo",
        reason="",
        blocked_on="",
        defer_count=0,
        title="one task",
        brief="{}",
        from_agent="poster",
        from_subject="poster",
        origin="",
        idempotency_key="k1",
        owner="",
        attempt=0,
        fence=0,
        outcome="{}",
        last_actor="",
        created_at=now,
        is_deleted=0,
    )
    seq = await repo.server_version(0)
    row = _new_version(task, seq)  # type: ignore[arg-type]
    await ch.insert("loom.tasks", [dict(row)], settings=_old_token())
    event = {
        "org": org,
        "inbox": "bot",
        "namespace": namespace,
        "task_id": task_id,
        "seq": seq,
        "event": "created",
        "from_status": "",
        "to_status": "todo",
        "actor": "process:poster",
        "agent": "poster",
        "from_subject": "poster",
        "fence": 0,
        "version": seq,
        "reason": "",
        "patch": "{}",
        "note": "",
        "ts": now,
    }
    await ch.insert("loom.task_events", [event], settings=_old_token())
    backend = await repo.lease_backend()
    await backend.ensure_row(task_id, org, "bot", 0)
    return task_id


async def _live_tasks(ch: Any, org: str) -> list[str]:
    rows = await ch.query_all(
        "SELECT task_id FROM loom.tasks FINAL WHERE org = {org:String} AND is_deleted = 0"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return [str(r["task_id"]) for r in rows]


async def _claimable(ch: Any, org: str, task_id: str) -> bool:
    """The task has its lease row (a claim needs one) and its created event."""
    leases = await ch.query_all(
        "SELECT fence FROM loom.task_leases WHERE task_id = {tid:String}",
        params={"tid": task_id},
    )
    events = await ch.query_all(
        "SELECT event FROM loom.task_events WHERE org = {org:String} AND task_id = {tid:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "tid": task_id},
    )
    return [int(r["fence"]) for r in leases] == [0] and [e["event"] for e in events] == ["created"]


@pytest.mark.asyncio
async def test_an_old_builds_create_after_this_builds_lands_a_second_task(ch: Any) -> None:
    # The old build looked the key up before this build's create, and inserts after it.
    org, body = await _registered("itest-mixed")
    new = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    old = await _old_build_create(ch, org, new.task.namespace)

    assert new.created
    assert sorted(await _live_tasks(ch, org)) == sorted([new.task.task_id, old])
    assert await _claimable(ch, org, new.task.task_id)
    assert await _claimable(ch, org, old)


@pytest.mark.asyncio
async def test_this_builds_create_after_an_old_builds_keeps_its_own_task_claimable(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This build looked the key up before the old build's create landed, and inserts
    # after it. Both tasks land; this build's must not be left without a lease row.
    org, body = await _registered("itest-mixed")
    lookup, old = repo.fetch_task_by_idempotency_key, []

    async def old_build_lands_after_the_lookup(*args: Any, **kwargs: Any) -> Any:
        found = await lookup(*args, **kwargs)
        if not old:
            old.append(await _old_build_create(ch, org, "public"))
        return found

    monkeypatch.setattr(repo, "fetch_task_by_idempotency_key", old_build_lands_after_the_lookup)
    new = await transitions.handle_task_create(wire.TaskCreateInput(**body))

    assert new.created and new.task.task_id != old[0]
    assert sorted(await _live_tasks(ch, org)) == sorted([new.task.task_id, old[0]])
    assert await _claimable(ch, org, new.task.task_id)
    assert await _claimable(ch, org, old[0])


@pytest.mark.asyncio
async def test_a_create_whose_lease_ran_out_answers_the_task_stored_meanwhile(ch: Any) -> None:
    # Two creates of this build: the first one's lease ran out before its insert, and
    # the second one stored the key's task meanwhile. The key's token drops the first
    # one's row, and it answers the second one's task.
    org, body = await _registered("itest-expired")
    stored = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    late = await repo.fetch_task(org, stored.task.task_id)
    assert late is not None
    late = {**late, "task_id": str(uuid.uuid4()), "version": int(late["version"]) + 1}

    answered = await repo.insert_created_task(late)  # type: ignore[arg-type]

    assert str(answered["task_id"]) == stored.task.task_id
    assert await _live_tasks(ch, org) == [stored.task.task_id]


async def _old_build_delivery(ch: Any, watch: Any) -> None:
    """The previous build's delivery once its check found no event: the ready event
    under a random token."""
    from server.code.app import watches as svc
    from server.code.store import coverage_watches as wrepo

    task_id = wrepo.event_id(watch.org, watch.namespace, watch.commit_sha, watch.inbox)
    row = svc._event_row(watch, task_id, [], await repo.server_version(0))
    await ch.insert("loom.task_events", [row], settings=_old_token())


async def _ready_events(ch: Any, watch: Any) -> int:
    from server.code.store import coverage_watches as wrepo

    task_id = wrepo.event_id(watch.org, watch.namespace, watch.commit_sha, watch.inbox)
    rows = await ch.query_all(
        "SELECT seq FROM loom.task_events WHERE org = {org:String} AND task_id = {tid:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": watch.org, "tid": task_id},
    )
    return len(rows)


@pytest.mark.asyncio
async def test_an_old_builds_delivery_after_this_builds_posts_a_second_event(ch: Any) -> None:
    from server.code.app import watches as svc
    from server.code.store import coverage_watches as wrepo

    org = f"itest-mixed-{uuid.uuid4().hex[:8]}"
    watch = svc.new_watch(org, "code", "c" * 40, "bot", "poster")
    await wrepo.insert_watch_version(watch)
    emitted, _ = await svc.deliver(watch, {"tiers": []})
    await _old_build_delivery(ch, watch)
    assert emitted and await _ready_events(ch, watch) == 2


@pytest.mark.asyncio
async def test_this_builds_delivery_after_an_old_builds_reports_its_own_event(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import watches as svc
    from server.code.store import coverage_watches as wrepo

    org = f"itest-mixed-{uuid.uuid4().hex[:8]}"
    watch = svc.new_watch(org, "code", "d" * 40, "bot", "poster")
    await wrepo.insert_watch_version(watch)
    check, old = wrepo.ready_event_exists, []

    async def old_build_lands_after_the_check(*args: Any, **kwargs: Any) -> bool:
        found = await check(*args, **kwargs)
        if not old:
            old.append(await _old_build_delivery(ch, watch))
        return found

    monkeypatch.setattr(wrepo, "ready_event_exists", old_build_lands_after_the_check)
    emitted, _ = await svc.deliver(watch, {"tiers": []})
    # Both events landed, so this delivery reports the one it stored.
    assert emitted and await _ready_events(ch, watch) == 2


async def _cancelled_mid_insert(
    body: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, past_window: bool = False
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """A create cancelled while ClickHouse still runs the INSERT of its created event:
    the caller is gone and the key's lease released, the insert lands once the
    returned event is set. `past_window` lands it under a token of its own, as storage
    takes it once its dedup window no longer holds the backfill's."""
    insert, sent = repo.insert_event, asyncio.Event()
    release = asyncio.Event()
    landing: list[asyncio.Task[None]] = []

    async def server_side(row: Any) -> None:
        await release.wait()
        if past_window:
            settings = {"async_insert": 0, "insert_deduplication_token": uuid.uuid4().hex}
            from server.core.ch import get_client

            await get_client().insert("loom.task_events", [dict(row)], settings=settings)
        else:
            await insert(row)

    async def in_flight(row: Any, **kw: Any) -> None:
        if row["event"] != "created" or landing:
            return await insert(row, **kw)
        landing.append(asyncio.create_task(server_side(row)))
        sent.set()
        await asyncio.Event().wait()  # the caller waits on the answer until cancelled

    monkeypatch.setattr(repo, "insert_event", in_flight)
    first = asyncio.create_task(transitions.handle_task_create(wire.TaskCreateInput(**body)))
    await sent.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    return release, landing[0]


async def _delivered(org: str, cursor: dict[str, Any] | None) -> list[str]:
    """The events the webhook notice for inbox `bot` names past `cursor`."""
    import json

    from server.tasks.workers import webhook_deliver

    bodies: list[bytes] = []

    async def post(transport: Any, url: str, headers: Any, body: bytes) -> int:
        bodies.append(body)
        return 200

    inbox = {"org": org, "inbox": "bot", "webhook_url": "https://example.com/hook"}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(webhook_deliver, "_post", post)
        m.setattr(webhook_deliver, "_secret_for", lambda row, env: b"secret")
        m.setattr(webhook_deliver.task_cursors, "LAG_SECONDS", 0)
        await webhook_deliver._deliver_inbox(
            None,
            inbox,
            cursor,
            now=datetime.now(UTC),
            env={},  # type: ignore[arg-type]
        )
    return [e["event"] for b in bodies for e in json.loads(b)["events"]]


async def _webhook_cursor(ch: Any, org: str) -> dict[str, Any]:
    (cursor,) = await ch.query_all(
        f"SELECT {repo.WEBHOOK_CURSOR_COLS} FROM loom.task_webhook_cursor FINAL"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return dict(cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize("past_window", [False, True])
async def test_a_create_cancelled_mid_insert_and_retried_logs_one_creation(
    ch: Any, monkeypatch: pytest.MonkeyPatch, past_window: bool
) -> None:
    # The first create is cancelled while its created event's INSERT still runs; its
    # retry finds the task without the event and backfills one. Then the first insert
    # lands. History holds one creation, and no cursor reader delivers two.
    org, body = await _registered("itest-cancel")
    release, landing = await _cancelled_mid_insert(body, monkeypatch, past_window=past_window)
    monkeypatch.undo()
    retry = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    assert not retry.created

    seen = await repo.events_since(org, since_seq=0, lag_s=0, inbox="bot")
    assert [e["event"] for e in seen] == ["created"]
    assert await _delivered(org, None) == ["created"]
    release.set()
    await landing

    history = await repo.task_history(org, retry.task.task_id, consistent=True)
    assert [e["event"] for e in history] == ["created"]
    assert await repo.events_since(org, since_seq=int(seen[-1]["seq"]), lag_s=0) == []
    assert await _delivered(org, await _webhook_cursor(ch, org)) == []
    # A reader that saw neither gets one.
    fresh = await repo.events_since(org, since_seq=0, lag_s=0, inbox="bot")
    assert [e["event"] for e in fresh] == ["created"]
    assert await _delivered(org, None) == ["created"]
    # Within the dedup window storage keeps one row; past it the readers keep the first.
    (stored,) = await ch.query_all(
        "SELECT count() AS n FROM loom.task_events WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert stored["n"] == (2 if past_window else 1)
