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
