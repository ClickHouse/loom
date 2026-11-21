"""Permanent retirement requires completion, never expiry or an API object's absence."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.core import provider_process
from server.insights.app import provider_barrier as barrier
from server.insights.store import provider_participants as store


@pytest.mark.asyncio
async def test_expired_quiet_participant_is_not_manual_death(monkeypatch):
    participant = uuid4()
    row = {
        "participant_id": participant,
        "transition_id": UUID(int=0),
        "version": 7,
        "state": "running",
        "expired": True,
        "host_proof": provider_process.host_proof(),
    }
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=[row]))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    monkeypatch.setattr(barrier, "record_participant", AsyncMock())
    assert await barrier.retire_participant(participant) == "live"
    barrier.record_participant.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_store_cannot_manufacture_retirement():
    ch = AsyncMock()
    with pytest.raises(ValueError, match="[Rr]etire|[Ss]tate"):
        await store.record_participant(uuid4(), UUID(int=0), "retired", ch=ch)
    ch.insert.assert_not_awaited()


def test_changed_namespace_does_not_prove_old_container_ended(monkeypatch):
    fields = provider_process.host_proof().split("|")
    assert len(fields) == 8
    fields[2] = "c" * 64
    fields[3] = "another-namespace"
    monkeypatch.setattr(provider_process, "_container", lambda: fields[2])
    assert not provider_process.proves_stopped("|".join(fields))


@pytest.mark.asyncio
async def test_close_is_permanent_through_an_awaited_retirement_write(monkeypatch):
    import asyncio

    from server.core import retirement

    gate = retirement.get_gate()
    gate.install("test")
    gate.enrolled = True
    gate.stopped()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def write(proof):
        assert gate.quiescent
        entered.set()
        await finish.wait()

    monkeypatch.setattr(barrier, "_OWN", barrier._Own())
    monkeypatch.setattr(barrier, "record_retirement", write)
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "flush_registered_buffers", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "_server_side_settled", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    task = asyncio.create_task(barrier.retire_process())
    await entered.wait()
    with pytest.raises(retirement.ProcessStopping):
        async with gate.admit():
            pytest.fail("Permanent closure must not reopen while insert is awaited")
    with pytest.raises(retirement.ProcessStopping):
        await barrier.register_process()
    finish.set()
    assert await task


def _request_app(handler):
    from fastapi import FastAPI

    from server.main import attach_browser_middleware
    from server.transport import http
    from server.transport.model_maintenance import ModelMaintenanceMiddleware
    from server.transport.retirement import RetirementMiddleware

    app = FastAPI()
    app.add_api_route("/oauth/token", handler, methods=["POST"])
    app.add_middleware(ModelMaintenanceMiddleware)
    attach_browser_middleware(app)
    http.attach(app)
    app.add_middleware(RetirementMiddleware)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_tail", [False, True])
async def test_composed_request_tail_holds_counter_after_dispatch_raises(monkeypatch, cancel_tail):
    import asyncio

    import httpx

    from server.core import request_tail, retirement

    gate = retirement.get_gate()
    entered, finish, canceled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def write():
        observed.append(gate.active)
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            canceled.set()
            await finish.wait()  # cancellation requested; this writer still exists

    async def handler():
        assert gate.active == 2
        assert request_tail.defer(write)
        raise ValueError("before response start")

    app = _request_app(handler)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    monkeypatch.setattr(barrier, "record_retirement", AsyncMock())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        task = asyncio.create_task(client.post("/oauth/token"))
        await asyncio.wait_for(entered.wait(), 5)
        assert gate.active == 1  # actual handler ended, outer tail remains counted
        assert observed == [1]
        gate.install("test")
        gate.enrolled = True
        gate.stopped()
        assert not await barrier.retire_process()
        assert not task.done()
        barrier.record_retirement.assert_not_awaited()
        if cancel_tail:
            task.cancel()
            await asyncio.wait_for(canceled.wait(), 5)
            assert gate.active == 1 and not task.done()
            assert not await barrier.retire_process()
        finish.set()
        await task
        assert gate.active == 0


@pytest.mark.asyncio
async def test_inner_scope_refuses_when_close_lands_after_outer_admission():
    from server.core import retirement
    from server.transport.model_maintenance import ModelMaintenanceMiddleware
    from server.transport.retirement import RetirementMiddleware

    gate = retirement.get_gate()
    handler, send = AsyncMock(), AsyncMock()

    async def between(scope, receive, send):
        assert gate.active == 1
        gate.close()
        await ModelMaintenanceMiddleware(handler)(scope, receive, send)
        assert gate.active == 1

    await RetirementMiddleware(between)(
        {"type": "http", "path": "/oauth/token", "method": "POST"}, AsyncMock(), send
    )
    handler.assert_not_awaited()
    assert send.await_args_list[0].args[0]["status"] == 503
    assert gate.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/oauth/token",
        "/oauth/callback",
        "/v1/auth/login",
        "/v1/scim/Users",
        "/v1/admin/models/reset",
    ],
)
async def test_canceled_control_dispatch_remains_counted_until_actual_completion(path):
    import asyncio

    from server.core import retirement
    from server.transport.model_maintenance import ModelMaintenanceMiddleware
    from server.transport.retirement import RetirementMiddleware

    gate = retirement.get_gate()
    entered, canceled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(scope, receive, send):
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            canceled.set()
            await finish.wait()
        assert gate.active == 2  # work could still mint/write here

    task = asyncio.create_task(
        RetirementMiddleware(ModelMaintenanceMiddleware(handler))(
            {"type": "http", "path": path, "method": "POST"}, AsyncMock(), AsyncMock()
        )
    )
    await entered.wait()
    task.cancel()
    await canceled.wait()
    gate.close()
    assert gate.active == 2 and not task.done()
    finish.set()
    await task
    assert gate.active == 0


@pytest.mark.asyncio
async def test_registered_descendant_blocks_after_outer_and_inner_completed(monkeypatch):
    import asyncio

    import httpx

    from server.core import retirement
    from server.core.background import create_deferred_task

    gate = retirement.get_gate()
    entered, finish = asyncio.Event(), asyncio.Event()
    tasks = []

    async def descendant():
        entered.set()
        await finish.wait()

    async def handler():
        tasks.append(create_deferred_task(descendant(), name="retirement-test-descendant"))
        return {"ok": True}

    app = _request_app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.post("/oauth/token")).status_code == 200
    await entered.wait()
    assert gate.active == 0
    gate.install("test")
    gate.enrolled = True
    gate.stopped()
    monkeypatch.setattr(barrier, "record_retirement", AsyncMock())
    assert not await barrier.retire_process()
    barrier.record_retirement.assert_not_awaited()
    finish.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_certificates_fail_closed_in_original_parsers():
    from pathlib import Path
    from types import ModuleType

    source = (
        Path(__file__).parents[1] / "fixtures" / "provider_process_before_retirement.py"
    ).read_text()
    previous = ModuleType("old_provider_process")
    exec(compile(source, "old_provider_process.py", "exec"), previous.__dict__)
    proof = provider_process.certified(provider_process.host_proof())
    assert len(proof.split("|")) == 9
    assert not previous.proves_stopped(proof)
    assert not await previous.pod_gone(proof)
    assert provider_process.has_certificate(proof)
    assert not provider_process.has_certificate(proof.replace("cell-retirement-v1", "unknown-v2"))
    assert not provider_process.has_certificate("||||||||cell-retirement-v1")


@pytest.mark.asyncio
async def test_uncertified_retired_rows_remain_census_blockers_and_unprunable():
    from types import SimpleNamespace

    identity = uuid4()
    row = {"participant_id": identity, "state": "retired", "version": 7, "host_proof": ""}
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[row]), execute=AsyncMock())
    assert await store.participants(ch=ch) == [row]
    assert await store.prune_retired(ch=ch) == 0
    ch.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_rechecks_latest_certificate_and_version():
    from types import SimpleNamespace

    identity = uuid4()
    proof = provider_process.certified(provider_process.host_proof())
    row = {"participant_id": identity, "state": "retired", "version": 7, "host_proof": proof}
    newer = {**row, "state": "running", "version": 8}
    ch = SimpleNamespace(query_all=AsyncMock(side_effect=[[row], [newer]]), execute=AsyncMock())
    assert await store.prune_retired(ch=ch) == 0
    ch.execute.assert_not_awaited()


def test_production_topology_installs_outer_and_inner_scopes():
    from server.main import verify_retirement_topology

    verify_retirement_topology()


@pytest.mark.parametrize("migrate", [False, True])
@pytest.mark.asyncio
async def test_server_enrolls_after_pair_verification_before_first_cell_read(monkeypatch, migrate):
    from types import SimpleNamespace

    from server import boot, main
    from server.auth import jwt
    from server.core import object_store
    from server.observability import instance
    from server.settings import settings

    events = []

    class FirstRead(Exception):
        pass

    async def paired(*args, **kwargs):
        events.append("paired")

    async def enrolled():
        assert events[-1] == "paired"
        events.append("enrolled")

    async def first_read():
        assert events[-1] == "enrolled"
        events.append("read")
        raise FirstRead

    async def migrations(*args, enabled, **kwargs):
        assert enabled is migrate
        return 0  # disabled and no-pending-migrations boots both need enrollment

    monkeypatch.setattr(instance, "require_instance_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(jwt, "require_real_signing_secret", lambda **kwargs: None)
    monkeypatch.setattr(boot, "require_identity_settings", lambda: None)
    monkeypatch.setattr(boot, "require_safe_bind", lambda **kwargs: None)
    monkeypatch.setattr(object_store, "require_client", lambda: None)
    monkeypatch.setattr(main, "get_client", lambda: SimpleNamespace(connect=AsyncMock()))
    monkeypatch.setattr(boot, "require_clickhouse_ready", AsyncMock())
    monkeypatch.setattr(boot, "register_readiness_checks", lambda ch: None)
    monkeypatch.setattr(boot, "run_fenced_boot_migrations", migrations)
    monkeypatch.setattr(boot, "require_keeper", AsyncMock())
    monkeypatch.setattr(boot, "require_storage_authority", paired)
    monkeypatch.setattr(boot, "start_cell_gate", first_read)
    monkeypatch.setattr(barrier, "register_process", enrolled)
    monkeypatch.setattr(settings, "boot_migrate", migrate)
    with pytest.raises(FirstRead):
        async with main.lifespan(main.app):
            pytest.fail("Only the first read is under test")
    assert events == ["paired", "enrolled", "read"]


def test_cli_connects_verified_then_enrolls_then_reads_acceptance(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from server.cli import _ch
    from server.core import pg_profile
    from server.insights.app import provider_command

    events = []
    client = SimpleNamespace(connected=True, disconnect=AsyncMock())

    async def verified(ch):
        assert ch is client
        events.append("verified")

    @asynccontextmanager
    async def command():
        events.append("enrolled")
        yield
        events.append("completed")

    async def accepting(orgs):
        assert events == ["verified", "enrolled"]
        events.append("read")

    async def work(ch):
        assert events[-1] == "read"
        events.append("work")
        return 7

    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(pg_profile, "check_configured", verified)
    monkeypatch.setattr(provider_command, "model_command", command)
    monkeypatch.setattr(_ch, "check_orgs", accepting)
    assert _ch.run_connected(work, orgs=("example",)) == 7
    assert events == ["verified", "enrolled", "read", "work", "completed"]


def test_retirement_writers_and_accepting_command_roots_are_an_explicit_census():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "server"
    constructors, writers, commands, stopped, plain = set(), set(), set(), set(), set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            relative = str(path.relative_to(root))
            if name == "RetirementProof":
                constructors.add(relative)
            if name == "record_retirement":
                writers.add(relative)
            if name == "model_command":
                commands.add(relative)
            if name == "participant_command":
                plain.add(relative)
            if name == "stopped" and isinstance(func, ast.Attribute):
                stopped.add(relative)
    assert constructors == {"core/retirement.py"}
    assert writers == {"insights/app/provider_barrier.py"}
    assert commands == {
        "cli/_ch.py",
        "cli/code_run.py",
        "cli/import_clickgap.py",
        "cli/import_recall.py",
        "cli/tracker_feed.py",
    }
    assert plain == {
        "cli/sql_access.py",
        "cli/analytics_export.py",
        "cli/maintenance.py",
        "insights/app/provider_command.py",
    }
    assert stopped == {"main.py", "worker_main.py", "insights/app/provider_command.py"}


def test_manual_retirement_has_no_affirmation_bypass():
    import argparse

    from server.cli.participant import register_participant_cli

    parser = argparse.ArgumentParser()
    register_participant_cli(parser.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["participant", "retire", "--participant", str(uuid4()), "--yes"])
    assert error.value.code == 2


def test_fork_cannot_inherit_admission(monkeypatch):
    from server.core import retirement

    gate = retirement.get_gate()
    gate.install("server")
    gate.enrolled = True
    monkeypatch.setattr(retirement, "_pid", -1)
    child = retirement.get_gate()
    assert child is not gate and child.role == "server" and not child.enrolled


@pytest.mark.asyncio
@pytest.mark.parametrize("api_state", ["404", "replacement", "Failed", "Succeeded", "unreachable"])
async def test_pod_object_death_never_retires_work_that_can_resume(
    monkeypatch, tmp_path, api_state
):
    import asyncio
    from pathlib import Path
    from types import ModuleType

    import httpx

    from server.core import retirement

    old = ModuleType("old_reaper")
    source = (
        Path(__file__).parents[1] / "fixtures" / "provider_process_before_retirement.py"
    ).read_text()
    exec(compile(source, "old_reaper.py", "exec"), old.__dict__)
    uid = str(uuid4())
    proof = "|".join(
        ("partitioned-host", "boot", uid + "/loom", "4026", "7", "99", "loom", "old-pod")
    )
    (tmp_path / "token").write_text("test-token")
    monkeypatch.setattr(old, "_SA", str(tmp_path))
    monkeypatch.setenv("POD_NAMESPACE", "loom")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.invalid")
    seen = []

    def answer(request):
        seen.append(request)
        if api_state == "unreachable":
            raise httpx.ConnectError("partitioned", request=request)
        return httpx.Response(
            404 if api_state == "404" else 200,
            json={
                "metadata": {"uid": str(uuid4()) if api_state == "replacement" else uid},
                "status": {"phase": api_state},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(answer))
    )
    # Demonstrate the exact old mistake before testing its replacement.
    assert await old.pod_gone(proof) is (api_state != "unreachable")
    participant = uuid4()
    row = {
        "participant_id": participant,
        "state": "running",
        "version": 9,
        "expired": True,
        "host_proof": proof,
    }
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=[row]))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    monkeypatch.setattr(barrier, "prune_retired", AsyncMock())
    monkeypatch.setattr(barrier, "record_retirement", AsyncMock())
    monkeypatch.setattr(barrier, "_STALE", None)
    resume, admitted = asyncio.Event(), asyncio.Event()
    effects = []

    async def paused():
        async with retirement.get_gate().admit():
            admitted.set()
            await resume.wait()
            effects.append("mint after object deletion")

    task = asyncio.create_task(paused())
    await admitted.wait()
    await barrier.sweep_stopped(now=5000)
    barrier.record_retirement.assert_not_awaited()
    assert barrier.stale_participants()[0]["participant_id"] == str(participant)
    assert len(seen) == 1  # the current reaper never asks the API for a death certificate
    resume.set()
    await task
    assert effects == ["mint after object deletion"]


@pytest.mark.asyncio
async def test_production_child_task_cancellation_stays_counted_until_completion():
    import asyncio
    import contextlib

    import anyio
    import httpx

    from server.core import retirement

    entered, finish = asyncio.Event(), asyncio.Event()
    gate = retirement.get_gate()

    async def handler():
        entered.set()
        with anyio.CancelScope(shield=True):
            await finish.wait()
        return {"ok": True}

    app = _request_app(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        task = asyncio.create_task(client.post("/oauth/token"))
        await entered.wait()
        task.cancel()
        # Let BaseHTTPMiddleware propagate cancellation to its downstream child.
        for _ in range(5):
            await asyncio.sleep(0)
        gate.close()
        assert gate.active > 0
        assert not task.done()
        finish.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert gate.active == 0


@pytest.mark.asyncio
async def test_shutdown_budget_does_not_wait_for_cancellation_deferring_drains(monkeypatch):
    import asyncio
    import time

    from server import boot, main
    from server.core import instances, retirement

    gate = retirement.get_gate()
    gate.install("test")
    gate.enrolled = True
    gate.stopped()
    canceled, finish = asyncio.Event(), asyncio.Event()
    owned = []

    async def drains(*args):
        owned.append(asyncio.current_task())
        try:
            await finish.wait()
        except asyncio.CancelledError:
            canceled.set()
            await finish.wait()

    async def leave(*args, **kwargs):
        assert not await barrier.retire_process()

    monkeypatch.setattr(instances, "stop", AsyncMock())
    monkeypatch.setattr(main, "_shutdown_drains", drains)
    monkeypatch.setattr(boot, "leave_deployment", leave)
    monkeypatch.setattr(boot, "SHUTDOWN_DRAIN_BUDGET_S", 0.01)
    monkeypatch.setattr(barrier, "record_retirement", AsyncMock())
    started = time.monotonic()
    await asyncio.wait_for(main._shut_down(None, None, None, None), timeout=0.5)
    assert time.monotonic() - started < 0.5
    await canceled.wait()
    assert not owned[0].done()
    barrier.record_retirement.assert_not_awaited()
    finish.set()
    await owned[0]


def test_nonmodel_principal_command_enrolls_before_acceptance(monkeypatch):
    from types import SimpleNamespace

    from server.cli import _ch, sql_access
    from server.core import pg_profile, retirement
    from server.insights.app import provider_command

    events = []
    client = SimpleNamespace(connected=True, disconnect=AsyncMock())

    async def verify(ch):
        events.append("verified")

    async def register():
        assert events == ["verified"]
        retirement.get_gate().enrolled = True
        events.append("registered")

    async def acceptance(orgs):
        assert events == ["verified", "registered"]
        events.append("acceptance")

    async def work(ch):
        assert events[-1] == "acceptance"
        assert retirement.get_gate().active == 1
        return 7

    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(pg_profile, "check_configured", verify)
    monkeypatch.setattr(provider_command, "register_process", register)
    monkeypatch.setattr(provider_command, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(provider_command, "flush_registered_buffers", AsyncMock(return_value=True))
    monkeypatch.setattr(provider_command, "_retire_until", AsyncMock(return_value=True))
    monkeypatch.setattr(_ch, "check_orgs", acceptance)
    assert sql_access._connected(work, orgs=("example",)) == 7
    assert retirement.get_gate().closed and retirement.get_gate().active == 0


@pytest.mark.asyncio
async def test_stream_and_response_background_cleanup_remain_counted(monkeypatch):
    import asyncio

    import httpx
    from starlette.background import BackgroundTask
    from starlette.responses import StreamingResponse

    from server.core import retirement

    gate = retirement.get_gate()
    body_entered, body_finish = asyncio.Event(), asyncio.Event()
    cleanup_entered, cleanup_finish = asyncio.Event(), asyncio.Event()

    async def body():
        yield b"first"
        body_entered.set()
        await body_finish.wait()
        yield b"last"

    async def cleanup():
        cleanup_entered.set()
        await cleanup_finish.wait()

    async def handler():
        return StreamingResponse(body(), background=BackgroundTask(cleanup))

    app = _request_app(handler)
    monkeypatch.setattr(barrier, "record_retirement", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        task = asyncio.create_task(client.post("/oauth/token"))
        await body_entered.wait()
        assert gate.active == 2
        gate.install("test")
        gate.enrolled = True
        gate.stopped()
        assert not await barrier.retire_process()
        body_finish.set()
        await cleanup_entered.wait()
        assert gate.active == 2 and not task.done()
        assert not await barrier.retire_process()
        cleanup_finish.set()
        assert (await task).content == b"firstlast"
    assert gate.active == 0


@pytest.mark.asyncio
async def test_websocket_lifetime_and_shutdown_refusal_share_the_same_counter():
    import asyncio

    from server.core import retirement

    entered, finish = asyncio.Event(), asyncio.Event()
    gate = retirement.get_gate()

    async def handler():
        return {}

    from starlette.websockets import WebSocket

    async def websocket(ws: WebSocket):
        await ws.accept()
        entered.set()
        await finish.wait()
        await ws.close()

    app = _request_app(handler)
    app.add_api_websocket_route("/socket", websocket)
    scope = {
        "type": "websocket",
        "path": "/socket",
        "scheme": "ws",
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "subprotocols": [],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 80),
    }
    task = asyncio.create_task(
        app(scope, AsyncMock(return_value={"type": "websocket.connect"}), AsyncMock())
    )
    await entered.wait()
    assert gate.active == 2
    gate.close()
    send = AsyncMock()
    await app(scope, AsyncMock(), send)
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1013})
    assert gate.active == 2
    finish.set()
    await task
    assert gate.active == 0
