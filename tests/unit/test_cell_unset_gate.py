"""A server process with LOOM_CELL unset is gated by its deployment's cell state
(server/core/cell.py, docs/architecture/cells.md): it serves while the deployment is
no cell or a first cell still recording, and is gated in any other state."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from server import boot
from server.core import cell, instances, readiness
from server.settings import settings
from server.workers import leader
from tests.unit.test_supervisor_lease import _auto, _Lock

_OWN = {"v": 1, "fields": {}}

# (first, mode, has_tenancy, has_own) -> state.
STATES = [
    ((None, "", False, False), "open"),
    (((True, "eu-1"), "recording", False, True), "recording"),
    (((True, "eu-1"), "recording", True, True), "recording"),
    (((True, "eu-1"), "enforced", True, True), "enforced"),
    (((False, "eu-2"), "enforced", False, True), "enforced"),
    (((False, "eu-2"), "recording", False, True), "enforced"),  # a later cell never records
    ((None, "", True, False), "partial"),  # a stray tenancy row
    ((None, "enforced", False, False), "partial"),
    (((True, "eu-1"), "", False, False), "partial"),
    ((None, "", False, True), "partial"),
    (((True, "eu-1"), "recording", False, False), "partial"),
    (((True, "eu-1"), "paused", False, True), "partial"),  # a mode this build does not know
]


@pytest.fixture(autouse=True)
def _unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(cell, "_memo", None)
    # A test's refresh() replaces the module's view; undone at teardown.
    monkeypatch.setattr(cell, "_view", cell._View())
    monkeypatch.setattr(cell, "_server_reads", False)
    monkeypatch.setattr(readiness, "draining", lambda: False)
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: [])


def _rows(monkeypatch: pytest.MonkeyPatch, state: str, **over) -> None:
    fields = {
        "open": {},
        "recording": {"first": (True, "eu-1"), "mode": "recording", "current": _OWN},
        "enforced": {"first": (True, "eu-1"), "mode": "enforced", "current": _OWN},
        "partial": {"has_tenancy": True},
    }[state]
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic(), **{**fields, **over}))


@pytest.mark.parametrize("rows,expected", STATES)
def test_the_state_rule(rows: tuple, expected: str) -> None:
    assert cell.state(*rows) == expected


@pytest.mark.parametrize("rows,expected", STATES)
def test_the_unset_gate_opens_only_in_open_and_recording(
    monkeypatch: pytest.MonkeyPatch, rows: tuple, expected: str
) -> None:
    first, mode, has_tenancy, has_own = rows
    monkeypatch.setattr(
        cell,
        "_view",
        cell._View(
            read_at=time.monotonic(),
            first=first,
            mode=mode,
            has_tenancy=has_tenancy,
            current=_OWN if has_own else None,
        ),
    )
    reason = cell.gate_reason()
    assert (reason == "") == (expected in ("open", "recording"))
    if expected == "enforced":
        assert "enforced" in reason and "LOOM_CELL" in reason and cell.TROUBLESHOOTING in reason
    if expected == "partial":
        assert "partial cell state" in reason and cell.TROUBLESHOOTING in reason


def test_partial_names_the_rows_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _rows(monkeypatch, "partial", mode="enforced")
    reason = cell.gate_reason()
    assert "loom.tenancy_mode, loom.tenancy present" in reason
    assert not cell.readiness_check().ok


def test_a_view_never_read_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workers child, a CLI and the ClickHouse authority read nothing: they are not
    gated by this pair (the server itself fails its boot on a failed first read)."""
    monkeypatch.setattr(cell, "_view", cell._View())
    assert cell.gate_reason() == ""
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    assert cell.gate_reason() == ""


@pytest.mark.parametrize("state", ["open", "recording", "enforced", "partial"])
def test_the_unset_gate_keeps_its_verdict_past_the_stale_bound(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _rows(monkeypatch, state)
    before = cell.gate_reason()
    # A failed read long ago: no stale bound with LOOM_CELL unset.
    stale = time.monotonic() - 10 * float(settings.jwt_revocation_max_stale_seconds)
    monkeypatch.setattr(
        cell, "_view", cell.dataclasses.replace(cell._view, read_at=stale, error="x")
    )
    assert cell.gate_reason() == before
    assert (before == "") == (state in ("open", "recording"))


def test_a_cell_process_past_the_stale_bound_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    stale = time.monotonic() - 10 * float(settings.jwt_revocation_max_stale_seconds)
    monkeypatch.setattr(cell, "_view", cell._View(read_at=stale))
    assert "LOOM_JWT_REVOCATION_MAX_STALE_SECONDS" in cell.gate_reason()


def test_a_failed_read_keeps_the_unset_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg

    _rows(monkeypatch, "enforced")
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(side_effect=OSError("down")))
    assert asyncio.run(cell.refresh()) is False
    assert "enforced" in cell.gate_reason()


