"""The settings singleton is read by attribute, never through getattr with a default.

`getattr(settings, "field", default)` repeats a default that `Settings`
already owns; the copies drift and they hide a reader from anyone deleting the field — the string survives
the deletion and the fallback quietly takes over. Plain `settings.field`
fails loudly instead.

The rule covers names bound to `server.settings.settings` by an import.
Functions that take a settings object as a parameter are out of scope: tests
pass stubs there.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"


class _Scope:
    def __init__(self, node: ast.AST) -> None:
        self.singleton: set[str] = set()
        self.other: set[str] = set()
        for child in _own_nodes(node):
            if isinstance(child, ast.ImportFrom) and child.module == "server.settings":
                for alias in child.names:
                    if alias.name == "settings":
                        self.singleton.add(alias.asname or alias.name)
                    else:
                        self.other.add(alias.asname or alias.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                self.other.update((a.asname or a.name).split(".")[0] for a in child.names)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                self.other.add(child.id)
            elif isinstance(child, ast.arg):
                self.other.add(child.arg)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.other.add(child.name)

    def resolves(self, name: str) -> bool | None:
        """True: the singleton; False: something else; None: not bound here."""
        if name in self.other:
            return False
        return True if name in self.singleton else None


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(node: ast.AST):
    """Nodes of `node`'s own scope: its body minus nested scopes' bodies."""
    stack = list(ast.iter_child_nodes(node))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        stack = list(ast.iter_child_nodes(node.args)) + [
            n for n in ast.iter_child_nodes(node) if n is not node.args
        ]
    while stack:
        child = stack.pop()
        yield child
        if isinstance(child, _SCOPES):
            continue  # its name is bound here; its body is its own scope
        stack.extend(ast.iter_child_nodes(child))


def settings_getattrs(tree: ast.Module) -> list[ast.Call]:
    """getattr(<settings singleton>, "<literal>", ...) calls in `tree`."""
    found: list[ast.Call] = []

    def visit(node: ast.AST, scopes: list[_Scope]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                visit(child, [*scopes, _Scope(child)])
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "getattr"
                and len(child.args) in (2, 3)
                and isinstance(child.args[0], ast.Name)
                and isinstance(child.args[1], ast.Constant)
                and isinstance(child.args[1].value, str)
                and child.args[1].value.isidentifier()
            ):
                name = child.args[0].id
                for scope in reversed(scopes):
                    verdict = scope.resolves(name)
                    if verdict is not None:
                        if verdict:
                            found.append(child)
                        break
            visit(child, scopes)

    visit(tree, [_Scope(tree)])
    return found


def test_settings_singleton_is_read_by_attribute() -> None:
    offenders = [
        f"{path.relative_to(SERVER.parent)}:{call.lineno} {ast.unparse(call)}"
        for path in sorted(SERVER.rglob("*.py"))
        for call in settings_getattrs(ast.parse(path.read_text()))
    ]
    assert not offenders, "read these as `settings.<field>`:\n" + "\n".join(offenders)


def test_detector_follows_scopes() -> None:
    source = (
        "from server.settings import settings\n"
        "a = getattr(settings, 'x', 1)\n"
        "def f(settings):\n"
        "    return getattr(settings, 'x', 1)\n"
        "def g():\n"
        "    from server.settings import settings as _s\n"
        "    return getattr(_s, 'y', 2)\n"
        "def h():\n"
        "    return getattr(settings, 'z')\n"
    )
    calls = settings_getattrs(ast.parse(source))
    assert [c.args[1].value for c in calls] == ["x", "y", "z"]
    assert [c.lineno for c in calls] == [2, 7, 9]


_SETTINGS_PARAMS = {"settings", "_settings", "settings_obj", "_s"}


def test_settings_passed_in_is_read_by_attribute() -> None:
    """A settings object handed to a function is the singleton in production,
    so a getattr default there is the same drifting copy; a test that needs
    other values passes `settings.model_copy(update=...)`."""
    from server.settings import Settings

    offenders = [
        f"{path.relative_to(SERVER.parent)}:{node.lineno} {ast.unparse(node)}"
        for path in sorted(SERVER.rglob("*.py"))
        if SERVER / "settings" not in path.parents
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) == 3
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in Settings.model_fields
        and ast.unparse(node.args[0]).rsplit(".", 1)[-1] in _SETTINGS_PARAMS
    ]
    assert not offenders, "read these as `<settings>.<field>`:\n" + "\n".join(offenders)
