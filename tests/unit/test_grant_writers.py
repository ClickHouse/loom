"""Every grant row written after a read of the grant goes through grant_writes.

Such a write can lower a raise that landed after its read, and only
`grant_writes` checks the grant log for that. The functions listed here insert
directly because their row cannot lower a grant.
"""

from __future__ import annotations

import ast

from server.core.paths import REPO_ROOT

_REPO = "server.identity.store.grants"
_TABLE = "loom.namespace_grants"
_INSERTS = {"insert_row", "insert_rows"}

_DIRECT = {
    "server/auth/self_signup.py::insert_owner_grant": "a new account's admin grant",
    "server/auth/self_signup.py::_provision_agent": "a new service account's grant",
    "server/code/app/index/maintainers.py::_insert": "admin copied to sibling branches",
    "server/identity/app/grants.py::adopt_namespace": "admin on an orphaned namespace",
    "server/identity/app/namespace.py::insert_admin_grant": "admin",
    "server/identity/app/registration.py::grant_self_admin": "admin",
    "server/identity/app/token_grants.py::grant_restricted_to_token": "a minted token's own grants",
    "server/identity/app/grant_writes.py::write_row": "the checked path",
    "server/identity/app/grant_writes.py::write_rows": "the checked path",
    "server/identity/app/grant_writes.py::finish_lowering": "the checked path",
    "server/identity/store/grants.py::insert_row": "the repository",
    "server/identity/store/grants.py::insert_rows": "the repository",
}


def _repo_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names a module binds to the grants repository, and to its insert
    functions, under whatever alias."""
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "server.identity.store":
            modules |= {a.asname or a.name for a in node.names if a.name == "grants"}
        elif isinstance(node, ast.ImportFrom) and node.module == _REPO:
            functions |= {a.asname or a.name for a in node.names if a.name in _INSERTS}
        elif isinstance(node, ast.Import):
            modules |= {a.asname for a in node.names if a.name == _REPO and a.asname}
    return modules, functions


def _table_constants(tree: ast.Module) -> set[str]:
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and node.value.value == _TABLE
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _writes_a_grant(
    node: ast.AST, modules: set[str], functions: set[str], tables: set[str]
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in functions
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _INSERTS:
        return isinstance(func.value, ast.Name) and func.value.id in modules
    if func.attr != "insert" or not node.args:
        return False
    table = node.args[0]
    return (isinstance(table, ast.Constant) and table.value == _TABLE) or (
        isinstance(table, ast.Name) and table.id in tables
    )


def test_only_writers_that_cannot_lower_a_grant_insert_directly() -> None:
    found = set()
    for path in sorted((REPO_ROOT / "server").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, functions = _repo_names(tree)
        tables = _table_constants(tree)
        module = path.relative_to(REPO_ROOT).as_posix()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                _writes_a_grant(n, modules, functions, tables) for n in ast.walk(fn)
            ):
                found.add(f"{module}::{fn.name}")
    assert found == set(_DIRECT)


def test_the_gate_sees_an_aliased_repository_and_a_table_constant() -> None:
    tree = ast.parse(
        "from server.identity.store import grants as repo\n"
        "from server.identity.store.grants import insert_rows as put\n"
        "GRANTS = 'loom.namespace_grants'\n"
        "async def a(): await repo.insert_row({})\n"
        "async def b(): await put([])\n"
        "async def c(ch): await ch.insert(GRANTS, [])\n"
    )
    modules, functions = _repo_names(tree)
    tables = _table_constants(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert sum(_writes_a_grant(n, modules, functions, tables) for n in calls) == 3
