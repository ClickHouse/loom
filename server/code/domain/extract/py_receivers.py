"""Receiver types for Python member calls.

Without a receiver type, `client.query(sql)` names a method the parser tier
binds only when `query` is unique in the repository (rung 3) or a method of
the caller's own class (rung 2). The compiler tier (scip-python) resolves it exactly
where it has run; the parser tier can do the same wherever the tree states
the receiver's type: a parameter annotation (`client: Client`), an annotated
assignment (`x: Client = …`), a construction (`x = Client(…)`), a class
field (`client: Client` in the class body, or `self.client = Client(…)` in
any method) behind `self.client`, `self` / `cls` inside a class, or a call
whose callee's return annotation is known (`x = make_client()` with
`def make_client() -> Client`). This module reads those bindings once per
file and answers "what is the receiver written at this line", the shape of
`ts_receivers.ReceiverTyper` so the call ladder's rung 2b
narrows the candidates of `query` to `…::Client::query` unchanged. A direct
loop can also recover a recursive call to the owning class when an initializer's
instance field declares that class in a fixed tuple position.

Pure over the parse; no I/O.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from server.code.domain.extract.spans import first_span_at
from server.code.domain.extract.treesitter import node_text

# Builtins, typing helpers and containers are not receivers a repository
# defines methods on; a container's element type is not the container.
_SKIP_TYPES: frozenset[str] = frozenset({
    "None", "Any", "object", "str", "int", "float", "bool", "bytes", "bytearray", "complex",
    "dict", "list", "set", "frozenset", "tuple", "type", "Self", "Callable", "Iterator",
    "Iterable", "Sequence", "Mapping", "MutableMapping", "Generator", "AsyncIterator",
    "AsyncGenerator", "Awaitable", "Coroutine", "Literal", "Union", "ClassVar", "Final",
    "Annotated", "TypeVar", "Protocol", "NamedTuple", "TypedDict",
})
_OPTIONAL_HEADS: frozenset[str] = frozenset({
    "Optional", "Final", "ClassVar", "Annotated", "Required", "NotRequired",
})
_CONTAINER_HEADS: frozenset[str] = frozenset({
    "list", "List", "dict", "Dict", "set", "Set", "frozenset", "FrozenSet", "tuple", "Tuple",
    "Sequence", "Iterable", "Iterator", "Mapping", "MutableMapping", "Generator",
    "AsyncIterator", "AsyncGenerator", "Awaitable", "Coroutine", "Callable", "Union", "Literal",
    "Type", "type",
})
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CALL_PREFIX = "call:"
_FIXTURE_PREFIX = "fixture:"  # a parameter typed by the fixture it requests
# a bare receiver the file never binds (`conf` in
# `conf.getboolean(...)`, imported) typed by its definition's annotation
NAME_PREFIX = "name:"
_FUNCTION_NODES: frozenset[str] = frozenset({"function_definition"})
_IMPORTS = frozenset({"import_statement", "import_from_statement"})


def _last(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def type_name_of(node: Any, source: bytes) -> str:
    """The one named type an annotation denotes, '' when it is a builtin, a
    union of several named types, a container or unknown. `Client` → Client,
    `pkg.Client` → Client, `Optional[Client]` / `Client | None` → Client,
    `list[Client]` → '' (the receiver is the element), `"Client"` → Client."""
    if node is None:
        return ""
    t = node.type
    if t == "type" and node.named_children:
        return type_name_of(node.named_children[0], source)
    if t == "identifier":
        text = node_text(node, source)
        return text if text not in _SKIP_TYPES and _NAME_RE.match(text) else ""
    if t == "attribute":
        return type_name_of(node.child_by_field_name("attribute"), source)
    if t == "string":
        inner = node_text(node, source).strip().strip("'\"")
        return inner if _NAME_RE.match(inner) and inner not in _SKIP_TYPES else ""
    if t == "none":
        return ""
    if t == "subscript":
        value = node.child_by_field_name("value")
        head = _last(node_text(value, source)) if value is not None else ""
        if head in _OPTIONAL_HEADS:
            subs = [c for c in node.named_children if c is not value]
            return type_name_of(subs[0], source) if subs else ""
        if head in _CONTAINER_HEADS:
            return ""
        return type_name_of(value, source)
    if t == "binary_operator":  # `Client | None`
        names = {type_name_of(c, source) for c in node.named_children}
        names.discard("")
        return names.pop() if len(names) == 1 else ""
    if t == "generic_type" and node.named_children:  # `Optional[Reply]`, `list[Row]`
        head = _last(node_text(node.named_children[0], source))
        params = [c for c in node.named_children[1:] if c.type == "type_parameter"]
        args = [a for pnode in params for a in pnode.named_children]
        if head in _OPTIONAL_HEADS:
            return type_name_of(args[0], source) if args else ""
        if head in _CONTAINER_HEADS:
            return ""
        return type_name_of(node.named_children[0], source)
    if t == "parenthesized_expression" and node.named_children:
        return type_name_of(node.named_children[0], source)
    return ""


def callee_text(value: Any, source: bytes) -> str:
    """The callee of a call as written (`Client`, `pkg.Client`, `make_client`,
    `self.make`), through `await`; a call inside the chain
    renders as `f()` (`validate_as(int).gt` → `validate_as().gt`,
    `a.b().c` → `a.b().c`) so a fluent chain types link by link; '' for
    anything else."""
    node = value
    if node is None:
        return ""
    if node.type == "await" and node.named_children:
        node = node.named_children[0]
    if node.type != "call":
        return ""
    fn = node.child_by_field_name("function")
    return _chain_text(fn, source)


def _chain_text(fn: Any, source: bytes) -> str:
    if fn is None:
        return ""
    if fn.type == "identifier":
        text = node_text(fn, source)
        return text if _NAME_RE.match(text) else ""
    if fn.type == "attribute":
        obj = fn.child_by_field_name("object")
        attr = fn.child_by_field_name("attribute")
        if obj is None or attr is None:
            return ""
        attr_text = node_text(attr, source)
        if not _NAME_RE.match(attr_text):
            return ""
        if obj.type in {"identifier", "attribute"}:
            head = _chain_text(obj, source)
        elif obj.type in {"call", "await"}:
            inner = callee_text(obj, source)
            head = inner + "()" if inner else ""
        else:
            head = ""
        return f"{head}.{attr_text}" if head else ""
    return ""


def _constructed(value: Any, source: bytes) -> str:
    """`Client(…)` / `pkg.Client(…)` / `await Client(…)` → Client: a callee
    whose last segment is capitalised is read as a class."""
    callee = callee_text(value, source)
    last = _last(callee)
    return last if last and last[0].isupper() and last not in _SKIP_TYPES else ""


def _called(value: Any, source: bytes) -> str:
    """A call to a lowercase callee — bound once its return type is known."""
    callee = callee_text(value, source)
    last = _last(callee)
    return callee if last and not last[0].isupper() else ""


@dataclass
class _Scope:
    start: int  # 1-indexed, inclusive
    end: int
    bindings: dict[str, str] = field(default_factory=dict)  # receiver text -> type name


def _param_bindings(params: Any, source: bytes, into: dict[str, str]) -> None:
    if params is None:
        return
    for p in params.named_children:
        if p.type in {"typed_parameter", "typed_default_parameter"}:
            name_node = p.child_by_field_name("name") or next(
                (c for c in p.named_children if c.type == "identifier"), None
            )
            tname = type_name_of(p.child_by_field_name("type"), source)
            if not tname and p.type == "typed_default_parameter":
                tname = _constructed(p.child_by_field_name("value"), source)
        elif p.type == "default_parameter":
            name_node = p.child_by_field_name("name")
            tname = _constructed(p.child_by_field_name("value"), source)
        else:
            continue
        if name_node is not None and name_node.type == "identifier" and tname:
            into[node_text(name_node, source)] = tname


def _assignment_binding(
    node: Any, source: bytes, params: dict[str, str] | None = None
) -> tuple[str, str]:
    """(target text, type or call:callee) of one `assignment`, ('', '') when
    it binds nothing the ladder can use. `params` (a method's typed
    parameters) types `self.conn = conn`."""
    left = node.child_by_field_name("left")
    if left is None:
        return "", ""
    if left.type == "identifier":
        target = node_text(left, source)
    elif left.type == "attribute":
        target = node_text(left, source)
        if target.count(".") != 1 or target.split(".")[0] not in {"self", "cls"}:
            return "", ""
    else:
        return "", ""
    tname = type_name_of(node.child_by_field_name("type"), source)
    right = node.child_by_field_name("right")
    if not tname and right is not None:
        tname = _value_binding(right, source, params)
    return (target, tname) if tname else ("", "")


def _value_binding(right: Any, source: bytes, params: dict[str, str] | None) -> str:
    """The type (or `call:callee`) one value expression states: a
    construction, a typed parameter's name, a call to be resolved by its
    return type; for `a or b` / `a and b` (`ch = ch or
    _default_client()`), the first operand that states one."""
    if right.type == "boolean_operator":
        for operand in right.named_children:
            found = _value_binding(operand, source, params)
            if found:
                return found
        return ""
    if right.type == "parenthesized_expression" and right.named_children:
        return _value_binding(right.named_children[0], source, params)
    tname = _constructed(right, source)
    if not tname and right.type == "identifier" and params:
        tname = params.get(node_text(right, source), "")
    if not tname:
        callee = _called(right, source)
        tname = _CALL_PREFIX + callee if callee else ""
    return tname


def _body_bindings(
    node: Any, source: bytes, into: dict[str, str], *, fields: bool,
    params: dict[str, str] | None = None,
) -> None:
    """Assignments inside a body, without entering nested functions or
    classes (their scopes are their own). `fields` also records `self.x`
    targets (for class bindings); a scope's own pass keeps only names."""
    for child in node.named_children:
        if child.type in _FUNCTION_NODES or child.type == "class_definition":
            continue
        if child.type == "assignment":
            target, tname = _assignment_binding(child, source, params)
            if target and (fields or "." not in target):
                if fields:
                    into.setdefault(target, tname)
                else:
                    into[target] = tname
        _body_bindings(child, source, into, fields=fields, params=params)


