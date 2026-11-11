"""Every CLI command that connects declares the orgs it writes, and a cell refuses one it
does not hold before the command runs (server/cli/_ch.py, docs/architecture/cells.md)."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.auth import tenancy
from server.cli import _ch
from server.core import cell, errors, pg, pg_profile
from server.settings import settings

ROOT = Path(__file__).resolve().parents[2]
_CONNECTS = {"run_connected", "connect_checked", "connect_declared", "_connected"}
# `.connect()` receivers that are not the deployment's ClickHouse writer: the setup
# command's own admin client and SQLite files an importer reads.
_OTHER_CLIENTS = {"preflight", "sqlite3"}


@pytest.fixture(autouse=True)
def _cell_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check_orgs` sets the deployment id and reads the rows: each test's are its own."""
    monkeypatch.setattr(cell, "_deployment_id", "")
    monkeypatch.setattr(cell, "_memo", None)
    monkeypatch.setattr(cell, "_view", cell._View())


def _sources() -> list[Path]:
    files = [*sorted((ROOT / "server" / "cli").glob("*.py"))]
    files += [ROOT / "bench" / "docparse" / "downstream.py"]
    files += [ROOT / "scripts" / "codegen" / "op_reach_census.py"]
    return [f for f in files if f.name != "_ch.py"]


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def test_every_connect_declares_the_orgs_it_writes() -> None:
    undeclared = []
    for path in _sources():
        tree = ast.parse(path.read_text())
        defined = {
            n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for call in _calls(tree):
            name = _name(call)
            if name not in _CONNECTS or (name == "_connected" and "_connected" not in defined):
                continue
            keywords = {k.arg for k in call.keywords}
            if not keywords & {"orgs", "org_free"}:
                undeclared.append(f"{path.relative_to(ROOT)}:{call.lineno} {name}")
    assert not undeclared, f"declare orgs= or org_free= on: {undeclared}"


def test_no_cli_command_connects_outside_the_one_module() -> None:
    stray = []
    for path in sorted((ROOT / "server" / "cli").glob("*.py")):
        if path.name == "_ch.py":
            continue
        for call in _calls(ast.parse(path.read_text())):
            f = call.func
            if not (isinstance(f, ast.Attribute) and f.attr == "connect"):
                continue
            receiver = f.value
            if isinstance(receiver, ast.Name) and receiver.id in _OTHER_CLIENTS:
                continue
            stray.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not stray, f"connect through server/cli/_ch.py instead: {stray}"


def test_a_code_command_that_addresses_an_org_connects_for_it() -> None:
    """A `loom code` coroutine resolving its org connects itself: the connect that
    `code_run` makes around it reads the repository registry and declares no org."""
    missing = []
    for path in sorted((ROOT / "server" / "cli").glob("code_*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            names = {_name(c) for c in _calls(fn)}
            if names & {"_identity", "_repo_cfg"} and not names & {
                "connect_code_plane",
                "connect_declared",
            }:
                missing.append(f"{path.relative_to(ROOT)}:{fn.lineno} {fn.name}")
    assert not missing, f"connect_code_plane (or connect_declared) in: {missing}"


def test_a_command_with_a_dry_run_declares_no_org_on_it() -> None:
    """A dry run only reads: `orgs=(args.org,)` there would record a mistyped org. The
    flag is read by the command or by a function of its module it calls."""
    literal = []
    for path in _sources():
        tree = ast.parse(path.read_text())
        defs = {
            n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for fn in defs.values():
            if "orgs=(args.org,)" not in ast.unparse(fn):
                continue
            seen, todo = set(), [fn.name]
            while todo:
                name = todo.pop()
                if name in seen:
                    continue
                seen.add(name)
                todo += [_name(c) for c in _calls(defs[name]) if _name(c) in defs]
            if any("args.dry_run" in ast.unparse(defs[name]) for name in seen):
                literal.append(f"{path.relative_to(ROOT)}:{fn.lineno} {fn.name}")
    assert not literal, f"declare orgs=dry_run_orgs(args) in: {literal}"


def test_a_connect_that_declares_nothing_is_refused() -> None:
    with pytest.raises(TypeError, match="declares the orgs"):
        _ch._declared(None, "")
    # A declared but unset org (an optional --org) names none, and checks nothing.
    assert _ch._declared((None, ""), "") == ()
    assert _ch._declared(("acme", None, ""), "") == ("acme",)
    assert _ch._declared(None, "the cell's settings") == ()


def test_outside_a_cell_nothing_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "")

    async def _never(*_a: Any) -> None:
        raise AssertionError("checked")

    monkeypatch.setattr(tenancy, "ensure_held", _never)
    asyncio.run(_ch.check_orgs(["acme"]))


def _serving_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cell whose rows read and let this process serve."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(cell, "gate_reason", lambda: "")


def test_a_cell_the_transports_would_close_refuses_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI applies the transports' gate: a shell whose LOOM_CELL is not the cell
    its Postgres names records nothing, as the server would serve nothing."""
    monkeypatch.setattr(settings, "cell", "eu-2")
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    first = {"is_first": True, "cell": "eu-1"}  # as the pool's json codec decodes it
    monkeypatch.setattr(
        pg, "fetchrow", AsyncMock(return_value={"first": first, "mode": "recording", "own": None})
    )
    never = AsyncMock(side_effect=AssertionError("checked"))
    monkeypatch.setattr(tenancy, "ensure_held", never)
    with pytest.raises(_ch.OrgNotHeld, match="names cell 'eu-1'"):
        asyncio.run(_ch.check_orgs(["acme"]))
    never.assert_not_called()


def test_an_unread_tenancy_refuses_with_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _serving_cell(monkeypatch)
    down = errors.BackendUnavailableError("this cell cannot tell whether it holds org 'acme'")
    monkeypatch.setattr(tenancy, "ensure_held", AsyncMock(side_effect=down))
    with pytest.raises(_ch.OrgNotHeld, match="cannot tell whether it holds org 'acme'"):
        asyncio.run(_ch.check_orgs(["acme"]))


def test_a_cli_counts_the_code_orgs_as_its_server_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its fingerprint holds the code config's orgs, so its placement call is accepted
    as the server's would be."""
    from server.code.domain.index import repos

    _serving_cell(monkeypatch)
    monkeypatch.setattr(cell, "_code_orgs", None)
    monkeypatch.setattr(tenancy, "ensure_held", AsyncMock(return_value=None))
    asyncio.run(_ch.check_orgs(["acme"]))
    assert cell._code_orgs is repos.operator_orgs


def test_a_dry_run_declares_no_org() -> None:
    import argparse

    assert _ch.dry_run_orgs(argparse.Namespace(org="acme", dry_run=True)) == ()
    assert _ch.dry_run_orgs(argparse.Namespace(org="acme", dry_run=False)) == ("acme",)


def test_a_connected_client_is_not_connected_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `loom code` command's connect inside `code_run`'s keeps the open driver."""
    monkeypatch.setattr(settings, "cell", "")
    connects: list[int] = []

    class _Client:
        connected = True

        async def connect(self) -> None:
            connects.append(1)

    asyncio.run(_ch.connect_declared(_Client(), org_free="reads"))
    assert connects == []


def test_a_cell_records_or_refuses_a_declared_org_and_never_places_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, Any]] = []

    async def _held(org: str, auth: Any) -> None:
        seen.append((org, auth))
        if org == "beta":
            raise errors.WrongCellError(org, cell="us-1")

    monkeypatch.setattr(tenancy, "ensure_held", _held)
    _serving_cell(monkeypatch)
    asyncio.run(_ch.check_orgs(["acme"]))
    with pytest.raises(_ch.OrgNotHeld, match="loom cell place --org beta"):
        asyncio.run(_ch.check_orgs(["beta"]))
    # No principal: the check records (in `recording`) or refuses, and places nothing.
    assert seen == [("acme", None), ("beta", None)]


@pytest.mark.parametrize("mode,recorded", [("recording", True), ("enforced", False)])
def test_a_cli_process_reads_the_cells_mode_before_it_checks(
    monkeypatch: pytest.MonkeyPatch, mode: str, recorded: bool
) -> None:
    """A CLI process starts with no cell rows read: the check reads them, so a
    `recording` cell records a new org and an `enforced` one refuses it."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    statements: list[str] = []

    async def _fetchrow(_sql: str) -> dict[str, Any]:
        return {"first": None, "mode": mode, "own": None}

    async def _fetchval(sql: str, org: str) -> bool:
        statements.append(sql)
        return sql != tenancy._HOLDS  # held only once recorded

    monkeypatch.setattr(pg, "fetchrow", _fetchrow)
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    if recorded:
        asyncio.run(_ch.check_orgs(["acme"]))
        assert statements == [tenancy._HOLDS, tenancy._RECORD]
    else:
        with pytest.raises(_ch.OrgNotHeld, match="loom cell place --org acme"):
            asyncio.run(_ch.check_orgs(["acme"]))
        assert statements == [tenancy._HOLDS]


def test_a_cli_recording_insert_that_finds_enforced_refuses_and_places_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cell's rows read `recording`, but attach wrote `enforced` before the insert:
    it records nothing, and the command refuses, naming `loom cell place`, with no
    placement call."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    statements: list[str] = []

    async def _fetchrow(_sql: str) -> dict[str, Any]:
        return {"first": None, "mode": "recording", "own": None}

    async def _fetchval(sql: str, org: str) -> None:
        statements.append(sql)  # nothing held, and the insert finds `enforced`

    monkeypatch.setattr(pg, "fetchrow", _fetchrow)
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    monkeypatch.setattr(tenancy, "_place", AsyncMock(side_effect=AssertionError("placed")))
    with pytest.raises(_ch.OrgNotHeld, match="loom cell place --org acme"):
        asyncio.run(_ch.check_orgs(["acme"]))
    assert statements == [tenancy._HOLDS, tenancy._RECORD, tenancy._HOLDS]


def test_a_cell_whose_rows_cannot_be_read_refuses_every_declared_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(side_effect=OSError("connection refused")))
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    with pytest.raises(_ch.OrgNotHeld, match="not read yet"):
        asyncio.run(_ch.check_orgs(["acme"]))
    # A command declaring no org reads nothing.
    asyncio.run(_ch.check_orgs([]))


def test_a_command_for_an_org_the_cell_does_not_hold_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[int] = []
    disconnected: list[int] = []

    class _Client:
        connected = False

        async def connect(self) -> None:
            return None

    async def _configured(_ch: Any) -> None:
        return None

    async def _disconnect(_c: Any) -> None:
        disconnected.append(1)

    async def _refuse(orgs: Any) -> None:
        if "beta" in tuple(orgs):
            raise _ch.OrgNotHeld("refusing: org 'beta' is not held by this cell")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr("server.core.pg_profile.check_configured", _configured)
    monkeypatch.setattr(_ch, "disconnect", _disconnect)
    monkeypatch.setattr(_ch, "check_orgs", _refuse)

    async def _work(_c: Any) -> int:
        ran.append(1)
        return 0

    with pytest.raises(_ch.OrgNotHeld):
        _ch.run_connected(_work, orgs=("beta",))
    assert ran == [] and disconnected == [1]


@pytest.mark.parametrize(
    "arg_org,code_org,dry_run,declared",
    [
        ("beta", "acme", False, ("beta",)),
        (None, "acme", False, ("acme",)),
        ("beta", "acme", True, ()),
    ],
)
def test_a_code_index_command_declares_the_org_it_writes(
    monkeypatch: pytest.MonkeyPatch,
    arg_org: str | None,
    code_org: str,
    dry_run: bool,
    declared: tuple,
) -> None:
    """Its `--org`, else the code plane's: `loom code init --org beta` is checked in beta.
    A dry run (`answers-purge --dry-run`) only reads, and declares none."""
    import argparse

    seen: list[tuple[str, ...]] = []

    class _Client:
        connected = False

        async def connect(self) -> None:
            return None

    async def _check(orgs: Any) -> None:
        seen.append(tuple(orgs))

    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "code_org", code_org)
    monkeypatch.setattr(_ch, "check_orgs", _check)
    args = argparse.Namespace(org=arg_org, namespace="code-x", dry_run=dry_run)
    asyncio.run(_ch.connect_code_plane(_Client(), args))
    assert seen == [declared]


def test_a_code_command_that_refuses_its_arguments_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`loom code backfill --org acmee` with no namespace exits before it connects, so a
    `recording` cell keeps no row for the mistyped org."""
    import argparse

    connected: list[int] = []

    class _Client:
        connected = False

        async def connect(self) -> None:
            connected.append(1)

    monkeypatch.setattr(settings, "code_namespace", "")
    monkeypatch.setattr(_ch, "check_orgs", AsyncMock(side_effect=AssertionError("checked")))
    with pytest.raises(SystemExit, match="no code namespace addressed"):
        asyncio.run(
            _ch.connect_code_plane(_Client(), argparse.Namespace(org="acmee", namespace=None))
        )
    assert connected == []


class _UnusedClient:
    """A client whose connect a test asserts never ran."""

    connected = False
    connects: list[int]

    def __init__(self) -> None:
        self.connects = []

    async def connect(self) -> None:
        self.connects.append(1)


@pytest.mark.parametrize("command", ["scip_full", "backfill"])
@pytest.mark.parametrize(
    "repos,refusal",
    [
        ([], "not a configured code repo entry"),
        # One entry, another org's: a mistyped --org is not its entry.
        ([("acme", "code-x")], "acmee/code-x is not a configured code repo entry: pass --org acme"),
    ],
)
def test_a_code_command_for_an_unconfigured_entry_records_nothing(
    monkeypatch: pytest.MonkeyPatch, command: str, repos: list, refusal: str
) -> None:
    """`loom code scip-full --org acmee --namespace code-x`, where acmee/code-x is no
    configured entry, exits before it connects."""
    import argparse
    from types import SimpleNamespace

    from server.cli import code_backfill, code_scip

    client = _UnusedClient()
    configured = [SimpleNamespace(org=o, namespace=n) for o, n in repos]
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(_ch, "check_orgs", AsyncMock(side_effect=AssertionError("checked")))
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: configured)
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda *_a: None)
    monkeypatch.setattr("server.code.domain.index.repos.page_namespaces", lambda: set())
    args = argparse.Namespace(org="acmee", namespace="code-x", phase="index")
    run = code_scip._scip_full if command == "scip_full" else code_backfill._backfill
    with pytest.raises(SystemExit, match=refusal):
        asyncio.run(run(args))
    assert client.connects == []


@pytest.mark.parametrize(
    "repos,remedy",
    [
        ([("beta", "main"), ("acme", "feature")], "pass --namespace feature,"),
        ([("beta", "main")], "pass --org beta for beta/main,"),
    ],
)
def test_an_org_with_no_entry_at_its_default_namespace_is_told_its_own_entries(
    monkeypatch: pytest.MonkeyPatch, repos: list, remedy: str
) -> None:
    """With no --namespace, an org whose entries are at other namespaces is told those,
    not another org's entry."""
    import argparse
    from types import SimpleNamespace

    from server.cli import code_run

    configured = [SimpleNamespace(org=o, namespace=n) for o, n in repos]
    monkeypatch.setattr(settings, "code_namespace", "code-default")
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: configured)
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda *_a: None)
    monkeypatch.setattr("server.code.domain.index.repos.page_namespaces", lambda: set())
    with pytest.raises(SystemExit, match=remedy):
        code_run._repo_cfg(argparse.Namespace(org="acme", namespace=None))


