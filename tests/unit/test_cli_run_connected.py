"""CLI commands own one ClickHouse connection and close it on every path —
an unclosed client at loop close leaks its HTTP session."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server.cli import _ch, maintenance, reports

pytestmark = pytest.mark.usefixtures("stub_model_command")

ROOT = Path(__file__).resolve().parents[2]


class _CH:
    """Records the connection's opens and closes; like the real client, a connect
    while connected opens nothing."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.open = False

    connected = False

    async def connect(self) -> None:
        if not self.open:
            self.events.append("connect")
            self.open = True

    async def disconnect(self) -> None:
        if self.open:
            self.events.append("disconnect")
            self.open = False


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    fake = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: fake)
    # `code._run` configures the root logger once per process (level INFO, a stderr
    # handler); the tests here are about the connection, so leave logging as found.
    monkeypatch.setattr("server.cli.code_run._LOGGING_CONFIGURED", True)
    return fake


def test_run_connected_disconnects_after_a_failure(ch: _CH) -> None:
    async def _boom(_: object) -> int:
        raise RuntimeError("mid-command")

    with pytest.raises(RuntimeError):
        _ch.run_connected(_boom, org_free=_ch.reads("a test command"))
    assert ch.events == ["connect", "disconnect"]


def test_a_command_for_another_authority_refuses_before_it_runs(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An erasure run from a shell configured for the other authority would skip its
    store's rows: the command refuses, as a replica so configured does."""
    from server.core import pg, pg_profile

    async def _mismatch(_: object) -> None:
        raise pg.AuthorityMismatch("this deployment's memory authority is postgres")

    ran: list[object] = []

    async def _run(c: object) -> int:
        ran.append(c)
        return 0

    monkeypatch.setattr(pg_profile, "check_configured", _mismatch)
    with pytest.raises(pg.AuthorityMismatch):
        _ch.run_connected(_run, org_free=_ch.reads("a test command"))
    assert ran == []
    assert ch.events == ["connect", "disconnect"]


def test_run_connected_closes_the_postgres_pool(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def _close_pool() -> None:
        closed.append("pool")

    async def _run(_: object) -> int:
        return 0

    monkeypatch.setattr("server.core.pg.close_pool", _close_pool)
    _ch.run_connected(_run, org_free=_ch.reads("a test command"))
    assert closed == ["pool"] and ch.events == ["connect", "disconnect"]


@pytest.mark.parametrize("importer", ["recall", "clickgap"])
def test_an_in_process_import_for_another_authority_refuses(
    ch: _CH, monkeypatch: pytest.MonkeyPatch, importer: str
) -> None:
    """The importers that write memory in-process connect on their own, through the same
    check: one run from a shell configured for the other authority would write rows the
    deployment's authority never holds."""
    import asyncio
    import contextlib

    from server.cli import import_clickgap, import_recall
    from server.core import pg, pg_profile

    async def _mismatch(_: object) -> None:
        raise pg.AuthorityMismatch("this deployment's memory authority is postgres")

    async def _connect() -> None:
        if importer == "recall":
            args = argparse.Namespace(base_url=None, token=None, org="o")
            await import_recall._make_writer(args, dict, timeout_s=1.0)
        else:
            async with contextlib.AsyncExitStack() as stack:
                await import_clickgap._clickgap_enroll(stack, "o")

    monkeypatch.setattr(pg_profile, "check_configured", _mismatch)
    with pytest.raises(pg.AuthorityMismatch):
        asyncio.run(_connect())
    assert ch.events == ["connect", "disconnect"]


def test_a_maintenance_command_closes_its_connection(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _reconcile(org: str, namespace: str, **kw: object) -> dict[str, int]:
        assert kw["ch"] is ch
        return {"divergent_rows": 0, "mirrored": 0}

    monkeypatch.setattr("server.memory.store.reconcile_embedding_status", _reconcile)
    args = argparse.Namespace(org="o", namespace="n", all_namespaces=False, limit=10, dry_run=True)
    assert maintenance._cmd_reconcile_status(args) == 0
    assert ch.events == ["connect", "disconnect"]


def test_entity_export_closes_its_connection(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _export(client: object, **_: object) -> str:
        return "<graphml/>"

    monkeypatch.setattr("server.memory.app.entity_export.export_entity_graph", _export)
    args = argparse.Namespace(org="o", namespace="n", format="graphml", all=False, output="-")
    assert reports._cmd_entity_export(args) == 0
    assert ch.events == ["connect", "disconnect"]


def test_every_code_subcommand_leaves_the_client_disconnected(ch: _CH) -> None:
    from server.cli import code_run

    async def _sub() -> int:
        await ch.connect()
        raise RuntimeError("mid-command")

    with pytest.raises(RuntimeError):
        code_run._run("stats", _sub())
    # The enrollment, registry read and command now share one connection.
    assert ch.events == ["connect", "disconnect"]
    assert not ch.open


def test_a_code_subcommand_sees_the_repositories_added_from_the_repositories_page(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from server.cli import code_run
    from server.code.app.index import repo_registry

    refreshed: list[bool] = []

    async def _refresh(*, force: bool = False, ch: object = None) -> list[object]:
        refreshed.append(force)
        return []

    monkeypatch.setattr(repo_registry, "refresh_db_repos", _refresh)

    async def _sub() -> int:
        assert refreshed == [True], "the registry is read before the subcommand runs"
        return 0

    assert asyncio.run(code_run._then_disconnect(_sub())) == 0
    assert ch.events == ["connect", "disconnect"]


def test_a_command_leaves_no_deployment_check_armed(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next command in the process checks against its own record, not this one's: a
    connection opened after this command reads no authority row."""
    import asyncio

    from server.core import pg, pg_profile

    async def _arm(_: object) -> None:
        pg.expect_deployment(("d", 1))

    async def _run(_: object) -> int:
        return 0

    class _Conn:
        async def fetchrow(self, sql: str) -> object:
            raise AssertionError("the check ran again")

    monkeypatch.setattr(pg_profile, "check_configured", _arm)
    _ch.run_connected(_run, org_free=_ch.reads("a test command"))
    asyncio.run(pg._check_deployment(_Conn()))


def test_a_skipped_namespace_names_why_the_entry_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import code_run
    from server.code.domain.index import repos

    monkeypatch.setattr(repos, "configured_repos", lambda: [])
    monkeypatch.setattr(repos, "_page_namespaces", frozenset({("acme", "code-x")}))
    with pytest.raises(SystemExit, match="an operator entry for this name, if any, is skipped"):
        code_run._repo_cfg(argparse.Namespace(org="acme", namespace="code-x"))


def test_a_code_subcommand_exits_with_one_line_while_the_registry_is_unread(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from server.cli import code_run
    from server.code.app.index import repo_registry
    from server.code.domain.index import repos
    from server.settings import settings

    async def _unread(*, force: bool = False, ch: object = None) -> list[object]:
        return []  # the read failed: the snapshot stays unread

    monkeypatch.setattr(repo_registry, "refresh_db_repos", _unread)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)

    async def _sub() -> int:
        code_run._repo_cfg(argparse.Namespace(org="acme", namespace="code-x"))
        return 0

    with pytest.raises(SystemExit, match="registry could not be read; retry"):
        asyncio.run(code_run._then_disconnect(_sub()))
    assert not ch.open


def test_run_connected_binds_required_account_retirement_before_command(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import account_lifecycle
    from server.knowledge.app.connected_accounts import stop_subjects

    monkeypatch.setattr(account_lifecycle, "_stopper", None)

    async def command(_: object) -> int:
        assert account_lifecycle._stopper is stop_subjects
        return 0

    assert _ch.run_connected(command, org_free=_ch.reads("a lifecycle composition test")) == 0
    assert ch.events == ["connect", "disconnect"]


def _interrupt_outside_the_selector(runner: str) -> None:
    """A child process: run a connected command that waits for nothing but Ctrl-C, and
    deliver SIGINT on a thread other than the one in the loop's selector. That leaves
    what a signal arriving just before the selector's system call leaves: the signal
    flagged, and the wait not interrupted. Exits 1 when the command stays there."""
    import asyncio
    import signal
    import threading
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from server.core import pg_profile
    from server.insights.app import provider_command

    waiting = threading.Event()
    cancelled = threading.Event()

    def interrupt() -> None:
        waiting.wait()
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        if not cancelled.wait(30):
            os._exit(1)

    async def command(_: object) -> None:
        loop = asyncio.get_running_loop()
        # The loop's last callback before it sleeps: the other thread gets the
        # interpreter, and sends the signal, once this one is in its selector.
        loop.call_soon(waiting.set)
        try:
            await loop.create_future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    @asynccontextmanager
    async def admitted() -> AsyncIterator[None]:
        yield

    async def paired(_: object) -> None:
        return None

    fake = _CH()
    # `asyncio.run` replaces only the default handler, and a launcher can leave another.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("server.core.ch.get_client", lambda: fake)
        patch.setattr(provider_command, "model_command", admitted)
        patch.setattr(pg_profile, "check_configured", paired)
        try:
            getattr(_ch, runner)(command, org_free=_ch.reads("a test command"))
        except KeyboardInterrupt:
            return
    raise SystemExit(2)


@pytest.mark.parametrize("runner", ["run_connected", "run_control_connected"])
def test_ctrl_c_ends_a_command_whose_selector_it_did_not_interrupt(runner: str) -> None:
    """Python runs the Ctrl-C handler of `asyncio.run` once the main thread is back in
    bytecode. A signal that arrives as the loop goes to sleep interrupts nothing: the
    signal has to wake the selector itself, or the command waits for another event."""
    child = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from tests.unit.test_cli_run_connected import _interrupt_outside_the_selector"
            " as run; import sys; run(sys.argv[1])",
            runner,
        ],
        cwd=ROOT,
        env={**os.environ, "OTEL_SDK_DISABLED": "true"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 0, child.stdout + child.stderr
