"""A cell's own state gates the process: requests, tokens, workers, readiness and the
heartbeat (server/core/cell.py, docs/architecture/cells.md)."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, call

import pytest
from pydantic import ValidationError

from server import boot, worker_main
from server.auth import jwt
from server.core import cell, instances, pg, pg_profile
from server.core import ch as ch_mod
from server.settings import Settings, settings
from server.transport.model_maintenance import ModelMaintenanceMiddleware
from server.workers import leader
from tests.unit.clock_fake import ManualClock


def _cell_settings(monkeypatch: pytest.MonkeyPatch, name: str = "eu-1") -> None:
    monkeypatch.setattr(settings, "cell", name)
    monkeypatch.setattr(settings, "public_base_url", "https://eu-1.example.test")


def _view(monkeypatch: pytest.MonkeyPatch, **fields) -> None:
    monkeypatch.setattr(cell, "_view", cell._View(**fields))


def _serving_view(monkeypatch: pytest.MonkeyPatch, *, name: str = "eu-1", **over) -> None:
    fields = {
        "read_at": time.monotonic(),
        "first": (False, name),
        "mode": "enforced",
        "current": cell.as_value(cell.mine()),
        "next": None,
    }
    fields.update(over)
    _view(monkeypatch, **fields)


@pytest.mark.parametrize(
    "value,host",
    [
        ("https://EU-1.Example.Test/", "eu-1.example.test"),
        ("https://eu-1.example.test:443/mcp", "eu-1.example.test"),
        ("http://eu-1.example.test:80", "eu-1.example.test"),
        ("https://eu-1.example.test:8443", "eu-1.example.test:8443"),
        ("eu-1.example.test.", "eu-1.example.test"),
        ("https://[2001:DB8::1]:443/", "[2001:db8::1]"),
        ("https://bücher.example", "xn--bcher-kva.example"),
        ("", ""),
    ],
)
def test_normalise_host_gives_one_spelling(value: str, host: str) -> None:
    assert cell.normalise_host(value) == host


def test_a_value_matches_field_by_field_and_a_missing_field_only_at_its_default() -> None:
    mine = cell.fingerprint("dep-1")
    assert cell.matches(mine, cell.as_value(mine))
    other = dict(mine, oidc_client_id="someone-else")
    assert not cell.matches(mine, cell.as_value(other))
    # An older build's value without a field: equal only while this process is at its default.
    older = {k: v for k, v in mine.items() if k != "bucket"}
    assert cell.matches(mine, cell.as_value(older))
    assert not cell.matches(dict(mine, bucket="docs"), cell.as_value(older))
    # A newer build's extra field is not this build's to judge.
    assert cell.matches(mine, cell.as_value(dict(mine, future_field="x")))
    assert not cell.matches(mine, None)
    assert not cell.matches(mine, {"v": 1})


def test_the_prefix_counts_only_with_a_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    assert cell.fingerprint("d")["bucket_prefix"] == ""
    monkeypatch.setattr(settings, "document_s3_bucket", "docs")
    assert cell.fingerprint("d")["bucket_prefix"] == settings.document_s3_prefix


def test_nothing_is_gated_without_loom_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "")
    _view(monkeypatch)
    assert cell.gate_reason() == ""
    assert cell.readiness_check().ok


@pytest.mark.parametrize(
    "fields,fragment",
    [
        ({}, "not read yet"),
        ({"first": None}, "first row is absent"),
        ({"first": (True, "other")}, "names cell 'other'"),
        ({"mode": ""}, "tenancy mode row is absent"),
        ({"current": None}, "own row is absent"),
        ({"current": {"v": 1, "fields": {"oidc_client_id": "x"}}}, "matches neither value"),
    ],
)
def test_a_cell_is_gated_until_its_rows_accept_it(
    monkeypatch: pytest.MonkeyPatch, fields: dict, fragment: str
) -> None:
    _cell_settings(monkeypatch)
    if fields:
        _serving_view(monkeypatch, **fields)
    else:
        _view(monkeypatch)
    assert fragment in cell.gate_reason()
    assert not cell.readiness_check().ok


def test_current_or_next_lets_the_process_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_settings(monkeypatch)
    _serving_view(monkeypatch)
    assert cell.gate_reason() == ""
    # During an update the old value is current and this process's is next.
    _serving_view(
        monkeypatch,
        current=cell.as_value(dict(cell.mine(), oidc_client_id="old")),
        next=cell.as_value(cell.mine()),
    )
    assert cell.gate_reason() == ""


def test_a_failed_read_keeps_the_last_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_settings(monkeypatch)
    _serving_view(monkeypatch)

    async def _down(*_a, **_k):
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(pg, "fetchrow", _down)

    assert asyncio.run(cell.refresh()) is False
    assert cell.gate_reason() == ""
    # Only the error's kind: its text could name a host, and the reason reaches callers.
    assert cell._view.error == "ConnectionError"


def test_the_rows_are_reread_every_tenancy_mode_cache_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOOM_TENANCY_MODE_CACHE_S: a change to the mode or the own row reaches every
    process within it; before the first read, the loop retries within a second."""
    monkeypatch.setattr(settings, "tenancy_mode_cache_s", 7.0)
    waits: list[float] = []
    reads: list[int] = []

    async def _sleep(s: float) -> None:
        waits.append(s)
        if len(waits) == 3:
            raise asyncio.CancelledError

    async def _refresh() -> bool:
        reads.append(1)
        cell._view = cell._View(read_at=time.monotonic())
        return True

    monkeypatch.setattr(cell, "_view", cell._View())
    monkeypatch.setattr(cell.asyncio, "sleep", _sleep)
    monkeypatch.setattr(cell, "refresh", _refresh)

    async def _run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await cell.start_refresh_loop()

    asyncio.run(_run())
    assert waits == [1.0, 7.0, 7.0] and len(reads) == 2


