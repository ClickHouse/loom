"""Current source authority cannot replace the requesting reader's authority."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth import reader_identity
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.auth.reader_identity import ReaderIdentity
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import source_access
from server.knowledge.app.mcp_contracts import encode_source
from server.knowledge.domain.access_list import AccessList, to_json
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.mcp_read_evidence import ReadProof
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
        assert not plan.sources and not plan._connected_pins and not plan._mcp_scopes
    else:
        assert plan.sources == {(MIRROR, harness.source.source_id): "collection"}
        assert len(plan._mcp_scopes[(MIRROR, harness.source.source_id)].selected_uris) == 1


async def installed_plan(reader, harness, monkeypatch, *, mode="connected"):
    row = {
        "namespace": "collection",
        "name": harness.source.name,
        "source_id": harness.source.source_id,
        "source_kind": "mcp",
        "source_contract_version": 1,
        "credential_mode": mode,
    }
    monkeypatch.setattr(
        authority.source_store, "latest_source", AsyncMock(side_effect=lambda *a: harness.source)
    )
    monkeypatch.setattr(
        source_access.mirrors_repo, "mirrors_of", AsyncMock(return_value={MIRROR: "collection"})
    )
    monkeypatch.setattr(source_access, "resolve_reader", authority.resolve_reader)
    monkeypatch.setattr(source_access.store, "listings", AsyncMock(return_value=[]))
    monkeypatch.setattr(source_access.store, "active_sources", AsyncMock(return_value=[row]))
    monkeypatch.setattr(source_access.pg, "enabled", lambda: False)
    monkeypatch.setattr(source_access, "canonical_subject", AsyncMock(side_effect=lambda org, s: s))
    plan = source_access.ReadPlan("org", _auth=reader.auth, _reader_subject=reader.auth.subject)
    await source_access._prepare(plan, [row], reader.auth.subject)
    plan._bindings = tuple(
        (mirror, sid, original) for (mirror, sid), original in plan.sources.items()
    )
    return plan


def checked_document(harness):
    return {
        "org": "org",
        "namespace": MIRROR,
        "source_uri": resource_uri(harness.source.source_id, harness.contract.resources[0].id),
        "tags": ["source-id:" + harness.source.source_id],
        "revision": 1,
        "version": 1,
        "access_json": to_json(AccessList(owner="owner")),
        "mcp_scope_sha256": authority.source_scope_sha256(harness.source, harness.contract),
        "mcp_read_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.asyncio
async def test_mcp_shared_mode_header_cannot_bypass_owner_checks(reader, harness, monkeypatch):
    plan = await installed_plan(reader, harness, monkeypatch, mode="shared")
    assert not plan.sources and not plan._mcp_scopes
    authority.source_store.latest_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_document_requires_its_own_current_read_not_a_listing(
    reader, harness, monkeypatch
):
    plan = await installed_plan(reader, harness, monkeypatch)
    doc = checked_document(harness)
    monkeypatch.setattr(source_access.store, "metadata", AsyncMock(return_value=[doc]))
    candidates = [{"namespace": MIRROR, "source_uri": doc["source_uri"]}]
    assert not plan.listing_starts
    admitted = await plan.check(candidates)
    assert len(admitted) == 1 and plan.current(admitted[0])
    evidence = source_access.AccessEvidence.from_admitted(admitted[0])
    assert evidence.mcp_proof == ReadProof.from_row(doc)
    assert "content" not in vars(evidence)
    for mutation in (
        {"mcp_read_at": ""},
        {"mcp_scope_sha256": "f" * 64},
        {"mcp_read_at": (datetime.now(UTC) - timedelta(days=365)).isoformat()},
        {"mcp_read_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
    ):
        altered = {**doc, **mutation}
        monkeypatch.setattr(source_access.store, "metadata", AsyncMock(return_value=[altered]))
        assert await plan.check(candidates) == []
    assert not plan.failed


@pytest.mark.asyncio
async def test_removed_identity_and_aba_scope_cannot_reuse_published_evidence(
    reader, harness, monkeypatch
):
    plan = await installed_plan(reader, harness, monkeypatch)
    doc = checked_document(harness)
    monkeypatch.setattr(source_access.store, "metadata", AsyncMock(return_value=[doc]))
    candidates = [{"namespace": MIRROR, "source_uri": doc["source_uri"]}]
    admitted = await plan.check(candidates)
    retained = source_access.AccessEvidence.from_admitted(admitted[0])
    assert plan.current_evidence(retained)
    harness.contract = harness.contract.model_copy(update={"scope_revision": 3})
    config, items = encode_source(harness.contract)
    harness.source = replace(harness.source, generic_config_json=config, generic_items_json=items)
    assert await plan.check(candidates) == []
    assert not plan.current_evidence(retained)
    # A new request also refuses the historical proof, despite the identical selected ID.
    new_plan = await installed_plan(reader, harness, monkeypatch)
    assert await new_plan.check(candidates) == []
    doc["mcp_scope_sha256"] = authority.source_scope_sha256(harness.source, harness.contract)
    assert len(await new_plan.check(candidates)) == 1


@pytest.mark.asyncio
async def test_unselected_uri_does_not_borrow_another_selected_read_proof(
    reader, harness, monkeypatch
):
    plan = await installed_plan(reader, harness, monkeypatch)
    doc = checked_document(harness)
    doc["source_uri"] = resource_uri(harness.source.source_id, "not-selected")
    monkeypatch.setattr(source_access.store, "metadata", AsyncMock(return_value=[doc]))
    assert await plan.check([{"namespace": MIRROR, "source_uri": doc["source_uri"]}]) == []
