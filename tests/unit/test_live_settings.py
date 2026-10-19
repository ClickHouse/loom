"""A LIVE setting is read at call time, so an override reaches its next read.

A read at module or class level, in a default argument, in `__init__` or in a
cached function keeps the value it saw first; a field named by a string (the
worker registry builds its specs once, at start) is read where the test cannot
follow. A field with either is not LIVE.
"""

from __future__ import annotations

import ast
from pathlib import Path

from server.settings import Settings
from server.settings._env import is_secret_field
from server.settings._live import is_live

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_DIR = ROOT / "server" / "settings"
LIVE_FIELDS = frozenset(n for n, f in Settings.model_fields.items() if is_live(f))
_ONCE = {"__init__", "__post_init__"}


def _cached(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("cache" in ast.unparse(d) for d in fn.decorator_list)


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
                if child.name in _ONCE:
                    body_where = child.name
                elif _cached(child) or where.startswith("a cached"):
                    body_where = "a cached function"
                else:
                    body_where = "call"
                for stmt in child.body:
                    visit_expr(stmt, body_where)
                continue
            if isinstance(child, ast.Lambda):
                visit_expr(child.body, "call" if where == "module level" else where)
                continue
            if isinstance(child, ast.ClassDef) and where == "module level":
                visit(child, "class level")
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
        if SETTINGS_DIR in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for field, line, where in _reads(tree, fields):
            by_field[field].append((f"{path.relative_to(ROOT)}:{line}", where))
    return by_field


def test_some_settings_are_live() -> None:
    assert len(LIVE_FIELDS) > 100


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
"""
    seen = {(f, w) for f, _, w in _reads(ast.parse(src), frozenset("abcdefg"))}
    assert seen == {
        ("a", "module level"),
        ("b", "class level"),
        ("c", "__init__"),
        ("d", "a default argument or decorator"),
        ("e", "call"),
        ("f", "a cached function"),
        ("g", "a string"),
    }
