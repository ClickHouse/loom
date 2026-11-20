"""Token policy is derived before cached admission and before startup writers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server import boot
from server.core import cell, cell_router, pg
from server.settings import settings
from tests.unit.clock_fake import ManualClock

OWN = "lsc_first_" + "a" * 64
LEGACY = "legacy-token-" + "x" * 40
INVALID = [LEGACY, "lsc_foreign_" + "b" * 64, "lsc_first_bad", ",".join([OWN] * 3)]


@pytest.fixture
def lifecycle(monkeypatch):
    clock = ManualClock()
    monkeypatch.setattr(cell.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "scim_token", OWN)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(cell, "_deployment_id", "unit-deployment")
    monkeypatch.setattr(cell, "_view", cell._View())
    monkeypatch.setattr(cell, "_memo", None)
    monkeypatch.setattr(cell, "_awaiting_first_read", False)
    monkeypatch.setattr(cell, "_code_orgs", None)
    monkeypatch.setattr(cell, "start_refresh_loop", lambda: "refresh-loop")
    monkeypatch.setattr(cell_router, "start_reader", lambda: "router-loop")
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", ())
    return clock


def _row(first=False):
    return {
        "first": {"is_first": first, "cell": "first"},
        "mode": "enforced",
        "own": {"current_fp": cell.as_value(cell.mine()), "next_fp": None},
        "tenancy": True,
        "marked": True,
    }


@pytest.mark.parametrize("raw", INVALID)
def test_late_first_read_cannot_open_invalid_policy(lifecycle, monkeypatch, raw):
    monkeypatch.setattr(settings, "scim_token", raw)
    assert cell.gate_reason()
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(return_value=_row()))
    assert asyncio.run(cell.refresh())
    assert cell.gate_reason(), "a successfully refreshed later cell must remain closed"
    assert not cell.readiness_check().ok


@pytest.mark.parametrize("raw", INVALID)
def test_policy_is_checked_before_an_accepted_view_memo(lifecycle, monkeypatch, raw):
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(return_value=_row()))
    assert asyncio.run(cell.refresh())
    assert cell.gate_reason() == ""
    assert cell._memo is not None
    view = cell._view
    monkeypatch.setattr(settings, "scim_token", raw)
    assert cell._view is view
    assert cell.gate_reason(), "the unchanged view memo must not bypass token policy"


@pytest.mark.parametrize("first,raw", [(True, LEGACY), (False, OWN)])
def test_valid_policy_survives_bounded_read_failure(lifecycle, monkeypatch, first, raw):
    monkeypatch.setattr(settings, "scim_token", raw)
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(return_value=_row(first)))
    assert asyncio.run(cell.refresh())
    assert cell.gate_reason() == ""
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(side_effect=OSError("unreachable")))
    assert not asyncio.run(cell.refresh())
    assert cell.gate_reason() == ""
    lifecycle.advance(settings.jwt_revocation_max_stale_seconds + 1)
    assert cell.gate_reason()


@pytest.mark.parametrize("entry", ["start_cell_gate", "start_cell_state"])
@pytest.mark.parametrize("raw", INVALID)
def test_known_invalid_policy_refuses_startup_before_background_tasks(
    lifecycle, monkeypatch, entry, raw
):
    monkeypatch.setattr(settings, "scim_token", raw)
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(return_value=_row()))
    loops = []
    monkeypatch.setattr(cell, "start_refresh_loop", lambda: loops.append("started"))
    with pytest.raises(RuntimeError, match="SCIM|SCIM_TOKEN"):
        asyncio.run(getattr(boot, entry)())
    assert loops == []


@pytest.mark.parametrize("entry", ["start_cell_gate", "start_cell_state"])
@pytest.mark.parametrize("first,raw", [(True, LEGACY), (False, OWN)])
def test_startup_reads_first_identity_before_judging_valid_policy(
    lifecycle, monkeypatch, entry, first, raw
):
    monkeypatch.setattr(settings, "scim_token", raw)
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(return_value=_row(first)))
    assert cell.first_identity() is None
    loops = asyncio.run(getattr(boot, entry)())
    assert loops and cell.gate_reason() == ""


@pytest.mark.parametrize("entry", ["start_cell_gate", "start_cell_state"])
def test_absent_identity_keeps_probe_capable_boot_gated(lifecycle, monkeypatch, entry):
    monkeypatch.setattr(settings, "scim_token", LEGACY)
    monkeypatch.setattr(pg, "fetchrow_reset", AsyncMock(return_value=dict(_row(), first=None)))
    loops = asyncio.run(getattr(boot, entry)())
    assert loops and cell.first_identity() is None and cell.gate_reason()


@pytest.mark.parametrize("failure", ["policy", "services"])
def test_worker_checks_cell_before_services_and_drains_before_leaving(
    lifecycle, monkeypatch, failure
):
    from server import worker_main
    from server.core import ch
    from server.insights.app import provider_barrier
    from server.workers import leader

    order = []
    tasks = []
    closed = []

    async def refuse():
        order.append("cell")
        if failure == "policy":
            raise RuntimeError("SCIM token policy refuses")
        tasks.append(asyncio.create_task(asyncio.Event().wait()))
        return list(tasks)

    async def services(_ch):
        order.append("services")
        raise RuntimeError("services refused")

    async def leave(*_args, **_kwargs):
        closed.extend(task.cancelled() for task in tasks)

    monkeypatch.setattr(worker_main.child, "read_deadlines", lambda *_a: None)
    monkeypatch.setattr(worker_main.child, "exit_past_deadline", lambda *_a: None)
    monkeypatch.setattr(worker_main.child, "beat", AsyncMock())
    monkeypatch.setattr(leader, "_CURRENT", leader.current())
    monkeypatch.setattr(ch, "get_client", lambda: type("Client", (), {"connect": AsyncMock()})())
    monkeypatch.setattr(boot, "open_storage_authority", AsyncMock())
    monkeypatch.setattr(boot, "close_storage_authority", AsyncMock())
    monkeypatch.setattr(boot, "leave_deployment", leave)
    monkeypatch.setattr(boot, "start_cell_state", refuse)
    monkeypatch.setattr(provider_barrier, "register_process", AsyncMock())
    monkeypatch.setattr(worker_main, "_start_services", services)
    with pytest.raises(RuntimeError, match="SCIM token policy|services refused"):
        asyncio.run(worker_main._serve(0, 0))
    assert order == (["cell"] if failure == "policy" else ["cell", "services"])
    assert closed == ([] if failure == "policy" else [True])


@pytest.mark.parametrize("raw,valid", [(OWN, True), (LEGACY, False)])
async def test_worker_enrolls_before_acceptance_and_checks_policy_before_business(
    lifecycle, monkeypatch, raw, valid
):
    from server import worker_main
    from server.core import ch, retirement
    from server.insights.app import provider_barrier
    from server.workers import leader

    events = []
    tasks = []
    observations = []
    gate = retirement.get_gate()
    monkeypatch.setattr(settings, "scim_token", raw)
    monkeypatch.setattr(provider_barrier, "_registered", False)
    monkeypatch.setattr(provider_barrier, "_OWN", provider_barrier._Own())

    class Heartbeat:
        def start(self):
            assert gate.role == "worker"
            events.append("enroll")

    async def acceptance(*_a, **_kwargs):
        observations.append((gate.role, gate.enrolled, provider_barrier._registered))
        events.append("acceptance")
        return _row()

    def refresh_loop():
        tasks.append(asyncio.create_task(asyncio.Event().wait()))
        return tasks[-1]

    async def business(_ch):
        assert cell.gate_reason() == ""
        events.append("business")
        raise RuntimeError("business refused")

    async def leave(*_a, **_kwargs):
        assert all(task.cancelled() for task in tasks)

    monkeypatch.setattr(provider_barrier, "_HEARTBEAT", Heartbeat())
    monkeypatch.setattr(pg, "fetchrow_reset", acceptance)
    monkeypatch.setattr(cell, "start_refresh_loop", refresh_loop)
    monkeypatch.setattr(worker_main.child, "read_deadlines", lambda *_a: None)
    monkeypatch.setattr(worker_main.child, "exit_past_deadline", lambda *_a: None)
    monkeypatch.setattr(worker_main.child, "beat", AsyncMock())
    monkeypatch.setattr(leader, "_CURRENT", leader.current())
    monkeypatch.setattr(ch, "get_client", lambda: type("Client", (), {"connect": AsyncMock()})())
    monkeypatch.setattr(boot, "open_storage_authority", AsyncMock())
    monkeypatch.setattr(boot, "close_storage_authority", AsyncMock())
    monkeypatch.setattr(boot, "leave_deployment", leave)
    monkeypatch.setattr(worker_main, "_start_services", business)
    with pytest.raises(RuntimeError, match="business refused|SCIM|SCIM_TOKEN"):
        await worker_main._serve(0, 0)
    assert observations == [("worker", True, True)]
    assert events == (["enroll", "acceptance", "business"] if valid else ["enroll", "acceptance"])
    assert bool(tasks) is valid
