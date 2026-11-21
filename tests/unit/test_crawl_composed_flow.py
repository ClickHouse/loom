"""Real registration, native worker publication and indexed authority share one fence."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any

import httpx
import pytest

from server.auth.jwt import verify_token
from server.clients import gdrive
from server.core import instances, request_work
from server.core.errors import ValidationError
from server.knowledge.api.models_sources import KnowledgeSourceAddInput, KnowledgeSourceUpdateInput
from server.knowledge.app import (
    crawl_exclusions,
    crawl_sources,
    mirrors,
    private_sync,
)
from server.knowledge.app import source_access as reads
from server.knowledge.app import source_sync as sync
from server.knowledge.app import sources as ops
from server.knowledge.domain.access_list import from_json
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, mirror_uri
from server.knowledge.store import crawl_publication, documents
from server.knowledge.store import mirrors as mirror_store
from server.knowledge.store import sources as source_store
from server.memory.domain.provenance import pipeline_actor
from server.operations.base import Role
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_publication import _MIRROR
from tests.unit.test_connected_source_publication import _World as _PublicationWorld
from tests.unit.test_crawl_authority import _World as _CrawlWorld
from tests.unit.test_crawl_registration import _token
from tests.unit.test_crawl_signing_authority import key_view as key_view
from tests.unit.test_crawl_worker_observation import _file
from tests.unit.test_google_workspace import account as account

pytestmark = [pytest.mark.asyncio, pytest.mark.claim_cap]


class _World(_CrawlWorld, _PublicationWorld):
    """Reuse real authority and upsert fixtures, extending only SQL and provider I/O."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient, account: Any):
        super().__init__(monkeypatch, http, account)
        self.source = None
        self.listings: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.lose_during_embedding = False
        self.lose_during_proof = False
        self.unhandled_reads: list[tuple[str, tuple[str, ...]]] = []
        monkeypatch.setattr(crawl_sources, "get_client", lambda: self)
        monkeypatch.setattr(crawl_exclusions, "get_client", lambda: self)
        monkeypatch.setattr(instances, "read_live", self.read_live)
        monkeypatch.setattr(mirrors, "mirrors_of", self.mirror_rows)
        monkeypatch.setattr(mirror_store, "_default_client", lambda: self)
        monkeypatch.setattr(crawl_publication, "_default_client", lambda: self)
        monkeypatch.setattr(reads.store, "_default_client", lambda: self)
        monkeypatch.setattr(reads.mirrors_repo, "mirrors_of", self.mirror_rows)
        monkeypatch.setattr(reads.pg, "enabled", lambda: False)
        monkeypatch.setattr(source_store, "every_copy", self.every_copy)
        monkeypatch.setattr(source_store, "read_states", self.read_states)
        monkeypatch.setattr(documents, "publications_owed", self.publications_owed)
        monkeypatch.setattr(private_sync, "write_private_cost_event_for", self.cost_event)

    async def read_live(self, ch: Any, *, all_deployments: bool):
        assert ch is self and all_deployments
        return [{"schema_version": 114}]

    async def cost_event(self, **values: Any) -> None:
        assert values["org"] == "team" and values["namespace"] == _MIRROR

    def assert_storage_leases(self) -> None:
        assert self.held.get(asyncio.current_task(), []) == [
            crawl_exclusions._fence("team"),
            "knowledge-source:team:shared-space",
        ]

    async def no_prior_document(self, org: str, namespace: str, uri: str, **kwargs: Any):
        assert (org, namespace) == ("team", _MIRROR)
        assert set(kwargs) == {"include_superseded"}
        return next((row for row in self.document_writes if row["source_uri"] == uri), None)

    async def every_copy(self, org: str, mirror: str, tag: str):
        assert self.source is not None
        assert (org, mirror, tag) == ("team", _MIRROR, "source-id:" + self.source.source_id)
        return []

    async def read_states(self, org: str, namespace: str, names: list[str]):
        assert self.source is not None and org == "team"
        assert (namespace, names) in (
            (_MIRROR, [self.source.source_id]),
            (self.source.namespace, [self.source.name]),
        )
        found = {}
        for row in self.states:
            if row["namespace"] == namespace:
                for name in names:
                    if row["state_key"] == source_store.state_key(name):
                        found[name] = {"state_value": row["state_value"], "detail": row["detail"]}
        return found

    async def publications_owed(self, org: str, mirror: str, suffix: str) -> bool:
        assert self.source is not None
        assert (org, mirror, suffix) == ("team", _MIRROR, "#" + self.source.source_id)
        return False

    async def mirror_rows(self, org: str, namespaces: list[str], **kwargs: Any):
        assert org == "team" and kwargs == {"live_only": True}
        return {_MIRROR: "shared-space"} if "shared-space" in namespaces else {}

    async def query_one(self, query: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if query.startswith("SELECT count() AS n FROM loom.knowledge_sources FINAL"):
            assert params == {"org": "team", "ns": "shared-space", "connected": False, "owner": ""}
            return {"n": int(self.source is not None and self.source.active)}
        if query.startswith("SELECT count() AS n FROM loom.documents FINAL"):
            assert params is not None and params["org"] == "team"
            return {"n": 0}
        return await super().query_one(query, params, **kwargs)

    async def query_all(self, query: str, params: dict[str, Any], **kwargs: Any):
        if "FROM loom.knowledge_group_members FINAL" in query:
            assert params["org"] == "team"
            return []
        if query.startswith("SELECT namespace, name, credential_mode"):
            assert params == {"org": "team", "names": ["shared-space"]}
            return (
                []
                if self.source is None
                else [
                    {
                        "namespace": self.source.namespace,
                        "name": self.source.name,
                        "credential_mode": self.source.credential_mode,
                        "source_id": self.source.source_id,
                    }
                ]
            )
        if "FROM loom.knowledge_source_listings FINAL" in query:
            assert params["org"] == "team"
            return [
                row
                for row in self.listings
                if (row["namespace"], row["source_id"]) in params["pairs"]
            ]
        if "FROM loom.documents FINAL" in query:
            assert params["org"] == "team"
            return [
                row
                for row in self.document_writes
                if (row["namespace"], row["source_uri"]) in params["pairs"]
            ]
        try:
            return await super().query_all(query, params, **kwargs)
        except AssertionError:
            # Preserve fail-closed behavior; surface only the unexpected fake-I/O
            # projection and parameter names, never substitute admission evidence.
            self.unhandled_reads.append((" ".join(query.split()), tuple(sorted(params))))
            raise

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == source_store.TABLE:
            self.assert_storage_leases()
            [row] = rows
            self.source = source_store._source(row)
            self.request = self.request.model_copy(
                update={"source_uri": mirror_uri(_file()["webViewLink"], self.source.source_id)}
            )
        elif table == source_store.STATE_TABLE:
            self.assert_storage_leases()
            self.states.extend(dict(row) for row in rows)
        elif table == mirror_store.LISTINGS:
            self.assert_storage_leases()
            self.listings = [dict(row) for row in rows]
        else:
            await super().insert(table, rows)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            assert not self.holders.get(asyncio.current_task(), 0)
            assert request.headers["authorization"] == "Bearer private-person-access"
            self.native_requests.append(request)
            if request.url.path == "/drive/v3/files/file/export":
                return httpx.Response(
                    200, text="# Notes\n\nThe delegated source publishes this revision."
                )
            file = _file()
            file["permissions"] = [
                {"type": "user", "role": "reader", "emailAddress": "person@example.com"}
            ]
            if request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": [file], "incompleteSearch": False})
            assert request.url.path == "/drive/v3/files/file"
            return httpx.Response(200, json=file)
        response = await super().handle(request)
        if self.lose_during_proof and self.claims_sent:
            self.grant = Role.VIEWER
        return response

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        result = await super().embed_many(texts)
        if self.lose_during_embedding:
            self.grant = Role.VIEWER
        return result


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch, account: Any, key_view: list[str]):
    async def provider(request: httpx.Request) -> httpx.Response:
        return await state.handle(request)

    marker = request_work._current.set(None)
    request_work.begin()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            state = _World(monkeypatch, http, account)
            yield state
    finally:
        request_work._current.reset(marker)