def _class_bindings(class_node: Any, source: bytes) -> dict[str, str]:
    """`self` / `cls` → the class; `self.<field>` from annotated fields in
    the class body and from `self.<field> = …` in any method."""
    out: dict[str, str] = {}
    name = class_node.child_by_field_name("name")
    if name is not None:
        out["self"] = node_text(name, source)
        out["cls"] = node_text(name, source)
    body = class_node.child_by_field_name("body")
    if body is None:
        return out
    for member in body.named_children:
        stmt = member
        if stmt.type == "expression_statement" and stmt.named_children:
            stmt = stmt.named_children[0]
        if stmt.type == "assignment":
            target, tname = _assignment_binding(stmt, source)
            if target and "." not in target:
                out.setdefault("self." + target, tname)
        fn = stmt
        if fn.type == "decorated_definition":
            fn = fn.child_by_field_name("definition") or fn
        if fn.type == "function_definition":
            fbody = fn.child_by_field_name("body")
            if fbody is not None:
                params: dict[str, str] = {}
                _param_bindings(fn.child_by_field_name("parameters"), source, params)
                fields: dict[str, str] = {}
                _body_bindings(fbody, source, fields, fields=True, params=params)
                for k, v in fields.items():
                    if k.startswith(("self.", "cls.")):
                        out.setdefault("self." + k.split(".", 1)[1], v)
    return out


