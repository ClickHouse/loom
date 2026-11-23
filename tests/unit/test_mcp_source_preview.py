"""A destination never supplies missing upstream publication authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_mcp_connections import MCPConnectionEntry
from server.knowledge.api.models_mcp_sources import MCPSourcePreviewInput
from server.knowledge.app import mcp_source_preview as preview
from server.knowledge.app.mcp_source_preview import access_rule
from server.operations.base import Role


@pytest.mark.parametrize("team", [False, True])
def test_owner_only_is_independent_of_destination_membership(team: bool) -> None:
    rule = access_rule("snapshot", "owner_only", copy_approved=False, administrator=team)
    assert rule.access == "owner_only"
    assert not rule.collection_readers_receive_copy
    assert rule.upstream_permissions == "unsupported"
    assert not rule.reader_list_complete


@pytest.mark.parametrize("mode", ["snapshot", "selected_refresh"])
def test_copy_rule_names_inherited_and_future_readers(mode: str) -> None:
    rule = access_rule(mode, "collection_copy", copy_approved=True, administrator=True)
    assert rule.access == "collection_wide"
    assert rule.collection_readers_receive_copy
    assert rule.inherited_grants_apply
    assert rule.membership_changes_affect_copy
    assert rule.publication_confirmation_required
    assert rule.upstream_permissions == "unsupported"


@pytest.mark.parametrize("approved,admin", [(False, False), (False, True), (True, False)])
def test_copy_requires_operator_policy_and_current_destination_authority(
    approved: bool, admin: bool
) -> None:
    rule = access_rule("snapshot", "collection_copy", copy_approved=approved, administrator=admin)
    assert rule.access == "unavailable"
    assert not rule.collection_readers_receive_copy
    assert rule.reason == "publication_not_authorized"


def test_live_copy_policy_cannot_create_implicit_shared_connection() -> None:
    rule = access_rule("live", "collection_copy", copy_approved=True, administrator=True)
    assert rule.access == "unavailable" and rule.reason == "shared_live_unsupported"


def test_live_owner_lookup_reports_no_stored_copy() -> None:
    rule = access_rule("live", "owner_only", copy_approved=False, administrator=False)
    assert rule.access == "owner_only" and not rule.stores_copy
    assert not rule.future_items_included


@pytest.mark.parametrize("mode,policy", [("unknown", "owner_only"), ("snapshot", "mirrored")])
def test_unknown_policy_is_unavailable_without_guessing(mode: str, policy: str) -> None:
    rule = access_rule(mode, policy, copy_approved=True, administrator=True)
    assert rule.access == "unavailable"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(admin=False, owner="owner-a", allowed=True, valid=True, enabled=True)
    account = SimpleNamespace(
        org="org-a",
        subject="owner-a",
        kind="mcp:" + "a" * 32,
        connection_id="b" * 32 + "/1" + "c" * 31,
    )

    async def owner(org: str, expected: str | None = None) -> str:
        if org != "org-a" or (expected is not None and expected != state.owner):
            raise preview.connections._Refused("MCP_CONNECTION_UNAVAILABLE")
        return state.owner

    async def scoped(org: str, namespace: str, role: Role) -> bool:
        assert (org, namespace) == ("org-a", "col-destination")
        return state.allowed and (state.admin or role == Role.WRITER)

    async def entry(account: object) -> MCPConnectionEntry:
        return MCPConnectionEntry(
            slot="mcp:" + "a" * 32,
            generation="b" * 32 + "/1" + "c" * 31,
            state="configured" if state.valid else "stopped",
        )

    monkeypatch.setattr(preview.connections, "_owner", owner)
    monkeypatch.setattr(preview.connections, "_entry", entry)
    state.get = AsyncMock(return_value=account)
    monkeypatch.setattr(preview.connections.store, "get", state.get)
    state.validate = AsyncMock()
    monkeypatch.setattr(preview.connections, "_validate", state.validate)
    monkeypatch.setattr(preview.authz, "row_in_scope", scoped)
    monkeypatch.setattr(preview.sources, "_require_source_namespace", AsyncMock())
    monkeypatch.setattr(preview.sources, "_require_no_erasure", AsyncMock())
    monkeypatch.setattr(
        preview.connections,
        "_binding",
        lambda account: (
            SimpleNamespace(metadata=SimpleNamespace(allow_collection_copy=True)),
            SimpleNamespace(profile_id="approved", profile_sha256="d" * 64),
        ),
    )
    monkeypatch.setattr(preview, "settings", SimpleNamespace(knowledge_source_sync_enabled=False))
    state.input = MCPSourcePreviewInput(
        org="org-a",
        namespace="col-destination",
        slot=account.kind,
        mode="selected_refresh",
        resource_ids=["opaque-selected-id"],
    )
    return state


@pytest.mark.asyncio
async def test_preview_is_explicit_about_disabled_refresh_and_unchecked_remote(
    world: SimpleNamespace,
) -> None:
    result = await preview.handle_mcp_source_preview(world.input)
    assert result.refresh_state == "disabled"
    assert result.compatibility == "unchecked" and result.permission_evidence == "unsupported"
    assert result.rule.access == "owner_only" and not result.preview_grants_authority
    assert "opaque-selected-id" not in result.model_dump_json()
    assert world.validate.await_count == 2


@pytest.mark.asyncio
async def test_wrong_org_refuses_before_account_read(world: SimpleNamespace) -> None:
    with pytest.raises(preview.connections._Refused):
        await preview.handle_mcp_source_preview(world.input.model_copy(update={"org": "org-b"}))
    world.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_scope_is_checked_on_returned_account(world: SimpleNamespace) -> None:
    world.owner = "owner-b"
    with pytest.raises(preview.connections._Refused):
        await preview.handle_mcp_source_preview(world.input)
    world.get.assert_awaited_once_with("org-a", "owner-b", "mcp:" + "a" * 32)


@pytest.mark.asyncio
async def test_stopped_account_has_no_readable_or_publishable_rule(world: SimpleNamespace) -> None:
    world.valid = False
    result = await preview.handle_mcp_source_preview(world.input)
    assert result.connection_state == "stopped" and result.rule.access == "unavailable"
    assert result.rule.reason == "connection_stopped"
    world.validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_does_not_treat_explanatory_copy_rule_as_confirmation(
    world: SimpleNamespace,
) -> None:
    world.admin = True
    input = world.input.model_copy(update={"reader_policy": "collection_copy"})
    result = await preview.handle_mcp_source_preview(input)
    assert result.rule.access == "collection_wide"
    assert result.rule.publication_confirmation_required and not result.preview_grants_authority