def _input(world: _World) -> KnowledgeSourceAddInput:
    return KnowledgeSourceAddInput(
        org="team",
        namespace="shared-space",
        name="documents",
        kind="gdrive",
        credential_mode="crawl",
        credential=world.credential,
        items=[{"type": "filter", "kinds": ["docs"]}],
        include=["docs"],
        crawl_options={
            "directory_subject": "directory@example.com",
            "subjects": [{"type": "person", "email": "one@example.com"}],
        },
    )


async def _register(world: _World) -> None:
    with acting_as(verify_token(_token())):
        result = await ops.handle_knowledge_source_add(_input(world))
        assert result.name == "documents" and not result.dry_run
    assert world.source is not None and world.source.crawl_customer_id == "workspace"


async def _turn(world: _World) -> sync.TurnResult:
    assert world.source is not None
    now = time.monotonic()
    run = sync.Pass(sync.PassBudget(now, now + 30), 10)
    with acting_as(None), pipeline_actor(SYNC_WORKER_ACTOR), private_sync.scope():
        return await sync._turn_of(world.source)(world.source, {}, run)


async def test_registration_native_publication_and_indexed_reader_share_exact_generation(
    world: _World,
) -> None:
    await _register(world)
    assert {claim["scope"] for claim in world.claims_sent} == {
        scope.value for scope in (*gdrive.ReadScope, *gdrive.WriteScope)
    }
    turn = await _turn(world)
    assert turn.status == "ok" and turn.complete and turn.changed == 1
    assert len(world.document_writes) == len(world.chunk_writes) == len(world.listings) == 1
    [document] = world.document_writes
    assert document["actor"] == SYNC_WORKER_ACTOR
    assert from_json(document["access_json"]).owner == ""
    assert "person@example.com" in from_json(document["access_json"]).emails
    assert world.source is not None
    assert world.listings[0]["crawl_generation"] == world.source.crawl_generation
    candidate = {key: document[key] for key in ("namespace", "source_uri", "revision")}
    candidate["doc_version"] = document["version"]
    before = len(world.provider_calls), len(world.native_requests)
    with acting_as(verify_token(_token())):
        plan = await reads.prepare("team", ["shared-space"])
        assert plan.sources == {(_MIRROR, world.source.source_id): "shared-space"}, (
            world.unhandled_reads,
            world.listings,
        )
        checked = await plan.check([candidate], chunks=True)
        assert checked, world.unhandled_reads
        [admitted] = checked
        assert admitted.document == document and plan.current(admitted)
    assert (len(world.provider_calls), len(world.native_requests)) == before