def _generic_parts(node: Any, source: bytes) -> tuple[str, list[Any]]:
    if node is None:
        return "", []
    if node.type == "type" and len(node.named_children) == 1:
        node = node.named_children[0]
    parts = node.named_children
    if node.type == "subscript" and parts:
        return node_text(parts[0], source), parts[1:]
    if node.type == "generic_type" and len(parts) == 2 and parts[1].type == "type_parameter":
        return node_text(parts[0], source), parts[1].named_children
    return "", []


def _typing_container(head: str, imports: dict[str, tuple[str, str]]) -> str:
    if not head:
        return ""
    owner, _, name = head.partition(".")
    if name:
        binding = imports.get(owner)
        return name.lower() if binding == ("typing", "") and name in ("List", "Tuple") else ""
    if (imported := imports.get(owner)) is not None:
        module, member = imported
        return member.lower() if module == "typing" and member in ("List", "Tuple") else ""
    return owner if owner in ("list", "tuple") else ""


def _tuple_items(
    annotation: Any, source: bytes, imports: dict[str, tuple[str, str]], owner: str
) -> tuple[str, ...]:
    head, args = _generic_parts(annotation, source)
    if _typing_container(head, imports) != "list" or len(args) != 1:
        return ()
    item_head, fields = _generic_parts(args[0], source)
    if _typing_container(item_head, imports) != "tuple":
        return ()
    items = [
        f.named_children[0] if f.type == "type" and len(f.named_children) == 1 else f
        for f in fields
    ]
    # A starred type or Unpack can describe any number of positions.
    if any(
        item.type not in {"identifier", "string"}
        or (item.type == "string" and not _NAME_RE.fullmatch(node_text(item, source)[1:-1]))
        for item in items
    ):
        return ()
    return tuple(owner if type_name_of(item, source) == owner else "" for item in items)


