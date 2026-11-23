"""Current source authority cannot replace the requesting reader's authority."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth import reader_identity
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.auth.reader_identity import ReaderIdentity
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import source_access
from server.operations.base import Role
from tests.unit.test_mcp_source_sync import MIRROR, Harness
from tests.unit.test_mcp_source_sync import harness as harness


@pytest.fixture
def reader(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    auth = AuthInfo(
        subject="owner",
        org="org",
        namespaces=("collection",),
        roles=(Role.VIEWER,),
        is_service_account=False,
        issued_at=1000,
        session_id="source-reader",
    )
    monkeypatch.setattr(
        reader_identity, "settings", SimpleNamespace(auth_actually_required=lambda: True)
    )
    monkeypatch.setattr(authority.authz, "row_in_scope", AsyncMock(return_value=True))
    monkeypatch.setattr(
        authority,
        "resolve_reader",
        AsyncMock(
            side_effect=lambda org, subject: ReaderIdentity(subject, frozenset(), frozenset())
        ),
    )
    token = current_auth_var.set(auth)
    try:
        yield SimpleNamespace(auth=auth, source=harness.source)
    finally:
        current_auth_var.reset(token)


@pytest.mark.asyncio
async def test_owner_can_read_selected_snapshot(reader: SimpleNamespace) -> None:
    bound = await authority.validate_indexed_reader(reader.source, reader.auth)
    assert len(bound.selected_uris) == 1


@pytest.mark.asyncio
async def test_another_destination_reader_cannot_read_owner_only_material(
    reader: SimpleNamespace,
) -> None:
    other = replace(reader.auth, subject="other")
    current_auth_var.set(other)
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_indexed_reader(reader.source, other)


@pytest.mark.asyncio
async def test_a_changed_auth_context_refuses_before_source_access(reader: SimpleNamespace) -> None:
    current_auth_var.set(replace(reader.auth, subject="other"))
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_indexed_reader(reader.source, reader.auth)


@pytest.mark.asyncio
async def test_machine_identity_cannot_infer_a_personal_source_owner(
    reader: SimpleNamespace,
) -> None:
    machine = replace(reader.auth, subject="machine", is_service_account=True)
    current_auth_var.set(machine)
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_indexed_reader(reader.source, machine)


@pytest.mark.asyncio
async def test_an_explicit_personal_reader_claim_keeps_existing_reader_semantics(
    reader: SimpleNamespace,
) -> None:
    personal = replace(
        reader.auth,
        subject="personal-token",
        is_service_account=True,
        reads_as_creator=True,
        creator="owner",
    )
    current_auth_var.set(personal)
    await authority.validate_indexed_reader(reader.source, personal)


@pytest.mark.asyncio
async def test_live_mode_cannot_serve_a_retained_indexed_copy(
    reader: SimpleNamespace,
    harness: Harness,
) -> None:
    harness.select(mode="live")
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_indexed_reader(harness.source, reader.auth)


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [False, True])
async def test_read_plan_admits_only_the_personal_source_reader(
    reader: SimpleNamespace,
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    other: bool,
) -> None:
    auth = replace(reader.auth, subject="other") if other else reader.auth
    current_auth_var.set(auth)
    plan = source_access.ReadPlan("org", _auth=auth)
    monkeypatch.setattr(
        authority.source_store, "latest_source", AsyncMock(side_effect=lambda *a: harness.source)
    )
    monkeypatch.setattr(
        source_access.mirrors_repo, "mirrors_of", AsyncMock(return_value={MIRROR: "collection"})
    )
    monkeypatch.setattr(source_access, "resolve_reader", authority.resolve_reader)
    monkeypatch.setattr(
        source_access.store,
        "listings",
        AsyncMock(
            return_value=[
                {
                    "namespace": MIRROR,
                    "source_id": harness.source.source_id,
                    "started_at": datetime.now(UTC),
                }
            ]
        ),
    )
    row = {
        "namespace": "collection",
        "name": harness.source.name,
        "source_id": harness.source.source_id,
        "source_kind": "mcp",
        "source_contract_version": 1,
        "credential_mode": "connected",
    }
    await source_access._prepare(plan, [row], auth.subject)
    if other:
        assert not plan.sources and not plan._connected_pins and not plan._mcp_uris
    else:
        assert plan.sources == {(MIRROR, harness.source.source_id): "collection"}
        assert len(plan._mcp_uris[("collection", harness.source.source_id)]) == 1
