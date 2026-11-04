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
from tests.integration._resources import unique_org

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
async def test_a_create_cut_off_after_its_insert_gets_its_event_and_a_lease_row_from_the_reconcile(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key's lease timed the first create out after its task row landed and before
    # its created event and lease row: the retry logs the event, and one reconcile past
    # its grace writes the lease row.
    from server.tasks.app import sweep

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
    # The retry writes no lease row: the reconcile writes the missing one after its grace.
    tid = retry.task.task_id
    assert await _lease(ch, tid) is None
    _only_strays_of(monkeypatch, tid)
    await sweep.reconcile_leases(_later(), limit=10_000)
    assert await _lease(ch, tid) == {"owner": "", "fence": 0}
    # One created event, and a second retry writes no other.
    await transitions.handle_task_create(wire.TaskCreateInput(**body))
    assert await _claimable(ch, org, tid)


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


@pytest.mark.asyncio
async def test_another_posters_create_with_the_key_is_not_dropped_by_its_token(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key's dedup token names the poster: another poster's task under the same key
    # lands on its first insert, and the first poster's task stays as it was.
    org, body = await _registered("itest-poster")
    first = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    stored = await repo.fetch_task(org, first.task.task_id)
    assert stored is not None
    other = {
        **stored,
        "task_id": str(uuid.uuid4()),
        "from_subject": "sa:other-poster",
        "version": int(stored["version"]) + 1,
    }
    insert, tokens = repo.insert_task_version, []

    async def counted(*args: Any, **kwargs: Any) -> None:
        tokens.append(kwargs.get("dedup_token", ""))
        await insert(*args, **kwargs)

    monkeypatch.setattr(repo, "insert_task_version", counted)
    answered = await repo.insert_created_task(other)  # type: ignore[arg-type]

    assert str(answered["task_id"]) == other["task_id"]
    assert len(tokens) == 1  # not dropped and inserted again under a fresh token
    assert sorted(await _live_tasks(ch, org)) == sorted([first.task.task_id, other["task_id"]])
    landed = await repo.fetch_task(org, other["task_id"])
    assert landed is not None
    assert {**landed, "task_id": str(landed["task_id"])} == other
    own = await repo.fetch_task_by_idempotency_key(org, "bot", str(stored["from_subject"]), "k1")
    assert own is not None and str(own["task_id"]) == first.task.task_id


# ---- the previous build beside this one (rolling upgrade) ----------------------------
#
# The previous build's create looks the key up, inserts the task, logs the created event
# and writes the lease row. It names its lease and dedup token without the poster, so
# neither ever matches this build's. Pausing that build mid-request takes hooks it does
# not have, so these tests make its create's inserts beside this build's call: the task
# row under that build's own key token, its event under the task's created token.
# Neither build's create keeps the other's task out: a race gives two tasks.

_OLD_SETTINGS = {"async_insert": 0, "wait_for_async_insert": 1}


def _old_key_token(org: str) -> dict[str, Any]:
    """The previous build's task insert settings: its key token, named without the poster."""
    return {**_OLD_SETTINGS, "insert_deduplication_token": f"task.create:{org}:bot:k1"}


async def _registered(prefix: str) -> tuple[str, dict[str, Any]]:
    org = unique_org(prefix)
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
        from_subject="",  # the subject this build stamps for a dev-mode create
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
    await ch.insert("loom.tasks", [dict(row)], settings=_old_key_token(org))
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
        "from_subject": "",
        "fence": 0,
        "version": seq,
        "reason": "",
        "patch": "{}",
        "note": "",
        "ts": now,
    }
    created_token = f"task.created:{org}:{task_id}"
    await ch.insert(
        "loom.task_events",
        [event],
        settings={**_OLD_SETTINGS, "insert_deduplication_token": created_token},
    )
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
async def test_this_builds_retry_finds_the_task_an_old_builds_create_stored(ch: Any) -> None:
    # One poster's create landed on the old build; its retry on this build finds it.
    org, body = await _registered("itest-mixed")
    old = await _old_build_create(ch, org, "private")
    retried = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    assert not retried.created and retried.task.task_id == old
    assert await _live_tasks(ch, org) == [old]


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

    from server.core import webhooks
    from server.tasks.workers import webhook_deliver

    bodies: list[bytes] = []

    async def post(transport: Any, url: str, headers: Any, body: bytes, **kw: Any) -> Any:
        bodies.append(body)
        return webhooks.Delivery(status=200)

    inbox = {"org": org, "inbox": "bot", "webhook_url": "https://example.com/hook"}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(webhooks, "post", post)
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


@pytest.mark.asyncio
async def test_paging_one_event_at_a_time_moves_past_a_discarded_backfill(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Past the dedup window both creations of the cancelled create are stored, and the
    # backfill is dropped from every read. A cursor sitting just before it, paging
    # task.updates with limit=1, still reaches the next task's creation.
    from server.tasks.app import reads
    from server.tasks.domain import cursors as task_cursors

    org, body = await _registered("itest-page")
    release, landing = await _cancelled_mid_insert(body, monkeypatch, past_window=True)
    monkeypatch.undo()
    retry = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    release.set()
    await landing
    second = await transitions.handle_task_create(
        wire.TaskCreateInput(**{**body, "idempotency_key": "k2", "title": "two"})
    )
    monkeypatch.setattr(task_cursors, "LAG_SECONDS", 0)
    seen: list[tuple[str, str]] = []
    since = 0
    for _ in range(5):
        page = await reads.handle_task_updates(
            wire.TaskUpdatesInput(
                org=org, namespace="private", agent="bot", inbox="bot", since_seq=since, limit=1
            )
        )
        if not page.events:
            break
        seen += [(str(e.task_id), str(e.event)) for e in page.events]
        since = page.next_seq
    assert seen == [
        (retry.task.task_id, "created"),
        (second.task.task_id, "created"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["cancel", "wont_do"])
async def test_a_create_retry_racing_an_end_leaves_no_lease_row(
    ch: Any, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    """A create retry that runs between an end's lease drop and its status row writes no
    lease row: the ended task is left with none."""
    org = f"itest-end-{uuid.uuid4().hex[:8]}"
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            org=org,
            namespace="private",
            agent="admin",
            inbox="bot",
            owner_agent="bot",
            accept_disclosure=["internal"],
        )
    )
    body = {
        "org": org,
        "namespace": "public" if op == "cancel" else "private",
        "agent": "poster",
        "inbox": "bot",
        "kind": "chore",
        "title": "t",
        "disclosure": "internal",
        "idempotency_key": "k1",
    }
    first = await transitions.handle_task_create(wire.TaskCreateInput(**body))
    real_drop = transitions._drop_lease
    drops = 0

    async def drop_then_retry(task: Any) -> None:
        nonlocal drops
        drops += 1
        await real_drop(task)
        if drops == 1:
            # The client's create retry lands here, before the end's own row.
            await transitions.handle_task_create(wire.TaskCreateInput(**body))

    monkeypatch.setattr(transitions, "_drop_lease", drop_then_retry)
    end = transitions.handle_task_cancel if op == "cancel" else transitions.handle_task_wont_do
    out = await end(
        wire.TaskReasonInput(
            org=org,
            namespace=body["namespace"],
            agent="poster" if op == "cancel" else "bot",
            task_id=first.task.task_id,
            reason="x",
        )
    )
    assert out.task.status == ("cancelled" if op == "cancel" else "wont_do")
    rows = await ch.query_all(
        "SELECT task_id FROM loom.task_leases WHERE task_id = {t:String}",
        params={"t": first.task.task_id},
    )
    assert rows == [], rows


@pytest.mark.asyncio
async def test_a_claim_racing_a_cancel_leaves_the_task_cancelled(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel dropped the lease row; a claim that read the task todo finds no row: it
    loses without writing one, and once the cancel finishes the task is cancelled with no
    lease row."""
    org = f"itest-cancel-{uuid.uuid4().hex[:8]}"
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            org=org,
            namespace="private",
            agent="admin",
            inbox="bot",
            owner_agent="bot",
            accept_disclosure=["internal"],
        )
    )
    first = await transitions.handle_task_create(
        wire.TaskCreateInput(
            org=org,
            namespace="private",
            agent="poster",
            inbox="bot",
            kind="chore",
            title="t",
            disclosure="internal",
        )
    )
    tid = first.task.task_id
    if first.task.status == "triage":
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(org=org, namespace="private", agent="bot", task_id=tid)
        )
    backend = await repo.lease_backend()
    await backend.delete(tid)  # the cancel's first drop

    async def no_lease_write(*a: Any, **k: Any) -> None:
        pytest.fail("a claim writes no lease row")

    monkeypatch.setattr(backend, "ensure_row", no_lease_write)
    with pytest.raises(Exception, match="TASK_ALREADY_CLAIMED"):
        await transitions.handle_task_claim(
            wire.TaskClaimInput(org=org, namespace="private", agent="bot", task_id=tid)
        )
    monkeypatch.undo()
    await transitions.handle_task_cancel(
        wire.TaskReasonInput(org=org, namespace="private", agent="bot", task_id=tid, reason="x")
    )
    assert (await repo.fetch_task(org, tid))["status"] == "cancelled"
    leases = await ch.query_all(
        "SELECT task_id FROM loom.task_leases WHERE task_id = {t:String}", params={"t": tid}
    )
    assert leases == []


async def _todo_task(org: str, **create: Any) -> str:
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            org=org,
            namespace="private",
            agent="admin",
            inbox="bot",
            owner_agent="bot",
            accept_disclosure=["internal"],
        )
    )
    first = await transitions.handle_task_create(
        wire.TaskCreateInput(
            org=org,
            namespace="private",
            agent="poster",
            inbox="bot",
            kind="chore",
            title="t",
            disclosure="internal",
            **create,
        )
    )
    tid = first.task.task_id
    if first.task.status == "triage":
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(org=org, namespace="private", agent="bot", task_id=tid)
        )
    return tid


def _reason(org: str, tid: str) -> Any:
    return wire.TaskReasonInput(org=org, namespace="private", agent="bot", task_id=tid, reason="x")


@pytest.mark.asyncio
async def test_a_claim_won_on_a_stray_lease_row_of_a_cancelled_task_backs_out(
    ch: Any,
) -> None:
    """A late write left an idle lease row for a cancelled task. A claim that read the
    task todo wins the row, reads the task again and backs out: the task stays
    cancelled, and the stray row is the lease reconcile's to remove."""
    org = f"itest-stray-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    stale = await repo.fetch_task(org, tid)  # a claimer reads todo
    await transitions.handle_task_cancel(_reason(org, tid))
    backend = await repo.lease_backend()
    await backend.ensure_row(tid, org, stale["inbox"], 0)  # the late write
    got = await transitions._claim_one(stale, claimant="bot", actor="process:bot", lease_seconds=60)
    assert got is None
    assert (await repo.fetch_task(org, tid))["status"] == "cancelled"
    assert await _lease(ch, tid) == {"owner": "", "fence": 0}


@pytest.mark.asyncio
async def test_a_reopen_takes_back_a_lease_row_a_late_write_left_at_an_older_fence(
    ch: Any,
) -> None:
    """A task claimed (fence 1) and cancelled, then a late write leaves an idle row at
    fence 0: the reopen moves that row to the task's fence, so the task is claimable."""
    org = f"itest-strand-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    await _claim(org, tid)
    await transitions.handle_task_cancel(_reason(org, tid))
    task = await repo.fetch_task(org, tid)
    assert int(task["fence"]) == 1
    backend = await repo.lease_backend()
    await backend.ensure_row(tid, org, task["inbox"], 0)  # the late write, an older fence
    await transitions.handle_task_reopen(_reason(org, tid))
    out = await transitions.handle_task_claim(
        wire.TaskClaimInput(org=org, namespace="private", agent="bot", task_id=tid)
    )
    assert out.task.status == "in_progress"


async def _lease(ch: Any, tid: str) -> Any:
    return await ch.query_one(
        "SELECT owner, fence FROM loom.task_leases WHERE task_id = {t:String}", params={"t": tid}
    )


async def _claim(org: str, tid: str) -> Any:
    return await transitions.handle_task_claim(
        wire.TaskClaimInput(org=org, namespace="private", agent="bot", task_id=tid)
    )


@pytest.mark.asyncio
async def test_a_claim_that_backs_out_of_a_deferred_task_leaves_it_claimable(ch: Any) -> None:
    """A pull reads the task todo; the owner defers it; the claim wins the row, reads it
    deferred and gives the row back at the fence it found. Resumed, the task is
    claimable."""
    org = f"itest-defer-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    stale = await repo.fetch_task(org, tid)
    await transitions.handle_task_defer(
        wire.TaskDeferInput(
            org=org, namespace="private", agent="bot", task_id=tid, reason="later", fence=0
        )
    )
    got = await transitions._claim_one(stale, claimant="bot", actor="process:bot", lease_seconds=60)
    assert got is None
    assert await _lease(ch, tid) == {"owner": "", "fence": 0}
    await transitions.handle_task_resume(
        wire.TaskResumeInput(org=org, namespace="private", agent="bot", task_id=tid)
    )
    assert (await _claim(org, tid)).task.status == "in_progress"


@pytest.mark.asyncio
async def test_a_claim_that_backs_out_never_removes_a_row_a_reopen_wrote(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim from a stale todo read wins the row a reopen's first write inserted and
    reads the task still cancelled; the reopen finishes before the claim backs out. The
    claim only undoes its own bump, so the reopened task stays claimable."""
    org = f"itest-reopen-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    stale = await repo.fetch_task(org, tid)
    await transitions.handle_task_cancel(_reason(org, tid))
    backend = await repo.lease_backend()
    # The reopen's first write lands; the stale claim wins it and reads 'cancelled'.
    await backend.ensure_row(tid, org, stale["inbox"], int(stale["fence"]), reset_idle=True)
    real_give_back = backend.give_back

    async def give_back(*a: Any, **k: Any) -> None:
        monkeypatch.setattr(backend, "give_back", real_give_back)
        await transitions.handle_task_reopen(_reason(org, tid))  # finishes first
        await real_give_back(*a, **k)

    monkeypatch.setattr(backend, "give_back", give_back)
    assert await transitions._claim_one(stale, claimant="bot", actor="p", lease_seconds=60) is None
    monkeypatch.undo()
    assert (await repo.fetch_task(org, tid))["status"] == "todo"
    assert (await _claim(org, tid)).task.status == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("stranded", ["no row", "idle row at another fence", "ended task"])
async def test_one_lease_sweep_puts_a_stranded_lease_row_right(
    ch: Any, monkeypatch: pytest.MonkeyPatch, stranded: str
) -> None:
    """Whatever race left a lease row out of step with its task, one sweep past the grace
    makes it agree: a todo task gets its row, an idle row takes the task's fence, and an
    ended task's row goes."""
    from datetime import timedelta

    from server.tasks.app import sweep

    org = f"itest-heal-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    backend = await repo.lease_backend()
    if stranded == "no row":
        await backend.delete(tid)
    elif stranded == "idle row at another fence":
        await backend.give_back(tid, "", held_fence=0, fence=5)
        assert await _lease(ch, tid) == {"owner": "", "fence": 5}
    else:
        await transitions.handle_task_cancel(_reason(org, tid))
        task = await repo.fetch_task(org, tid)
        await backend.ensure_row(tid, org, task["inbox"], int(task["fence"]))
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    _only_strays_of(monkeypatch, tid)
    await sweep.reconcile_leases(later, limit=10_000)
    if stranded == "ended task":
        assert await _lease(ch, tid) is None
    else:
        assert (await _claim(org, tid)).task.status == "in_progress"


def _later() -> datetime:
    from datetime import timedelta

    return datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)


def _only_strays_of(monkeypatch: pytest.MonkeyPatch, *task_ids: str) -> None:
    """The lease reconcile acts only on these tasks: the shared ClickHouse holds every
    other test's tasks too. The tests assert on the lease rows it leaves, not on what it
    counts, which depends on what else sits within its limit."""
    strays = repo.lease_strays

    async def only(**kw: Any) -> Any:
        missing, stray = await strays(**kw)
        return (
            [t for t in missing if t["task_id"] in task_ids],
            [r for r in stray if r["task_id"] in task_ids],
        )

    monkeypatch.setattr(repo, "lease_strays", only)


@pytest.mark.asyncio
async def test_the_lease_reconcile_never_deletes_a_row_a_claim_took_since_its_read(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray idle row of a cancelled task; between the reconcile's read and its delete
    the task is reopened and claimed. The delete is guarded on the idle row it read, so
    the claimant keeps its lease and its heartbeat passes."""
    from server.tasks.app import sweep

    org = f"itest-recdel-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    await transitions.handle_task_cancel(_reason(org, tid))
    task = await repo.fetch_task(org, tid)
    backend = await repo.lease_backend()
    await backend.ensure_row(tid, org, task["inbox"], int(task["fence"]))  # the stray row
    real_delete = backend.delete_held
    claimed: dict[str, Any] = {}

    async def delete_held(*a: Any, **k: Any) -> None:
        monkeypatch.setattr(backend, "delete_held", real_delete)
        await transitions.handle_task_reopen(_reason(org, tid))
        claimed["task"] = (await _claim(org, tid)).task
        await real_delete(*a, **k)

    monkeypatch.setattr(backend, "delete_held", delete_held)
    _only_strays_of(monkeypatch, tid)
    await sweep.reconcile_leases(_later(), limit=10_000)
    monkeypatch.undo()
    fence = claimed["task"].fence
    assert await _lease(ch, tid) == {"owner": "bot", "fence": fence}
    await transitions.handle_task_heartbeat(
        wire.TaskHeartbeatInput(org=org, namespace="private", agent="bot", task_id=tid, fence=fence)
    )


@pytest.mark.asyncio
async def test_the_lease_reconcile_gives_back_a_row_a_claim_cut_off_after_winning_holds(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim wins the row (fence 1) and its process dies before the status write: the
    task stays todo at fence 0 under an expired row at 1. One sweep gives the row back
    idle at 0, and the task is claimable."""
    from datetime import timedelta

    from server.tasks.app import sweep

    org = f"itest-cutoff-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    backend = await repo.lease_backend()
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
    assert (await backend.try_claim(tid, "dead-bot", expected_fence=0, lease_until=past)).won
    _only_strays_of(monkeypatch, tid)
    await sweep.reconcile_leases(_later(), limit=10_000)
    assert await _lease(ch, tid) == {"owner": "", "fence": 0}
    assert (await _claim(org, tid)).task.status == "in_progress"


@pytest.mark.asyncio
async def test_a_claim_racing_a_cancel_never_raises_a_keeper_error(ch: Any) -> None:
    """A claim and a cancel of one task at once: the cancel may delete the lease row
    under the claim's strict-mode write ("No node"). That is a lost race: the claim
    answers lost (409) or won, never a Keeper error."""
    raised = []
    for _ in range(30):
        org = f"itest-race-{uuid.uuid4().hex[:8]}"
        tid = await _todo_task(org)
        results = await asyncio.gather(
            _claim(org, tid),
            transitions.handle_task_cancel(_reason(org, tid)),
            return_exceptions=True,
        )
        raised += [r for r in results if isinstance(r, Exception) and "KEEPER" in str(r)]
    assert not raised, raised


@pytest.mark.asyncio
async def test_one_failing_lease_repair_does_not_stop_the_others(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two todo tasks lost their lease rows; writing the first one's fails: the second
    is still written."""
    from server.tasks.app import sweep

    org = f"itest-onebad-{uuid.uuid4().hex[:8]}"
    first, second = await _todo_task(org), await _todo_task(f"{org}-b")
    backend = await repo.lease_backend()
    await backend.delete(first)
    await backend.delete(second)
    real_ensure = backend.ensure_row

    async def ensure(task_id: str, *a: Any, **k: Any) -> bool:
        if task_id == first:
            raise RuntimeError("keeper down")
        return await real_ensure(task_id, *a, **k)

    monkeypatch.setattr(backend, "ensure_row", ensure)
    _only_strays_of(monkeypatch, first, second)
    await sweep.reconcile_leases(_later(), limit=10_000)
    monkeypatch.undo()
    assert await _lease(ch, first) is None
    assert await _lease(ch, second) == {"owner": "", "fence": 0}


@pytest.mark.asyncio
async def test_the_lease_reconcile_counts_only_the_rows_it_changed(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray row a claim took between the reconcile's read and its write is not a
    repair: the guarded write changes nothing, and the count says so."""
    from server.tasks.app import sweep

    org = f"itest-count-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    backend = await repo.lease_backend()
    await backend.give_back(tid, "", held_fence=0, fence=5)  # an idle row at another fence
    strays = repo.lease_strays

    async def only_this(**kw: Any) -> Any:
        missing, stray = await strays(**kw)
        await _claim_one_at(backend, tid, fence=5)  # a claim takes it after the read
        return [], [r for r in stray if r["task_id"] == tid]

    monkeypatch.setattr(repo, "lease_strays", only_this)
    assert await sweep.reconcile_leases(_later(), limit=10_000) == 0


async def _claim_one_at(backend: Any, tid: str, *, fence: int) -> None:
    from datetime import timedelta

    until = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    assert (await backend.try_claim(tid, "bot", expected_fence=fence, lease_until=until)).won


@pytest.mark.asyncio
async def test_a_reopen_and_a_claim_right_after_a_cancel_keep_the_claimants_lease(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after a cancel's status row, the task is reopened and claimed: the cancel
    writes nothing more, so the claimant keeps its lease and its heartbeat passes."""
    org = f"itest-enddrop-{uuid.uuid4().hex[:8]}"
    tid = await _todo_task(org)
    real = transitions._record
    claimed: dict[str, Any] = {}

    async def record(task: Any, **kw: Any) -> Any:
        out = await real(task, **kw)
        if kw.get("event") == "cancelled":
            monkeypatch.setattr(transitions, "_record", real)
            await transitions.handle_task_reopen(_reason(org, tid))
            claimed["task"] = (await _claim(org, tid)).task
        return out

    monkeypatch.setattr(transitions, "_record", record)
    await transitions.handle_task_cancel(_reason(org, tid))
    monkeypatch.undo()
    fence = claimed["task"].fence
    assert await _lease(ch, tid) == {"owner": "bot", "fence": fence}
    await transitions.handle_task_heartbeat(
        wire.TaskHeartbeatInput(org=org, namespace="private", agent="bot", task_id=tid, fence=fence)
    )


@pytest.mark.asyncio
async def test_the_lease_reconcile_repairs_no_more_rows_than_its_limit(ch: Any) -> None:
    """Three stray idle rows of cancelled tasks and a limit of one: one pass repairs one,
    and the next passes the rest."""
    from server.tasks.app import sweep

    await sweep.reconcile_leases(_later(), limit=10_000)  # what other tests left behind
    org = f"itest-reclimit-{uuid.uuid4().hex[:8]}"
    backend = await repo.lease_backend()
    tids = []
    for _ in range(3):
        tid = await _todo_task(org)
        await transitions.handle_task_cancel(_reason(org, tid))
        task = await repo.fetch_task(org, tid)
        await backend.ensure_row(tid, org, task["inbox"], int(task["fence"]))  # the stray row
        tids.append(tid)
    assert await sweep.reconcile_leases(_later(), limit=1) == 1
    assert [await _lease(ch, tid) is None for tid in tids].count(True) == 1
    assert await sweep.reconcile_leases(_later(), limit=10_000) >= 2
    assert [await _lease(ch, tid) for tid in tids] == [None, None, None]
