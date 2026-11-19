"""Connected source controls keep the real caller, native checks and mirror privacy."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import httpx
import pytest

from server.auth.context import current_auth
from server.clients import gdrive
from server.clients._checked_access import CheckedAccess
from server.core import request_work
from server.core.errors import ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.api.models_sources import (
    KnowledgeSourceCheckInput,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceSyncNowInput,
    KnowledgeSourceUpdateInput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import sources
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import mirrors, source_push, source_retirements
from server.operations.base import Role, grant_rank
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import (
    _ACCESS,
    _KEY,
    _ROTATED,
    _auth,
)
from tests.unit.test_connected_source_authority import (
    _World as _AuthorityWorld,
)

_SOURCE_ID = "11111111-2222-4333-8444-555555555555"
_FILE = "owned-file-0001"
_ADDED = "added-file-0002"
_TITLE = "private provider title canary"
_MIRROR = "acl-" + "a" * 24
_SCOPE = {"org": "team", "namespace": "shared-space", "name": "documents"}


class _World(_AuthorityWorld):
    """External rows/lease store and HTTP; source and account decisions stay real."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        super().__init__(monkeypatch, http)
        assert self.source is not None
        self.source = replace(
            self.source,
            source_id=_SOURCE_ID,
            config={"include": ["pdf"]},
            items=[{"type": "file", "id": _FILE}],
            mirror=_MIRROR,
        )
        self.roles = {"person": Role.WRITER, "operator": Role.ADMIN}
        self.source_writes: list[KnowledgeSource] = []
        self.state_writes: list[dict[str, Any]] = []
        self.retirement_rows: list[dict[str, Any]] = []
        self.native_callers: list[str] = []
        self.pause_native = False
        self.native_entered = asyncio.Event()
        self.native_release = asyncio.Event()
        monkeypatch.setattr(accounts.leases, "holding", self.holding)
        monkeypatch.setattr(sources.store, "insert_source", self.insert_source)
        monkeypatch.setattr(sources.store, "read_states", self.read_states)
        monkeypatch.setattr(sources.store, "write_state", self.write_state)
        monkeypatch.setattr(sources.store, "count_documents", self.no_document_count)
        monkeypatch.setattr(mirrors, "_default_client", lambda: self)
        monkeypatch.setattr(source_retirements, "_default_client", lambda: self)
        monkeypatch.setattr(source_push, "get", self.no_push)
        monkeypatch.setattr(gdrive, "get_client", lambda: http)

    @staticmethod
    def row_owner(subject: str) -> str:
        return {
            "person": "person",
            "person@example.com": "person",
            "operator": "operator",
            "operator@example.com": "operator",
        }[subject.lower()]

    async def query_all(self, query: str, params: dict[str, Any], **_kw: Any):
        if "argMax(schema_version" in query:
            assert params["all_deployments"] is True
            return [{"schema_version": 111}]
        assert params["org"] == "team"
        if "FROM loom.knowledge_mirrors" in query:
            assert params["namespaces"] == ["shared-space"]
            return [{"mirror": _MIRROR, "namespace": "shared-space"}]
        assert "FROM loom.namespaces" in query
        self.directory_reads += 1
        subject = self.row_owner(params["sub"])
        assert subject in self.roles
        return [
            {
                "namespace": "personal-" + subject,
                "owner_email": subject,
                "contact_email": subject + "@example.com",
                "password_hash": "verified-password-hash",
                "hosted_domain": "",
                "issuer": "",
            }
        ]

    async def query_one(self, query: str, params: dict[str, Any], **kw: Any):
        if "AS grants" in query:
            subject = self.row_owner(params["sub"])
            role = self.roles[subject]
            spellings = [subject, subject + "@example.com"]
            return {
                "grants": [("shared-space", grant_rank(role.name.lower()))],
                "spellings": spellings,
                "held": spellings,
            }
        if "SELECT role FROM loom.namespace_grants" in query:
            assert params["org"] == "team" and params["ns"] == "shared-space"
            role = max(self.roles[self.row_owner(subject)] for subject in params["subs"])
            return {"role": role.name.lower()}
        return await super().query_one(query, params, **kw)

    async def active(self, org: str, spellings: list[str]) -> bool:
        assert org == "team" and all(self.row_owner(s) in self.roles for s in spellings)
        return self.account_active

    async def deactivated(self, email: str) -> bool:
        assert self.row_owner(email) in self.roles
        return self.scim_ended

    @asynccontextmanager
    async def holding(self, key: str, **kw: Any) -> AsyncIterator[bool]:
        async with self.lease(key, **kw):
            yield True

    async def insert_source(self, source: KnowledgeSource, **_kw: Any) -> None:
        assert self.holders.get(asyncio.current_task(), 0)
        self.source_writes.append(source)
        self.source = source

    async def read_states(self, org: str, namespace: str, names: list[str], **_kw: Any):
        assert (org, namespace, names) == ("team", "shared-space", ["documents"])
        return {}

    async def write_state(self, org: str, namespace: str, name: str, **kw: Any) -> None:
        assert (org, namespace, name) == ("team", "shared-space", "documents")
        assert self.holders.get(asyncio.current_task(), 0)
        self.state_writes.append(kw)

    async def no_document_count(self, *_args: Any, **_kw: Any) -> int:
        raise AssertionError("a mirrored control must not count provider documents")

    async def no_push(self, org: str, namespace: str, source_id: str, **_kw: Any):
        assert (org, namespace, source_id) == ("team", "shared-space", _SOURCE_ID)
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], **_kw: Any) -> None:
        assert table == "loom.code_index_state"
        assert self.holders.get(asyncio.current_task(), 0)
        self.retirement_rows.extend(dict(row) for row in rows)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return await super().handle(request)
        assert not self.holders.get(asyncio.current_task(), 0)
        assert request.url.host == "www.googleapis.com" and request.method == "GET"
        assert request.url.path in ("/drive/v3/files/" + _FILE, "/drive/v3/files/" + _ADDED)
        assert request.headers["Authorization"] == "Bearer " + _ACCESS
        auth = current_auth()
        assert auth is not None
        self.native_callers.append(auth.subject)
        self.native_requests.append(request)
        self.native_entered.set()
        if self.pause_native:
            await self.native_release.wait()
        return httpx.Response(
            200,
            json={
                "id": request.url.path.rsplit("/", 1)[1],
                "name": _TITLE,
                "mimeType": "application/pdf",
                "trashed": False,
                "modifiedTime": "2020-01-01T00:00:00Z",
            },
        )

    async def await_stage(self, event: asyncio.Event, work: asyncio.Task[Any]) -> None:
        entered = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait({entered, work}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
                pytest.fail("the operation completed before its external I/O boundary")
        finally:
            if not entered.done():
                entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    async def handle(request: httpx.Request) -> httpx.Response:
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        state = _World(monkeypatch, http)
        yield state, request_work.RequestWork()


def _operator(role: Role = Role.ADMIN):
    return _auth(subject="operator@example.com", roles=(role,))


def _private_answer(result: Any, bag: request_work.RequestWork) -> dict[str, Any]:
    assert bag.mirror_marked
    answer = request_work.flag_response(result.model_dump())
    assert answer["mirrored"] is True
    assert _TITLE not in json.dumps(answer) and _ACCESS not in json.dumps(answer)
    return answer


async def test_owner_update_probes_and_commits_only_the_selected_connected_source(world) -> None:
    state, bag = world
    with request_work.bind(bag), acting_as(_auth()):
        result = await sources.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(**_SCOPE, add_items=[{"type": "file", "id": _ADDED}])
        )
        answer = _private_answer(result, bag)
    assert result.added == [{"type": "file", "id": _ADDED}]
    assert result.items == [{"type": "file", "id": _FILE}, {"type": "file", "id": _ADDED}]
    assert result.probe.documents == 0 and result.probe.sample == [] and not result.probe.more
    assert len(state.source_writes) == len(state.state_writes) == 1
    saved = state.source_writes[0]
    assert (saved.source_id, saved.owner_subject, saved.access) == (
        _SOURCE_ID,
        "person",
        "mirrored",
    )
    assert saved.connection_id == state.rows[_KEY].connection_id and saved.credential_enc == ""
    assert state.state_writes[0]["detail"]["full"] is True
    assert state.state_writes[0]["detail"]["requested_at"]
    assert len(state.refresh_requests) == len(state.native_requests) == 1
    assert state.native_callers == ["person@example.com"]
    assert answer["probe"]["documents"] == 0


