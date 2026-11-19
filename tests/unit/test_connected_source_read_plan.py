"""Indexed connected content retains its owner generation and the actual reader ACL."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from server.auth.context import current_auth_var
from server.core import request_work
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import source_access as app
from server.knowledge.domain.access_list import AccessList, to_json
from server.operations.base import Role, grant_rank
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import _auth
from tests.unit.test_connected_source_authority import _World as _AuthorityWorld

_SID = "11111111-1111-4111-8111-111111111111"
_MIRROR = "acl-" + "a" * 24
_URI = "https://docs.example/file#" + _SID


class _World(_AuthorityWorld):
    """External directory, source discovery and indexed permission metadata."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        super().__init__(monkeypatch, http)
        assert self.source is not None
        self.source = replace(self.source, source_id=_SID, mirror=_MIRROR)
        self.started = datetime.now(UTC)
        self.source_queries: list[str] = []
        self.document = {
            "org": "team",
            "namespace": _MIRROR,
            "source_uri": _URI,
            "tags": ["source-id:" + _SID],
            "version": 10,
            "revision": 1,
            "title": "A checked document",
            "superseded_at": None,
            "access_json": to_json(AccessList(owner="person", emails=("reader@example.com",))),
        }
        monkeypatch.setattr(app.store, "_default_client", lambda: self)
        monkeypatch.setattr(app.mirrors_repo, "mirrors_of", self.mirrors)
        monkeypatch.setattr(app.store, "listings", self.listings)
        monkeypatch.setattr(app.store, "metadata", self.metadata)
        monkeypatch.setattr(app.pg, "enabled", lambda: False)
        monkeypatch.setattr(app.settings, "knowledge_source_acl_stale_seconds", 3600)

    async def query_all(self, query: str, params: dict[str, Any], **kw: Any):
        if "FROM loom.knowledge_sources FINAL" in query:
            assert params == {"org": "team", "names": ["shared-space"]}
            self.source_queries.append(query)
            # This fake applies the discovery SQL's persisted credential-mode predicate.
            if self.source is None or "credential_mode = 'shared'" in query:
                return []
            return [
                {
                    "namespace": "shared-space",
                    "name": "documents",
                    "credential_mode": self.source.credential_mode,
                    "source_id": self.source.source_id,
                }
            ]
        if params.get("sub") in {"reader", "reader@example.com"}:
            assert "FROM loom.namespaces" in query and params["org"] == "team"
            return [
                {
                    "namespace": "personal-reader",
                    "owner_email": "reader",
                    "contact_email": "reader@example.com",
                    "password_hash": "verified-reader-password-hash",
                    "hosted_domain": "",
                    "issuer": "",
                }
            ]
        return await super().query_all(query, params, **kw)

    async def query_one(self, query: str, params: dict[str, Any], **kw: Any):
        if "AS grants" in query and params["sub"] in {"reader", "reader@example.com"}:
            assert params["org"] == "team"
            return {
                "grants": [("shared-space", grant_rank("viewer"))],
                "spellings": ["reader", "reader@example.com"],
                "held": ["reader", "reader@example.com"],
            }
        if "SELECT role FROM loom.namespace_grants" in query and "reader" in params["subs"]:
            assert params["org"] == "team" and params["ns"] == "shared-space"
            return {"role": "viewer"}
        return await super().query_one(query, params, **kw)

    async def mirrors(self, org: str, namespaces: list[str], **kw: Any):
        assert org == "team" and kw == {"live_only": True}
        return {_MIRROR: "shared-space"} if "shared-space" in namespaces else {}

    async def listings(self, org: str, pairs: list[tuple[str, str]]):
        assert org == "team"
        return (
            [{"namespace": _MIRROR, "source_id": _SID, "started_at": self.started}]
            if (_MIRROR, _SID) in pairs
            else []
        )

    async def metadata(self, org: str, pairs: list[tuple[str, str]]):
        assert org == "team" and pairs == [(_MIRROR, _URI)]
        return [dict(self.document)]


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    async def no_provider(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("indexed reads cannot mint or use a provider token")

    token = request_work._current.set(None)
    bag = request_work.begin()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_provider)) as http:
            state = _World(monkeypatch, http)
            yield state, bag
            assert state.refresh_requests == state.native_requests == []
            assert state.writes == []
    finally:
        request_work._current.reset(token)


def _candidate() -> dict[str, Any]:
    return {"namespace": _MIRROR, "source_uri": _URI, "doc_version": 10, "revision": 1}


@pytest.mark.parametrize("subject", ("person@example.com", "reader@example.com"))
async def test_current_connected_owner_and_independently_admitted_reader_can_read(
    world, subject: str
) -> None:
    state, bag = world
    with acting_as(_auth(subject=subject)):
        plan = await app.prepare("team", ["shared-space"])
        assert plan.sources == {(_MIRROR, _SID): "shared-space"}
        [admitted] = await plan.check([_candidate()], chunks=True)
        assert admitted.document == state.document and admitted.namespace == "shared-space"
        assert plan.current(admitted)
    assert plan.marked and not plan.unread and bag.mirror_marked
    assert state.source_queries


async def test_namespace_viewer_without_the_documents_acl_is_not_its_owner(world) -> None:
    state, bag = world
    state.document["access_json"] = to_json(AccessList(owner="person"))
    with acting_as(_auth(subject="reader@example.com")):
        plan = await app.prepare("team", ["shared-space"])
        assert plan.sources == {(_MIRROR, _SID): "shared-space"}
        assert await plan.check([_candidate()], chunks=True) == []
    assert bag.mirror_marked


@pytest.mark.parametrize("change", ("generation", "owner-grant", "scim", "items", "auth"))
async def test_scope_refresh_removes_retained_evidence_after_authority_changes(
    world, change: str
) -> None:
    state, _bag = world
    auth = _auth()
    with acting_as(auth):
        plan = await app.prepare("team", ["shared-space"])
        [admitted] = await plan.check([_candidate()], chunks=True)
        evidence = app.AccessEvidence.from_admitted(admitted)
        assert plan.current_evidence(evidence)
        assert state.source is not None
        if change == "generation":
            account = state.rows["team", "person", "gdrive"]
            generation = accounts._next_connection(account.connection_id)
            state.rows["team", "person", "gdrive"] = replace(account, connection_id=generation)
            state.fences[accounts._fence_key(generation)] = generation
            state.source = replace(state.source, connection_id=generation)
        elif change == "owner-grant":
            state.grant = Role.VIEWER
        elif change == "scim":
            state.scim_ended = True
        elif change == "items":
            # In-place mutation must not mutate the request's deep-copied source pin.
            state.source.items.append({"id": "another-file", "kind": "file"})
        else:
            current_auth_var.set(replace(auth))
        assert await plan.check([_candidate()], chunks=True) == []
        assert not plan.current_evidence(evidence)
        assert plan.sources == {} and plan.listing_starts == {}


async def test_a_replaced_registration_cannot_join_an_existing_discovery_plan(world) -> None:
    state, _bag = world
    with acting_as(_auth()):
        plan = await app.prepare("team", ["shared-space"])
        [admitted] = await plan.check([_candidate()], chunks=True)
        assert state.source is not None
        state.source = replace(state.source, source_id="22222222-2222-4222-8222-222222222222")
        assert await plan.check([_candidate()], chunks=True) == []
        assert not plan.current(admitted)
        assert plan.discovery_sources == {(_MIRROR, _SID): "shared-space"}