def _first_parameter(node: Any, source: bytes) -> str:
    params = node.child_by_field_name("parameters")
    if params is None or not params.named_children:
        return ""
    first = params.named_children[0]
    if first.type in {"default_parameter", "typed_default_parameter"}:
        first = first.child_by_field_name("name")
    elif first.type == "typed_parameter":
        first = first.named_children[0] if first.named_children else None
    return node_text(first, source) if first is not None and first.type == "identifier" else ""


def _method_body(node: Any, source: bytes) -> Any:
    if node.type == "function_definition" and _first_parameter(node, source) == "self":
        return node.child_by_field_name("body")
    return None


def _self_field(node: Any, source: bytes, receiver: str = "self") -> str:
    if node is None or node.type != "attribute":
        return ""
    obj, member = (node.child_by_field_name(k) for k in ("object", "attribute"))
    if obj is None or member is None or obj.type != "identifier":
        return ""
    return "self." + node_text(member, source) if node_text(obj, source) == receiver else ""


def _body_nodes(body: Any) -> Iterator[Any]:
    stack = list(reversed(body.named_children))
    while stack:
        node = stack.pop()
        yield node
        if node.type not in _FUNCTION_NODES and node.type not in ("class_definition", "lambda"):
            stack.extend(reversed(node.named_children))


def _binding_imports(node: Any, source: bytes) -> dict[str, tuple[str, str]]:
    try:
        (statement,) = ast.parse(node_text(node, source)).body
    except (SyntaxError, ValueError):
        return {}
    if not isinstance(statement, (ast.Import, ast.ImportFrom)):
        return {}
    imports = {}
    for alias in statement.names:
        head = alias.asname or alias.name.split(".", 1)[0]
        if isinstance(statement, ast.Import):
            imports[head] = (alias.name if alias.asname else head, "")
        else:
            imports[head] = ("." * statement.level + (statement.module or ""), alias.name)
    return imports


def _loop_type_imports(module: Any, source: bytes) -> dict[str, tuple[str, str]]:
    imports: dict[str, tuple[str, str]] = {}
    for node in _body_nodes(module):
        if node.type == "match_statement":
            return dict.fromkeys(("list", "tuple"), ("", ""))
        if node.type not in _IMPORTS:
            continue
        aliases = _binding_imports(node, source)
        if "*" in aliases:
            return dict.fromkeys(("list", "tuple"), ("", ""))
        if node.parent.type != "module":
            aliases = dict.fromkeys(aliases, ("", ""))
        imports.update(aliases)
    dependent = imports.keys() | {"list", "tuple"}
    for name in _written_names(module, source, imports=False) & dependent:
        imports[name] = ("", "")
    return imports


