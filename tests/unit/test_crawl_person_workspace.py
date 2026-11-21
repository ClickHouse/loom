"""Deletion captures stable Google ids through real source and Directory boundaries."""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import httpx
import pytest

from server.auth import person_erasure as erasure
from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import CredentialRefused
from server.core.errors import BackendUnavailableError
from server.knowledge.app import crawl_capture
from server.knowledge.store import sources
from server.settings import settings
from tests.unit.test_crawl_authority import crawl_world as crawl_world
from tests.unit.test_crawl_signing_authority import key_view as key_view
from tests.unit.test_google_workspace import account as account

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def captured_world(crawl_world, monkeypatch: pytest.MonkeyPatch):
    world = crawl_world
    monkeypatch.setattr(erasure, "get_client", lambda: world)
    monkeypatch.setattr(crawl_capture, "get_client", lambda: world)
    monkeypatch.setattr(erasure, "_preparer", crawl_capture.prepare)
    original_query_one, original_query_all = world.query_one, world.query_all
    original_handle = world.handle
    world.pending = {
        "active": True,
        "deleted": False,
        "crawl_erasure_required": True,
        "crawl_erasure_done": False,
        "crawl_erasure_enc": "",
    }
    world.after_person = lambda: None
    world.current_contact = "person@example.com"
    written: list[dict[str, Any]] = []

    class Rows:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            assert table == sources.TABLE
            written.extend(rows)

    await sources.insert_source(world.source, ch=Rows())
    world.history = written

    async def query_one(sql: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if sql.startswith("SELECT active, deleted, crawl_erasure_enc"):
            assert params == {"id": "scim-person-id"}
            return dict(world.pending)
        return await original_query_one(sql, params, **kwargs)

    async def query_all(sql: str, params: dict[str, Any], **kwargs: Any):
        if "FROM loom.knowledge_sources WHERE" in sql:
            assert sql.startswith("SELECT DISTINCT") and "FINAL" not in sql
            assert params["org"] == "team"
            return world.history[: params["limit"]]
        result = await original_query_all(sql, params, **kwargs)
        if "FROM loom.namespaces" in sql:
            return [{**row, "contact_email": world.current_contact} for row in result]
        return result

    async def handle(request: httpx.Request):
        if request.url.host == "admin.googleapis.com" and request.url.path.endswith(
            "/person@example.com"
        ):
            world.provider_calls.append(request)
            assert request.headers["authorization"] == "Bearer private-directory-access"
            world.after_person()
            return httpx.Response(
                200,
                json={
                    "id": "stable-google-user",
                    "primaryEmail": "person@example.com",
                    "aliases": ["old-address@example.com"],
                    "customerId": "workspace",
                    "suspended": False,
                    "archived": False,
                },
            )
        return await original_handle(request)

    monkeypatch.setattr(world, "query_one", query_one)
    monkeypatch.setattr(world, "query_all", query_all)
    monkeypatch.setattr(world, "handle", handle)
    encoded = await erasure.capture("scim-person-id", "person@example.com", {"person": {"team"}})
    world.pending["crawl_erasure_enc"] = encoded
    yield world, encoded


async def test_real_predelete_capture_keeps_google_id_distinct_from_canonical_subject(
    captured_world,
):
    world, encoded = captured_world
    prepared = await erasure.prepare("scim-person-id", encoded)
    result = erasure.load("scim-person-id", prepared)
    assert result.prepared and result.people == (
        erasure.Person("team", "person", ("person@example.com",), ("person", "person@example.com")),
    )
    assert result.workspaces == (
        erasure.Workspace(
            "team",
            "person",
            "workspace",
            "stable-google-user",
            ("old-address@example.com", "person@example.com"),
        ),
    )
    assert {claim["scope"] for claim in world.claims_sent} == {
        gdrive.ReadScope.DIRECTORY_USERS.value
    }
    assert {claim["sub"] for claim in world.claims_sent} == {"directory@example.com"}
    assert not world.native_requests and not world.writes
    assert world.pending["crawl_erasure_enc"] == encoded
    assert "person@example.com" not in prepared and "stable-google-user" not in prepared


async def test_absent_crawl_history_does_not_invent_google_provider_configuration(captured_world):
    world, encoded = captured_world
    world.history = []
    prepared = erasure.load("scim-person-id", await erasure.prepare("scim-person-id", encoded))
    assert prepared.prepared and prepared.workspaces == ()
    assert not world.provider_calls and not world.native_requests


async def test_retired_customer_without_current_authority_cannot_use_another_workspaces_key(
    captured_world,
):
    world, encoded = captured_world
    world.history.append(
        dict(world.history[0], source_id="old-source", crawl_customer_id="old-workspace")
    )
    with pytest.raises(CredentialRefused, match="verified repair"):
        await erasure.prepare("scim-person-id", encoded)
    assert not world.provider_calls and world.pending["crawl_erasure_enc"] == encoded


async def test_provider_observation_cannot_outlive_source_retirement(captured_world):
    world, encoded = captured_world
    world.after_person = lambda: setattr(
        world, "source", dataclasses.replace(world.source, active=False)
    )
    with pytest.raises(CredentialRefused):
        await erasure.prepare("scim-person-id", encoded)
    assert world.pending["crawl_erasure_enc"] == encoded
    assert not erasure.load("scim-person-id", encoded).prepared and not world.native_requests


async def test_deleted_or_replaced_capture_never_resolves_a_potentially_reused_email(
    captured_world,
):
    world, encoded = captured_world
    world.pending.update(active=False, deleted=True)
    with pytest.raises(BackendUnavailableError):
        await erasure.prepare("scim-person-id", encoded)
    assert not world.provider_calls and world.pending["crawl_erasure_enc"] == encoded


async def test_capture_replacement_during_directory_response_cannot_publish_old_proof(
    captured_world,
):
    world, encoded = captured_world
    world.after_person = lambda: world.pending.update(crawl_erasure_enc="different-capture")
    with pytest.raises(BackendUnavailableError):
        await erasure.prepare("scim-person-id", encoded)
    assert not world.native_requests and not world.writes


async def test_canonical_contact_rebinding_during_directory_response_refuses_old_identity(
    captured_world,
):
    world, encoded = captured_world
    world.after_person = lambda: setattr(world, "current_contact", "different-person@example.com")
    with pytest.raises(BackendUnavailableError):
        await erasure.prepare("scim-person-id", encoded)
    assert world.pending["crawl_erasure_enc"] == encoded
    assert not erasure.load("scim-person-id", encoded).prepared and not world.native_requests


@pytest.mark.parametrize("budget", [0.25, 1.0])
async def test_configured_capture_deadline_rechecks_the_actual_directory_response(
    captured_world,
    monkeypatch: pytest.MonkeyPatch,
    budget: float,
) -> None:
    world, encoded = captured_world
    clock = time.monotonic()
    monkeypatch.setattr(settings, "knowledge_crawl_identity_capture_timeout_s", budget)
    monkeypatch.setattr(crawl_capture, "_now", lambda: clock)
    monkeypatch.setattr(google, "_now", lambda: clock)

    def spent() -> None:
        nonlocal clock
        clock += 0.5

    world.after_person = spent
    if budget < 0.5:
        with pytest.raises(TimeoutError):
            await erasure.prepare("scim-person-id", encoded)
    else:
        prepared = await erasure.prepare("scim-person-id", encoded)
        captured = erasure.load("scim-person-id", prepared)
        assert captured.prepared and captured.workspaces[0].user == "stable-google-user"
    assert world.pending["crawl_erasure_enc"] == encoded
    assert not world.native_requests and not world.writes


async def test_inactive_person_can_prepare_but_deleted_or_replaced_capsule_cannot(
    captured_world,
) -> None:
    world, encoded = captured_world
    world.pending["active"] = False
    prepared = await erasure.prepare("scim-person-id", encoded)
    assert erasure.load("scim-person-id", prepared).prepared
    assert world.pending["crawl_erasure_enc"] == encoded
    before = len(world.provider_calls)
    world.pending["deleted"] = True
    with pytest.raises(BackendUnavailableError):
        await erasure.prepare("scim-person-id", encoded)
    world.pending.update(deleted=False, crawl_erasure_enc="replacement")
    with pytest.raises(BackendUnavailableError):
        await erasure.prepare("scim-person-id", encoded)
    assert len(world.provider_calls) == before and not world.native_requests
