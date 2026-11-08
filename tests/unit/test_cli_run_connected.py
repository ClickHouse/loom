"""CLI commands own one ClickHouse connection and close it on every path —
an unclosed client at loop close leaks its HTTP session."""

from __future__ import annotations

import argparse

import pytest

from server.cli import _ch, maintenance, reports

pytestmark = pytest.mark.usefixtures("stub_model_command")


class _CH:
    """Records the connection's opens and closes; like the real client, a connect
    while connected opens nothing."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.open = False

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
        _ch.run_connected(_boom)
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
        _ch.run_connected(_run)
    assert ran == []
    assert ch.events == ["connect", "disconnect"]


def test_run_connected_closes_the_postgres_pool(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def _close_pool() -> None:
        closed.append("pool")

    async def _run(_: object) -> int:
        return 0

    monkeypatch.setattr("server.core.pg.close_pool", _close_pool)
    _ch.run_connected(_run)
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
            args = argparse.Namespace(base_url=None, token=None)
            await import_recall._make_writer(args, dict, timeout_s=1.0)
        else:
            async with contextlib.AsyncExitStack() as stack:
                await import_clickgap._clickgap_enroll(stack)

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
    _ch.run_connected(_run)
    asyncio.run(pg._check_deployment(_Conn()))


def test_a_skipped_namespace_names_why_the_entry_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import code_run
    from server.code.domain.index import repos

    monkeypatch.setattr(repos, "configured_repos", lambda: [])
    monkeypatch.setattr(repos, "_page_namespaces", frozenset({("acme", "code-x")}))
    with pytest.raises(SystemExit, match="added that namespace from the"):
        code_run._repo_cfg(argparse.Namespace(org="acme", namespace="code-x"))