async def test_namespace_admin_cannot_choose_new_items_for_another_owners_account(world) -> None:
    state, bag = world
    with (
        request_work.bind(bag),
        acting_as(_operator()),
        pytest.raises(ValidationError, match="^the connected account is unavailable$"),
    ):
        await sources.handle_knowledge_source_update(
            KnowledgeSourceUpdateInput(**_SCOPE, add_items=[{"type": "file", "id": _ADDED}])
        )
    assert bag.mirror_marked
    assert state.refresh_requests == state.native_requests == state.source_writes == []


async def test_admin_check_accepts_credential_standing_without_an_item_existence_oracle(
    world,
) -> None:
    state, bag = world
    assert state.source is not None
    state.source = replace(state.source, items=[{"type": "file", "id": "not-shared-with-owner"}])
    with request_work.bind(bag), acting_as(_operator()):
        result = await sources.handle_knowledge_source_check(KnowledgeSourceCheckInput(**_SCOPE))
        answer = _private_answer(result, bag)
    assert result.reachable and result.summary == "the connected credential was accepted"
    assert (result.documents, result.more, result.sample, result.checked) == (0, False, [], 0)
    assert len(state.refresh_requests) == 1 and state.native_requests == []
    assert "not-shared-with-owner" not in json.dumps(answer)
    assert state.source_writes == [] and state.state_writes == []


