"""Stopped mirrored sources queue deletion independently of their replaceable names."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from server.knowledge.api.models_sources import KnowledgeSourceRemoveInput
from server.knowledge.app import source_retirements as cleanup
from server.knowledge.app import sources
from server.knowledge.domain.sources import MIRRORED_ACCESS
from server.knowledge.store import source_retirements as queue
from server.knowledge.store import sources as store
from tests.unit.test_knowledge_sources import NS, ORG, _add
from tests.unit.test_knowledge_sources import fake as fake


@pytest.mark.parametrize("delete_documents", [False, True])
def test_mirrored_remove_always_queues_without_counting(
    fake: Any, monkeypatch: pytest.MonkeyPatch, delete_documents: bool
) -> None:
    asyncio.run(sources.handle_knowledge_source_add(_add()))
    key = (ORG, NS, "design-decks")
    fake.rows[key] = dataclasses.replace(fake.rows[key], access=MIRRORED_ACCESS)
    queued = []

    async def enqueue(src: Any) -> bool:
        assert fake.rows[key].active  # durable request precedes inactivation
        queued.append(src.source_id)
        return True

    async def forbidden(*args: Any, **kw: Any) -> int:
        raise AssertionError("a mirrored remove must not count documents")

    monkeypatch.setattr(sources, "_queue_retirement", enqueue, raising=False)
    monkeypatch.setattr(store, "count_documents", forbidden)
    out = asyncio.run(
        sources.handle_knowledge_source_remove(
            KnowledgeSourceRemoveInput(
                org=ORG,
                namespace=NS,
                name="design-decks",
                confirm="design-decks",
                delete_documents=delete_documents,
            )
        )
    )
    assert out.documents_pending == 0
    assert queued == [fake.rows[key].source_id]
    assert not fake.rows[key].active and fake.rows[key].pending_delete
    assert fake.rows[key].credential_enc == fake.rows[key].secret_enc == ""


def test_namespace_retirement_queues_mirrors_before_inactivation(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(sources.handle_knowledge_source_add(_add()))
    key = (ORG, NS, "design-decks")
    fake.rows[key] = dataclasses.replace(fake.rows[key], access=MIRRORED_ACCESS)
    queued = []

    async def enqueue(src: Any) -> bool:
        assert fake.rows[key].active
        queued.append(src.source_id)
        return True

    monkeypatch.setattr(sources, "_queue_retirement", enqueue, raising=False)
    assert asyncio.run(sources.deactivate_namespace_sources(ORG, NS)) == ["design-decks"]
    assert queued == [fake.rows[key].source_id]
    assert fake.rows[key].pending_delete


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> Any:
    world = SimpleNamespace(
        items=[],
        pending=set(),
        current={},
        docs={},
        retired=[],
        owed={},
        cursor="",
        failures=[],
        busy=set(),
        cleared=[],
        identities=[],
    )

    def row(item: queue.Retirement) -> dict[str, Any]:
        return {
            "org": item.org,
            "namespace": item.mirror,
            "state_key": item.key,
            "org_cursor": hashlib.sha256(item.org.encode()).hexdigest().upper(),
            "detail": json.dumps(
                {
                    "namespace": item.namespace,
                    "name": item.name,
                    "source_id": item.source_id,
                    "kind": item.kind,
                }
            ),
        }

    async def page(after: tuple[str, str]) -> Any:
        rows = [row(i) for i in world.items if i.source_id in world.pending]
        return sorted(
            [r for r in rows if (r["state_key"], r["org_cursor"]) > after],
            key=lambda r: (r["state_key"], r["org_cursor"]),
        )[: queue.PAGE_SIZE]

    async def pending(item: queue.Retirement) -> bool:
        return item.source_id in world.pending

    async def write(item: queue.Retirement, *, pending: bool) -> None:
        assert not pending
        world.pending.remove(item.source_id)
        world.cleared.append(item.source_id)

    async def latest(org: str, ns: str, name: str) -> Any:
        return world.current.get((org, ns, name))

    async def docs(org: str, ns: str, tag: str) -> Any:
        return [{"source_uri": uri} for uri in world.docs.get((org, ns, tag), [])]

    async def count(org: str, ns: str, tag: str) -> int:
        return len(await docs(org, ns, tag))

    async def retire(src: Any, uri: str) -> bool:
        key = (src.org, src.mirror, "source-id:" + src.source_id)
        world.docs[key].remove(uri)
        world.retired.append((key, uri))
        return True

    async def owed(org: str, ns: str, suffix: str) -> str:
        return world.owed.get(suffix.removeprefix("#"), "")

    async def insert(src: Any) -> None:
        world.current[(src.org, src.namespace, src.name)] = src

    @contextlib.asynccontextmanager
    async def lease(name: str, **kw: Any) -> Any:
        if name in world.busy:
            raise cleanup.leases.LeaseBusy(name)
        yield None

    @contextlib.asynccontextmanager
    async def sources_lease(*args: Any, **kw: Any) -> Any:
        yield None

    async def no_erasure(*args: Any) -> None:
        pass

    monkeypatch.setattr(queue, "page", page)
    monkeypatch.setattr(queue, "pending", pending)
    monkeypatch.setattr(queue, "write", write)
    monkeypatch.setattr(cleanup.store, "latest_source", latest)
    monkeypatch.setattr(cleanup.store, "source_documents", docs)
    monkeypatch.setattr(cleanup.store, "count_documents", count)
    monkeypatch.setattr(cleanup.store, "insert_source", insert)
    monkeypatch.setattr(cleanup.documents, "publications_owed", owed)
    monkeypatch.setattr(cleanup.source_sync, "retire", retire)
    monkeypatch.setattr(cleanup.leases, "cluster_lease", lease)
    monkeypatch.setattr(sources, "sources_lease", sources_lease)
    monkeypatch.setattr(sources, "_require_no_erasure", no_erasure)
    monkeypatch.setattr(cleanup.pass_context, "resume_cursor", lambda: world.cursor)
    monkeypatch.setattr(cleanup.pass_context, "set_cursor", lambda s: setattr(world, "cursor", s))
    monkeypatch.setattr(
        cleanup.pass_context, "note_failure", lambda e, what: world.failures.append(str(e))
    )
    monkeypatch.setattr(
        cleanup, "set_worker_identity", lambda *a, **kw: world.identities.append((a, kw))
    )
    return world


def _item(n: int = 1, **kw: Any) -> queue.Retirement:
    return queue.Retirement(
        org=kw.get("org", "acme"),
        namespace="team",
        name=kw.get("name", "decks"),
        source_id=str(UUID(int=n)),
        kind="gdrive",
        mirror=kw.get("mirror", "acl-" + "a" * 24),
    )


def _put(work: Any, item: queue.Retirement, *uris: str) -> None:
    work.items.append(item)
    work.pending.add(item.source_id)
    work.docs[(item.org, item.mirror, "source-id:" + item.source_id)] = list(uris)


def test_replacement_preserves_old_cleanup_and_new_documents(work: Any) -> None:
    old, new = _item(), _item(2)
    _put(work, old, "old-address#old-id")
    work.current[(new.org, new.namespace, new.name)] = dataclasses.replace(
        new.source(), active=True
    )
    new_key = (new.org, new.mirror, "source-id:" + new.source_id)
    work.docs[new_key] = ["same-address#new-id"]
    asyncio.run(cleanup.run_once())
    assert work.docs[new_key] == ["same-address#new-id"]
    assert work.cleared == [old.source_id]
    assert work.current[(new.org, new.namespace, new.name)].active
    assert work.identities == [((cleanup.WORKER,), {"org": old.org, "namespace": old.mirror})]


def test_crash_before_inactivation_never_deletes_active_source(work: Any) -> None:
    item = _item()
    _put(work, item, "private-address")
    work.current[(item.org, item.namespace, item.name)] = dataclasses.replace(
        item.source(), active=True
    )
    asyncio.run(cleanup.run_once())
    assert work.retired == [] and work.pending == {item.source_id}


@pytest.mark.parametrize("owed", ["owed", "parked"])
def test_zero_clickhouse_documents_cannot_clear_pending_publication(work: Any, owed: str) -> None:
    item = _item()
    _put(work, item)
    work.owed[item.source_id] = owed
    asyncio.run(cleanup.run_once())
    assert work.pending == {item.source_id}
    assert work.failures == ["SYNC_FAILED"]
    work.owed.clear()
    asyncio.run(cleanup.run_once())
    assert work.cleared == [item.source_id]


def test_independent_bound_rotates_to_next_stopped_source(
    work: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _item(), _item(2, name="notes")
    _put(work, first, "one", "two")
    _put(work, second, "three")
    monkeypatch.setattr(cleanup.settings, "knowledge_source_retire_documents_per_pass", 1)
    asyncio.run(cleanup.run_once())
    assert [uri for _, uri in work.retired] == ["one"]
    assert "acl-" not in work.cursor
    asyncio.run(cleanup.run_once())
    assert [uri for _, uri in work.retired] == ["one", "three"]
    assert first.source_id in work.pending and second.source_id not in work.pending


def test_long_org_cannot_overflow_the_persistent_cursor(
    work: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _item(org="x" * 2048), _item(2, name="notes")
    _put(work, first, "one", "two")
    _put(work, second, "three")
    monkeypatch.setattr(cleanup.settings, "knowledge_source_retire_documents_per_pass", 1)
    asyncio.run(cleanup.run_once())
    assert len(work.cursor) < cleanup.pass_context.MAX_CURSOR
    assert first.org not in work.cursor
    asyncio.run(cleanup.run_once())
    assert [uri for _, uri in work.retired] == ["one", "three"]


def test_a_page_of_active_markers_cannot_starve_a_stopped_source(work: Any) -> None:
    for n in range(1, queue.PAGE_SIZE + 1):
        item = _item(n, name=f"source-{n}")
        _put(work, item, "kept")
        work.current[(item.org, item.namespace, item.name)] = dataclasses.replace(
            item.source(), active=True
        )
    stopped = _item(queue.PAGE_SIZE + 1, name="stopped")
    _put(work, stopped, "removed")
    asyncio.run(cleanup.run_once())
    assert work.retired == [] and work.cursor
    asyncio.run(cleanup.run_once())
    assert [uri for _, uri in work.retired] == ["removed"]
    assert stopped.source_id not in work.pending


def test_busy_source_does_not_hold_back_another_source(work: Any) -> None:
    first, second = _item(), _item(2, name="notes")
    _put(work, first, "kept")
    _put(work, second, "removed")
    work.busy.add(cleanup.turn_lease_name(first.org, first.namespace, first.name))
    asyncio.run(cleanup.run_once())
    assert [uri for _, uri in work.retired] == ["removed"]
    assert work.pending == {first.source_id}


def test_failed_source_reports_only_a_code_and_other_cleanup_continues(
    work: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = _item(), _item(2, name="notes")
    _put(work, first, "private-file-a")
    _put(work, second, "removed")
    retire = cleanup.source_sync.retire

    async def fail(src: Any, uri: str) -> bool:
        if src.source_id == first.source_id:
            raise ValueError("private-file-a")
        return bool(await retire(src, uri))

    monkeypatch.setattr(cleanup.source_sync, "retire", fail)
    asyncio.run(cleanup.run_once())
    assert work.failures == ["SYNC_FAILED"] and "private-file-a" not in caplog.text
    assert [uri for _, uri in work.retired] == ["removed"]
    assert work.pending == {first.source_id}


@pytest.mark.parametrize("fence_at", [1, 2])
def test_erasure_fence_prevents_queue_resurrection(
    work: Any, monkeypatch: pytest.MonkeyPatch, fence_at: int
) -> None:
    item = _item()
    _put(work, item)
    calls = []

    async def fenced(*args: Any) -> None:
        calls.append(args)
        if len(calls) == fence_at:
            raise RuntimeError("private backend detail")

    monkeypatch.setattr(sources, "_require_no_erasure", fenced)
    asyncio.run(cleanup.run_once())
    assert work.cleared == [] and work.failures == ["SYNC_FAILED"]


def test_failed_enqueue_keeps_source_active(fake: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(sources.handle_knowledge_source_add(_add()))
    key = (ORG, NS, "design-decks")
    fake.rows[key] = dataclasses.replace(fake.rows[key], access=MIRRORED_ACCESS)
    before = fake.rows[key]

    async def failed(src: Any) -> bool:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(sources, "_queue_retirement", failed)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        asyncio.run(
            sources.handle_knowledge_source_remove(
                KnowledgeSourceRemoveInput(
                    org=ORG,
                    namespace=NS,
                    name="design-decks",
                    confirm="design-decks",
                )
            )
        )
    assert fake.rows[key] == before


@pytest.mark.parametrize("wrap", ["empty", "short"])
def test_cursor_wrap_survives_the_actual_supervisor_handoff(
    work: Any, monkeypatch: pytest.MonkeyPatch, wrap: str
) -> None:
    from server.workers import pass_context, supervisor

    state = supervisor.LoopState(cursor=json.dumps([_item().key, "F" * 64]))
    if wrap == "short":
        active = _item(2)
        _put(work, active, "kept")
        work.current[(active.org, active.namespace, active.name)] = dataclasses.replace(
            active.source(),
            active=True,
        )
    saved = []
    monkeypatch.setattr(supervisor.status_store, "record_pass", lambda name, **kw: saved.append(kw))
    monkeypatch.setattr(pass_context, "resume_cursor", lambda: state.cursor)

    async def run() -> None:
        context = pass_context.PassContext(worker=cleanup.WORKER)
        monkeypatch.setattr(
            pass_context, "set_cursor", lambda value: setattr(context, "cursor", value)
        )
        await cleanup.run_once()
        supervisor._record_status_row(
            cleanup.WORKER,
            started=cleanup.time.monotonic(),
            success=True,
            partial=context,
            state=state,
        )

    asyncio.run(run())
    assert state.cursor == json.dumps(["", ""])
    assert saved[-1]["resume_cursor"] == state.cursor
    stopped = _item(0, name="notes")
    _put(work, stopped, "removed")
    asyncio.run(run())
    assert [uri for _, uri in work.retired] == ["removed"]


@pytest.mark.parametrize("sync_enabled", [False, True])
@pytest.mark.parametrize("auth_required", [False, True])
def test_retirement_worker_runs_independently_of_content_sync_and_auth(
    monkeypatch: pytest.MonkeyPatch, sync_enabled: bool, auth_required: bool
) -> None:
    from server.knowledge.workers import source_retirements
    from server.workers import registry

    monkeypatch.setattr(cleanup.settings, "knowledge_source_sync_enabled", sync_enabled)
    monkeypatch.setattr(cleanup.settings, "auth_required", auth_required)
    worker = next(w for w in registry.enabled_workers() if w.name == "knowledge_source_retire")
    assert worker.pass_callable is source_retirements.run_pass
    assert worker.deadline_seconds() == 60
    assert worker.interval_seconds == cleanup.settings.knowledge_source_sync_interval_seconds
    assert (
        worker.idle_interval_seconds == cleanup.settings.knowledge_source_sync_idle_interval_seconds
    )