def _written_names(
    body: Any, source: bytes, *, imports: bool = True, ignore_definition: Any = None
) -> set[str]:
    names: set[str] = set()
    for node in _body_nodes(body):
        if node == ignore_definition:
            continue
        if node.type in _BINDING_NODES or node.type == "delete_statement":
            names.update(_bound_identifiers(node, source))
        elif imports and node.type in _IMPORTS:
            names.update(_binding_imports(node, source))
        elif node.type in ("function_definition", "class_definition"):
            n = node.child_by_field_name("name")
            if n is not None:
                names.add(node_text(n, source))
    return names


def _written_fields(target: Any, source: bytes, receiver: str) -> Iterator[str]:
    if target is None:
        return
    if name := _self_field(target, source, receiver):
        yield name
    elif target.type in {
        "pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern",
        "as_pattern_target", "delete_statement", "expression_list", "parenthesized_expression",
    }:
        for child in target.named_children:
            yield from _written_fields(child, source, receiver)


def _field_write_target(node: Any) -> Any:
    if node.type == "delete_statement":
        return node
    if node.type == "with_item":
        value = node.child_by_field_name("value")
        return value.child_by_field_name("alias") if value is not None else None
    field = _BINDING_FIELDS.get(node.type)
    return node.child_by_field_name(field) if field else None


def _tuple_fields(
    body: Any, source: bytes, loop_imports: Callable[[], dict[str, tuple[str, str]]], owner: str
) -> dict[str, tuple[str, ...]]:
    initializers = [
        n
        for n in body.named_children
        if n.type == "function_definition"
        and (name := n.child_by_field_name("name")) is not None
        and node_text(name, source) == "__init__"
    ]
    if len(initializers) != 1:
        return {}
    (initializer,) = initializers
    if initializer.child_by_field_name("type_parameters") is not None or (
        init := _method_body(initializer, source)
    ) is None:
        return {}
    declarations = []
    for stmt in init.named_children:
        if stmt.type != "expression_statement" or len(stmt.named_children) != 1:
            continue
        assign = stmt.named_children[0]
        if (
            assign.type == "assignment"
            and assign.child_by_field_name("right") is not None
            and (name := _self_field(assign.child_by_field_name("left"), source))
            and (annotation := assign.child_by_field_name("type")) is not None
        ):
            _, arguments = _generic_parts(annotation, source)
            if len(arguments) == 1 and _generic_parts(arguments[0], source)[0]:
                declarations.append((name, assign.start_byte, annotation))
    if not declarations:
        return {}
    if any(n.type == "match_statement" for n in _body_nodes(init)):
        return {}
    own_names = _written_names(init, source)
    if "self" in own_names:
        return {}
    own_names.update(_bound_identifiers(initializer.child_by_field_name("parameters"), source))
    if owner in own_names:
        return {}
    local_imports = {**loop_imports(), **dict.fromkeys(own_names, ("", ""))}
    fields = {
        name: (byte_offset, items)
        for name, byte_offset, annotation in declarations
        if any(items := _tuple_items(annotation, source, local_imports, owner))
    }
    if not fields:
        return {}
    class_nodes = list(_body_nodes(body))
    class_writes = _written_names(body, source, ignore_definition=initializer)
    if class_writes & {"__init__", "__getattribute__", "__setattr__"} or any(
        n.type == "match_statement" for n in class_nodes
    ):
        return {}
    for name in class_writes:
        fields.pop(f"self.{name}", None)
    for method in class_nodes:
        if method.type != "function_definition" or (
            commands := method.child_by_field_name("body")
        ) is None:
            continue
        receiver = _first_parameter(method, source)
        for child in _body_nodes(commands):
            for name in _written_fields(_field_write_target(child), source, receiver):
                if name in fields and child.start_byte != fields[name][0]:
                    fields.pop(name)
        if not fields:
            return {}
    return {name: items for name, (_, items) in fields.items()}