async def test_owner_check_withholds_provider_counts_titles_and_marks_the_response(world) -> None:
    state, bag = world
    with request_work.bind(bag), acting_as(_auth()):
        result = await sources.handle_knowledge_source_check(KnowledgeSourceCheckInput(**_SCOPE))
        _private_answer(result, bag)
    assert result.reachable and result.summary == "the connected source was reached"
    assert (result.documents, result.more, result.sample) == (0, False, [])
    assert result.checked == 1
    assert len(state.refresh_requests) == len(state.native_requests) == 1
    assert state.source_writes == [] and state.state_writes == []


@pytest.mark.parametrize("stage", ("refresh", "native"))
async def test_admin_probe_keeps_its_actual_role_across_external_waits(world, stage: str) -> None:
    state, bag = world
    # A namespace grant supplies ADMIN; the token itself must not retain global ADMIN.
    auth = _operator(Role.VIEWER)
    with request_work.bind(bag), acting_as(auth):
        if stage == "refresh":
            state.pause_refresh = True
            work = asyncio.create_task(
                sources.handle_knowledge_source_check(KnowledgeSourceCheckInput(**_SCOPE))
            )
            entered, release = state.refresh_entered, state.refresh_release
        else:
            assert state.source is not None
            authority = await accounts.resolve_probe(state.source, deadline=time.monotonic() + 60)
            assert authority.requester == "operator" and authority.source.owner_subject == "person"

            async def native_read(access: CheckedAccess):
                return await gdrive.file_meta(access, _FILE)

            state.pause_native = True
            work = asyncio.create_task(authority.run(native_read))
            entered, release = state.native_entered, state.native_release
        try:
            await state.await_stage(entered, work)
            state.roles["operator"] = Role.VIEWER
            release.set()
            with pytest.raises(ValidationError, match="^the connected account is unavailable$"):
                await work
            assert current_auth() is auth
        finally:
            release.set()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    assert len(state.refresh_requests) == 1
    if stage == "refresh":
        assert state.native_requests == []
        assert state.rows[_KEY].active and not state.rows[_KEY].refresh_id
        assert unseal(CREDENTIAL_PURPOSE, state.rows[_KEY].credential_enc) == _ROTATED
        assert state.source is not None and state.source.active
        assert state.source_writes == state.retirement_rows == []
    else:
        assert len(state.native_requests) == 1
        assert state.native_callers == ["operator@example.com"]
        assert state.source_writes == []


async def _local_operation(operation: str):
    if operation == "sync":
        return await sources.handle_knowledge_source_sync_now(
            KnowledgeSourceSyncNowInput(**_SCOPE, full=True)
        )
    assert operation == "remove"
    return await sources.handle_knowledge_source_remove(
        KnowledgeSourceRemoveInput(**_SCOPE, confirm="documents", delete_documents=True)
    )


def _assert_local_result(state: _World, result: Any, operation: str) -> None:
    assert state.refresh_requests == state.native_requests == []
    if operation == "sync":
        assert len(state.state_writes) == 1 and state.state_writes[0]["detail"]["full"] is True
        assert state.source_writes == [] and state.retirement_rows == []
        return
    assert result.documents_pending == 0 and len(state.retirement_rows) == 1
    assert len(state.source_writes) == 1
    saved = state.source_writes[0]
    assert not saved.active and saved.pending_delete
    assert saved.credential_enc == saved.secret_enc == "" and saved.owner_subject == "person"
    queued = state.retirement_rows[0]
    assert (queued["org"], queued["namespace"], queued["state_key"]) == (
        "team",
        _MIRROR,
        "knowledge_source_retire:" + _SOURCE_ID,
    )
    assert queued["state_value"] == "pending"
    assert json.loads(queued["detail"])["source_id"] == _SOURCE_ID


@pytest.mark.parametrize("operation", ("sync", "remove"))
async def test_owner_can_request_local_sync_or_retirement_without_provider_work(
    world, operation: str
) -> None:
    state, bag = world
    with request_work.bind(bag), acting_as(_auth()):
        result = await _local_operation(operation)
        _private_answer(result, bag)
    _assert_local_result(state, result, operation)


@pytest.mark.parametrize("operation", ("sync", "remove"))
async def test_foreign_writer_is_refused_but_current_admin_can_run_local_controls(
    world, operation: str
) -> None:
    state, bag = world
    state.roles["operator"] = Role.WRITER
    with (
        request_work.bind(bag),
        acting_as(_operator(Role.WRITER)),
        pytest.raises(ValidationError, match="^the connected account is unavailable$"),
    ):
        await _local_operation(operation)
    assert state.source_writes == state.state_writes == state.retirement_rows == []
    assert state.refresh_requests == state.native_requests == []
    state.roles["operator"] = Role.ADMIN
    with request_work.bind(bag), acting_as(_operator()):
        result = await _local_operation(operation)
        _private_answer(result, bag)
    _assert_local_result(state, result, operation)
