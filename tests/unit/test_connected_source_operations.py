"""Local controls retain legacy ownership without executing retired connectors."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.core import request_work
from server.core.errors import ValidationError
from server.knowledge.api.models_sources import (
    KnowledgeSourceRemoveInput,
    KnowledgeSourceSyncNowInput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import sources
from server.knowledge.domain.sources import MIRRORED_ACCESS
from server.operations.base import Role
from tests.unit.principals import acting_as, principal
from tests.unit.test_knowledge_sources import NS, ORG, _add
from tests.unit.test_knowledge_sources import fake as fake


@pytest.fixture
def world(fake, monkeypatch):
    asyncio.run(sources.handle_knowledge_source_add(_add()))
    key = ORG, NS, "design-decks"
    source = replace(
        fake.rows[key],
        kind="gdrive",
        credential_mode="connected",
        owner_subject="owner",
        access=MIRRORED_ACCESS,
        mirror="acl-" + "a" * 24,
        config={},
        generic_config_json='{"retained":true}',
        generic_items_json='[{"opaque":true}]',
    )
    fake.rows[key] = source
    state = SimpleNamespace(store=fake, source=source, key=key, queued=[], role=Role.WRITER)

    async def subject(auth):
        assert auth.org == ORG and not auth.is_service_account
        return ORG, auth.subject

    async def queue(selected):
        state.queued.append(selected)
        return True

    monkeypatch.setattr(accounts, "own_subject", subject)

    async def in_scope(org, namespace, role):
        assert (org, namespace) == (ORG, NS)
        return state.role >= role

    monkeypatch.setattr(sources.authz, "row_in_scope", in_scope)
    monkeypatch.setattr(sources, "_queue_retirement", queue)
    fake.probes.clear()
    return state


def _auth(subject="owner", role=Role.WRITER):
    return principal(role, subject=subject, org=ORG, namespaces=(NS,), service_account=False)


async def _local_operation(operation):
    scope = {"org": ORG, "namespace": NS, "name": "design-decks"}
    if operation == "sync":
        return await sources.handle_knowledge_source_sync_now(
            KnowledgeSourceSyncNowInput(**scope, full=True)
        )
    return await sources.handle_knowledge_source_remove(
        KnowledgeSourceRemoveInput(**scope, confirm="design-decks", delete_documents=True)
    )


def _assert_result(state, result, operation):
    assert state.store.probes == []
    if operation == "sync":
        assert state.store.rows[state.key] == state.source
        assert state.queued == []
        detail = json.loads(state.store.states["design-decks"]["detail"])
        assert detail["full"] and detail["requested_at"]
    else:
        assert result.documents_pending == 0
        assert state.queued == [state.source]
        saved = state.store.rows[state.key]
        assert saved == replace(
            state.source, active=False, pending_delete=True, credential_enc="", secret_enc=""
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove"])
async def test_owner_can_request_local_sync_or_retirement_without_provider_work(world, operation):
    bag = request_work.RequestWork()
    with request_work.bind(bag), acting_as(_auth()):
        result = await _local_operation(operation)
        assert bag.mirror_marked
        assert request_work.flag_response(result.model_dump())["mirrored"] is True
    _assert_result(world, result, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove"])
async def test_foreign_writer_is_refused_but_current_admin_can_run_local_controls(world, operation):
    bag = request_work.RequestWork()
    with request_work.bind(bag), acting_as(_auth("operator")), pytest.raises(ValidationError):
        await _local_operation(operation)
    assert world.store.rows[world.key] == world.source
    assert world.store.states["design-decks"]["detail"] == "{}"
    assert world.queued == [] and world.store.probes == []
    world.role = Role.ADMIN
    with request_work.bind(bag), acting_as(_auth("operator", Role.ADMIN)):
        result = await _local_operation(operation)
        assert bag.mirror_marked
        assert request_work.flag_response(result.model_dump())["mirrored"] is True
    _assert_result(world, result, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.WRITER, Role.ADMIN])
async def test_retained_native_source_sync_refuses_without_executing_or_mutating(world, role):
    before = dict(world.store.states)
    with acting_as(_auth(role=role)), pytest.raises(ValidationError) as caught:
        await _local_operation("sync")
    assert caught.value.to_detail()["code"] == "SOURCE_NOT_SERVED"
    assert world.store.rows[world.key] == world.source
    assert world.store.states == before and world.queued == [] and world.store.probes == []