def _tuple_loop_scope(
    node: Any, fields: dict[str, tuple[str, ...]], source: bytes
) -> _Scope | None:
    if (items := fields.get(_self_field(node.child_by_field_name("right"), source))) is None:
        return None
    target, body = (node.child_by_field_name(k) for k in ("left", "body"))
    if (
        target is None
        or body is None
        or target.type not in {"pattern_list", "tuple_pattern", "list_pattern"}
    ):
        return None
    targets = target.named_children
    if len(targets) != len(items) or any(t.type != "identifier" for t in targets):
        return None
    names = [node_text(t, source) for t in targets]
    if len(set(names)) != len(names):
        return None
    bound: set[str] = set()
    for child in _body_nodes(body):
        if child.type in {
            "class_definition",
            "function_definition",
            "lambda",
            "match_statement",
            "import_statement",
            "import_from_statement",
        }:
            return None
        if child.type in _BINDING_NODES or child.type == "delete_statement":
            bound.update(_bound_identifiers(child, source))
    bindings = {
        name: item for name, item in zip(names, items, strict=True) if item and name not in bound
    }
    return _Scope(body.start_point[0] + 1, body.end_point[0] + 1, bindings) if bindings else None


def _class_tuple_loop_scopes(
    node: Any, source: bytes, loop_imports: Callable[[], dict[str, tuple[str, str]]]
) -> list[_Scope]:
    body = node.child_by_field_name("body")
    name = node.child_by_field_name("name")
    if (
        node.parent.type != "module"
        or node.child_by_field_name("type_parameters") is not None
        or body is None
        or name is None
    ):
        return []
    owner = node_text(name, source)
    if not (fields := _tuple_fields(body, source, loop_imports, owner)) or owner in _written_names(
        node.parent, source, ignore_definition=node
    ):
        return []
    methods = [
        statement
        for child in body.named_children
        if (statement := _method_body(child, source)) is not None
    ]
    return [
        scope
        for method in methods
        if "self" not in _written_names(method, source)
        and not any(item.type == "match_statement" for item in _body_nodes(method))
        for block in _body_nodes(method)
        if block.type == "for_statement"
        and (scope := _tuple_loop_scope(block, fields, source)) is not None
    ]


