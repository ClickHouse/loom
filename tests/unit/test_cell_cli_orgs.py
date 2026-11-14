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
    monkeypatch.setattr(cell, "_code_orgs", None)


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


# What an `org_free=` may be: a typed value (`reads`, `writes`, `revokes`), one a helper
# picks per subcommand, a conditional between those and None, or a parameter passed on.
_KINDS = {"reads", "writes", "revokes"}
_PICKERS = {"_org_free", "_revoke_kind"}


def _typed(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        return _name(node) in _KINDS | _PICKERS or _name(node).endswith("_org_free")
    if isinstance(node, ast.IfExp):
        return all(
            _typed(b) or (isinstance(b, ast.Constant) and b.value is None)
            for b in (node.body, node.orelse)
        )
    return isinstance(node, ast.Name) and node.id == "org_free"


def test_every_org_free_connect_says_whether_it_reads_writes_or_revokes() -> None:
    """A `write` or `revocation` is gated on the cell's `first` row; a `read` is not
    (docs/architecture/cells.md, "The CLI declares its orgs")."""
    untyped = []
    for path in _sources():
        for call in _calls(ast.parse(path.read_text())):
            if _name(call) not in _CONNECTS:
                continue
            for k in call.keywords:
                if k.arg == "org_free" and not _typed(k.value):
                    untyped.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not untyped, f"pass reads(...), writes(...) or revokes(...) on: {untyped}"


def test_each_signing_key_subcommand_has_its_own_kind() -> None:
    from server.cli import signing_keys

    kinds = {c: signing_keys._org_free(c).kind for c in ("list", "retire", "rotate", "record-root")}
    assert kinds == {
        "list": "read",
        "retire": "revocation",
        "rotate": "write",
        "record-root": "write",
    }


def _cell_set(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    """LOOM_CELL set under the Postgres authority, the state read answering `answer`
    (a (state, cell) pair, or an exception it raises)."""

    async def _read() -> tuple[str, str]:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(_ch, "_read_state", _read)


@pytest.mark.parametrize("free", [_ch.writes("w"), _ch.revokes("r")])
def test_a_write_or_revocation_on_another_cells_stores_refuses(
    monkeypatch: pytest.MonkeyPatch, free: _ch.OrgFree
) -> None:
    _cell_set(monkeypatch, ("enforced", "us-1"))
    with pytest.raises(_ch.OrgNotHeld, match="first row names cell 'us-1', not LOOM_CELL='eu-1'"):
        asyncio.run(_ch.check_org_free(free))


@pytest.mark.parametrize(
    "answer", [("enforced", "eu-1"), ("recording", "eu-1"), ("open", ""), ("partial", "")]
)
def test_a_write_on_its_own_cell_or_one_not_prepared_yet_runs(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[str, str]
) -> None:
    """Its own `first` row passes; no row, or tables not migrated yet, passes too, so a
    later cell runs these before it is prepared."""
    _cell_set(monkeypatch, answer)
    asyncio.run(_ch.check_org_free(_ch.writes("w")))


def test_a_read_is_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _cell_set(monkeypatch, AssertionError("read"))
    asyncio.run(_ch.check_org_free(_ch.reads("r")))


def test_a_write_refuses_when_the_state_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg as pg_mod

    down = pg_mod.PostgresUnavailableError("Postgres is unavailable")
    _cell_set(monkeypatch, down)
    with pytest.raises(_ch.OrgNotHeld, match="could not be read"):
        asyncio.run(_ch.check_org_free(_ch.writes("w")))


def test_a_revocation_runs_through_an_outage_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core import pg as pg_mod

    _cell_set(monkeypatch, pg_mod.PostgresUnavailableError("Postgres is unavailable"))
    asyncio.run(_ch.check_org_free(_ch.revokes("r")))
    assert "state was not checked" in capsys.readouterr().err
    _cell_set(monkeypatch, TimeoutError())
    asyncio.run(_ch.check_org_free(_ch.revokes("r")))
    assert "state was not checked" in capsys.readouterr().err


def test_a_revocation_refuses_on_a_missing_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an outage lets a revocation through; any other error refuses."""
    denied = Exception("permission denied for table cell_first")
    denied.sqlstate = "42501"  # type: ignore[attr-defined]
    _cell_set(monkeypatch, denied)
    with pytest.raises(_ch.OrgNotHeld, match="could not be read .*permission denied"):
        asyncio.run(_ch.check_org_free(_ch.revokes("r")))


@pytest.mark.parametrize(
    ("org_free", "cell_name", "checked"),
    [
        (_ch.writes("w"), "eu-1", True),
        (_ch.revokes("x"), "eu-1", True),
        (_ch.reads("r"), "eu-1", False),
        (_ch.writes("w"), "", False),
    ],
)
def test_an_org_free_write_or_revocation_checks_its_clickhouse_against_its_postgres(
    monkeypatch: pytest.MonkeyPatch, org_free: _ch.OrgFree, cell_name: str, checked: bool
) -> None:
    """On a cell, `check_configured` ties ClickHouse's recorded deployment to
    Postgres's, so a CLI whose ClickHouse is another cell's is refused with its writes;
    a read, or a write with LOOM_CELL unset, reads nothing new."""
    monkeypatch.setattr(settings, "cell", cell_name)
    seen: list[int] = []

    async def _configured(_c: Any) -> None:
        seen.append(1)

    monkeypatch.setattr(pg_profile, "check_configured", _configured)
    monkeypatch.setattr(_ch, "check_org_free", AsyncMock(return_value=None))

    class _Client:
        connected = True

    asyncio.run(_ch.connect_declared(_Client(), org_free=org_free))
    assert seen == ([1] if checked else [])


# A command whose kind depends on its flags: the picker, the flags of its read-only mode,
# and the kind each mode gets.
_MODE_PICKED = [
    ("secrets", "_reseal_org_free", {"dry_run": True, "check": False}, "read"),
    ("secrets", "_reseal_org_free", {"dry_run": False, "check": True}, "read"),
    ("secrets", "_reseal_org_free", {"dry_run": False, "check": False}, "write"),
    ("restore", "_reconcile_org_free", {"dry_run": True}, "read"),
    ("restore", "_reconcile_org_free", {"dry_run": False}, "write"),
    ("ops", "_sweep_org_free", {"dry_run": True}, "read"),
    ("ops", "_sweep_org_free", {"dry_run": False}, "write"),
    ("ops", "_repair_org_free", {"dry_run": True}, "read"),
    ("ops", "_repair_org_free", {"dry_run": False}, "write"),
    ("hygiene", "_re_verify_org_free", {"resume": False, "erase": False}, "read"),
    ("hygiene", "_re_verify_org_free", {"resume": True, "erase": False}, "write"),
    ("hygiene", "_re_verify_org_free", {"resume": False, "erase": True}, "write"),
    ("code_maintenance", "_compact_org_free", {"dry_run": True}, "read"),
    ("code_maintenance", "_compact_org_free", {"dry_run": False}, "write"),
    ("deployment", "_publish_org_free", {"check": True}, "read"),
    ("deployment", "_publish_org_free", {"check": False}, "write"),
    ("sso", "_unlink_org_free", {"dry_run": True}, "read"),
    ("sso", "_unlink_org_free", {"dry_run": False}, "revocation"),
]


@pytest.mark.parametrize(("module", "picker", "flags", "kind"), _MODE_PICKED)
def test_a_command_whose_kind_depends_on_its_flags_picks_it_from_them(
    module: str, picker: str, flags: dict[str, Any], kind: str
) -> None:
    """A mode that writes nothing reads, so with LOOM_CELL set and Postgres down it still
    runs; the mode that writes is gated."""
    import argparse
    import importlib

    mod = importlib.import_module(f"server.cli.{module}")
    assert getattr(mod, picker)(argparse.Namespace(**flags)).kind == kind


def test_every_flag_picked_connect_uses_a_tested_picker() -> None:
    tested = {(m, p) for m, p, _f, _k in _MODE_PICKED}
    used = set()
    for path in sorted((ROOT / "server" / "cli").glob("*.py")):
        for call in _calls(ast.parse(path.read_text())):
            for k in call.keywords:
                if k.arg == "org_free" and isinstance(k.value, ast.Call):
                    name = _name(k.value)
                    if name.endswith("_org_free") and name != "_org_free":
                        used.add((path.stem, name))
    assert used == tested


@pytest.mark.parametrize(
    ("flags", "kind"),
    [
        ({"list": True}, "read"),
        ({"jti": "j1"}, "revocation"),
        ({"subject": "s1"}, "revocation"),
        ({"subject": "s1", "scim": True, "issued_before": "t"}, "write"),
        ({"jti": "j1", "expires_at": "t"}, "write"),
        ({"subject": "s1", "issued_before": "t"}, "write"),
    ],
)
def test_a_revocation_that_can_give_access_back_is_gated_as_a_write(
    flags: dict[str, Any], kind: str
) -> None:
    """Narrowing SCIM's revocation, or one that lapses or covers older tokens only, is
    not run through an outage: only a plain revocation is."""
    import argparse

    from server.cli import access

    args = argparse.Namespace(**{"list": False, "scim": False, "jti": None, **flags})
    picked = access._revoke_kind(
        args, expires_at=flags.get("expires_at"), issued_before=flags.get("issued_before")
    )
    assert picked.kind == kind


def test_ch_setup_is_refused_before_it_writes_a_user_or_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On another cell's stores, `loom ch-setup` refuses after its preflight connect and
    before `_set_up` writes the reader or a grant."""
    import argparse

    from server.cli import ch_setup

    class _Admin:
        def close(self) -> None:
            return None

    class _Client:
        async def connect(self) -> None:
            return None

    async def _recorded(_ch: Any) -> None:
        return None  # a fresh service: no record yet

    _cell_set(monkeypatch, ("enforced", "us-1"))
    monkeypatch.setattr(ch_setup.preflight, "connect", lambda: _Admin())
    monkeypatch.setattr(ch_setup, "_set_up", lambda *_a: pytest.fail("set ClickHouse up"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock(return_value=None))
    monkeypatch.setattr(pg_profile, "recorded_authority", _recorded)
    monkeypatch.setattr(settings, "ch_user", "loom")
    monkeypatch.setattr(settings, "ch_reader_user", "")
    monkeypatch.setenv("LOOM_CH_READER_USER", "")
    args = argparse.Namespace(print_analyst_sql=False, print_sql=False)
    with pytest.raises(_ch.OrgNotHeld, match="first row names cell 'us-1'"):
        ch_setup._cmd_ch_setup(args)


def test_ch_setup_is_refused_on_another_deployments_clickhouse_before_it_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ClickHouse that records another deployment than this Postgres is refused before
    the reader or a grant is written; the deployment check runs on the state session."""
    import argparse

    from server.cli import ch_setup

    class _Client:
        async def connect(self) -> None:
            return None

    async def _recorded(_ch: Any) -> dict[str, Any]:
        return {"deployment_id": "dep-other", "epoch": 1}

    armed: list[Any] = []

    async def _read() -> tuple[str, str]:
        armed.append(pg._expected)
        raise pg.AuthorityMismatch("Postgres records deployment dep-eu-1")

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(_ch, "_read_state", _read)
    monkeypatch.setattr(pg, "_expected", None)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock(return_value=None))
    monkeypatch.setattr(pg_profile, "recorded_authority", _recorded)

    class _Admin:
        def close(self) -> None:
            return None

    monkeypatch.setattr(ch_setup.preflight, "connect", lambda: _Admin())
    monkeypatch.setattr(ch_setup, "_set_up", lambda *_a: pytest.fail("set ClickHouse up"))
    monkeypatch.setattr(settings, "ch_user", "loom")
    monkeypatch.setenv("LOOM_CH_READER_USER", "")
    monkeypatch.setattr(settings, "ch_reader_user", "")
    with pytest.raises(pg.AuthorityMismatch):
        ch_setup._cmd_ch_setup(argparse.Namespace(print_analyst_sql=False, print_sql=False))
    assert armed == [("dep-other", 1)]


def test_ch_setup_on_a_fresh_clickhouse_passes_the_gate_and_sets_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service with no Loom database or record yet is set up: the gate reads only the
    Postgres `first` row there."""
    import argparse

    from server.cli import ch_setup

    class _NoLoom(Exception):
        pass

    class _Client:
        async def connect(self) -> None:
            return None

    async def _recorded(_ch: Any) -> None:
        raise _NoLoom("Code: 81")

    class _Admin:
        def close(self) -> None:
            return None

    _cell_set(monkeypatch, ("open", ""))
    monkeypatch.setattr("server.core.ch_errors.error_code", lambda e: 81)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock(return_value=None))
    monkeypatch.setattr(pg_profile, "recorded_authority", _recorded)
    monkeypatch.setattr(ch_setup.preflight, "connect", lambda: _Admin())
    set_up: list[int] = []
    monkeypatch.setattr(ch_setup, "_set_up", lambda *_a: set_up.append(1) or 0)
    monkeypatch.setattr(settings, "ch_user", "loom")
    monkeypatch.setenv("LOOM_CH_READER_USER", "")
    monkeypatch.setattr(settings, "ch_reader_user", "")
    assert ch_setup._cmd_ch_setup(argparse.Namespace(print_analyst_sql=False, print_sql=False)) == 0
    assert set_up == [1]


def test_ch_setups_optional_analyst_step_is_left_unwritten_when_a_state_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.cli import ch_setup

    def _refused(*_a: Any, **_k: Any) -> None:
        raise _ch.OrgNotHeld("refusing: this cell's state could not be read (TimeoutError)")

    monkeypatch.setattr(ch_setup, "run_connected", _refused)
    ch_setup._write_analyst()
    assert "loom_analyst (insights.query): not written" in capsys.readouterr().out


def test_a_setup_check_on_a_service_with_no_loom_database_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver refuses a database that does not exist yet at connect (code 81): a
    service set up before the first boot, which passes to the Postgres check."""

    class _Client:
        async def connect(self) -> None:
            raise RuntimeError("Code: 81. DB::Exception: Database loom does not exist")

    _cell_set(monkeypatch, ("open", ""))
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock(return_value=None))
    asyncio.run(_ch.check_setup_stores())


def test_a_setup_check_that_cannot_read_clickhouse_refuses_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        async def connect(self) -> None:
            raise RuntimeError("Code: 497. DB::Exception: loom: Not enough privileges")

    _cell_set(monkeypatch, ("open", ""))
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock(return_value=None))
    import logging

    driver = logging.getLogger("clickhouse_connect")
    monkeypatch.setattr(driver, "level", logging.INFO)
    with pytest.raises(_ch.OrgNotHeld, match="could not be read for the cell check"):
        asyncio.run(_ch.check_setup_stores())
    assert driver.level == logging.INFO  # the mute is undone


def test_the_setup_check_mutes_the_drivers_own_failed_connect_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    class _Client:
        async def connect(self) -> None:
            logging.getLogger("clickhouse_connect").error("failed to connect")
            raise RuntimeError("Code: 81. DB::Exception: Database loom does not exist")

    _cell_set(monkeypatch, ("open", ""))
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(_ch, "disconnect", AsyncMock(return_value=None))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(_ch.check_setup_stores())
    assert not [r for r in caplog.records if r.name == "clickhouse_connect"]


def test_the_loom_code_drain_after_a_subcommand_is_a_read() -> None:
    """The reconnect that drains buffered writes runs after every `loom code`
    subcommand, reads included; the subcommand's own connect gated its writes, so the
    drain must not gate again (an outage would turn a read's exit 0 into a refusal)."""
    drained = [
        k.value
        for call in _calls(ast.parse((ROOT / "server/cli/code_run.py").read_text()))
        if _name(call) == "connect_declared"
        for k in call.keywords
        if k.arg == "org_free"
    ]
    assert drained and all(isinstance(v, ast.Call) and _name(v) == "reads" for v in drained)


def test_an_org_free_connect_runs_its_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def _gate(free: _ch.OrgFree) -> None:
        seen.append(free.kind)

    monkeypatch.setattr(_ch, "check_org_free", _gate)
    monkeypatch.setattr(pg_profile, "check_configured", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "cell", "")

    class _Client:
        connected = True

    asyncio.run(_ch.connect_declared(_Client(), org_free=_ch.writes("w")))
    assert seen == ["write"]


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
        _ch._declared(None, None)
    # A declared but unset org (an optional --org) names none, and checks nothing.
    assert _ch._declared((None, ""), None) == ()
    assert _ch._declared(("acme", None, ""), None) == ("acme",)
    assert _ch._declared(None, _ch.writes("the cell's settings")) == ()


def test_outside_a_cell_nothing_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the ClickHouse authority no cell runs: with LOOM_CELL unset nothing is read."""
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")

    async def _never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("checked")

    monkeypatch.setattr(tenancy, "ensure_held", _never)
    monkeypatch.setattr(pg, "dedicated", _never)
    asyncio.run(_ch.check_orgs(["acme"]))


class _StateConn:
    """A dedicated session answering the state read: how many state tables exist, then
    the state rows."""

    def __init__(self, present: int, row: dict[str, Any] | None = None) -> None:
        self.present = present
        self.row = row or {}
        self.reads: list[str] = []

    async def fetchval(self, sql: str, *_args: Any) -> int:
        self.reads.append(sql)
        return self.present

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any]:
        self.reads.append(sql)
        return self.row


def _unset_on(
    monkeypatch: pytest.MonkeyPatch, conn: _StateConn | BaseException, *, fail: int = 0
) -> list[int]:
    """LOOM_CELL unset under the Postgres authority, `pg.dedicated` answering with
    `conn` (or raising it), after `fail` refusals for too many connections."""
    import contextlib

    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(_ch, "_BUSY_PAUSE_S", 0.0)
    opened: list[int] = []

    @contextlib.asynccontextmanager
    async def _dedicated(**_kw: Any) -> Any:
        opened.append(1)
        if len(opened) <= fail:
            full = Exception("too many connections for role")
            full.sqlstate = "53300"  # type: ignore[attr-defined]
            raise errors.BackendUnavailableError("Postgres is unavailable") from full
        if isinstance(conn, BaseException):
            raise conn
        yield conn

    monkeypatch.setattr(pg, "dedicated", _dedicated)
    monkeypatch.setattr(
        tenancy, "ensure_held", AsyncMock(side_effect=AssertionError("ensure_held ran"))
    )
    return opened


def _rows(*, mode: str, first: tuple[bool, str] | None = (True, "eu-1")) -> dict[str, Any]:
    return {
        "is_first": first[0] if first else None,
        "cell": first[1] if first else None,
        "mode": mode,
        "has_own": True,
        "has_tenancy": True,
    }


def test_unset_cell_lets_an_open_or_recording_deployment_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With LOOM_CELL unset, a deployment that is no cell, or a first cell still
    recording, serves an org-declaring CLI as it serves a server with LOOM_CELL unset,
    and `ensure_held` never runs."""
    _unset_on(monkeypatch, _StateConn(4, _rows(mode="recording")))
    asyncio.run(_ch.check_orgs(["acme"]))
    empty = {"is_first": None, "cell": None, "mode": None, "has_own": False, "has_tenancy": False}
    _unset_on(monkeypatch, _StateConn(4, empty))
    asyncio.run(_ch.check_orgs(["acme"]))


@pytest.mark.parametrize(
    ("row", "named"),
    [
        (_rows(mode="enforced"), "enforced mode (cell 'eu-1')"),
        (_rows(mode="recording", first=(False, "eu-2")), "enforced mode (cell 'eu-2')"),
        (_rows(mode="", first=(True, "eu-1")), "partial cell state"),
    ],
)
def test_unset_cell_refuses_a_cell_past_recording(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], named: str
) -> None:
    _unset_on(monkeypatch, _StateConn(4, row))
    with pytest.raises(_ch.OrgNotHeld, match="and LOOM_CELL is unset") as refused:
        asyncio.run(_ch.check_orgs(["acme"]))
    assert named in str(refused.value)


def test_unset_cell_reads_absent_state_tables_as_open_and_some_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema that predates the state tables cannot be a cell; one with some of them
    is a partial state, refused (judged by `to_regclass`, not by a privilege error)."""
    none = _StateConn(0)
    _unset_on(monkeypatch, none)
    asyncio.run(_ch.check_orgs(["acme"]))
    assert len(none.reads) == 1 and "to_regclass" in none.reads[0]
    _unset_on(monkeypatch, _StateConn(2))
    with pytest.raises(_ch.OrgNotHeld, match="partial cell state .*troubleshooting"):
        asyncio.run(_ch.check_orgs(["acme"]))


def test_unset_cell_refuses_when_the_state_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role missing a grant, or a Postgres that does not answer, refuses: never read
    as `open`."""
    denied = Exception("permission denied for schema loom")
    _unset_on(monkeypatch, denied)
    with pytest.raises(_ch.OrgNotHeld, match="could not be read .*permission denied"):
        asyncio.run(_ch.check_orgs(["acme"]))


def test_unset_cell_refuses_a_read_that_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Hung(_StateConn):
        async def fetchval(self, sql: str, *_args: Any) -> int:
            await asyncio.Event().wait()
            return 0

    _unset_on(monkeypatch, _Hung(4))
    monkeypatch.setattr(_ch, "_STATE_READ_S", 0.05)
    with pytest.raises(_ch.OrgNotHeld, match="TimeoutError: no answer within 0.05 s"):
        asyncio.run(_ch.check_orgs(["acme"]))


def test_unset_cell_retries_a_full_connection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _unset_on(monkeypatch, _StateConn(4, _rows(mode="recording")), fail=2)
    asyncio.run(_ch.check_orgs(["acme"]))
    assert len(opened) == 3
    opened = _unset_on(monkeypatch, _StateConn(4, _rows(mode="recording")), fail=3)
    with pytest.raises(_ch.OrgNotHeld, match="could not be read"):
        asyncio.run(_ch.check_orgs(["acme"]))
    assert len(opened) == _ch._BUSY_TRIES


def test_an_org_free_command_with_loom_cell_unset_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unset_on(monkeypatch, AssertionError("read"))
    asyncio.run(_ch.check_orgs([]))


def test_an_org_declaring_command_checks_its_deployment_cell_or_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_configured` runs for every org-declaring command, code-plane ones
    included, so the cell state is read from this deployment's Postgres; an org-free
    command without `check_authority` skips it."""
    monkeypatch.setattr(settings, "cell", "")
    checked: list[int] = []

    async def _configured(_ch_client: Any) -> None:
        checked.append(1)

    monkeypatch.setattr(pg_profile, "check_configured", _configured)
    monkeypatch.setattr(_ch, "check_orgs", AsyncMock(return_value=None))

    class _Client:
        connected = True

    asyncio.run(_ch.connect_declared(_Client(), orgs=("acme",)))
    assert checked == [1]
    asyncio.run(_ch.connect_declared(_Client(), org_free=_ch.reads("reads")))
    assert checked == [1]


def test_a_cli_with_loom_cell_set_judges_only_its_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI verifies no token and its fingerprint never reaches the router: a CI image
    whose issuer settings differ from the servers' passes, one pointed at another
    bucket refuses."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "document_s3_bucket", "docs")
    monkeypatch.setattr(cell, "_deployment_id", "d1")
    servers = dict(cell.fingerprint("d1"), oidc_issuer="https://idp.example", scim=True)
    first = {"is_first": True, "cell": "eu-1"}
    own = {"current_fp": cell.as_value(servers), "next_fp": None}
    monkeypatch.setattr(
        cell,
        "_view",
        cell._decoded({"first": first, "mode": "enforced", "own": own, "tenancy": True}),
    )
    assert cell.gate_reason() != ""
    assert cell.cli_gate_reason() == ""
    monkeypatch.setattr(settings, "document_s3_bucket", "other")
    assert "this CLI's stores (deployment_id, bucket, bucket_prefix)" in cell.cli_gate_reason()
    monkeypatch.setattr(settings, "document_s3_bucket", "docs")
    monkeypatch.setattr(settings, "document_s3_prefix", "elsewhere")
    assert "match neither value" in cell.cli_gate_reason()
    monkeypatch.setattr(settings, "document_s3_prefix", servers["bucket_prefix"])
    monkeypatch.setattr(cell, "_deployment_id", "d2")
    assert "match neither value" in cell.cli_gate_reason()


@pytest.fixture(autouse=True)
def _directory_attaching(monkeypatch: pytest.MonkeyPatch) -> None:
    """The directory answers that the first cell is `attaching`, as during the rollout
    (the state read's own tests are in test_cell_tenancy.py)."""
    monkeypatch.setattr(tenancy, "directory_says_attaching", AsyncMock(return_value=True))


def _serving_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cell whose rows read and let this process serve."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(cell, "cli_gate_reason", lambda: "")


def test_a_cell_the_transports_would_close_refuses_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI applies the transports' gate: a shell whose LOOM_CELL is not the cell
    its Postgres names records nothing, as the server would serve nothing."""
    monkeypatch.setattr(settings, "cell", "eu-2")
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    first = {"is_first": True, "cell": "eu-1"}  # as the pool's json codec decodes it
    monkeypatch.setattr(
        pg,
        "fetchrow",
        AsyncMock(
            return_value={"first": first, "mode": "recording", "own": None, "tenancy": False}
        ),
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
    """A cell's accepted fingerprint holds its code orgs (its server counts them from
    boot): a CLI process counts them too, so its fingerprint matches and the gate lets
    it through."""
    from server.code.domain.index import repos

    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(repos, "operator_orgs", lambda: {"beta"})
    monkeypatch.setattr(cell, "_code_orgs", repos.operator_orgs)
    mine = cell.fingerprint("d1")
    assert "beta" in mine["configured_orgs"]
    accepted = cell.as_value(mine)
    monkeypatch.setattr(cell, "_code_orgs", None)
    first = {"is_first": True, "cell": "eu-1"}
    own = {"current_fp": accepted, "next_fp": None}
    monkeypatch.setattr(
        pg,
        "fetchrow",
        AsyncMock(return_value={"first": first, "mode": "enforced", "own": own, "tenancy": False}),
    )
    monkeypatch.setattr(tenancy, "ensure_held", AsyncMock(return_value=None))
    asyncio.run(_ch.check_orgs(["acme"]))
    assert cell.gate_reason() == ""


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

    asyncio.run(_ch.connect_declared(_Client(), org_free=_ch.reads("reads")))
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
        return {"first": None, "mode": mode, "own": None, "tenancy": False}

    async def _fetchval(sql: str, org: str) -> bool:
        statements.append(sql)
        return sql != tenancy._HOLDS  # held only once recorded

    monkeypatch.setattr(pg, "fetchrow", _fetchrow)
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "cli_gate_reason", lambda: "")
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
        return {"first": None, "mode": "recording", "own": None, "tenancy": False}

    async def _fetchval(sql: str, org: str) -> None:
        statements.append(sql)  # nothing held, and the insert finds `enforced`

    monkeypatch.setattr(pg, "fetchrow", _fetchrow)
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "cli_gate_reason", lambda: "")
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
    monkeypatch.setattr(pg_profile, "check_configured", AsyncMock(return_value=None))
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
    monkeypatch.setattr(pg_profile, "check_configured", AsyncMock(return_value=None))
    args = argparse.Namespace(org="acmee", namespace="code-x", batch_id="b-1")
    assert asyncio.run(code_scip._scip_retry(args)) == (0 if found else 2)
    # The org is checked only once its batch is found, and before anything is written.
    assert steps == (["read", "check ('acmee',)", "write"] if found else ["read"])


def test_a_declared_connect_verifies_the_deployment_first(
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

    row = {"first": None, "mode": "recording", "own": None, "tenancy": False}
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value=row))
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(pg_profile, "check_configured", AsyncMock())
    monkeypatch.setattr(cell, "cli_gate_reason", lambda: "")
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


def _cli_cell(monkeypatch: pytest.MonkeyPatch, mode: str, statements: list[str]) -> None:
    """A CLI process on cell eu-1 whose rows read `mode`; `statements` records its reads."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})

    async def _fetchval(sql: str, org: str) -> bool:
        statements.append(sql)
        return sql != tenancy._HOLDS  # held only once recorded

    row = {"first": None, "mode": mode, "own": None, "tenancy": False}
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value=row))
    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(pg_profile, "deployment_id", AsyncMock(return_value="d1"))
    monkeypatch.setattr(cell, "cli_gate_reason", lambda: "")


def test_a_restored_first_cell_the_directory_marks_serving_records_nothing_for_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    _cli_cell(monkeypatch, "recording", statements)
    monkeypatch.setattr(tenancy, "directory_says_attaching", AsyncMock(return_value=False))
    with pytest.raises(_ch.OrgNotHeld, match="attaching"):
        asyncio.run(_ch.check_orgs(["acme"]))
    assert statements == [tenancy._HOLDS]


@pytest.mark.parametrize("mode,refused", [("enforced", True), ("recording", False)])
def test_a_cli_holds_the_router_credential_only_on_a_recording_first_cell(
    monkeypatch: pytest.MonkeyPatch, mode: str, refused: bool
) -> None:
    """The credential can place orgs in the shared directory: an org-declaring command
    refuses to run with it set, naming it, unless the cell reads `recording`."""
    statements: list[str] = []
    _cli_cell(monkeypatch, mode, statements)
    monkeypatch.setattr(settings, "cell_router_token", "lcr_" + "0" * 128)
    if refused:
        with pytest.raises(_ch.OrgNotHeld, match="LOOM_CELL_ROUTER_TOKEN"):
            asyncio.run(_ch.check_orgs(["acme"]))
        assert statements == []
    else:
        asyncio.run(_ch.check_orgs(["acme"]))
        assert statements == [tenancy._HOLDS, tenancy._RECORD]


def test_a_cli_declaring_an_org_name_past_the_limit_refuses_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serving_cell(monkeypatch)
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    monkeypatch.setattr(pg, "fetchval", AsyncMock(return_value=None))
    monkeypatch.setattr(cell, "mode", lambda: "recording")
    long = "a" * (cell.ORG_MAX_BYTES + 1)
    with pytest.raises(_ch.OrgNotHeld, match=f"longer than the {cell.ORG_MAX_BYTES}"):
        asyncio.run(_ch.check_orgs([long]))
