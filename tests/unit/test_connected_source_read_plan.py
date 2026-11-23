"""Retained MCP source evidence keeps its owner, selection and reader boundaries."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth.context import current_auth_var
from server.auth.reader_identity import ReaderIdentity
from server.core import request_work
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import source_access as app
from server.knowledge.app.mcp_contracts import encode_source
from server.knowledge.domain.access_list import AccessList, to_json
from server.operations.base import Role
from tests.unit.test_mcp_source_read_policy import checked_document, installed_plan
from tests.unit.test_mcp_source_read_policy import reader as reader
from tests.unit.test_mcp_source_sync import MIRROR
from tests.unit.test_mcp_source_sync import harness as harness

pytestmark = pytest.mark.asyncio


@pytest.fixture
def world(reader, harness, monkeypatch):
    token = request_work._current.set(None)
    bag = request_work.begin()
    document = checked_document(harness)
    monkeypatch.setattr(app.store, "metadata", AsyncMock(side_effect=lambda *a: [dict(document)]))
    try:
        yield SimpleNamespace(reader=reader, harness=harness, bag=bag, document=document)
    finally:
        request_work._current.reset(token)


def _candidate(world):
    return {
        "namespace": MIRROR,
        "source_uri": world.document["source_uri"],
        "doc_version": 1,
        "revision": 1,
    }


async def _plan(world, monkeypatch):
    await installed_plan(world.reader, world.harness, monkeypatch)
    monkeypatch.setattr(app, "check_authorized", AsyncMock())
    monkeypatch.setattr(app.settings, "auth_required", True)
    return await app.prepare("org", ["collection"])


async def test_current_connected_owner_reads_exact_selected_document(world, monkeypatch):
    plan = await _plan(world, monkeypatch)
    assert plan.sources == {(MIRROR, world.harness.source.source_id): "collection"}
    [admitted] = await plan.check([_candidate(world)], chunks=True)
    assert admitted.document == world.document and admitted.namespace == "collection"
    assert plan.current(admitted)
    assert plan.marked and not plan.unread and world.bag.mirror_marked
    assert not world.harness.reads and not world.harness.doc_rows


async def test_namespace_viewer_with_a_document_acl_cannot_borrow_owner_only_authority(
    world, monkeypatch
):
    world.document["access_json"] = to_json(
        AccessList(owner="owner", emails=("reader@example.test",))
    )
    world.reader.auth = replace(world.reader.auth, subject="reader@example.test")
    current_auth_var.set(world.reader.auth)
    plan = await _plan(world, monkeypatch)
    assert plan.sources == {}
    assert await plan.check([_candidate(world)], chunks=True) == []
    assert not world.harness.reads and not world.harness.doc_rows


@pytest.mark.parametrize(
    "change", ["generation", "owner-grant", "scim", "selection", "scope", "auth"]
)
async def test_scope_refresh_removes_retained_evidence_after_authority_changes(
    world, monkeypatch, change
):
    h = world.harness
    plan = await _plan(world, monkeypatch)
    [admitted] = await plan.check([_candidate(world)], chunks=True)
    evidence = app.AccessEvidence.from_admitted(admitted)
    assert plan.current_evidence(evidence)
    if change == "generation":
        h.account = replace(
            h.account, connection_id=authority.accounts._next_connection(h.account.connection_id)
        )
    elif change == "owner-grant":
        monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=Role.VIEWER))
    elif change == "scim":
        monkeypatch.setattr(
            authority.accounts.scim, "refuses_subject", AsyncMock(return_value=True)
        )
    elif change == "selection":
        h.select(resources=("different-resource",))
    elif change == "scope":
        h.contract = h.contract.model_copy(update={"scope_revision": h.contract.scope_revision + 1})
        config, items = encode_source(h.contract)
        h.source = replace(h.source, generic_config_json=config, generic_items_json=items)
    else:
        current_auth_var.set(replace(world.reader.auth))
    assert await plan.check([_candidate(world)], chunks=True) == []
    assert not plan.current_evidence(evidence)
    assert plan.sources == {} and plan.listing_starts == {}
    assert not h.reads and not h.doc_rows


async def test_a_replaced_registration_cannot_join_an_existing_discovery_plan(world, monkeypatch):
    h = world.harness
    plan = await _plan(world, monkeypatch)
    [admitted] = await plan.check([_candidate(world)], chunks=True)
    original = h.source.source_id
    h.source = replace(h.source, source_id="22222222-2222-4222-8222-222222222222")
    assert await plan.check([_candidate(world)], chunks=True) == []
    assert not plan.current(admitted)
    assert plan.discovery_sources == {(MIRROR, original): "collection"}


@pytest.mark.parametrize("has_acl", [True, False])
async def test_explicit_collection_copy_keeps_independent_reader_acl(world, monkeypatch, has_acl):
    h = world.harness
    h.select(policy="collection_copy")
    monkeypatch.setattr(authority.authz, "grant_role_for", AsyncMock(return_value=Role.ADMIN))
    world.document.update(checked_document(h))
    world.document["access_json"] = to_json(
        AccessList(owner="owner", emails=("reader@example.test",) if has_acl else ())
    )
    world.reader.auth = replace(world.reader.auth, subject="reader")
    monkeypatch.setattr(
        authority,
        "resolve_reader",
        AsyncMock(
            return_value=ReaderIdentity("reader", frozenset({"reader@example.test"}), frozenset())
        ),
    )
    current_auth_var.set(world.reader.auth)
    plan = await _plan(world, monkeypatch)
    assert plan.sources == {(MIRROR, h.source.source_id): "collection"}
    admitted = await plan.check([_candidate(world)], chunks=True)
    if has_acl:
        assert len(admitted) == 1 and plan.current(admitted[0])
        assert admitted[0].document == world.document
    else:
        assert admitted == []
    assert world.bag.mirror_marked and not h.reads and not h.doc_rows