class PyReceiverTyper:
    """Answers `type_of(line, receiver)` for one parsed Python file."""

    def __init__(self, pf: Any, callee_returns: dict[str, str] | None = None) -> None:
        self._scopes: list[_Scope] = []
        self._module = _Scope(1, 1 << 30)
        self._returns: dict[str, str] = dict(callee_returns or {})
        self._same_file: dict[str, str] = {}
        # `get_client().query_all()` — the receiver is a call;
        # its callee's return type types it (`type_of_call`)
        self._receiver_callees: set[str] = set()
        self._receiver_names: set[str] = set()  # bare receivers the file names
        self._bound_names: set[str] = set()  # every name the file binds itself
        self._fixture_params: dict[int, set[str]] = {}
        for _req, param, line in getattr(pf, "fixture_requests", ()) or ():
            self._fixture_params.setdefault(int(line), set()).add(param)
        if pf.tree is not None:
            root, source = pf.tree.root_node, pf.source
            loop_imports = cache(lambda: _loop_type_imports(root, source))
            self._collect(root, source, class_bindings=None, loop_imports=loop_imports)
            _body_bindings(pf.tree.root_node, pf.source, self._module.bindings, fields=False)
            self._collect_receiver_callees(pf.tree.root_node, pf.source)
            self._receiver_names -= self._bound_names
        self._scopes.sort(key=lambda s: s.end - s.start)
        # Per receiver, the scopes binding it, innermost first; looked up by line (a scan
        # of every scope per call site is quadratic in a file of many functions)
        self._binders: dict[str, list[_Scope]] = {}
        for scope in self._scopes:
            for key in scope.bindings:
                self._binders.setdefault(key, []).append(scope)
        self._binder_at: dict[str, Callable[[int], _Scope | None]] = {}

    def _collect_receiver_callees(self, node: Any, source: bytes) -> None:
        for child in node.named_children:
            if child.type in _BINDING_NODES:
                # a parameter, an assignment target, a loop or
                # `with … as` name, an except alias — bound here, not imported
                self._bound_names.update(_bound_identifiers(child, source))
            if child.type == "call":
                fn = child.child_by_field_name("function")
                obj = None
                if fn is not None and fn.type == "attribute":
                    obj = fn.child_by_field_name("object")
                if obj is not None and obj.type == "identifier":
                    name = node_text(obj, source)
                    if name and name[0].islower() and name not in ("self", "cls"):
                        self._receiver_names.add(name)
                if obj is not None:
                    callee = callee_text(obj, source)
                    if callee and not _last(callee)[0].isupper():
                        self._receiver_callees.add(callee)
                    # every call in the chain — `validate_as()`
                    # inside `validate_as().gt` — is a callee to type
                    for part in _chain_calls(callee):
                        if not _last(part)[0].isupper():
                            self._receiver_callees.add(part)
            self._collect_receiver_callees(child, source)

    def type_of_expr(self, line: int, text: str) -> str:
        """The named type of a receiver expression — a name
        (`client`, `self.client`), a call (`make()`), a chain
        (`validate_as().gt()`, `x.gt()`): a call's type is its callee's
        return, looked up as `<ReceiverType>::method` when the chain's
        receiver is typed, else by the callee as written."""
        text = text.strip()
        if not text:
            return ""
        if text.endswith("()"):
            inner = text[:-2]
            if "." in inner:
                obj, method = inner.rsplit(".", 1)
                recv = self.type_of_expr(line, obj)
                if recv:
                    typed = self._returns.get(f"{recv}::{method}", "")
                    if typed:
                        return typed
            return self.type_of_call(inner)
        return self.type_of(line, text)

    def type_of_call(self, callee: str) -> str:
        """The named type a call to `callee` yields, by its return
        annotation (same file or the rows the ingest read); '' when unknown."""
        return self._resolve(_CALL_PREFIX + callee.strip())

    def _collect(
        self, node: Any, source: bytes, class_bindings: dict[str, str] | None,
        loop_imports: Callable[[], dict[str, tuple[str, str]]],
    ) -> None:
        for child in node.named_children:
            if child.type == "class_definition":
                self._scopes.extend(_class_tuple_loop_scopes(child, source, loop_imports))
                self._collect(child, source, _class_bindings(child, source), loop_imports)
                continue
            if child.type in _FUNCTION_NODES:
                scope = _Scope(child.start_point[0] + 1, child.end_point[0] + 1)
                if class_bindings:
                    scope.bindings.update(class_bindings)
                _param_bindings(child.child_by_field_name("parameters"), source, scope.bindings)
                # an untyped parameter a test or fixture
                # requests is the fixture's yield — typed once the fixture's
                # row is read (`fixture:<name>` waits like `call:<callee>`)
                for param in self._fixture_params.get(child.start_point[0] + 1, ()):
                    scope.bindings.setdefault(param, _FIXTURE_PREFIX + param)
                body = child.child_by_field_name("body")
                if body is not None:
                    _body_bindings(body, source, scope.bindings, fields=False)
                self._scopes.append(scope)
                rt = type_name_of(child.child_by_field_name("return_type"), source)
                name = child.child_by_field_name("name")
                if rt and name is not None:
                    bare = node_text(name, source)
                    # two functions of one bare name returning different
                    # types: declined, never guessed
                    self._same_file[bare] = "" if self._same_file.get(bare, rt) != rt else rt
            self._collect(child, source, class_bindings, loop_imports)

    def same_file_returns(self) -> dict[str, str]:
        """Bare callee name → the return annotation this file states."""
        return {k: v for k, v in self._same_file.items() if v}

    def type_of(self, line: int, receiver: str) -> str:
        """The named type of `receiver` (`client`, `self.client`, `self`) as
        written at `line`, '' when the tree does not say."""
        key = receiver.strip()
        if not key or not all(_NAME_RE.match(p) for p in key.split(".")):
            return ""
        if key not in self._binder_at:
            self._binder_at[key] = first_span_at(
                [(s.start, s.end, s) for s in self._binders.get(key, [])]
            )
        scope = self._binder_at[key](line)
        if scope is not None:
            return self._resolve(scope.bindings[key])
        bound = self._resolve(self._module.bindings.get(key, ""))
        if bound or "." in key:
            return bound
        # a name no scope binds is an import's — its
        # definition's annotation (`conf: AirflowConfigParser = …`) is its type
        return self._returns.get(NAME_PREFIX + key, "")

    def _resolve(self, binding: str) -> str:
        if binding.startswith(_FIXTURE_PREFIX):
            return self._returns.get(binding, "")
        if not binding.startswith(_CALL_PREFIX):
            return binding
        callee = binding[len(_CALL_PREFIX):]
        return self._returns.get(callee, "") or self._returns.get(_last(callee), "")

    def callees(self) -> set[str]:
        """Every callee some binding's type waits on."""
        out: set[str] = set(self._receiver_callees)
        bound_anywhere: set[str] = set(self._module.bindings)
        for scope in (*self._scopes, self._module):
            bound_anywhere.update(scope.bindings)
            for v in scope.bindings.values():
                if v.startswith(_CALL_PREFIX):
                    out.add(v[len(_CALL_PREFIX):])
                elif v.startswith(_FIXTURE_PREFIX):
                    out.add(v)  # `fixture:<name>`, resolved by the fixture ladder
        # `name:<receiver>` for a bare receiver no scope binds
        out.update(NAME_PREFIX + n for n in self._receiver_names if n not in bound_anywhere)
        return out

    def bind_returns(self, returns: dict[str, str]) -> None:
        self._returns.update({k: v for k, v in returns.items() if v})