def test_refresh_reads_the_three_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    value = cell.as_value(cell.mine())

    async def _row(sql, *_a):
        assert "loom.cell_first" in sql and "loom.tenancy_mode" in sql and "loom.cell_self" in sql
        return {
            # As the pool's json codec decodes them.
            "first": {"is_first": True, "cell": "eu-1"},
            "mode": "recording",
            "own": {"current_fp": value, "next_fp": None},
        }

    monkeypatch.setattr(pg, "fetchrow", _row)

    assert asyncio.run(cell.refresh()) is True
    assert cell.mode() == "recording"
    assert cell.gate_reason() == ""


@pytest.mark.parametrize(
    "over,fragment",
    [
        ({"cell": "EU_1"}, "is not 1 to 32"),
        ({"storage_authority": "clickhouse"}, "LOOM_STORAGE_AUTHORITY=postgres"),
        ({"mcp_auth_mode": "open"}, "authentication on every transport"),
        ({"public_base_url": ""}, "LOOM_PUBLIC_BASE_URL"),
    ],
)
def test_a_cell_refuses_to_boot_without_what_it_needs(
    monkeypatch: pytest.MonkeyPatch, over: dict, fragment: str
) -> None:
    _cell_settings(monkeypatch)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    assert cell.settings_problem() == ""
    for name, value in over.items():
        monkeypatch.setattr(settings, name, value)
    assert fragment in cell.settings_problem()
    with pytest.raises(RuntimeError, match="refusing to start"):
        cell.require_settings()


def test_verify_token_refuses_first_while_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    with pytest.raises(jwt.CellUnavailable, match="not read yet") as raised:
        jwt.verify_token("not.even.a.token")
    assert isinstance(raised.value, jwt.AuthUnavailable)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/memory.get"),
        ("POST", "/oauth/token"),
        ("GET", "/oauth/authorize"),
        ("POST", "/v1/auth/login"),
        ("GET", "/scim/v2/Users"),
        ("GET", "/"),
        ("GET", "/.well-known/jwks.json"),
        ("POST", "/mcp"),
    ],
)
async def test_a_gated_cell_answers_503_to_everything_but_health(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    app, send = AsyncMock(), AsyncMock()
    await ModelMaintenanceMiddleware(app)(
        {"type": "http", "method": method, "path": path}, AsyncMock(), send
    )
    app.assert_not_awaited()
    assert send.await_args_list[0].args[0]["status"] == 503
    assert b"AUTH_UNAVAILABLE" in send.await_args_list[1].args[0]["body"]


@pytest.mark.asyncio
async def test_a_gated_cell_closes_a_websocket_before_it_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    app, send = AsyncMock(), AsyncMock()
    await ModelMaintenanceMiddleware(app)({"type": "websocket", "path": "/mcp"}, AsyncMock(), send)
    app.assert_not_awaited()
    assert send.await_args_list == [call({"type": "websocket.close", "code": 1013})]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/health", "/v1/health/live", "/v1/health/ready"])
async def test_a_gated_cell_still_answers_its_health_probes(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    app = AsyncMock()
    await ModelMaintenanceMiddleware(app)(
        {"type": "http", "method": "GET", "path": path}, AsyncMock(), AsyncMock()
    )
    app.assert_awaited_once()


def test_a_gated_process_stands_aside_from_the_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leader.readiness, "draining", lambda: False)
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: [])
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    assert "this cell does not serve" in leader._stand_aside_reason()
    assert leader.SupervisorLease("auto").advertised() == ""
    _serving_view(monkeypatch)
    assert leader._stand_aside_reason() == ""


