"""A tunable number is a setting, not a module constant.

A module-level constant whose name says it bounds or paces something (a TTL, a
timeout, an interval, a limit, a batch, a budget, a retry count, a concurrency, a
page or a threshold) and whose value is a number is an operator's knob hidden from
the configuration reference and from `loom settings`. The ones that predate this rule are listed in
tests/unit/tunable_constants.txt. The list only shrinks: a new one goes into
`server/settings/` instead, and one moved there leaves the list. A number that must
not be a knob (another value is derived from it, or changing it breaks what is stored)
says so on its line, or on a comment line directly above it: `# not a setting: <reason>`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = Path(__file__).parent / "tunable_constants.txt"
_TUNABLE = re.compile(
    r"(TTL|TIMEOUT|INTERVAL|LIMIT|BATCH|BUDGET|_CAP\b|WINDOW|_S$|_MS$|SECONDS|_MAX\b|^MAX_|_MAX_"
    r"|ATTEMPTS|RETRIES|RETRY|PER_PASS|AT_ONCE|CONCURRENCY|PARALLEL|WORKERS\b|_DELAY|_WAIT"
    r"|BACKOFF|_DAYS\b|_HOURS\b|_MINUTES\b|_SIZE\b|_ROWS\b|_PAGE\b|_TOP_K\b|_THRESHOLD\b|_EVERY)"
)


def _numeric(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int | float) and not isinstance(node.value, bool)
    if isinstance(node, ast.BinOp):
        return _numeric(node.left) and _numeric(node.right)
    if isinstance(node, ast.UnaryOp):
        return _numeric(node.operand)
    return False


def _marked(lines: list[str], node: ast.AST) -> bool:
    """`# not a setting: <reason>` on one of the node's lines, or on a comment line
    directly above it (a marker ending the statement before covers that one only)."""
    first = node.lineno - 1  # type: ignore[attr-defined]
    last = getattr(node, "end_lineno", None) or first + 1
    above = lines[first - 1] if first > 0 else ""
    own = lines[first:last] + ([above] if above.lstrip().startswith("#") else [])
    return any("# not a setting: " in line for line in own)


def _constants(tree: ast.Module, lines: list[str]) -> list[str]:
    out = []
    for stmt in tree.body:
        if _marked(lines, stmt):
            continue
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
        src = path.read_text(encoding="utf-8")
        for name in _constants(ast.parse(src), src.splitlines()):
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
    src = (
        "A_TTL = 60\n_FETCH_TIMEOUT_S = 2 * 60.0\nNAME = 'x'\nMAX_ROWS: int = 5\nFLAG = True\n"
        "B_TTL = 9  # not a setting: tokens issued under it read it back\n"
        "C_TIMEOUT = 3\n"
        "# not a setting: a comment line above covers the line below\n"
        "D_TIMEOUT = 4\n"
    )
    assert _constants(ast.parse(src), src.splitlines()) == [
        "A_TTL",
        "_FETCH_TIMEOUT_S",
        "MAX_ROWS",
        "C_TIMEOUT",
    ]


# Keyword arguments that bound a call's wait or its connection pool.
_TIMEOUT_ARGS = {
    "timeout",
    "connect",
    "read",
    "write",
    "pool",
    "connect_timeout",
    "send_receive_timeout",
    "max_connections",
    "max_keepalive_connections",
    "total",
}
# Calls whose first positional argument is a wait, in seconds.
_WAIT_FIRST = ("asyncio.timeout", ".join", ".wait")


def _literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and _numeric(node)


def test_no_call_site_waits_a_literal_timeout() -> None:
    """A timeout or pool size written at a server call site is a knob nobody can turn:
    it reads a setting, or the call says why it is not one (`# not a setting: ...` on
    one of its lines, or on a comment line directly above it).
    The CLI waits on the user's terminal, so its own calls are out of scope."""
    found = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("server/settings/", "server/cli/")):
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            func = ast.unparse(node.func)
            spots = [k.value for k in node.keywords if k.arg in _TIMEOUT_ARGS and _literal(k.value)]
            if func.endswith("Timeout") and node.args and _literal(node.args[0]):
                spots.append(node.args[0])
            if func.endswith("wait_for") and len(node.args) > 1 and _literal(node.args[1]):
                spots.append(node.args[1])
            if func.endswith(_WAIT_FIRST) and node.args and _literal(node.args[0]):
                spots.append(node.args[0])
            if spots and not _marked(lines, node):
                found.append(f"{rel}:{node.lineno}: {func}")
    assert not found, (
        "read the timeout from server/settings/catalog.py, or say on the line why it is "
        f"not a setting (`# not a setting: <reason>`): {found}"
    )