async def test_current_administrator_loss_during_registration_never_publishes_source(
    world: _World,
) -> None:
    world.lose_during_proof = True
    with acting_as(verify_token(_token())), pytest.raises((ValidationError, PermissionError)):
        await ops.handle_knowledge_source_add(_input(world))
    assert world.provider_calls and world.source is None and not world.document_writes


async def test_actual_embedding_wait_cannot_publish_after_registrant_loses_namespace_admin(
    world: _World,
) -> None:
    await _register(world)
    world.lose_during_embedding = True
    with pytest.raises(PermissionError):
        await _turn(world)
    assert (
        world.embedded
        and not world.document_writes
        and not world.chunk_writes
        and not world.listings
    )


@pytest.mark.parametrize("change", ["pending", "epoch", "generation"])
async def test_indexed_evidence_is_removed_after_person_or_registration_fence_changes(
    world: _World, change: str
) -> None:
    await _register(world)
    assert (await _turn(world)).status == "ok"
    [document] = world.document_writes
    candidate = {key: document[key] for key in ("namespace", "source_uri", "revision")}
    candidate["doc_version"] = document["version"]
    with acting_as(verify_token(_token())):
        plan = await reads.prepare("team", ["shared-space"])
        assert plan.sources == {(_MIRROR, world.source.source_id): "shared-space"}, (
            world.unhandled_reads,
            world.listings,
        )
        checked = await plan.check([candidate], chunks=True)
        assert checked, world.unhandled_reads
        [admitted] = checked
        if change == "pending":
            world.person_erasure_pending = True
        elif change == "epoch":
            world.fences[crawl_exclusions._fence("team")] = "blocked:person-cleanup"
        else:
            assert world.source is not None
            world.source = dataclasses.replace(world.source, crawl_generation="replaced")
        assert await plan.check([candidate], chunks=True) == []
        assert not plan.current(admitted)


