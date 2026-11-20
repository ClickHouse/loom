"""Session-content admission uses global durable state before reading payloads."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.auth.session_protection import request_declaration
from server.core.errors import SessionProtectionRequiredError, classified
from server.knowledge.store import mirror_latch
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import gates


@pytest.mark.parametrize(
    "operation",
    [op for op in OPERATIONS_BY_NAME.values() if op.session_content],
    ids=lambda op: op.name,
)
@pytest.mark.parametrize("local_on", [False, True])
async def test_every_classified_operation_refuses_undeclared_clients(
    monkeypatch, operation, local_on
):
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", local_on)
    monkeypatch.setattr(mirror_latch, "enabled", AsyncMock(return_value=True))
    ensure = AsyncMock()
    monkeypatch.setattr(mirror_latch, "ensure", ensure)
    with request_declaration({"headers": []}):
        with pytest.raises(SessionProtectionRequiredError):
            await gates.session_content(operation, None)
    assert ensure.await_count == int(local_on)


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize(
    ("switch", "latched", "unavailable", "allowed"),
    [
        (False, False, False, True),
        (False, True, False, False),
        (False, False, True, False),
        (True, False, False, False),
        (True, True, False, False),
        (True, False, True, False),
    ],
)
async def test_durable_state_and_local_switch_have_one_constant_refusal(
    monkeypatch, declared, switch, latched, unavailable, allowed
):
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", switch)
    enabled = AsyncMock(return_value=latched)
    ensure = AsyncMock()
    if unavailable:
        enabled.side_effect = mirror_latch.LatchUnavailable()
        ensure.side_effect = mirror_latch.LatchUnavailable()
    monkeypatch.setattr(mirror_latch, "enabled", enabled)
    monkeypatch.setattr(mirror_latch, "ensure", ensure)
    headers = [(b"x-loom-session-protection", b"v1")] if declared else []
    with request_declaration({"headers": headers}):
        may_admit = (declared and not (switch and unavailable)) or allowed
        if may_admit:
            await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
        else:
            with pytest.raises(SessionProtectionRequiredError) as caught:
                await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
            assert classified(caught.value.to_detail(), caught.value.status_code) == {
                "code": "SESSION_PROTECTION_REQUIRED",
                "message": "This operation requires a client that protects session content",
                "category": "configuration",
                "retryable": False,
            }
    assert ensure.await_count == int(switch)
    assert enabled.await_count == int(not switch and not declared)


async def test_an_ordinary_op_never_reads_or_repairs_privacy_state(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", True)
    enabled, ensure = AsyncMock(), AsyncMock()
    monkeypatch.setattr(mirror_latch, "enabled", enabled)
    monkeypatch.setattr(mirror_latch, "ensure", ensure)
    await gates.session_content(OPERATIONS_BY_NAME["memory.get"], None)
    enabled.assert_not_awaited()
    ensure.assert_not_awaited()


async def test_an_offline_unknown_client_cannot_reuse_an_absent_decision(monkeypatch):
    enabled = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(mirror_latch, "enabled", enabled)
    with request_declaration({"headers": []}):
        await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
        with pytest.raises(SessionProtectionRequiredError):
            await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
    assert enabled.await_count == 2


async def test_cancellation_during_durable_repair_is_not_an_admission(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", True)
    ensure = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(mirror_latch, "ensure", ensure)
    with request_declaration({"headers": [(b"x-loom-session-protection", b"v1")]}):
        with pytest.raises(asyncio.CancelledError):
            await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)


@pytest.mark.parametrize("declared", [False, True])
async def test_http_protection_runs_after_authentication_before_body_or_validation(
    monkeypatch, declared
):
    from fastapi import Request, Response

    from server.transport import http

    class ReachedBody(Exception):
        pass

    phases = []

    async def authenticate(request):
        phases.append("authenticated")
        return None

    async def body(*args):
        phases.append("body")
        raise ReachedBody()

    monkeypatch.setattr(http, "_extract_auth", authenticate)
    monkeypatch.setattr(http, "_read_body", body)
    monkeypatch.setattr(mirror_latch, "enabled", AsyncMock(return_value=True))
    scope = {"type": "http", "headers": []}
    endpoint = http._make_endpoint(OPERATIONS_BY_NAME["conversation.create"])
    headers = [(b"x-loom-session-protection", b"v1")] if declared else []
    with request_declaration({"headers": headers}):
        with pytest.raises(ReachedBody if declared else SessionProtectionRequiredError):
            await endpoint(Request(scope), Response())
    assert phases == (["authenticated", "body"] if declared else ["authenticated"])


async def test_http_authentication_refusal_precedes_the_deployment_read(monkeypatch):
    from fastapi import HTTPException, Request, Response

    from server.transport import http

    async def authenticate(request):
        raise HTTPException(401, "credential refused")

    monkeypatch.setattr(http, "_extract_auth", authenticate)
    enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(mirror_latch, "enabled", enabled)
    endpoint = http._make_endpoint(OPERATIONS_BY_NAME["conversation.create"])
    with pytest.raises(HTTPException) as caught:
        await endpoint(Request({"type": "http", "headers": []}), Response())
    assert caught.value.status_code == 401
    enabled.assert_not_awaited()


@pytest.mark.parametrize("declared", [False, True])
async def test_mcp_protection_precedes_identity_validation_replay_and_implicit_runs(
    monkeypatch, declared
):
    from server.transport import mcp_call

    class ReachedIdentity(Exception):
        pass

    op = OPERATIONS_BY_NAME["conversation.create"]
    monkeypatch.setattr(mcp_call, "_resolve_connection_tool", AsyncMock(return_value=op))
    identity = AsyncMock(side_effect=ReachedIdentity())
    monkeypatch.setattr(mcp_call, "_another_cells_org", identity)
    monkeypatch.setattr(mirror_latch, "enabled", AsyncMock(return_value=True))
    headers = [(b"x-loom-session-protection", b"v1")] if declared else []
    params = {"name": op.mcp_tool_name, "arguments": {"malformed": "private-canary"}}
    with request_declaration({"headers": headers}):
        if declared:
            with pytest.raises(ReachedIdentity):
                await mcp_call._call_tool(7, params)
        else:
            result = await mcp_call._call_tool(7, params)
            assert result["id"] == 7 and result["result"]["isError"]
            assert result["result"]["structuredContent"] == {
                "code": "SESSION_PROTECTION_REQUIRED",
                "message": "This operation requires a client that protects session content",
                "status": 409,
            }
            assert "private-canary" not in str(result)
    assert identity.await_count == int(declared)


async def test_unit_store_seam_runs_real_absence_proofs_and_observes_later_enablement(
    mirror_latch_world,
):
    world = mirror_latch_world
    with request_declaration({"headers": []}):
        await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
        await gates.session_content(OPERATIONS_BY_NAME["run.begin"], None)
        assert world.counts["barrier"] == 2 and not world.leases
        world.enabled = True  # The existing backend view still reports absence.
        with pytest.raises(SessionProtectionRequiredError):
            await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
        assert world.counts["barrier"] == 3 and not world.leases


async def test_unit_store_unreadability_refuses_before_http_body(monkeypatch, mirror_latch_world):
    from fastapi import Request, Response

    from server.transport import http

    world = mirror_latch_world
    world.fault = "wrong_engine"
    monkeypatch.setattr(http, "_extract_auth", AsyncMock(return_value=None))
    body = AsyncMock(side_effect=AssertionError("private body must not be read"))
    monkeypatch.setattr(http, "_read_body", body)
    endpoint = http._make_endpoint(OPERATIONS_BY_NAME["conversation.create"])
    with request_declaration({"headers": []}):
        with pytest.raises(SessionProtectionRequiredError):
            await endpoint(Request({"type": "http", "headers": []}), Response())
    body.assert_not_awaited()
    assert world.calls == ["engines"]


async def test_declared_switch_on_client_reasserts_the_real_latch_before_admission(
    monkeypatch, mirror_latch_world
):
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", True)
    with request_declaration({"headers": [(b"x-loom-session-protection", b"v1")]}):
        await gates.session_content(OPERATIONS_BY_NAME["conversation.create"], None)
    assert mirror_latch_world.enabled and mirror_latch_world.counts["set"] == 1


async def test_first_on_startup_and_existing_beat_reassert_durable_state(
    monkeypatch, mirror_latch_world
):
    from server import boot
    from server.settings import settings
    from server.workers import leader

    world = mirror_latch_world
    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", True)

    async def after_repair(ch):
        assert ch is world and world.enabled and world.counts["set"] == 2

    read_holder = AsyncMock(side_effect=after_repair)
    monkeypatch.setattr(leader, "read_holder", read_holder)
    await boot.require_session_protection(world)
    assert world.enabled and world.counts["set"] == 1
    # A lost row while the process is on must not look like a never-enabled boot.
    world.enabled = False
    await boot.refresh_instance_state(world)
    assert world.enabled and world.counts["set"] == 2
    read_holder.assert_awaited_once_with(world)


async def test_first_on_startup_failure_cannot_advance_to_the_instance_read(
    monkeypatch, mirror_latch_world
):
    from server import boot
    from server.settings import settings
    from server.workers import leader

    monkeypatch.setattr(settings, "knowledge_mirrored_sources_enabled", True)
    mirror_latch_world.fault = "wrong_engine"
    read_holder = AsyncMock()
    monkeypatch.setattr(leader, "read_holder", read_holder)
    with pytest.raises(mirror_latch.LatchUnavailable):
        await boot.require_session_protection(mirror_latch_world)
    with pytest.raises(mirror_latch.LatchUnavailable):
        await boot.refresh_instance_state(mirror_latch_world)
    read_holder.assert_not_awaited()
