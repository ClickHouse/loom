"""A tunable number is a setting, not a module constant.

A module-level constant whose name says it bounds or paces something (a TTL, a
timeout, an interval, a limit, a batch, a budget) and whose value is a number is
an operator's knob hidden from the configuration reference and from `loom
settings`. The ones that predate this rule are listed in
tests/unit/tunable_constants.txt. The list only shrinks: a new one goes into
`server/settings/` instead, and one moved there leaves the list.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = Path(__file__).parent / "tunable_constants.txt"
_TUNABLE = re.compile(
    r"(TTL|TIMEOUT|INTERVAL|LIMIT|BATCH|BUDGET|_CAP\b|WINDOW|_S$|_MS$|SECONDS|_MAX\b|^MAX_|_MAX_)"
)


def _numeric(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int | float) and not isinstance(node.value, bool)
    if isinstance(node, ast.BinOp):
        return _numeric(node.left) and _numeric(node.right)
    if isinstance(node, ast.UnaryOp):
        return _numeric(node.operand)
    return False


def _constants(tree: ast.Module) -> list[str]:
    out = []
    for stmt in tree.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        for t in targets:
            if (
                isinstance(t, ast.Name)
                and t.id.lstrip("_").isupper()
                and _TUNABLE.search(t.id.lstrip("_"))
                and _numeric(value)
            ):
                out.append(t.id)
    return out


def _found() -> set[str]:
    found = set()
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("server/settings/"):
            continue
        for name in _constants(ast.parse(path.read_text(encoding="utf-8"))):
            found.add(f"{rel}::{name}")
    return found


def _allowed() -> set[str]:
    lines = ALLOWED.read_text().splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def test_no_new_tunable_constant() -> None:
    new = sorted(_found() - _allowed())
    assert not new, (
        "a tunable number belongs in server/settings/ (LOOM_<NAME>, the configuration "
        "reference, `loom settings`), not in a module constant:\n" + "\n".join(new)
    )


def test_the_list_only_names_constants_that_exist() -> None:
    gone = sorted(_allowed() - _found())
    assert not gone, f"drop these from {ALLOWED.name}:\n" + "\n".join(gone)


def test_the_walk_sees_a_tunable_number() -> None:
    src = "A_TTL = 60\n_FETCH_TIMEOUT_S = 2 * 60.0\nNAME = 'x'\nMAX_ROWS: int = 5\nFLAG = True\n"
    assert _constants(ast.parse(src)) == ["A_TTL", "_FETCH_TIMEOUT_S", "MAX_ROWS"]