def test_the_read_asks_whether_a_tenancy_row_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg

    seen: list[str] = []

    async def _row(sql: str, *_a):
        seen.append(sql)
        return {"first": None, "mode": None, "own": None, "tenancy": True}

    monkeypatch.setattr(pg, "fetchrow", _row)
    assert asyncio.run(cell.refresh()) is True
    assert "EXISTS (SELECT 1 FROM loom.tenancy)" in seen[0]
    assert cell._view.state() == "partial"


def test_a_failed_first_read_refuses_the_servers_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fails() -> bool:
        monkeypatch.setattr(cell, "_view", cell._View(error="connection refused"))
        return False

    monkeypatch.setattr(cell, "refresh", _fails)
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", ())
    with pytest.raises(RuntimeError, match=r"could not be read \(connection refused\)"):
        asyncio.run(boot.start_cell_gate())


def test_pending_postgres_migrations_boot_unready_rather_than_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With LOOM_BOOT_MIGRATE off and 008 pending the state tables do not exist yet: the
    process boots, unready on its `migrations` check, and keeps reading."""
    monkeypatch.setattr(cell, "refresh", AsyncMock(return_value=False))
    monkeypatch.setattr(cell, "start_refresh_loop", lambda: "loop")
    monkeypatch.setattr(boot, "SKIPPED_PG_MIGRATIONS", ("pg:008_cells.sql",))
    assert asyncio.run(boot.start_cell_gate()) == ["loop"]
    # Gated until a read lands: a failed read is never taken for an absent row.
    assert "not read yet" in cell.gate_reason()
    assert asyncio.run(leader.decide_supervisor("on")).held is False
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic()))
    assert cell.gate_reason() == ""


def test_the_server_reads_the_state_once_and_runs_one_refresh_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell, "refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(cell, "start_refresh_loop", lambda: "loop")
    assert asyncio.run(boot.start_cell_gate()) == ["loop"]
    # Under the ClickHouse authority with LOOM_CELL unset nothing is read.
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    assert asyncio.run(boot.start_cell_gate()) == []


def test_token_state_starts_no_cell_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan's own read replaces it: a booted web process runs one loop."""
    from server.auth import end_user_keys, keyring, revocation
    from server.core import grant_cache

    for mod, name in (
        (revocation, "load_at_boot"),
        (keyring, "refresh"),
        (grant_cache, "poll"),
        (end_user_keys, "refresh"),
    ):
        monkeypatch.setattr(mod, name, AsyncMock())
    for mod in (revocation, keyring, end_user_keys):
        monkeypatch.setattr(mod, "start_refresh_loop", lambda *_a: "loop")
    monkeypatch.setattr(grant_cache, "start_poll_loop", lambda *_a: "loop")
    read = AsyncMock(return_value=True)
    monkeypatch.setattr(cell, "refresh", read)
    monkeypatch.setattr(settings, "cell", "eu-1")
    assert asyncio.run(boot.start_token_state()) == ["loop"] * 4
    read.assert_not_awaited()