async def test_reapproved_generation_cannot_reuse_an_older_completed_listing(world: _World) -> None:
    await _register(world)
    assert (await _turn(world)).status == "ok"
    [document] = world.document_writes
    candidate = {key: document[key] for key in ("namespace", "source_uri", "revision")}
    candidate["doc_version"] = document["version"]
    assert world.source is not None
    before = world.source.crawl_generation
    with acting_as(verify_token(_token())):
        await ops.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(
                org="team",
                namespace="shared-space",
                name="documents",
                include=["docs", "pdf"],
            )
        )
        assert world.source.crawl_generation != before
        plan = await reads.prepare("team", ["shared-space"])
        assert plan.sources == {} and await plan.check([candidate], chunks=True) == []


async def test_reactivation_does_not_readmit_until_an_explicit_verified_update(
    world: _World,
) -> None:
    from server.knowledge.domain.source_crawl import Person

    await _register(world)
    index = await crawl_exclusions.Index.open("team", ch=world)
    person = Person("workspace", "provider-one", "one@example.com", ())
    for digest in index.person_digests(person):
        world.exclusion_rows["team", digest] = True
    assert await index.contains(person)
    before = await index.version()
    with acting_as(verify_token(_token())):
        ordinary = await ops.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(
                org="team",
                namespace="shared-space",
                name="documents",
                include=["docs", "pdf"],
            )
        )
        assert ordinary.crawl_proof is not None and ordinary.crawl_proof.members == 0
        assert await index.contains(
            person
        )  # Editing the selectors or signing in is no readmission.
        explicit = await ops.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(
                org="team",
                namespace="shared-space",
                name="documents",
                crawl_readmit=["one@example.com"],
            )
        )
    current = await crawl_exclusions.Index.open("team", ch=world)
    assert await current.version() != before and not await current.contains(person)
    assert explicit.crawl_proof is not None and explicit.crawl_proof.members == 1
    assert not world.listings and not world.document_writes  # No old content was republished.
    assert "one@example.com" not in explicit.crawl_proof.model_dump_json()
    assert set(explicit.crawl_proof.read_scopes) == {scope.value for scope in gdrive.ReadScope}


async def test_still_deactivated_person_cannot_be_explicitly_readmitted(world: _World) -> None:
    await _register(world)
    before = world.source
    index = await crawl_exclusions.Index.open("team", ch=world)
    epoch = await index.version()
    world.ended_people.add("one@example.com")
    with acting_as(verify_token(_token())), pytest.raises(ValidationError):
        await ops.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(
                org="team",
                namespace="shared-space",
                name="documents",
                crawl_readmit=["one@example.com"],
            )
        )
    assert world.source == before and await index.version() == epoch
    assert not world.native_requests and not world.document_writes and not world.listings
