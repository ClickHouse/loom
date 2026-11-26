"""Exercise worker source history on ClickHouse without a live worker or credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from server.core.ch import CHClient, set_query_tag
from server.core.errors import OverloadedError
from server.erasure.app.connected_accounts import require_sources_erased
from server.knowledge.app.connected_accounts import account_lease
from server.knowledge.app.connected_restore import _metadata
from server.knowledge.app.sources import sources_lease
from server.knowledge.domain.sources import mirror_synced, served
from server.knowledge.store import sources
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")
NAMESPACE = "selected"
OWNER = "fixture-owner"
RAW_CONFIG = ' {"unknown": '
RAW_ITEMS = '[{"selected": 1}, '


@dataclass
class Registry:
    ch: CHClient
    org: str
    owned: list[tuple[str, str, str]] = field(default_factory=list)

    @asynccontextmanager
    async def locked(self, kind: str) -> AsyncIterator[None]:
        async with account_lease(self.org, OWNER, kind, ch=self.ch):
            async with sources_lease(self.org, NAMESPACE, ch=self.ch):
                yield

    async def seed(self, name: str, *, worker: bool) -> tuple[str, str, str]:
        source_id = str(uuid4())
        connection = uuid4()
        kind = f"mcp:{connection.hex}" if worker else "notion"
        self.owned.append((name, source_id, kind))
        row = {
            "org": self.org,
            "namespace": NAMESPACE,
            "name": name,
            "source_id": source_id,
            "kind": "",
            "source_kind": "mcp" if worker else kind,
            "credential_mode": "worker" if worker else "connected",
            "owner_subject": OWNER,
            "connection_id": str(connection) if worker else f"{connection.hex}/0{uuid4().hex[1:]}",
            "access": "mirrored",
            "config_json": RAW_CONFIG if worker else "{}",
            "items_json": RAW_ITEMS if worker else "[]",
            "created_by": OWNER,
            "active": False,
            "pending_delete": False,
        }
        async with self.locked(kind):
            await self.ch.insert("loom.knowledge_sources", [row])
        return source_id, kind, row["connection_id"]

    async def raw(self, name: str) -> dict[str, Any] | None:
        return await self.ch.query_one(
            "SELECT * FROM loom.knowledge_sources FINAL WHERE org = {org:String} "
            "AND namespace = {namespace:String} AND name = {name:String} LIMIT 1",
            params={"org": self.org, "namespace": NAMESPACE, "name": name},
        )

    async def cleanup(self) -> None:
        failures: list[Exception] = []
        for name, source_id, kind in self.owned:
            try:
                async with self.locked(kind):
                    await self.ch.execute(
                        "DELETE FROM loom.knowledge_sources WHERE org = {org:String} "
                        "AND namespace = {namespace:String} AND name = {name:String} "
                        "AND source_id = {source_id:UUID}",
                        params={
                            "org": self.org,
                            "namespace": NAMESPACE,
                            "name": name,
                            "source_id": source_id,
                        },
                        settings={"lightweight_deletes_sync": 2},
                    )
            except Exception as error:  # noqa: BLE001 — attempt cleanup for every owned identity
                failures.append(error)
        if failures:
            raise ExceptionGroup("Worker history fixture cleanup failed", failures)


@pytest.fixture
async def registry() -> AsyncIterator[Registry]:
    ch = CHClient()
    await ch.connect()
    state = Registry(ch, unique_org("mcp-worker-source-history"))
    try:
        yield state
    finally:
        try:
            set_query_tag("test.mcp-worker-source-history.cleanup")
            await state.cleanup()
        finally:
            await ch.disconnect()


async def test_worker_source_payload_and_slot_erasure(registry: Registry) -> None:
    set_query_tag("test.mcp-worker-source-history")
    source_id, slot, connection = await registry.seed("target", worker=True)
    await registry.seed("worker-neighbor", worker=True)
    await registry.seed("native-neighbor", worker=False)
    neighbors = ["worker-neighbor", "native-neighbor"]
    snapshots = [await registry.raw(name) for name in neighbors]
    assert all(snapshot is not None for snapshot in snapshots)
    owner = (registry.org, OWNER, slot)
    args = (registry.org, NAMESPACE, OWNER, slot)

    async with registry.locked(slot):
        source = await sources.latest_source(registry.org, NAMESPACE, "target", ch=registry.ch)
        assert source is not None and source.source_id == source_id
        assert not served(source) and not mirror_synced(source)
        await sources.insert_source(source, ch=registry.ch)
        raw = await registry.raw("target")
        assert raw is not None
        assert (raw["config_json"], raw["items_json"]) == (RAW_CONFIG, RAW_ITEMS)
        assert not raw["credential_enc"] and not raw["secret_enc"] and not raw["active"]
        assert await sources.connected_owner_namespaces(*owner, ch=registry.ch) == [NAMESPACE]
        keys, history = await _metadata(registry.ch, owner=owner)
        assert keys == [(*owner, connection)]
        assert [item.source_id for item in history] == [source_id]
        with pytest.raises(OverloadedError, match="cleanup is incomplete"):
            await require_sources_erased(*args, ch=registry.ch)
        await sources.erase_connected_owner(*args, ch=registry.ch)
        await require_sources_erased(*args, ch=registry.ch)

    assert await registry.raw("target") is None
    assert [await registry.raw(name) for name in neighbors] == snapshots