_BINDING_NODES = frozenset({
    "parameters", "lambda_parameters", "assignment", "augmented_assignment",
    "for_statement", "for_in_clause", "with_item", "except_clause", "named_expression",
    "global_statement", "nonlocal_statement", "type_alias_statement",
})
_BINDING_FIELDS = {
    "assignment": "left", "augmented_assignment": "left", "for_statement": "left",
    "for_in_clause": "left", "named_expression": "name",
}


def _bound_identifiers(node: Any, source: bytes) -> set[str]:
    """The plain names a binding node introduces — for a
    parameter list every identifier under it; for an assignment or loop its
    left side's identifiers (a tuple target unpacked, an attribute or
    subscript target skipped: `self.x = …` binds no bare name)."""
    field = _BINDING_FIELDS.get(node.type)
    root = node.child_by_field_name(field) if field else node
    if root is None:
        return set()
    if node.type == "type_alias_statement":
        left = node.child_by_field_name("left")
        if left is None:
            return set()
        generic, _ = _generic_parts(left, source)
        return {generic or node_text(left, source)}
    if node.type == "with_item":
        alias = root.child_by_field_name("alias") if root.type == "as_pattern" else None
        value = root.child_by_field_name("value") if root.type == "as_pattern" else None
        root = alias if alias is not None else value if value is not None else root
    out: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            out.add(node_text(n, source))
        elif n.type in ("attribute", "subscript", "call", "keyword_argument"):
            continue
        else:
            stack.extend(n.named_children)
    return out


def _chain_calls(callee: str) -> list[str]:
    """The inner callees of a chain rendered by `callee_text`:
    `validate_as().gt` → ['validate_as']; `a.b().c().d` → ['a.b', 'a.b().c']."""
    out: list[str] = []
    idx = callee.find("()")
    while idx != -1:
        out.append(callee[:idx])
        idx = callee.find("()", idx + 2)
    return out
