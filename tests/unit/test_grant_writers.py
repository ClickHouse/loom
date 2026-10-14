"""Every grant row written after a read of the grant goes through grant_writes.

Such a write can lower a raise that landed after its read, and only
`grant_writes` checks the grant log for that. The functions listed here insert
directly because their row cannot lower a grant.
"""

from __future__ import annotations

import ast

from server.core.paths import REPO_ROOT

_DIRECT = {
    "server/auth/self_signup.py::insert_owner_grant": "a new account's admin grant",
    "server/auth/self_signup.py::_provision_agent": "a new service account's grant",
    "server/code/app/index/maintainers.py::_insert": "admin copied to sibling branches",
    "server/identity/app/grants.py::adopt_namespace": "admin on an orphaned namespace",
    "server/identity/app/namespace.py::insert_admin_grant": "admin",
    "server/identity/app/registration.py::grant_self_admin": "admin",
    "server/identity/app/token_grants.py::grant_restricted_to_token": "a minted token's own grants",
    "server/identity/app/grant_writes.py::insert_row": "the checked path",
    "server/identity/app/grant_writes.py::insert_rows": "the checked path",
    "server/identity/store/grants.py::insert_row": "the repository",
    "server/identity/store/grants.py::insert_rows": "the repository",
}


def _writes_a_grant(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    func = node.func
    if func.attr in ("insert_row", "insert_rows"):
        return isinstance(func.value, ast.Name) and func.value.id == "grants_repo"
    return (
        func.attr == "insert"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "loom.namespace_grants"
    )


def test_only_writers_that_cannot_lower_a_grant_insert_directly() -> None:
    found = set()
    for path in sorted((REPO_ROOT / "server").rglob("*.py")):
        module = path.relative_to(REPO_ROOT).as_posix()
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                _writes_a_grant(n) for n in ast.walk(fn)
            ):
                found.add(f"{module}::{fn.name}")
    assert found == set(_DIRECT)
