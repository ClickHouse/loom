"""Real-store scale seeds and lazy source turns; registration throughput is excluded."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from server.clients import mcp_source
from server.core.ch import get_client, set_query_tag
from server.knowledge.app import mcp_connections, mcp_profiles, source_sync
from server.knowledge.app.mcp_source_schedule import Limits
from server.knowledge.store import sources as source_store
from server.knowledge.store.mcp_source_schedule import Header, QueryLimits
from server.settings import settings
from tests.integration._resources import unique_org
from tests.integration.mcp_source_qualification_fixtures import CatalogInput, World, reader
from tests.integration.mcp_source_qualification_servers import SELECTED, SourceTransport
from tests.integration.mcp_source_scale_servers import ROLES, ScaleSource


def operator_limits() -> Limits:
    return Limits(
        page_size=settings.knowledge_source_mcp_schedule_page_size,
        scan_pages=settings.knowledge_source_mcp_schedule_scan_pages,
        ready=settings.knowledge_source_mcp_schedule_ready,
        global_inflight=settings.knowledge_source_mcp_schedule_global,
        per_org=settings.knowledge_source_mcp_schedule_per_org,
        per_endpoint=settings.knowledge_source_mcp_schedule_per_endpoint,
        documents=settings.knowledge_source_mcp_schedule_documents,
        source_seconds=settings.knowledge_source_mcp_schedule_source_seconds,
    )


def query_limits() -> QueryLimits:
    return QueryLimits(
        settings.knowledge_source_mcp_schedule_query_rows,
        settings.knowledge_source_mcp_schedule_query_bytes,
        settings.knowledge_source_mcp_schedule_query_seconds,
    )


def inject(patch: pytest.MonkeyPatch, sources: list[ScaleSource], catalog: CatalogInput) -> None:
    real_init = mcp_source.MCPSourceClient.__init__
    by_url = {source.endpoint: source for source in sources}

    def initialized(self: Any, *args: Any, **kwargs: Any) -> None:
        assert "_transport" not in kwargs
        real_init(self, *args, **kwargs, _transport=SourceTransport(sources))

    async def pin(url: str, **kwargs: Any) -> tuple[str, str, str, str]:
        source = by_url[url]
        assert kwargs == {"schemes": ("https",)}
        return "", "https://203.0.113.8/mcp", source.host, source.host

    patch.setattr(mcp_source.MCPSourceClient, "__init__", initialized)
    patch.setattr(mcp_source, "pinned_target", pin)
    patch.setattr(mcp_profiles, "load_mcp_profile_catalog", lambda: catalog.current)
    patch.setattr(mcp_connections, "load_mcp_profile_catalog", lambda: catalog.current)


@dataclass
class ScaleWorld:
    stack: AsyncExitStack
    catalog: CatalogInput
    sources: list[ScaleSource]
    orgs: list[str]
    eligible: dict[str, World] = field(default_factory=dict)
    live: dict[str, World] = field(default_factory=dict)
    owners: list[Any] = field(default_factory=list)
    catalog_scopes: list[tuple[Any, str]] = field(default_factory=list)
    healthy_done: set[str] = field(default_factory=set)
    attempted: set[str] = field(default_factory=set)
    first_tenant: dict[str, tuple[float, int]] = field(default_factory=dict)
    live_latencies: list[float] = field(default_factory=list)
    refresh_latencies: list[float] = field(default_factory=list)
    failed_turns: int = 0
    original_readbacks: int = 0
    started: float = 0
    round: int = 1

    def endpoint_key(self, header: Header) -> str:
        profile = self.catalog.current.get(header.org, header.profile_id)
        if profile.metadata.profile_sha256 != header.profile_sha256:
            raise ValueError("scale_profile_changed")
        endpoint = str(httpx.URL(json.loads(profile.config_snapshot)["config"]["endpoint"]))
        return hashlib.sha256(endpoint.encode()).hexdigest()

    async def seed_owner(self, app: Any, base: Path, org: str, user: int) -> None:
        owner = await self.stack.enter_async_context(
            reader(app, base, org, f"owner{user}@example.test")
        )
        self.owners.append(owner)
        namespace = "col-scale-" + uuid4().hex
        await owner.call("collection.create", namespace=namespace)
        self.catalog_scopes.append((owner, namespace))
        for source in self.sources:
            set_query_tag("worker.knowledge_source_sync")
            connected = await owner.call(
                "knowledge.connection.mcp_connect",
                profile_id=source.profile([org])["id"],
                request_id=str(uuid4()),
                bearer=source.bearer,
            )
            template = World(
                owner,
                owner,
                owner,
                source,
                namespace,
                namespace,
                namespace,
                self.catalog,
                slot=connected["connection"]["slot"],
            )
            await template.add()
            await self.seed_sources(template)

    async def seed_sources(self, template: World) -> None:
        source = await template.source_row()
        original = json.loads(source.generic_config_json)
        live: World | None = None
        selected: list[World] = []
        for index in range(50):
            set_query_tag("worker.knowledge_source_sync")
            identity = source.source_id if index == 0 else str(uuid4())
            config = dict(original)
            if index >= 5:
                config["mode"] = "live" if index % 2 else "selected_refresh"
                config["paused"] = index % 2 == 0
            if index == 49:
                config["version"] = 2
            row = replace(
                source,
                source_id=identity,
                name=source.name if index == 0 else "mcp-" + identity.replace("-", ""),
                source_contract_version=2 if index == 49 else 1,
                generic_config_json=json.dumps(config, separators=(",", ":")),
            )
            if index:
                await source_store.insert_source(row)
            case = replace(
                template,
                entry={"source_id": identity, "name": row.name, "namespace": row.namespace},
            )
            if index < 5:
                selected.append(case)
                self.eligible[identity] = case
            elif index == 5:
                live = case
        assert live is not None
        for case in selected:
            self.live[case.entry["source_id"]] = live

    async def counts(self) -> dict[str, int]:
        set_query_tag("worker.knowledge_source_sync")
        from server.knowledge.store.mcp_source_schedule import _ELIGIBLE

        row = await get_client().query_one(
            "SELECT count() AS registered, uniqExact(source_id) AS registered_distinct, "
            f"countIf({_ELIGIBLE}) AS eligible, "
            f"uniqExactIf(source_id, {_ELIGIBLE}) AS eligible_distinct, "
            "uniqExact(org) AS orgs, uniqExact(tuple(org, owner_subject)) AS users "
            "FROM loom.knowledge_sources FINAL WHERE org IN {orgs:Array(String)}",
            params={"orgs": self.orgs},
        )
        assert row is not None
        return {key: int(value) for key, value in row.items()}

    async def execute(self, header: Header, run: source_sync.Pass) -> None:
        self.attempted.add(header.source_id)
        case = self.eligible[header.source_id]
        assert (header.org, header.namespace, header.name) == (
            case.owner.org,
            case.namespace,
            case.entry["name"],
        )
        set_query_tag("worker.knowledge_source_sync")
        row = await case.source_row()
        states = await source_sync._states([row])
        state = states.get(source_sync._cursor_of(row), {})
        refresh_started = time.monotonic()
        turn = await source_sync._sync_one(row, state, run, source_sync.PassResult())
        assert turn is not None and turn.status == "ok" and turn.complete
        refresh_seconds = time.monotonic() - refresh_started
        if case.source.role != "healthy":
            raise AssertionError("fault_fixture_unexpected_success")
        if header.source_id not in self.healthy_done:
            await self.verify_copy(case)
            live_seconds = await self.read_live(header.source_id)
            self.healthy_done.add(header.source_id)
            self.live_latencies.append(live_seconds)
            self.refresh_latencies.append(refresh_seconds)
            self.first_tenant.setdefault(header.org, (time.monotonic() - self.started, self.round))

    async def read_live(self, source_id: str) -> float:
        live = self.live[source_id]
        started = time.monotonic()
        result = await live.owner.call(
            "knowledge.source.mcp_read",
            namespace=live.namespace,
            name=live.entry["name"],
            resource_id=SELECTED,
        )
        assert result["text"] == live.source.text and len(result["text"].encode()) == 8192
        assert result["source_id"] == live.entry["source_id"]
        return time.monotonic() - started

    async def verify_copy(self, case: World) -> None:
        head = await case.head()
        assert head.content == case.source.text and head.revision == 1
        chunks = await case.chunks()
        assert chunks and all(len(chunk["embedding"]) == 1024 for chunk in chunks)
        original = await case.download()
        assert original.status_code == 200 and original.headers["x-loom-download-source"] == "blob"
        assert original.content == case.source.text.encode()
        self.original_readbacks += 1


async def setup_world(stack: AsyncExitStack, patch: pytest.MonkeyPatch) -> ScaleWorld:
    orgs = [unique_org("mcp-scale") for _ in range(10)]
    sources = [
        ScaleSource(kind, json_response=kind == "resources", role=role)
        for kind in ("resources", "tools")
        for role in ROLES
    ]
    catalog = CatalogInput(
        mcp_profiles.MCPProfileCatalog.parse(
            json.dumps({"version": 1, "profiles": [source.profile(orgs) for source in sources]})
        )
    )
    inject(patch, sources, catalog)
    for source in sources:
        await stack.enter_async_context(source.application())
    return ScaleWorld(stack, catalog, sources, orgs)


async def catalog_latencies(world: ScaleWorld) -> list[float]:
    values: list[float] = []
    # The actual owner API hydrates only its existing 20-row page, without a fixture list substitute.
    for owner, namespace in world.catalog_scopes:
        cursor = None
        found: set[str] = set()
        for _ in range(26):
            set_query_tag("worker.knowledge_source_sync")
            start = time.monotonic()
            page = await owner.call(
                "knowledge.source.mcp_list", namespace=namespace, limit=20, cursor=cursor
            )
            values.append(time.monotonic() - start)
            assert len(page["sources"]) <= 20
            for source in page["sources"]:
                assert source["source_id"] not in found
                found.add(source["source_id"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            raise AssertionError("scale_catalog_did_not_terminate")
        assert len(found) == 500
    return values
