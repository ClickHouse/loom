"""Check the declaration behind a static Python re-export."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DECLARATIONS = (ast.ClassDef, *_FUNCTIONS)
_STATEMENTS = (
    *_DECLARATIONS,
    ast.Import,
    ast.ImportFrom,
    ast.Assign,
    ast.AnnAssign,
    ast.If,
    ast.Pass,
)
_BUILTIN_DECORATORS = {"classmethod", "property", "staticmethod"}
_REFLECTIVE = {
    "__builtins__",
    "__dict__",
    "__getattribute__",
    "__import__",
    "__setattr__",
    "currentframe",
    "delattr",
    "eval",
    "exec",
    "f_globals",
    "f_locals",
    "getattr",
    "globals",
    "locals",
    "modules",
    "setattr",
    "vars",
    "_getframe",
}


def is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and is_literal(key) for key in node.keys) and all(
            is_literal(value) for value in node.values
        )
    return False


def _import_name(alias: ast.alias) -> str:
    return alias.asname or alias.name.split(".", 1)[0]


def _executed(body: list[ast.stmt]) -> Iterator[tuple[ast.AST, ast.ClassDef | None, bool]]:
    """Inspect executed scope and declaration headers, omitting dormant function bodies."""
    pending: list[tuple[ast.AST, ast.ClassDef | None, bool]] = [
        (node, None, False) for node in body
    ]
    while pending:
        node, owner, in_header = pending.pop()
        yield node, owner, in_header
        if isinstance(node, (*_DECLARATIONS, ast.Lambda)):
            if isinstance(node, ast.ClassDef):
                pending.extend((statement, node, False) for statement in node.body)
            pending.extend((child, owner, True) for child in _header(node))
        else:
            pending.extend((child, owner, in_header) for child in ast.iter_child_nodes(node))


def _header(node: ast.AST) -> Iterator[ast.AST]:
    for field, value in ast.iter_fields(node):
        if field != "body":
            if isinstance(value, ast.AST):
                yield value
            elif isinstance(value, list):
                yield from (item for item in value if isinstance(item, ast.AST))


def _direct(
    body: list[ast.stmt], name: str
) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | None:
    matches = [node for node in body if isinstance(node, _DECLARATIONS) and node.name == name]
    return matches[0] if len(matches) == 1 else None


def _property_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef, owner: ast.ClassDef | None, decorator: ast.AST
) -> bool:
    return (
        owner is not None
        and isinstance(decorator, ast.Attribute)
        and decorator.attr in {"setter", "getter", "deleter"}
        and isinstance(decorator.value, ast.Name)
        and decorator.value.id == node.name
        and any(
            isinstance(prior, _FUNCTIONS)
            and prior.lineno < node.lineno
            and prior.name == node.name
            and any(isinstance(d, ast.Name) and d.id == "property" for d in prior.decorator_list)
            for prior in owner.body
        )
    )


def _stable(
    body: list[ast.stmt], declarations: set[ast.AST], protected: set[str], binding_line: int
) -> bool:
    imports = {
        _import_name(alias)
        for node in body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    bindings: Counter[str] = Counter()
    calls: set[str] = set()
    for node, owner, in_header in _executed(body):
        if isinstance(node, ast.stmt) and node.lineno >= binding_line:
            if not _safe_after_binding(node):
                return False
        if isinstance(node, _DECLARATIONS):
            if node.name in protected and node not in declarations:
                return False
            bindings[node.name] += 1
            if isinstance(node, ast.ClassDef):
                if (
                    node.keywords
                    or node.decorator_list
                    or any(not isinstance(base, (ast.Name, ast.Attribute)) for base in node.bases)
                ):
                    return False
            if isinstance(node, _FUNCTIONS):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Name):
                        calls.add(decorator.id)
                    elif not isinstance(decorator, ast.Call) and not _property_decorator(
                        node, owner, decorator
                    ):
                        return False
        elif isinstance(node, ast.Expr):
            if not isinstance(node.value, ast.Constant):
                return False
        elif isinstance(node, ast.stmt) and not isinstance(node, _STATEMENTS):
            return False
        elif isinstance(node, ast.Name):
            if node.id in protected or node.id in _REFLECTIVE:
                return False
            if isinstance(node.ctx, ast.Store):
                bindings[node.id] += 1
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            if not isinstance(node.ctx, ast.Load):
                return False
            if isinstance(node, ast.Attribute) and (
                node.attr in protected or node.attr in _REFLECTIVE
            ):
                return False
        elif isinstance(node, ast.alias):
            local = _import_name(node)
            if node.name == "*" or node.name in _REFLECTIVE or local in protected:
                return False
            bindings[local] += 1
        elif isinstance(node, ast.Call):
            # Import-bound annotation and decorator calls are part of a declaration.
            # An initializer call after the selected binding may replace it indirectly.
            if not isinstance(node.func, ast.Name) or (
                not in_header and node.lineno >= binding_line
            ):
                return False
            calls.add(node.func.id)
    return all(
        (name in imports and bindings[name] == 1)
        or (name in _BUILTIN_DECORATORS and bindings[name] == 0)
        for name in calls
    )


def _safe_after_binding(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return node.value is None or is_literal(node.value)
    return isinstance(node, (*_DECLARATIONS, ast.Expr, ast.Pass))


def verified_definition_line(source: bytes, names: list[str], kind: str) -> int:
    """Line of an unconditional declaration that survives local namespace bindings."""
    try:
        body = ast.parse(source.decode("utf-8")).body
    except (SyntaxError, UnicodeDecodeError, RecursionError):
        return 0
    root = _direct(body, names[0])
    if root is None:
        return 0
    target = root
    if len(names) == 2:
        if not isinstance(root, ast.ClassDef) or (member := _direct(root.body, names[1])) is None:
            return 0
        target = member
    expected = (ast.ClassDef,) if kind == "class" else _FUNCTIONS
    # A class binds after its body. A function binds after its declaration header.
    binding_line = (
        (target.end_lineno or target.lineno) if isinstance(target, ast.ClassDef) else target.lineno
    )
    if not isinstance(target, expected) or not _stable(
        body, {root, target}, set(names), binding_line
    ):
        return 0
    return target.lineno
