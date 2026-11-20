"""Operator placement keeps the directory winner and bounds its local session."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import pytest
from starlette.testclient import TestClient

from server.core import cell
from server.router import app, directory
from server.settings import settings
from tests.unit.test_router import _row

OPERATOR = "lco_" + "7" * 128


@pytest.fixture(autouse=True)
def _code_org_reader(monkeypatch):
    monkeypatch.setattr(cell, "_code_orgs", None)


class DirectoryConnection:
    def __init__(self, winner: str | None = "eu-1") -> None:
        self.winner = winner
        self.row = _row()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.committed = False

    @asynccontextmanager
    async def transaction(self):
        yield
        self.committed = True

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM loom_router.cells WHERE" in sql:
            return asdict(self.row)
        if self.winner is not None:
            return {"cell": self.winner, "deployment_id": f"dep-{self.winner}"}
        return None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        self.winner = args[1]
        return args[0]


@pytest.fixture
def connected(monkeypatch):
    conn = DirectoryConnection()

    @asynccontextmanager
    async def connection(**kw):
        yield conn

    monkeypatch.setattr(directory, "_connection", connection)
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    return conn


def request(client, **over):
    return client.post(
        "/cells/v1/operator/cells/eu-1/place",
        json={
            "org": "acme",
            "deployment_id": "dep-eu-1",
            "fingerprint": cell.as_value(
                {"public_host": "eu-1.example.test", "deployment_id": "dep-eu-1"}
            ),
            **over,
        },
        headers={"Authorization": f"Bearer {OPERATOR}"},
    )


def test_existing_winner_emits_audit_wal_before_returning(connected):
    client = TestClient(app.build_app(app.Router()))
    result = request(client)
    assert result.status_code == 200
    assert result.json() == {"org": "acme", "cell": "eu-1", "deployment_id": "dep-eu-1"}
    audit = [(sql, args) for sql, args in connected.calls if "INSERT INTO loom_router.audit" in sql]
    assert len(audit) == 1 and audit[0][1] == ("operator", {"org": "acme", "cell": "eu-1"})
    assert connected.committed
    assert not any("UPDATE loom_router.cells SET placed" in sql for sql, _ in connected.calls)


@pytest.mark.parametrize(
    "posted",
    [{"deployment_id": "other"}, {"fingerprint": {}}, {"org": "x" * (cell.ORG_MAX_BYTES + 1)}],
)
def test_existing_winner_refuses_operator_identity_mismatch(connected, posted):
    result = request(TestClient(app.build_app(app.Router())), **posted)
    assert result.status_code in (400, 409)
    assert not connected.committed
    assert not any("INSERT" in sql for sql, _ in connected.calls)


def test_operator_authentication_precedes_directory_access(connected):
    result = TestClient(app.build_app(app.Router())).post(
        "/cells/v1/operator/cells/eu-1/place", json={}
    )
    assert result.status_code == 401 and connected.calls == []


def test_live_duplicate_also_emits_audit_wal(connected):
    assert asyncio.run(
        directory.place("acme", "eu-1", lambda row: "closed", actor="cell:eu-1")
    ) == ("eu-1", "dep-eu-1")
    assert connected.committed
    assert any("INSERT INTO loom_router.audit" in sql for sql, _ in connected.calls)


class LocalConnection:
    def __init__(self):
        self.calls = []
        self.committed = False
        self.terminated = False
        self.closed = False
        self.stall_commit = False
        self.stall_close = False
        self.fail_insert = False
        self.commit_started = asyncio.Event()

    def transaction(self):
        conn = self

        class Transaction:
            async def start(self):
                pass

            async def commit(self):
                conn.commit_started.set()
                if conn.stall_commit:
                    await asyncio.Event().wait()
                conn.committed = True

        return Transaction()

    async def execute(self, sql, *args):
        self.calls.append(sql)
        if "INSERT" in sql and self.fail_insert:
            raise OSError("local insert failed")
        return "INSERT 0 0"

    async def close(self, **kwargs):
        self.calls.append("close")
        if self.stall_close:
            await asyncio.Event().wait()
        self.closed = True

    def terminate(self):
        self.terminated = True

    def is_closed(self):
        return self.closed or self.terminated


@pytest.fixture
def local(monkeypatch):
    import asyncpg

    from server.cli import cell_place
    from server.core import pg

    monkeypatch.setattr(settings, "pg_dsn", "postgres://not-connected")
    monkeypatch.setattr(pg, "_expected", None)
    conn = LocalConnection()
    state = {"first": "eu-1", "state": "enforced", "deployment": "dep-eu-1", "row": None}

    async def read_state(c):
        return state["state"], state["first"]

    async def fetchval(sql):
        return state["deployment"]

    async def fetchrow(sql):
        return state["row"]

    conn.fetchval = fetchval
    conn.fetchrow = fetchrow

    async def connect(*args, **driver_kw):
        kw = {"statement_timeout_ms": int(driver_kw["server_settings"]["statement_timeout"])}
        assert kw == {"statement_timeout_ms": settings.pg_statement_timeout_ms}
        return conn

    async def initialize(c):
        assert c is conn

    async def direct(c, what):
        return []

    async def take_lock(c, what):
        conn.calls.append("lock")

    async def held(c):
        return True

    async def directory_call(*a):
        conn.calls.append("directory")

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(pg, "_check_deployment", initialize)
    monkeypatch.setattr(pg, "init_connection", initialize)
    monkeypatch.setattr(cell, "read_state_if_present", read_state)
    monkeypatch.setattr(cell_place.apg, "direct_refusals", direct)
    monkeypatch.setattr(cell_place.apg, "take_lock", take_lock)
    monkeypatch.setattr(cell_place.apg, "lock_held", held)
    monkeypatch.setattr(cell_place, "_directory", directory_call)
    monkeypatch.setattr(cell_place, "_bound_s", lambda: 0.03)
    fields = cell.fingerprint("dep-eu-1")
    state["row"] = {"current_fp": cell.as_value(fields), "next_fp": None}
    return conn, state


async def invoke_place():
    import argparse

    import httpx

    from server.cli import cell_place

    return await cell_place.place(
        argparse.Namespace(cell="eu-1", org="acme"), httpx.URL("https://router.test")
    )


async def test_local_noop_insert_still_commits_durability_message(local):
    from server.core import pg

    conn, state = local
    assert await invoke_place() == {"org": "acme", "cell": "eu-1", "deployment_id": "dep-eu-1"}
    assert conn.committed and conn.closed and not conn.terminated
    assert conn.calls[0:2] == ["lock", "directory"]
    assert conn.calls[-2:] == [pg._DURABLE_MESSAGE, "close"]


@pytest.mark.parametrize(
    "change",
    [
        {"state": "recording"},
        {"state": "partial"},
        {"first": "us-1"},
        {"deployment": "other"},
        {"row": None},
    ],
)
async def test_local_invalid_identity_refuses_before_directory(local, change):
    from server.cli import cell_place

    conn, state = local
    state.update(change)
    with pytest.raises(cell_place.PlacementRefused):
        await invoke_place()
    assert conn.calls == ["lock", "close"] and conn.closed and not conn.terminated


@pytest.mark.parametrize("change", [{"state": "recording"}, {"first": "us-1"}, {"row": None}])
async def test_local_identity_is_read_again_before_insert(local, monkeypatch, change):
    from server.cli import cell_control, cell_place
    from server.core import pg
    from tests.unit.clock_fake import ManualClock

    phase_clock = ManualClock()
    monkeypatch.setattr(cell_control, "time", phase_clock)
    monkeypatch.setattr(pg, "time", phase_clock)
    conn, state = local

    async def changed(*a):
        state.update(change)

    monkeypatch.setattr(cell_place, "_directory", changed)
    with pytest.raises(cell_place.PlacementRefused):
        await invoke_place()
    assert not any("INSERT" in sql for sql in conn.calls)
    assert conn.closed and not conn.terminated and not conn.committed


async def test_local_insert_failure_can_be_retried(local):
    conn, state = local
    conn.fail_insert = True
    with pytest.raises(OSError, match="local insert failed"):
        await invoke_place()
    assert not conn.committed and conn.closed and not conn.terminated
    conn.fail_insert = False
    await invoke_place()
    assert conn.committed


async def test_local_commit_and_cleanup_stalls_are_bounded(local):
    conn, state = local
    conn.stall_commit = conn.stall_close = True
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(invoke_place(), 0.5)
    assert conn.commit_started.is_set() and conn.terminated and not conn.committed
    conn.stall_commit = conn.stall_close = False
    await invoke_place()
    assert conn.committed


async def test_local_commit_cancellation_closes_the_session(local):
    conn, state = local
    conn.stall_commit = True
    task = asyncio.create_task(invoke_place())
    try:
        try:
            await asyncio.wait_for(conn.commit_started.wait(), 0.5)
        except TimeoutError:
            if task.done():
                await task
            raise
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert conn.closed and not conn.terminated and not conn.committed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stalled_direct_context_entry_is_bounded(local, monkeypatch):
    import asyncpg

    conn, state = local
    cleaned = False

    async def stalled(*args, **kw):
        nonlocal cleaned
        try:
            await asyncio.Event().wait()
        finally:
            cleaned = True

    monkeypatch.setattr(asyncpg, "connect", stalled)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(invoke_place(), 0.5)
    assert cleaned and conn.calls == []


async def test_query_timeout_cannot_enter_an_unbounded_rollback(local):
    conn, state = local

    class Transaction:
        async def start(self):
            pass

        async def commit(self):
            pytest.fail("a stalled statement cannot commit")

        async def __aenter__(self):
            await self.start()

        async def __aexit__(self, *exc):
            await asyncio.Event().wait()

    async def stalled(sql, *args):
        await asyncio.Event().wait()

    conn.transaction = Transaction
    conn.execute = stalled
    task = asyncio.create_task(invoke_place())
    try:
        done, _ = await asyncio.wait([task], timeout=0.15)
        assert done, "the statement deadline must include ending its transaction"
        with pytest.raises(TimeoutError):
            await task
        assert conn.closed and not conn.terminated
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "status,answer",
    [
        (200, {"org": "acme", "cell": "us-1", "deployment_id": "dep-us-1"}),
        (200, {"org": "acme", "cell": "eu-1", "deployment_id": "wrong"}),
        (200, []),
        (200, {"org": "someone-else", "cell": "eu-1", "deployment_id": "dep-eu-1"}),
        (503, {"error": "standby stalled", "code": "BACKEND_UNAVAILABLE"}),
        (504, {"error": "gateway timeout"}),
    ],
)
async def test_cli_refuses_unknown_response_or_other_winner(monkeypatch, status, answer):
    import httpx

    from server.cli import cell_place

    async def respond(request):
        assert request.headers["authorization"] == f"Bearer {settings.router_operator_token}"
        return httpx.Response(status, json=answer)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(respond), **kw)
    )
    with pytest.raises(cell_place.PlacementRefused):
        await cell_place._directory(
            httpx.URL("https://router.test"), "eu-1", "acme", "dep-eu-1", {}
        )


async def test_repository_only_configured_orgs_are_in_full_cli_fingerprint(local, monkeypatch):
    from server.cli import cell_place
    from server.code.domain.index import repos

    conn, state = local
    monkeypatch.setattr(repos, "operator_orgs", lambda: {"repo-only"})
    fields = cell.fingerprint("dep-eu-1")
    fields["configured_orgs"] = sorted(set(fields["configured_orgs"]) | {"repo-only"})
    state["row"] = {"current_fp": cell.as_value(fields), "next_fp": None}
    _, value = await cell_place._identity(conn, "eu-1")
    assert "repo-only" in value["fields"]["configured_orgs"]


def test_new_placement_uses_counted_statement_bound(monkeypatch):
    from tests.unit.test_router_attach import _cell_row, _Conn, _Store

    store = _Store(_cell_row(state="serving"))

    @asynccontextmanager
    async def connection(**kw):
        yield _Conn(store)

    monkeypatch.setattr(directory, "_connection", connection)
    asyncio.run(
        directory.place("new", "eu-1", lambda row: "", actor="operator", verify=lambda row: "")
    )
    assert store.statements == directory.PLACE_STATEMENTS
    assert directory.PLACE_MAX_S == directory._bound_s(store.statements)


def test_operator_placement_body_bound_refuses_before_directory(connected):
    from server.router.limits import MAX_BODY_BYTES

    result = TestClient(app.build_app(app.Router())).post(
        "/cells/v1/operator/cells/eu-1/place",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Authorization": f"Bearer {OPERATOR}"},
    )
    assert result.status_code == 413 and connected.calls == []


def test_operator_place_bypasses_cell_token_rate_limiter(connected, monkeypatch):
    router = app.Router()
    monkeypatch.setattr(
        router.placements, "admit", lambda name: pytest.fail("operator used cell rate")
    )
    assert request(TestClient(app.build_app(router))).status_code == 200


@pytest.mark.parametrize("org", ["", "-", "*", "bad\x00org"])
def test_operator_place_refuses_malformed_org_before_directory(connected, org):
    assert request(TestClient(app.build_app(app.Router())), org=org).status_code == 400
    assert connected.calls == []


def test_cli_cancellation_names_the_idempotent_retry_and_preserves_cancellation(
    monkeypatch, capsys
):
    import argparse

    import httpx

    from server.cli import cell_place

    cancelled = asyncio.CancelledError("commit response was cancelled")

    async def cancel(*args):
        raise cancelled

    monkeypatch.setattr(cell_place, "_args_problem", lambda args: "")
    monkeypatch.setattr(
        cell_place.cell_cli, "operator_router", lambda args: httpx.URL("https://router.test")
    )
    monkeypatch.setattr(cell_place, "place", cancel)
    monkeypatch.setattr(cell_place, "run_connected", lambda run, **kw: asyncio.run(run(None)))
    with pytest.raises(asyncio.CancelledError) as caught:
        cell_place.cmd_place(argparse.Namespace(cell="eu-1", org="acme"))
    assert caught.value is cancelled
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "placement may have committed; rerun the same command" in printed.err


async def test_local_phases_remain_separate_from_directory_and_exit(local, monkeypatch):
    from server.cli import cell_control, cell_place

    observed = []
    actual_call = cell_control.Owner.call

    async def timed(self, work, *, total_s):
        observed.append(total_s)
        return await actual_call(self, work, total_s=total_s)

    async def directory_call(*args):
        # The complete HTTP exchange has its independent directory-derived phase.
        await asyncio.sleep(0.06)

    monkeypatch.setattr(cell_control.Owner, "call", timed)
    monkeypatch.setattr(cell_place, "_directory", directory_call)
    await invoke_place()
    assert observed == [0.03, 0.03, cell_place.REQUEST_S, 0.03]
    conn, _ = local
    assert conn.committed and conn.closed and not conn.terminated


@pytest.mark.parametrize("where", ["initialization", "initial", "transaction", "directory"])
async def test_original_error_survives_failed_required_close(local, monkeypatch, where):
    from server.cli import cell_place
    from server.core import pg

    original = ValueError("original placement failure")
    conn, _ = local

    async def fail(*args):
        raise original

    async def bad_close(**kwargs):
        raise RuntimeError("close failed")

    conn.close = bad_close
    if where == "initialization":
        monkeypatch.setattr(pg, "init_connection", fail)
    elif where == "initial":
        monkeypatch.setattr(cell_place, "_identity", fail)
    elif where == "directory":
        monkeypatch.setattr(cell_place, "_directory", fail)
    else:
        conn.execute = fail
    with pytest.raises(ValueError) as caught:
        await invoke_place()
    assert caught.value is original
    assert conn.terminated and not conn.committed


async def test_success_refuses_when_required_close_fails(local):
    conn, _ = local

    async def fail(**kwargs):
        raise OSError("required close failed")

    conn.close = fail
    with pytest.raises(OSError, match="required close failed"):
        await invoke_place()
    assert conn.committed and conn.terminated


async def test_lost_original_lock_refuses_without_reconnecting(local, monkeypatch):
    from server.cli import cell_place

    conn, _ = local

    async def lost(c):
        assert c is conn
        return False

    monkeypatch.setattr(cell_place.apg, "lock_held", lost)
    with pytest.raises(cell_place.PlacementRefused, match="lock was lost"):
        await invoke_place()
    assert conn.calls == ["lock", "directory", "close"]
    assert conn.closed and not conn.committed


@pytest.mark.parametrize("inside_reserve", [False, True])
async def test_late_placement_connection_never_reaches_body(local, monkeypatch, inside_reserve):
    import asyncpg

    conn, _ = local
    released, discarded = asyncio.Event(), asyncio.Event()
    real_terminate = conn.terminate
    real_close = conn.close

    def terminate():
        real_terminate()
        discarded.set()

    async def close(**kw):
        await real_close(**kw)
        discarded.set()

    async def late(*args, **kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if not inside_reserve:
                while not released.is_set():
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        pass
            return conn

    conn.terminate, conn.close = terminate, close
    monkeypatch.setattr(asyncpg, "connect", late)
    with pytest.raises(TimeoutError):
        await invoke_place()
    released.set()
    await asyncio.wait_for(discarded.wait(), 0.2)
    assert conn.calls == (["close"] if inside_reserve else [])
    assert conn.closed if inside_reserve else conn.terminated
    assert not conn.committed