def test_a_code_command_with_an_unknown_model_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reembed` and `vector-twin` refuse an unknown model before they connect."""
    import argparse

    from server.cli import code_maintenance

    client = _UnusedClient()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(_ch, "check_orgs", AsyncMock(side_effect=AssertionError("checked")))
    args = argparse.Namespace(
        org="acmee",
        namespace="code-x",
        embedding_model="no-such-model",
        source_model="",
        prefix="",
        all_namespaces=False,
    )
    with pytest.raises(ValueError, match="not enabled"):
        asyncio.run(code_maintenance._reembed(args))
    assert asyncio.run(code_maintenance._vector_twin(args)) == 2
    assert client.connects == []


@pytest.mark.parametrize("found", [False, True])
def test_scip_retry_declares_its_org_only_once_it_has_the_batch(
    monkeypatch: pytest.MonkeyPatch, found: bool
) -> None:
    import argparse

    from server.cli import code_scip

    steps: list[str] = []

    async def _check(orgs: Any) -> None:
        steps.append(f"check {tuple(orgs)}")

    class _Repo:
        async def get_scip_batch(self, *_a: Any) -> dict | None:
            steps.append("read")
            return {"commit_sha": "c", "total_parts": 1} if found else None

        async def upsert_scip_batch(self, *_a: Any, **_k: Any) -> None:
            steps.append("write")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _UnusedClient())
    monkeypatch.setattr("server.code.store.index.CodeRepo", _Repo)
    monkeypatch.setattr(_ch, "check_orgs", _check)
    args = argparse.Namespace(org="acmee", namespace="code-x", batch_id="b-1")
    assert asyncio.run(code_scip._scip_retry(args)) == (0 if found else 2)
    # The org is checked only once its batch is found, and before anything is written.
    assert steps == (["read", "check ('acmee',)", "write"] if found else ["read"])


def test_on_a_cell_a_declared_connect_verifies_the_deployment_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class _Client:
        connected = False

        async def connect(self) -> None:
            order.append("connect")

    async def _configured(_c: Any) -> None:
        order.append("deployment")

    async def _check(orgs: Any) -> None:
        order.append("orgs")

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr("server.core.pg_profile.check_configured", _configured)
    monkeypatch.setattr(_ch, "check_orgs", _check)
    asyncio.run(_ch.connect_declared(_Client(), orgs=("acme",)))
    assert order == ["connect", "deployment", "orgs"]


@pytest.mark.parametrize("heal", [False, True])
def test_reconcile_records_its_org_on_a_recording_cell_only_when_it_heals(
    monkeypatch: pytest.MonkeyPatch, heal: bool
) -> None:
    """Without --heal `loom code reconcile` only reads, so a recording cell records
    nothing for it; with --heal it writes the entry's org, which is recorded."""
    import argparse
    import types

    from server.cli import code_maintenance
    from server.code.store import index as code_index
    from server.core import ch as ch_mod

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    statements: list[tuple[str, str]] = []

    async def _fetchval(sql: str, org: str) -> bool:
        statements.append((sql, org))
        return sql != tenancy._HOLDS  # held only once recorded

    row = {"first": None, "mode": "recording", "own": None}
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value=row))
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(pg_profile, "check_configured", AsyncMock())
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    monkeypatch.setattr(ch_mod, "get_client", lambda: types.SimpleNamespace(connected=True))
    monkeypatch.setattr(code_maintenance, "_repo_cfg", lambda _a: types.SimpleNamespace(org="acme"))

    class _Stop(Exception):
        pass

    def _past_the_connect() -> None:
        raise _Stop

    monkeypatch.setattr(code_index, "CodeRepo", _past_the_connect)
    with pytest.raises(_Stop):
        asyncio.run(code_maintenance._reconcile(argparse.Namespace(heal=heal, no_fetch=True)))
    assert statements == ([(tenancy._HOLDS, "acme"), (tenancy._RECORD, "acme")] if heal else [])


@pytest.mark.parametrize("pending", [False, True])
def test_the_embed_phase_declares_its_org_only_once_it_has_chunks_to_embed(
    monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    """`loom code backfill --phase embed` reads its first page before it declares its
    org: a mistyped --org has nothing pending, so a `recording` cell records nothing,
    and the pass's status row, keyed by namespace, still lands."""
    import argparse

    from server.cli import code_backfill

    declared: list[str] = []
    recorded: list[dict] = []

    async def _declare(_ch_client: Any, args: argparse.Namespace) -> None:
        declared.append(args.org)
        raise _ch.OrgNotHeld("stop after the declaration")

    async def _record(*a: Any, **k: Any) -> None:
        recorded.append({"args": a, **k})

    class _Repo:
        async def pending_embed_chunks(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return [{"org": "acme", "namespace": "code-x", "content": "x"}] if pending else []

    class _Embedder:
        model_name = "m"

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(code_backfill, "connect_code_plane", _declare)
    monkeypatch.setattr(code_backfill, "_record_manual_pass", _record)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", AsyncMock())
    args = argparse.Namespace(org="acmee", namespace="code-x", phase="embed")
    if pending:
        with pytest.raises(_ch.OrgNotHeld):
            asyncio.run(code_backfill._backfill_embed(_Repo(), "acmee", "code-x", args))
        assert declared == ["acmee"]
    else:
        assert asyncio.run(code_backfill._backfill_embed(_Repo(), "acmee", "code-x", args)) == 0
        (row,) = recorded
        assert declared == [] and row["args"][1:] == ("acmee", "code-x")
