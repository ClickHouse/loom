"""Real application and store fixtures for the hosted finite MCP qualification."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from loom.session_protection import SessionProtection
from loom.transport import Transport

from server.auth.jwt import mint_token
from server.clients import mcp_source
from server.core.ch import get_client, set_query_tag
from server.embedding.embedder import get_embedder
from server.knowledge.app import mcp_connections, mcp_profiles, source_sync
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.store import documents
from server.knowledge.store import sources as source_store
from tests.integration._resources import unique_org
from tests.integration.mcp_source_qualification_servers import (
    SECOND,
    SELECTED,
    SourceTransport,
    SyntheticSource,
)


@dataclass
class Reader:
    org: str
    subject: str
    transport: Transport
    protection: SessionProtection

    async def call(self, op: str, **body: Any) -> dict[str, Any]:
        set_query_tag("test.mcp_source_qualification.api")
        return await self.transport.post("/v1/" + op, {"org": self.org, **body})


@asynccontextmanager
async def reader(app: Any, base: Path, org: str, subject: str) -> AsyncIterator[Reader]:
    protection = SessionProtection.create(base=base)
    token = mint_token(
        subject,
        org,
        ["*"],
        ["admin"],
        ttl_seconds=1800,
        is_service_account=False,
        session_id=str(uuid4()),
    )
    transport = Transport(
        "https://loom-qualification.example",
        api_key=token,
        pacing=False,
        retry={"max_attempts": 1},
        session_protection=protection,
    )
    # Preserve every SDK header and hook when replacing the HTTP connection.
    original = transport._client
    await original.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=transport.base_url,
        headers=original.headers,
        auth=original.auth,
        timeout=60,
        event_hooks=original.event_hooks,
    )
    try:
        yield Reader(org, subject, transport, protection)
    finally:
        await transport._client.aclose()


@pytest_asyncio.fixture
async def qualification_app() -> AsyncIterator[Any]:
    # These checks run before importing the application or connecting to a store.
    if not os.environ.get("LOOM_MCP_SOURCE_QUALIFICATION"):
        pytest.skip("requires the owned hosted MCP qualification stores")
    assert os.environ.get("LOOM_MCP_SOURCE_QUALIFICATION") == "github-owned"
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    assert os.environ.get("LOOM_CH_HOST") == "127.0.0.1"
    from server.main import app
    from server.settings import settings

    assert settings.auth_actually_required()
    assert settings.background_workers == "off"
    assert settings.llm_provider == "fake"
    assert settings.embedding_model == "fake-ngram-1024"
    assert not settings.llm_fallback_model.strip()
    assert settings.document_object_store == "s3"
    await asyncio.to_thread(require_source_checkout)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://loom-qualification.example"
        ) as client:
            health = await client.get("/v1/health")
            assert health.status_code == 200
            status = health.json()
            assert status["readiness"]["ready"] is True
            assert status["fake_embedding"] is status["fake_llm"] is True
            assert status["build_commit"] == os.environ["QUALIFICATION_SHA"]
            denied = await client.post("/v1/knowledge.get", json={})
            assert denied.status_code == 401
        yield app


def require_source_checkout() -> None:
    import server

    assert Path(server.__file__).resolve().parent == Path.cwd() / "server"


@dataclass
class CatalogInput:
    current: mcp_profiles.MCPProfileCatalog


@dataclass
class World:
    owner: Reader
    other: Reader
    foreign: Reader
    source: SyntheticSource
    namespace: str
    team: str
    child: str
    catalog: CatalogInput
    embed_calls: list[int] = field(default_factory=list)
    slot: str = ""
    entry: dict[str, Any] = field(default_factory=dict)
    private_marker: str = ""

    def __post_init__(self) -> None:
        if not self.private_marker:
            self.private_marker = self.source.marker

    @property
    def uri(self) -> str:
        return resource_uri(self.entry["source_id"], SELECTED)

    async def add(
        self,
        *,
        mode: str = "selected_refresh",
        copy: bool = False,
        namespace: str | None = None,
        ids: list[str] | None = None,
        confirm: bool | None = None,
    ) -> dict[str, Any]:
        reads = list(self.source.reads)
        output = await self.owner.call(
            "knowledge.source.mcp_add",
            namespace=namespace or self.namespace,
            slot=self.slot,
            mode=mode,
            reader_policy="collection_copy" if copy else "owner_only",
            resource_ids=ids or [SELECTED],
            request_id=str(uuid4()),
            confirm_copy_authority=copy if confirm is None else confirm,
        )
        self.entry = output["source"]
        assert self.source.reads == reads
        for table in ("documents", "chunks"):
            set_query_tag("test.mcp_source_qualification.registration")
            assert await get_client().query_one(
                f"SELECT count() AS count FROM loom.{table} WHERE org = {{org:String}} "
                "AND source_uri = {uri:String}",
                params={"org": self.owner.org, "uri": self.uri},
            ) == {"count": 0}
        return output

    async def source_row(self) -> Any:
        row = await source_store.get_source(
            self.owner.org, self.entry["namespace"], self.entry["name"]
        )
        assert row is not None
        assert row.config == {} and row.items == []
        assert row.kind == "mcp" and row.source_id == self.entry["source_id"]
        return row

    async def sync(self) -> Any:
        row = await self.source_row()
        states = await source_sync._states([row])
        state = states.get(source_sync._cursor_of(row), {})
        result = await source_sync._sync_one(
            row, state, source_sync.Pass(source_sync._budget(), left=8), source_sync.PassResult()
        )
        assert result is not None
        return result

    async def head(self, identity: str = SELECTED) -> Any:
        row = await self.source_row()
        assert row.mirror
        uri = resource_uri(row.source_id, identity)
        found = await documents.get_many_by_uris(row.org, row.mirror, [uri])
        assert len(found) == 1
        return found[0]

    async def chunks(self) -> list[dict[str, Any]]:
        row = await self.source_row()
        return await get_client().query_all(
            "SELECT chunk_idx, content, embedding_model, embedding_dim, embedding, doc_version "
            "FROM loom.chunks WHERE org = {org:String} AND namespace = {namespace:String} "
            "AND source_uri = {uri:String} ORDER BY doc_version, chunk_idx LIMIT 64",
            params={"org": row.org, "namespace": row.mirror, "uri": self.uri},
        )

    async def control(self, action: str, **body: Any) -> dict[str, Any]:
        result = await self.owner.call(
            "knowledge.source.mcp_" + action,
            namespace=self.entry["namespace"],
            name=self.entry["name"],
            **body,
        )
        if "source" in result:
            self.entry = result["source"]
        return result

    async def get(self, who: Reader | None = None, **body: Any) -> dict[str, Any]:
        return await (who or self.owner).call(
            "knowledge.get", namespace=self.entry["namespace"], source_uri=self.uri, **body
        )

    async def download(self, who: Reader | None = None) -> httpx.Response:
        actor = who or self.owner
        return await actor.transport.download(
            "/v1/knowledge.download",
            params={"org": actor.org, "namespace": self.entry["namespace"], "source_uri": self.uri},
        )


async def initialize(world: World) -> None:
    for namespace, parent in ((world.namespace, ""), (world.team, ""), (world.child, world.team)):
        set_query_tag("test.mcp_source_qualification.setup")
        await world.owner.call("collection.create", namespace=namespace, parent=parent)
    # A real collection grant makes same-collection source isolation nontrivial.
    for namespace in (world.namespace, world.team):
        set_query_tag("test.mcp_source_qualification.setup")
        await world.owner.call(
            "grants.create", namespace=namespace, subject=world.other.subject, role="viewer"
        )
    connected = await world.owner.call(
        "knowledge.connection.mcp_connect",
        profile_id="qualification-" + world.source.kind,
        request_id=str(uuid4()),
        bearer=world.source.bearer,
    )
    world.slot = connected["connection"]["slot"]
    assert connected["connection"]["state"] == "configured"


@pytest_asyncio.fixture
async def world(
    request: pytest.FixtureRequest,
    qualification_app: Any,
    tmp_path: Path,
) -> AsyncIterator[World]:
    source = SyntheticSource(request.param, json_response=request.param == "resources")
    org, foreign_org = unique_org("mcp-qualification"), unique_org("mcp-qualification-other")
    catalog = CatalogInput(
        mcp_profiles.MCPProfileCatalog.parse(
            json.dumps({"version": 1, "profiles": [source.profile([org, foreign_org])]})
        )
    )
    patch = pytest.MonkeyPatch()
    embed_calls: list[int] = []
    try:
        install_transport(patch, source, catalog)
        install_embedding_counter(patch, embed_calls)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(source.application())
            owner = await stack.enter_async_context(
                reader(qualification_app, tmp_path / "owner", org, "owner@example.test")
            )
            other = await stack.enter_async_context(
                reader(qualification_app, tmp_path / "other", org, "other@example.test")
            )
            foreign = await stack.enter_async_context(
                reader(qualification_app, tmp_path / "foreign", foreign_org, "owner@example.test")
            )
            prefix = "col-q-" + uuid4().hex[:12]
            result = World(
                owner,
                other,
                foreign,
                source,
                prefix,
                prefix + "-team",
                prefix + "-child",
                catalog,
                embed_calls,
            )
            await initialize(result)
            yield result
    finally:
        source.release.set()
        patch.undo()
        assert source.in_flight == 0


def install_transport(
    patch: pytest.MonkeyPatch, source: SyntheticSource, catalog: CatalogInput
) -> None:
    real_init = mcp_source.MCPSourceClient.__init__

    def injected_init(self: Any, *args: Any, **kwargs: Any) -> None:
        assert "_transport" not in kwargs
        real_init(self, *args, **kwargs, _transport=SourceTransport([source]))

    async def pin(url: str, **kwargs: Any) -> tuple[str, str, str, str]:
        assert url == source.endpoint and kwargs == {"schemes": ("https",)}
        return "", "https://203.0.113.8/mcp", source.host, source.host

    patch.setattr(mcp_source.MCPSourceClient, "__init__", injected_init)
    patch.setattr(mcp_source, "pinned_target", pin)
    # Trusted operator input uses the actual parser and binding checks.
    patch.setattr(mcp_profiles, "load_mcp_profile_catalog", lambda: catalog.current)
    patch.setattr(mcp_connections, "load_mcp_profile_catalog", lambda: catalog.current)


def install_embedding_counter(patch: pytest.MonkeyPatch, embed_calls: list[int]) -> None:
    embedder = get_embedder()
    embed_many = type(embedder).embed_many

    async def counted(self: Any, texts: list[str]) -> list[list[float]]:
        embed_calls.append(len(texts))
        return await embed_many(self, texts)

    patch.setattr(type(embedder), "embed_many", counted)


async def cancel_owned(task: asyncio.Task[Any], source: SyntheticSource) -> None:
    task.cancel()
    source.release.set()
    try:
        async with asyncio.timeout(10):
            await task
    except asyncio.CancelledError:
        pass
    assert task.done() and source.in_flight == 0


def require_clean_selection(world: World) -> None:
    assert set(world.source.reads) <= {SELECTED, SECOND}
    assert not any(
        name not in {"read_document", "search_documents", "resources/list"}
        for name in world.source.calls
    )
