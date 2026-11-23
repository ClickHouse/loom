"""Authenticated operator metadata pages use the common signed cursor."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError as ModelValidationError

import server.settings as settings_module
from server.auth import reader_identity
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.core.cursors import decode_bound_cursor, encode_bound_cursor
from server.core.errors import LoomServerError, ValidationError
from server.knowledge.api.models_mcp_profiles import MCPProfilesInput, MCPProfilesOutput
from server.knowledge.api.ops_mcp_profiles import OPERATIONS
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mcp_profile_api, mcp_profiles
from server.knowledge.app.mcp_profile_api import handle_mcp_profiles
from server.knowledge.app.mcp_profiles import MCPProfileError, load_mcp_profile_catalog
from server.operations.base import Role
from server.settings._live import LIVE
from server.settings.catalog import KnowledgeSettings


def profile(identity: str, orgs: list[str], **changes: Any) -> dict[str, Any]:
    value = {
        "id": identity,
        "label": "Opaque document label",
        "auth": "bearer",
        "allowed_orgs": orgs,
        "config": {
            "endpoint": "https://offline.invalid/mcp",
            "bearer_audience": "https://offline.invalid/audience",
        },
    }
    value.update(changes)
    return value


def configure(world: SimpleNamespace, *rows: dict[str, Any]) -> None:
    world.settings.knowledge_source_mcp_profiles_json = json.dumps({"version": 1, "profiles": rows})


def auth(subject: str = "person-a", org: str = "org-a", **changes: Any) -> AuthInfo:
    caller = AuthInfo(
        subject=subject,
        org=org,
        namespaces=("personal",),
        roles=(Role.VIEWER,),
        is_service_account=False,
        issued_at=1000,
        session_id="session-" + subject,
    )
    return replace(caller, **changes)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    fake = SimpleNamespace(
        knowledge_source_mcp_profiles_json="",
        knowledge_source_self_service_enabled=True,
        jwt_secret="unit-profile-cursor-key",
        auth_actually_required=lambda: True,
    )
    state = SimpleNamespace(settings=fake, active=True, managed_denial=False, revoked={}, seen=[])

    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("metadata listing must not open HTTP, sockets, DNS, sessions or account rows")

    async def canonical(org: str, subject: str) -> str:
        state.seen.append((org, subject))
        return subject

    async def identity(org: str, subject: str) -> SimpleNamespace:
        return SimpleNamespace(subject=subject, emails=())

    async def spellings(org: str, subject: str) -> list[str]:
        return [subject]

    async def active(org: str, subjects: list[str]) -> bool:
        return state.active

    async def managed(subject: str, *, org: str, also: tuple[str, ...]) -> bool:
        return state.managed_denial

    async def cutoffs(keys: list[str]) -> dict[str, int]:
        return state.revoked

    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(mcp_profiles, "MCPSourceClient", refuse)
    monkeypatch.setattr(accounts.store, "owned", refuse)
    monkeypatch.setattr(mcp_profiles, "settings", fake)
    monkeypatch.setattr(mcp_profile_api, "settings", fake, raising=False)
    monkeypatch.setattr(settings_module, "settings", fake)
    monkeypatch.setattr(accounts, "settings", fake)
    monkeypatch.setattr(reader_identity, "settings", fake)
    monkeypatch.setattr(accounts.aliases, "canonical_subject", canonical)
    monkeypatch.setattr(accounts.aliases, "verified_spellings", spellings)
    monkeypatch.setattr(accounts, "resolve_reader", identity)
    monkeypatch.setattr(accounts.store, "account_active", active)
    monkeypatch.setattr(accounts.scim, "refuses_subject", managed)
    monkeypatch.setattr(accounts.revocation, "cutoffs", cutoffs)
    token = current_auth_var.set(auth())
    try:
        yield state
    finally:
        current_auth_var.reset(token)


def test_setting_is_empty_bounded_and_not_live() -> None:
    field = KnowledgeSettings.model_fields["knowledge_source_mcp_profiles_json"]
    assert field.default == ""
    assert field.repr is False
    assert KnowledgeSettings().knowledge_source_mcp_profiles_json == ""
    assert LIVE not in field.metadata
    with pytest.raises(ModelValidationError):
        KnowledgeSettings(knowledge_source_mcp_profiles_json="é" * (4 * 1024 * 1024 + 1))


@pytest.mark.asyncio
async def test_empty_setting_returns_no_profiles(world: SimpleNamespace) -> None:
    result = await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    assert result.model_dump() == {
        "profiles": [],
        "next_cursor": None,
        "self_service_enabled": True,
    }
    assert world.seen == [("org-a", "person-a")]


def test_loader_uses_actual_content_and_detaches_metadata(world: SimpleNamespace) -> None:
    configure(world, profile("first", ["org-a"]))
    first = load_mcp_profile_catalog()
    assert load_mcp_profile_catalog() is first
    configure(world, profile("second", ["org-b"]))
    second = load_mcp_profile_catalog()
    assert second.sha256 != first.sha256
    assert [item.id for item in first.page("org-a").items] == ["first"]
    assert second.page("org-a").items == ()
    world.settings.knowledge_source_mcp_profiles_json = "{secret-invalid-config"
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        load_mcp_profile_catalog()
    world.settings.knowledge_source_mcp_profiles_json = ""
    assert load_mcp_profile_catalog().page("org-a").items == ()


@pytest.mark.asyncio
async def test_two_org_users_only_see_eligible_safe_metadata(world: SimpleNamespace) -> None:
    configure(
        world,
        profile("a", ["org-a"], allow_collection_copy=True),
        profile("b", ["org-b"]),
        profile("c", []),
    )
    first = await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    current_auth_var.set(auth("person-b", "org-b"))
    second = await handle_mcp_profiles(MCPProfilesInput(org="org-b"))
    assert [item.id for item in first.profiles] == ["a"]
    assert [item.id for item in second.profiles] == ["b"]
    assert first.profiles[0].allow_collection_copy is True
    assert set(first.profiles[0].model_dump()) == {
        "id",
        "label",
        "auth",
        "profile_sha256",
        "endpoint_origin",
        "approved_tools",
        "allow_collection_copy",
    }
    serialized = first.model_dump_json()
    assert first.profiles[0].endpoint_origin == "https://offline.invalid"
    for forbidden in ("allowed_orgs", '"endpoint"', "/mcp", "audience", "config", "total"):
        assert forbidden not in serialized
    first.profiles[0].approved_tools.append("unapproved")
    current_auth_var.set(auth())
    fresh = await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    assert fresh.profiles[0].approved_tools == []
    assert world.seen == [("org-a", "person-a"), ("org-b", "person-b"), ("org-a", "person-a")]


@pytest.mark.asyncio
async def test_wrong_org_refuses_before_loading_invalid_operator_setting(
    world: SimpleNamespace,
) -> None:
    world.settings.knowledge_source_mcp_profiles_json = "{private-malformed-setting"
    with pytest.raises(ValidationError) as caught:
        await handle_mcp_profiles(MCPProfilesInput(org="org-b", query="private"))
    assert caught.value.to_detail() == {
        "code": "CONNECTION_UNAVAILABLE",
        "message": "the connected account is unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"is_service_account": True},
        {"reads_as_creator": True, "creator": "person-a"},
        {"issued_at": None},
        {"session_id": ""},
        {"org": "*"},
        {"impersonated_by": "other"},
    ],
)
async def test_connection_list_personal_identity_contract_is_retained(
    world: SimpleNamespace,
    changes: dict[str, Any],
) -> None:
    world.settings.knowledge_source_mcp_profiles_json = "{private-malformed-setting"
    current_auth_var.set(auth(**changes))
    with pytest.raises(LoomServerError) as caught:
        await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    assert "MCP_PROFILE_INVALID" not in str(caught.value)
    assert world.seen == []


@pytest.mark.asyncio
async def test_missing_auth_refuses_before_catalog(world: SimpleNamespace) -> None:
    current_auth_var.set(None)
    with pytest.raises(LoomServerError):
        await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    assert world.seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["active", "managed", "revoked"])
async def test_current_identity_standing_is_rechecked(world: SimpleNamespace, denial: str) -> None:
    configure(world, profile("approved", ["org-a"]))
    assert (await handle_mcp_profiles(MCPProfilesInput(org="org-a"))).profiles
    if denial == "active":
        world.active = False
    elif denial == "managed":
        world.managed_denial = True
    else:
        world.revoked = {"person-a": 1001}
    with pytest.raises(LoomServerError):
        await handle_mcp_profiles(MCPProfilesInput(org="org-a"))


@pytest.mark.asyncio
async def test_signed_paging_filters_org_before_continuation(world: SimpleNamespace) -> None:
    configure(world, profile("c", ["org-a"]), profile("b", ["org-b"]), profile("a", ["org-a"]))
    first = await handle_mcp_profiles(MCPProfilesInput(org="org-a", limit=1))
    assert [item.id for item in first.profiles] == ["a"]
    assert first.next_cursor is not None and first.next_cursor != "a"
    catalog = load_mcp_profile_catalog()
    scope = ("knowledge.source.mcp_profiles", "org-a", "person-a", "", catalog.sha256, "1")
    anchor, after = decode_bound_cursor(first.next_cursor, scope)
    assert anchor == datetime(1970, 1, 1, tzinfo=UTC) and after == "a"
    last = await handle_mcp_profiles(
        MCPProfilesInput(org="org-a", limit=1, cursor=first.next_cursor)
    )
    assert [item.id for item in last.profiles] == ["c"]
    assert last.next_cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["query", "org", "user", "catalog", "limit", "key"])
async def test_common_cursor_refuses_scope_or_catalog_drift(
    world: SimpleNamespace, change: str
) -> None:
    configure(world, profile("a", ["org-a", "org-b"]), profile("b", ["org-a", "org-b"]))
    first = await handle_mcp_profiles(MCPProfilesInput(org="org-a", limit=1))
    args: dict[str, Any] = {"org": "org-a", "limit": 1, "cursor": first.next_cursor}
    if change == "query":
        args["query"] = "Opaque"
    elif change == "org":
        current_auth_var.set(auth("person-b", "org-b"))
        args["org"] = "org-b"
    elif change == "user":
        current_auth_var.set(auth("person-b"))
    elif change == "catalog":
        configure(world, profile("a", ["org-a"]), profile("c", ["org-a"]))
    elif change == "limit":
        args["limit"] = 2
    else:
        world.settings.jwt_secret = "rotated-unit-cursor-key"
    with pytest.raises(ValidationError, match="cursor is not one this op issued"):
        await handle_mcp_profiles(MCPProfilesInput(**args))


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["a", "!", "é", "", "not-an-issued-cursor"])
async def test_raw_or_invalid_continuation_is_refused(world: SimpleNamespace, cursor: str) -> None:
    configure(world, profile("a", ["org-a"]))
    with pytest.raises(ValidationError, match="cursor is not one this op issued"):
        await handle_mcp_profiles(MCPProfilesInput(org="org-a", cursor=cursor))


@pytest.mark.asyncio
async def test_wrong_common_cursor_anchor_refuses(world: SimpleNamespace) -> None:
    configure(world, profile("a", ["org-a"]))
    scope = (
        "knowledge.source.mcp_profiles",
        "org-a",
        "person-a",
        "",
        load_mcp_profile_catalog().sha256,
        "100",
    )
    cursor = encode_bound_cursor(scope, datetime(2000, 1, 1, tzinfo=UTC), "a")
    with pytest.raises(ValidationError, match="cursor is not one this op issued"):
        await handle_mcp_profiles(MCPProfilesInput(org="org-a", cursor=cursor))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [" ", "{secret", '{"version":1,"profiles":[],"secret":1}'])
async def test_invalid_operator_setting_has_fixed_public_error(
    world: SimpleNamespace, payload: str
) -> None:
    world.settings.knowledge_source_mcp_profiles_json = payload
    with pytest.raises(ValidationError) as caught:
        await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    assert caught.value.to_detail() == {
        "code": "VALIDATION_FAILED",
        "message": "MCP_PROFILE_INVALID",
    }
    assert "secret" not in repr(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"query": "a" * 257},
        {"cursor": "a" * 2049},
        {"endpoint": "https://injected.invalid"},
        {"token": "secret"},
        {"profile_json": "{}"},
    ],
)
def test_wire_request_accepts_only_bounded_listing_arguments(changes: dict[str, Any]) -> None:
    with pytest.raises(ModelValidationError):
        MCPProfilesInput(org="org-a", **changes)


def test_operation_is_read_only_and_uses_connection_list_role_contract() -> None:
    assert len(OPERATIONS) == 1
    operation = OPERATIONS[0]
    assert operation.name == "knowledge.source.mcp_profiles"
    assert operation.handler is handle_mcp_profiles
    assert operation.input_model is MCPProfilesInput
    assert operation.output_model is MCPProfilesOutput
    assert operation.read_only and operation.required_role == Role.VIEWER
    assert operation.skip_namespace_authz
    assert not operation.deployment_scoped and not operation.writes and not operation.audit
    assert operation.maturity == "experimental"
    assert mcp_profile_api.handle_mcp_profiles is operation.handler


@pytest.mark.asyncio
async def test_query_matches_only_eligible_id_or_label(world: SimpleNamespace) -> None:
    configure(
        world,
        profile("alpha", ["org-a"], label="Read manuals"),
        profile("beta", ["org-a"], label="Other documents"),
        profile("hidden", ["org-b"], label="Read private material"),
    )
    result = await handle_mcp_profiles(MCPProfilesInput(org="org-a", query="READ"))
    assert [item.id for item in result.profiles] == ["alpha"]
    assert result.next_cursor is None
    missing = await handle_mcp_profiles(MCPProfilesInput(org="org-a", query="hidden"))
    assert missing.model_dump() == {
        "profiles": [],
        "next_cursor": None,
        "self_service_enabled": True,
    }


@pytest.mark.asyncio
async def test_signed_cursor_does_not_preserve_a_revoked_identity(world: SimpleNamespace) -> None:
    configure(world, profile("a", ["org-a"]), profile("b", ["org-a"]))
    first = await handle_mcp_profiles(MCPProfilesInput(org="org-a", limit=1))
    assert first.next_cursor is not None
    world.active = False
    world.settings.knowledge_source_mcp_profiles_json = "{private-invalid-config"
    with pytest.raises(LoomServerError) as caught:
        await handle_mcp_profiles(MCPProfilesInput(org="org-a", limit=1, cursor=first.next_cursor))
    assert "MCP_PROFILE_INVALID" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_operator_registration_policy_is_visible_without_hiding_approved_metadata(
    world: SimpleNamespace, enabled: bool
) -> None:
    configure(world, profile("approved", ["org-a"]))
    world.settings.knowledge_source_self_service_enabled = enabled
    result = await handle_mcp_profiles(MCPProfilesInput(org="org-a"))
    assert result.self_service_enabled is enabled
    assert [item.id for item in result.profiles] == ["approved"]
