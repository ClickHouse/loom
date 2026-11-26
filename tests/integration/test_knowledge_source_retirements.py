"""Durable stopped-source cleanup and erasure against the task's real stores."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import uuid

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


async def test_replaced_source_cleanup_keeps_the_new_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg
    from server.core.ch import get_client
    from server.core.naming import new_mirror_name
    from server.knowledge.app import source_retirements as cleanup
    from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, source_id_tag
    from server.knowledge.store import documents, sources
    from server.knowledge.store import source_retirements as queue
    from server.settings import settings

    ch = get_client()
    org, mirror = unique_org("source-retire"), new_mirror_name()
    old = queue.Retirement(org, "team", "decks", str(uuid.uuid4()), "gdrive", mirror)
    new = dataclasses.replace(old, source_id=str(uuid.uuid4()))
    try:
        await ch.connect()
        await queue.write(old, pending=True)
        await sources.insert_source(old.source())
        await sources.insert_source(
            dataclasses.replace(new.source(), active=True, pending_delete=False)
        )
        for item in (old, new):
            await ch.insert(
                "loom.documents",
                [
                    {
                        "org": org,
                        "namespace": mirror,
                        "source_uri": "https://docs.example.com/d/one#" + item.source_id,
                        "content": "Private copy.",
                        "actor": SYNC_WORKER_ACTOR,
                        "tags": ["source:decks", source_id_tag(item.source_id)],
                    }
                ],
            )
        new_uri = "https://docs.example.com/d/one#" + new.source_id
        before = await documents.get_one(org, mirror, new_uri)
        assert before is not None
        monkeypatch.setattr(settings, "knowledge_source_sync_enabled", False)
        monkeypatch.setattr(settings, "auth_required", False)
        await cleanup.run_once()
        assert await sources.count_documents(org, mirror, source_id_tag(old.source_id)) == 0
        after = await documents.get_one(org, mirror, new_uri)
        assert after is not None and after.model_dump() == before.model_dump()
        assert not await queue.pending(old)
        latest = await sources.latest_source(org, "team", "decks")
        assert latest is not None and latest.source_id == new.source_id and latest.active
    finally:
        await ch.disconnect()
        if pg.enabled():
            await pg.close_pool()


async def test_queue_roundtrip_cursor_tenant_isolation_and_erasure() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg
    from server.core.ch import get_client
    from server.core.naming import new_mirror_name
    from server.erasure.app.saga_steps import _step_purge_code_index_by_ns
    from server.knowledge.store import source_retirements as queue
    from server.knowledge.store import sources

    ch = get_client()
    source_id = str(uuid.uuid4())
    first = queue.Retirement(
        unique_org("retire-a"), "team", "decks", source_id, "gdrive", new_mirror_name()
    )
    other = dataclasses.replace(first, org=unique_org("retire-b"), mirror=new_mirror_name())
    try:
        await ch.connect()
        for item in (first, other):
            await queue.write(item, pending=True)
        await sources.write_state(
            first.org,
            first.mirror,
            first.source_id,
            state_value="",
            detail={"error": "Private turn error", "changed": 47},
        )
        assert await queue.pending(first)
        page = [row for row in await queue.page(("", "")) if row["org"] in {first.org, other.org}]

        def org_cursor(item: queue.Retirement) -> str:
            return hashlib.sha256(item.org.encode()).hexdigest().upper()

        ordered = sorted((first, other), key=org_cursor)
        assert [queue.from_row(row) for row in page] == ordered
        assert [
            queue.from_row(row) for row in await queue.page((first.key, org_cursor(ordered[0])))
        ] == [ordered[1]]
        raw = await ch.query_one(
            "SELECT org, namespace, state_key, state_value, detail FROM loom.code_index_state FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND state_key = {key:String}",
            {"org": first.org, "ns": first.mirror, "key": first.key},
        )
        assert raw is not None and raw["state_value"] == "pending"
        assert queue.from_row(dict(raw)) == first
        assert (raw["org"], raw["namespace"], raw["state_key"]) == (
            first.org,
            first.mirror,
            first.key,
        )
        await queue.write(first, pending=False)
        assert not await queue.pending(first) and await queue.pending(other)
        state = await sources.read_states(first.org, first.mirror, [first.source_id])
        assert state[first.source_id]["detail"] == '{"changed": 47, "error": "Private turn error"}'
        await _step_purge_code_index_by_ns(other.org, other.mirror)
        assert not await queue.pending(other)
    finally:
        await ch.disconnect()
        if pg.enabled():
            await pg.close_pool()