def test_the_lifespan_reads_the_state_before_its_heartbeat_and_lease() -> None:
    """The calls the lifespan makes, in the order it makes them (its syntax tree: a
    comment or a renamed variable changes nothing here)."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(boot.__file__).parent / "main.py").read_text())
    lifespan = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )
    calls = [
        ast.unparse(n.func)
        for n in sorted(
            ast.walk(lifespan), key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))
        )
        if isinstance(n, ast.Call)
    ]
    wanted = [
        "boot.require_storage_authority",
        "boot.start_cell_gate",
        "_instances.start",
        "boot.ensure_default_namespace",
        "_leader.decide_supervisor",
    ]
    assert [c for c in calls if c in wanted] == wanted


def test_keep_lease_starts_and_stops_an_unset_on_processs_workers_with_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leader, "_period_s", lambda _held: 0.01)
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
        _rows(monkeypatch, "enforced")
        lease = await leader.decide_supervisor("on")
        assert not lease.held
        task = asyncio.create_task(
            leader.keep_lease(lease, lambda: events.append("start"), _on_lost)
        )
        await asyncio.sleep(0.1)
        assert events == []  # gated: nothing starts
        _rows(monkeypatch, "open")
        await _wait_for(1)
        _rows(monkeypatch, "partial")
        await _wait_for(2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert events[:2] == ["start", "stop"]


@pytest.mark.parametrize(
    "state,held", [("open", True), ("recording", True), ("enforced", False), ("partial", False)]
)
def test_an_on_process_starts_its_workers_at_boot_only_where_it_serves(
    monkeypatch: pytest.MonkeyPatch, state: str, held: bool
) -> None:
    _rows(monkeypatch, state)
    lease = asyncio.run(leader.decide_supervisor("on"))
    assert lease.held is held


def test_an_on_process_under_the_clickhouse_authority_holds_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(cell, "_view", cell._View())
    assert asyncio.run(leader.decide_supervisor("on")).held is True


@pytest.mark.parametrize("state", ["open", "recording"])
def test_an_auto_process_takes_the_lease_where_it_serves(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _rows(monkeypatch, state)
    monkeypatch.setattr(leader, "newer_build", lambda _name: 0)
    lease = _auto(_Lock([True]))
    lease.step()
    assert lease.held
    lease.step()
    assert lease.held


def test_an_auto_process_in_enforced_does_not_take_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rows(monkeypatch, "enforced")
    lease = _auto(_Lock([True]))
    lease.step()
    assert not lease.held and "the cell gate" in lease.reason


def test_an_auto_cell_process_with_a_matching_state_takes_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "public_base_url", "https://eu-1.example.test")
    monkeypatch.setattr(cell, "_code_orgs", None)
    monkeypatch.setattr(leader, "newer_build", lambda _name: 0)
    _rows(monkeypatch, "enforced", current=cell.as_value(cell.mine()))
    lease = _auto(_Lock([True]))
    lease.step()
    assert lease.held


def test_a_cell_on_process_with_a_matching_state_starts_its_workers_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "public_base_url", "https://eu-1.example.test")
    monkeypatch.setattr(cell, "_code_orgs", None)
    _rows(monkeypatch, "enforced", current=cell.as_value(cell.mine()))
    assert asyncio.run(leader.decide_supervisor("on")).held is True


def test_the_lease_loop_runs_for_an_unset_on_process_under_postgres_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        task = boot.start_lease_loop(leader.SupervisorLease("on"), object(), object())
        assert isinstance(task, asyncio.Task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(settings, "storage_authority", "clickhouse")
        assert boot.start_lease_loop(leader.SupervisorLease("on"), object(), object()) is None

    _rows(monkeypatch, "open")
    asyncio.run(_run())


def test_the_cell_readiness_check_is_registered_under_postgres_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        readiness,
        "register",
        lambda name, _check, required=True, **_kw: names.append((name, required)),
    )
    boot.register_readiness_checks(object())
    assert ("cell", True) in names
    names.clear()
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    boot.register_readiness_checks(object())
    assert all(name != "cell" for name, _ in names)


def test_a_gated_process_first_heartbeat_reports_not_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "serves_traffic", True)
    _rows(monkeypatch, "enforced")
    assert instances.row()["serving"] == 0
    assert instances.cell_row()["tenancy_mode"] == ""
    _rows(monkeypatch, "open")
    assert instances.row()["serving"] == 1


def test_the_default_namespace_boot_write_waits_for_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import default_namespace

    write = AsyncMock()
    monkeypatch.setattr(default_namespace, "ensure_default_namespace_cached", write)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _self: False)
    _rows(monkeypatch, "enforced")
    asyncio.run(boot.ensure_default_namespace())
    write.assert_not_awaited()
    _rows(monkeypatch, "open")
    asyncio.run(boot.ensure_default_namespace())
    write.assert_awaited_once()


def test_a_gated_unset_process_answers_503_naming_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport.model_maintenance import ModelMaintenanceMiddleware

    _rows(monkeypatch, "enforced")
    app, send = AsyncMock(), AsyncMock()
    asyncio.run(
        ModelMaintenanceMiddleware(app)(
            {"type": "http", "method": "POST", "path": "/v1/memory.set"}, AsyncMock(), send
        )
    )
    app.assert_not_awaited()
    assert send.await_args_list[0].args[0]["status"] == 503
    body = send.await_args_list[1].args[0]["body"]
    assert b"AUTH_UNAVAILABLE" in body and b"run with LOOM_CELL unset, serves nothing now" in body
    assert b"as its cell" not in body


def test_an_unset_on_process_runs_the_leaders_boot_duties_when_its_gate_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callbacks `start_lease_loop` builds: the workers start, the retention
    overrides and the deployment config follow, and only the deployment's leader
    registers the tracker namespaces."""
    _rows(monkeypatch, "open")
    lease = asyncio.run(leader.decide_supervisor("on"))
    monkeypatch.setattr(leader, "current", lambda: lease)
    acquired: list = []

    async def _keep(_lease, on_acquired, _on_lost) -> None:
        acquired.append(on_acquired)

    spawned: list[str] = []

    def _spawn(coro, *, name: str) -> None:
        coro.close()
        spawned.append(name)

    published: list[bool] = []
    monkeypatch.setattr(leader, "keep_lease", _keep)
    monkeypatch.setattr(boot, "_spawn_boot_task", _spawn)
    monkeypatch.setattr(boot, "publish_deployment_config", lambda _ch: published.append(True))
    started: list[bool] = []

    class _Supervisor:
        def start_all(self) -> None:
            started.append(True)

    async def _run(deployment: bool) -> list[str]:
        monkeypatch.setattr(settings, "deployment", deployment)
        spawned.clear()
        await boot.start_lease_loop(lease, _Supervisor(), object())
        acquired.pop()()
        return list(spawned)

    assert asyncio.run(_run(False)) == ["loom-retention-overrides"]
    assert "loom-tracker-namespaces" in asyncio.run(_run(True))
    assert started == [True, True] and published == [True, True]