def test_an_on_process_runs_its_workers_only_while_its_cell_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    lease = leader.SupervisorLease("on")
    assert lease.held is False  # nothing starts at boot before the rows are read
    lease.step()
    assert lease.held is False and "not read yet" in lease.reason
    _serving_view(monkeypatch)
    lease.step()
    assert lease.held is True
    _view(monkeypatch)
    lease.step()
    assert lease.held is False


def test_an_on_process_outside_a_cell_holds_from_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "")
    lease = leader.SupervisorLease("on")
    assert lease.held is True
    lease.step()
    assert lease.held is True


def test_the_heartbeat_reports_the_cell_and_a_gated_process_is_not_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(instances.readiness, "draining", lambda: False)
    monkeypatch.setattr(instances.deployment_config, "drift", lambda: [])
    monkeypatch.setattr(settings, "serves_traffic", True)
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    assert instances.row()["serving"] == 0
    _serving_view(monkeypatch)
    assert instances.row()["serving"] == 1
    reported = instances.cell_row()
    assert reported["cell"] == "eu-1"
    assert reported["tenancy_mode"] == "enforced"
    assert reported["instance_id"] == instances.row()["instance_id"]
    assert json.loads(reported["fingerprint"]) == cell.as_value(cell.mine())


def test_outside_a_cell_the_heartbeat_still_reports_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prepare reads it from every live process before LOOM_CELL is rolled out.
    monkeypatch.setattr(settings, "cell", "")
    reported = instances.cell_row()
    assert reported["cell"] == "" and reported["tenancy_mode"] == ""
    assert json.loads(reported["fingerprint"])["v"] == cell.FINGERPRINT_VERSION


@pytest.mark.parametrize(
    "value",
    [
        "https://a..b",
        "https://" + "x" * 64 + ".example",
        "https://[::1",
        "https://[zz]/",
        "https://eu-1.example.test:99999",
        "https://eu-1.example.test:abc",
    ],
)
def test_an_invalid_host_normalises_to_empty(value: str) -> None:
    assert cell.normalise_host(value) == ""


def test_a_cell_refuses_to_boot_with_an_invalid_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_settings(monkeypatch)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "oauth_issuer", "https://a..b")
    assert "LOOM_OAUTH_ISSUER has no valid host" in cell.settings_problem()
    monkeypatch.setattr(settings, "public_base_url", "https://a..b")
    assert "LOOM_PUBLIC_BASE_URL with a valid host" in cell.settings_problem()


def test_a_value_of_a_later_fingerprint_version_matches_nothing() -> None:
    mine = cell.fingerprint("dep-1")
    assert not cell.matches(mine, {"v": cell.FINGERPRINT_VERSION + 1, "fields": dict(mine)})
    assert not cell.matches(mine, {"fields": dict(mine)})


def test_an_unprepared_cell_reads_absent_rows_and_stays_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cell_settings(monkeypatch)
    _view(monkeypatch)

    async def _empty(*_a, **_k):
        return {"first": None, "mode": None, "own": None}

    monkeypatch.setattr(pg, "fetchrow", _empty)
    assert asyncio.run(cell.refresh()) is True
    assert "first row is absent" in cell.gate_reason()


