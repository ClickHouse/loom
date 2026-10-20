"""A LIVE setting is read at call time, so an override reaches its next read.

A read at module or class level, in a default argument, in `__init__`, in a cached
function or in a function that assigns a module global keeps the value it saw
first. A field named by a string (the worker registry builds its specs once, at
start) is read where the test cannot follow. A field passed as the fallback of a
calibration (`embedder_calibration.effective_*`) is replaced by the calibrated
value whenever there is one. A field with any of these is not LIVE.

The LIVE set is pinned in tests/unit/snapshots/live_settings.json, so making a
setting live, or no longer live, is a reviewed change:
LOOM_UPDATE_SNAPSHOT=1 rewrites it.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from server.memory.app import embedder_calibration
from server.settings import Settings
from server.settings._env import is_secret_field
from server.settings._live import is_live

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_DIR = ROOT / "server" / "settings"
SNAPSHOT = Path(__file__).parent / "snapshots" / "live_settings.json"
LIVE_FIELDS = frozenset(n for n, f in Settings.model_fields.items() if is_live(f))
_ONCE = {"__init__", "__post_init__"}
# The calibration helpers that return the calibrated value over their fallback.
CALIBRATED = frozenset(n for n in dir(embedder_calibration) if n.startswith("effective_"))


def _cached(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("cache" in ast.unparse(d) for d in fn.decorator_list)


def _sets_global(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(n, ast.Global) for n in ast.walk(fn))


def _body_where(fn: ast.FunctionDef | ast.AsyncFunctionDef, where: str) -> str:
    if fn.name in _ONCE:
        return fn.name
    if _cached(fn) or where == "a cached function":
        return "a cached function"
    if _sets_global(fn) or where == "a function that assigns a global":
        return "a function that assigns a global"
    return "call"


def _calibrated(node: ast.AST) -> bool:
    func = node.func if isinstance(node, ast.Call) else None
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in CALIBRATED


def _reads(tree: ast.Module, fields: frozenset[str]) -> list[tuple[str, int, str]]:
    """(field, line, where) for every `x.<field>` and every string naming a field;
    `where` is "call" for a read in a plain function body."""
    out: list[tuple[str, int, str]] = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                for d in [*child.args.defaults, *child.args.kw_defaults, *child.decorator_list]:
                    if d is not None:
                        visit_expr(d, "a default argument or decorator")
                for stmt in child.body:
                    visit_expr(stmt, _body_where(child, where))
                continue
            if isinstance(child, ast.Lambda):
                visit_expr(child.body, "call" if where == "module level" else where)
                continue
            if isinstance(child, ast.ClassDef) and where == "module level":
                visit(child, "class level")
                continue
            if _calibrated(child):
                visit_expr(child, "a calibration fallback")
                continue
            note(child, where)
            visit(child, where)

    def visit_expr(node: ast.AST, where: str) -> None:
        note(node, where)
        visit(node, where)

    def note(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.Attribute) and node.attr in fields:
            out.append((node.attr, node.lineno, where))
        elif isinstance(node, ast.Constant) and node.value in fields:
            out.append((str(node.value), node.lineno, "a string"))

    visit(tree, "module level")
    return out


def _all_reads(fields: frozenset[str]) -> dict[str, list[tuple[str, str]]]:
    by_field: dict[str, list[tuple[str, str]]] = {f: [] for f in fields}
    for path in sorted((ROOT / "server").rglob("*.py")):
        # The settings package declares and loads the settings; `budgets.py` is the one
        # module there that reads them while Loom runs, so it is checked like any other.
        if SETTINGS_DIR in path.parents and path.name != "budgets.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for field, line, where in _reads(tree, fields):
            by_field[field].append((f"{path.relative_to(ROOT)}:{line}", where))
    return by_field


def test_the_live_set_is_pinned() -> None:
    if os.environ.get("LOOM_UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(sorted(LIVE_FIELDS), indent=1) + "\n")
    pinned = set(json.loads(SNAPSHOT.read_text()))
    assert pinned == LIVE_FIELDS, (
        f"made live: {sorted(LIVE_FIELDS - pinned)}; no longer live: "
        f"{sorted(pinned - LIVE_FIELDS)}. If intended, run LOOM_UPDATE_SNAPSHOT=1 pytest "
        "tests/unit/test_live_settings.py"
    )


def test_a_live_setting_is_read_only_at_call_time() -> None:
    problems = []
    for field, reads in sorted(_all_reads(LIVE_FIELDS).items()):
        if not any(where == "call" for _, where in reads):
            problems.append(f"{field}: no `settings.{field}` read at call time")
        problems += [f"{field}: read in {where} at {at}" for at, where in reads if where != "call"]
    assert not problems, (
        "a LIVE setting must be read inside a function body each time it is used; "
        "drop LIVE from the field or move the read:\n" + "\n".join(problems)
    )


def test_no_secret_is_live() -> None:
    # An override is stored as plain JSON in loom.runtime_overrides.
    assert not [f for f in LIVE_FIELDS if is_secret_field(f)]


def test_the_check_sees_each_kind_of_capture() -> None:
    src = """
from server.settings import settings
TOP = settings.a
class C:
    X = settings.b
    def __init__(self):
        self.c = settings.c
def f(x=settings.d):
    return settings.e
@functools.cache
def g():
    return settings.f
SPEC = ("g",)
def h():
    global _client
    _client = Client(settings.h)
def i():
    return effective_noise_floor(settings.i)
"""
    seen = {(f, w) for f, _, w in _reads(ast.parse(src), frozenset("abcdefghi"))}
    assert seen == {
        ("a", "module level"),
        ("b", "class level"),
        ("c", "__init__"),
        ("d", "a default argument or decorator"),
        ("e", "call"),
        ("f", "a cached function"),
        ("g", "a string"),
        ("h", "a function that assigns a global"),
        ("i", "a calibration fallback"),
    }