def test_a_cells_on_process_runs_the_lease_loop(monkeypatch: pytest.MonkeyPatch) -> None:

    async def _run() -> None:
        monkeypatch.setattr(settings, "cell", "")
        assert boot.start_lease_loop(leader.SupervisorLease("on"), object(), object()) is None
        _cell_settings(monkeypatch)
        task = boot.start_lease_loop(leader.SupervisorLease("on"), object(), object())
        assert isinstance(task, asyncio.Task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _view(monkeypatch)
    asyncio.run(_run())


def test_keep_lease_starts_and_stops_a_cells_on_workers_with_its_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leader, "_period_s", lambda _held: 0.01)
    monkeypatch.setattr(leader.readiness, "draining", lambda: False)
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    events: list[str] = []

    async def _on_lost() -> None:
        events.append("stop")

    async def _wait_for(n: int) -> None:
        for _ in range(500):
            if len(events) >= n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {events}")

    async def _run() -> None:
        lease = leader.SupervisorLease("on")
        task = asyncio.create_task(
            leader.keep_lease(lease, lambda: events.append("start"), _on_lost)
        )
        await asyncio.sleep(0.1)
        assert events == []  # gated: nothing starts
        _serving_view(monkeypatch)
        await _wait_for(1)
        _view(monkeypatch)
        await _wait_for(2)
        _serving_view(monkeypatch)
        await _wait_for(3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert events[:3] == ["start", "stop", "start"]


@pytest.mark.parametrize("bad", ['"x"', "[1, 2]", "3"])
def test_a_malformed_own_row_is_a_failed_read_and_keeps_the_last_view(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _cell_settings(monkeypatch)
    _serving_view(monkeypatch)

    async def _row(*_a, **_k):
        return {
            "first": {"is_first": False, "cell": "eu-1"},
            "mode": "enforced",
            "own": {"current_fp": json.loads(bad), "next_fp": None},
        }

    monkeypatch.setattr(pg, "fetchrow", _row)
    assert asyncio.run(cell.refresh()) is False
    assert cell._view.error == "MalformedRow"
    assert cell.gate_reason() == ""  # the last view, as for any failed read


@pytest.mark.parametrize("version", [0, -1, True, "1", None])
def test_only_this_builds_fingerprint_version_matches(version: object) -> None:
    mine = cell.fingerprint("dep-1")
    assert not cell.matches(mine, {"v": version, "fields": dict(mine)})
    assert not cell.matches(mine, [mine])  # type: ignore[arg-type]


def test_an_on_lease_waits_without_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_settings(monkeypatch)
    _serving_view(monkeypatch)
    lease = leader.SupervisorLease("on")
    lease.step()
    assert lease.held and lease.remaining_s() == float("inf")
    stopped: list[int] = []

    async def _on_lost() -> None:
        stopped.append(1)

    async def _run() -> bool:
        return await leader._until_done(asyncio.sleep(0.05), lease, True, _on_lost)

    assert asyncio.run(_run()) is True and stopped == []


def test_a_failed_read_keeps_the_last_rows_only_up_to_the_stale_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cell_settings(monkeypatch)
    monkeypatch.setattr(settings, "jwt_revocation_max_stale_seconds", 60.0)
    clock = ManualClock()
    monkeypatch.setattr(cell.time, "monotonic", clock.monotonic)
    _serving_view(monkeypatch, read_at=clock.monotonic(), error="ConnectionError")
    assert cell.gate_reason() == ""  # judged fresh, and the verdict kept for this read
    clock.advance(59)
    assert cell.gate_reason() == ""
    # The same view, no read since: its age is checked past the kept verdict.
    clock.advance(2)
    reason = cell.gate_reason()
    assert "last read 61 s ago" in reason and "ConnectionError" in reason
    assert not cell.readiness_check().ok


def test_the_workers_child_reads_the_cell_rows_before_its_workers_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    order: list[str] = []

    class _Done(Exception):
        pass

    class _Supervisor:
        def start_all(self) -> None:
            order.append("start_all")
            raise _Done

    class _Client:
        async def connect(self) -> None:
            return None

    async def _nothing(*_a, **_k) -> None:
        return None

    async def _services(_ch) -> list:
        order.append("services")
        return []

    async def _cell_state() -> list:
        order.append("cell")
        return []

    async def _beat(_fd) -> None:
        await asyncio.sleep(3600)

    _cell_settings(monkeypatch)
    # `_serve` sets the process's lease: restored after, so later passes see none.
    monkeypatch.setattr(leader, "_CURRENT", leader.current())
    monkeypatch.setattr(worker_main.child, "read_deadlines", lambda *_a: None)
    monkeypatch.setattr(worker_main.child, "exit_past_deadline", lambda *_a: None)
    monkeypatch.setattr(worker_main.child, "beat", _beat)
    monkeypatch.setattr(worker_main, "get_supervisor", lambda: _Supervisor())
    monkeypatch.setattr(worker_main, "_start_services", _services)
    monkeypatch.setattr(boot, "start_cell_state", _cell_state)
    monkeypatch.setattr(boot, "open_storage_authority", _nothing)
    monkeypatch.setattr(boot, "close_storage_authority", _nothing)
    monkeypatch.setattr(boot, "leave_deployment", _nothing)

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Client())
    with pytest.raises(_Done):
        asyncio.run(worker_main._serve(0, 0))
    assert order == ["services", "cell", "start_all"]


def test_the_stale_bound_leaves_room_for_three_cell_reads() -> None:

    with pytest.raises(ValidationError, match="three times LOOM_TENANCY_MODE_CACHE_S"):
        Settings(cell="eu-1", tenancy_mode_cache_s=900, jwt_revocation_max_stale_seconds=600)
    assert Settings(cell="eu-1", tenancy_mode_cache_s=200, jwt_revocation_max_stale_seconds=600)
    # Outside a cell nothing reads the rows, so nothing changes for a deployment that
    # is not one.
    assert Settings(tenancy_mode_cache_s=900, jwt_revocation_max_stale_seconds=600)


def test_a_failed_instance_beat_still_writes_the_cell_beat(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []

    class _CH:
        async def insert(self, table: str, rows: list) -> None:
            if table == instances.TABLE:
                raise ConnectionError("keeper")
            written.append(table)

        async def query_all(self, *_a, **_k) -> list:
            return []

    async def _nothing(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(instances.deployment_config, "check", _nothing)
    asyncio.run(instances.tick(_CH(), leader=False))
    assert written == [instances.CELL_TABLE]


@pytest.mark.parametrize("failing", ["loom.instances", "loom.cell_heartbeats"])
def test_a_failed_sweep_leaves_the_other_tables_sweep_to_run(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    swept: list[str] = []

    class _CH:
        async def execute(self, sql: str) -> None:
            swept.append(sql.split()[2])
            if failing in sql:
                raise ConnectionError("keeper")

    monkeypatch.setattr(instances, "_pruned_at", float("-inf"))
    asyncio.run(instances._prune(_CH()))  # each failure is logged by its own sweep
    assert swept == ["loom.instances", "loom.cell_heartbeats"]


@pytest.mark.parametrize("known", ["", "dep-boot"])
def test_a_process_without_the_deployment_id_reads_it_before_its_rows(
    monkeypatch: pytest.MonkeyPatch, known: str
) -> None:
    """The workers child has not run the authority check that records the id: its
    fingerprint would match no value of its row without it."""

    monkeypatch.setattr(cell, "_deployment_id", known)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="dep-pg"))
    monkeypatch.setattr(cell, "refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(cell, "start_refresh_loop", lambda: "loop")
    assert asyncio.run(boot.start_cell_state()) == ["loop"]
    assert cell.deployment_id() == (known or "dep-pg")


@pytest.mark.parametrize(
    "first,stored,read",
    [
        ((True, "eu-1"), "recording", "recording"),
        ((True, "eu-1"), "enforced", "enforced"),
        ((False, "eu-1"), "recording", "enforced"),  # only the first cell ever records
        ((False, "eu-1"), "enforced", "enforced"),
        (None, "", ""),
    ],
)
def test_only_the_first_cell_reads_recording(
    monkeypatch: pytest.MonkeyPatch, first: tuple[bool, str] | None, stored: str, read: str
) -> None:
    _view(monkeypatch, read_at=time.monotonic(), first=first, mode=stored)
    assert cell.mode() == read


def test_a_cells_on_process_registers_tracker_namespaces_only_in_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When its gate opens a cell's `on` process starts the workers, but only the
    deployment's leader registers the tracker namespaces."""
    _cell_settings(monkeypatch)
    _view(monkeypatch)
    lease = leader.SupervisorLease("on")
    monkeypatch.setattr(lease, "held", True)
    monkeypatch.setattr(leader, "current", lambda: lease)
    acquired: list = []

    async def _keep(_lease, on_acquired, _on_lost) -> None:
        acquired.append(on_acquired)

    spawned: list[str] = []

    def _spawn(coro, *, name: str) -> None:
        coro.close()
        spawned.append(name)

    monkeypatch.setattr(leader, "keep_lease", _keep)
    monkeypatch.setattr(boot, "_spawn_boot_task", _spawn)
    monkeypatch.setattr(boot, "publish_deployment_config", lambda _ch: None)
    started: list[bool] = []

    class _Supervisor:
        def start_all(self) -> None:
            started.append(True)

    async def _run(deployment: str) -> list[str]:
        monkeypatch.setattr(settings, "deployment", deployment)
        spawned.clear()
        await boot.start_lease_loop(lease, _Supervisor(), object())
        acquired.pop()()
        return list(spawned)

    assert "loom-tracker-namespaces" not in asyncio.run(_run(""))
    assert "loom-tracker-namespaces" in asyncio.run(_run("prod"))
    assert started == [True, True]
