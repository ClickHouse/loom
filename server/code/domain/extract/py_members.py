"""Python members and module bindings as symbols.

Beyond defs and classes, a module defines its constants, its type aliases,
its `__all__`, the attributes a class body declares (dataclass, pydantic,
NamedTuple and TypedDict fields, plain class attributes) and an `Enum`'s
members. Without them as symbols they are only text inside a chunk, and
`code.symbol("QueueName")`, `code.outline` and the member fold have nothing
to answer with for Python.

This module reads those bindings off the tree, pure over the parse, and
`lang_python.parse_python` appends them to the file's symbols under the
kinds the TypeScript lane already uses (members are `field` rows so the
tiers fold by qualified name; module bindings are `const`; a type alias is
`type` as in Go and TypeScript):

- a module-level `X = …` / `X: T = …` is `<module>::X`, kind `const`, the
  annotation as its signature; `__all__` keeps its list as the signature
  so the re-exports are readable; a tuple target names each identifier;
- `Alias = dict[str, int]`, `Alias: TypeAlias = …`, `T = TypeVar("T")`,
  `type Alias = …` (3.12) are kind `type`, the aliased expression as the
  signature;
- a class-body `x: T [= v]` / `x = v` is `<Class>::x`, kind `field`, the
  annotation as its signature and `return_type` (the field
  for the one named type) so a reader knows the attribute's type; an
  `Enum` subclass's members keep their literal value as the signature
  (`= "b"`), which is what an agent asks for ("which member is 'b'").

A name a def or class of the same scope already takes is not repeated;
bindings inside a function body are locals, never symbols; the guards a
module wraps its bindings in (`try: … except ImportError: x = None`, an
`if sys.version_info` fork) are read, an `if __name__ == "__main__":`
block is not (its bindings are the script's, not the module's).
"""

from __future__ import annotations

from typing import Any

from server.code.domain.extract.py_receivers import type_name_of
from server.code.domain.extract.treesitter import Symbol, node_text

KIND_CONST = "const"
KIND_TYPE = "type"
KIND_FIELD = "field"

_MAX_SIG = 200
_MAX_ALL_SIG = 500
_MAX_MODULE_BINDINGS = 400
_MAX_CLASS_MEMBERS = 300

# the bases that make a class an enumeration, by bare name
_ENUM_BASES: frozenset[str] = frozenset({
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum",
})
# the heads of a subscript / call that make `X = <expr>` a type alias
_ALIAS_HEADS: frozenset[str] = frozenset({
    "dict", "list", "tuple", "set", "frozenset", "type", "Dict", "List", "Tuple", "Set",
    "FrozenSet", "Type", "Union", "Optional", "Callable", "Literal", "Mapping", "Sequence",
    "Iterable", "Iterator", "MutableMapping", "Awaitable", "Coroutine", "Annotated",
    "TypeGuard", "Final", "ClassVar",
})
_ALIAS_CALLS: frozenset[str] = frozenset({"TypeVar", "NewType", "ParamSpec", "TypeVarTuple"})
_ALIAS_ANNOTATIONS: frozenset[str] = frozenset({"TypeAlias", "typing.TypeAlias"})
# module-level statements whose bodies still hold the module's bindings
_MODULE_GUARDS: frozenset[str] = frozenset({
    "if_statement", "try_statement", "with_statement", "else_clause", "elif_clause",
    "except_clause", "finally_clause", "block",
})


def is_enum_class(bases: list[str]) -> bool:
    """`class Color(str, Enum)`, `class Perm(IntFlag)`: an enumeration by
    its base names (the tail of a dotted base: `enum.Enum`)."""
    return any(b.rsplit(".", 1)[-1] in _ENUM_BASES for b in bases)


def _assignment_of(stmt: Any) -> Any:
    """The `assignment` node an expression statement wraps, else None."""
    if stmt.type != "expression_statement" or not stmt.named_children:
        return None
    expr = stmt.named_children[0]
    return expr if expr.type == "assignment" else None


def _targets(left: Any, source: bytes) -> list[str]:
    """The identifiers an assignment target names: `x`, `a, b = …`, `(a, b)`."""
    if left.type == "identifier":
        return [node_text(left, source)]
    if left.type in {"pattern_list", "tuple_pattern"}:
        return [node_text(c, source) for c in left.named_children if c.type == "identifier"]
    return []


def _head(node: Any, source: bytes) -> str:
    """The bare head of a subscript or call: `typing.Dict[...]` → Dict."""
    value = node.child_by_field_name("value") if node.type == "subscript" else (
        node.child_by_field_name("function") if node.type == "call" else None
    )
    return node_text(value, source).rsplit(".", 1)[-1] if value is not None else ""


def _is_type_alias(annotation: Any, value: Any, source: bytes) -> bool:
    """`X: TypeAlias = …`, `X = dict[str, int]`, `X = int | None`, `T = TypeVar("T")`."""
    if annotation is not None and node_text(annotation, source).strip() in _ALIAS_ANNOTATIONS:
        return True
    if value is None:
        return False
    if value.type == "subscript":
        return _head(value, source) in _ALIAS_HEADS
    if value.type == "call":
        return _head(value, source) in _ALIAS_CALLS
    if value.type == "binary_operator":
        # `int | None`, `Foo | Bar`: a union of names / subscripts / None
        op = next((c for c in value.children if c.type == "|"), None)
        return op is not None and all(
            c.type in {"identifier", "attribute", "subscript", "none", "string", "binary_operator"}
            for c in value.named_children
        )
    return False


def _binding_signature(annotation: Any, value: Any, source: bytes, kind: str, name: str) -> str:
    if name == "__all__" and value is not None:
        return node_text(value, source)[:_MAX_ALL_SIG]
    if kind == KIND_TYPE and value is not None:
        return node_text(value, source)[:_MAX_SIG]
    if annotation is not None:
        return node_text(annotation, source)[:_MAX_SIG]
    return ""


def module_bindings(root: Any, source: bytes, mod: str, path: str, taken: set[str]) -> list[Symbol]:
    """The module-level bindings of a parsed file as `const` / `type`
    symbols, first binding of a name wins, names in `taken` (the module's
    defs and classes) skipped."""
    out: list[Symbol] = []
    seen: set[str] = set(taken)

    def visit(node: Any) -> None:
        for stmt in node.named_children:
            if len(out) >= _MAX_MODULE_BINDINGS:
                return
            if stmt.type == "type_alias_statement":
                left = stmt.child_by_field_name("left")
                ident = next((c for c in left.named_children if c.type == "identifier"), None) \
                    if left is not None else None
                right = stmt.child_by_field_name("right")
                if ident is not None:
                    _add(out, seen, node_text(ident, source), KIND_TYPE, stmt, mod, path,
                         node_text(right, source)[:_MAX_SIG] if right is not None else "", "")
                continue
            if stmt.type in _MODULE_GUARDS:
                if stmt.type == "if_statement" and "__name__" in node_text(
                    stmt.child_by_field_name("condition") or stmt, source
                ):
                    continue  # `if __name__ == "__main__":` — the script's, not the module's
                visit(stmt)
                continue
            asg = _assignment_of(stmt)
            if asg is None:
                continue
            left = asg.child_by_field_name("left")
            annotation = asg.child_by_field_name("type")
            value = asg.child_by_field_name("right")
            if left is None:
                continue
            # `a = b = 1`: the inner assignment is the value; both names bind
            while value is not None and value.type == "assignment":
                for nm in _targets(left, source):
                    _add(out, seen, nm, KIND_CONST, stmt, mod, path, "", "")
                left, value = value.child_by_field_name("left"), value.child_by_field_name("right")
                if left is None:
                    break
            if left is None:
                continue
            for nm in _targets(left, source):
                kind = KIND_TYPE if _is_type_alias(annotation, value, source) else KIND_CONST
                sig = _binding_signature(annotation, value, source, kind, nm)
                rt = ""
                if kind == KIND_CONST and annotation is not None:
                    rt = type_name_of(annotation, source)
                _add(out, seen, nm, kind, stmt, mod, path, sig, rt)

    visit(root)
    return out


def _add(
    out: list[Symbol], seen: set[str], name: str, kind: str, stmt: Any, scope: str, path: str,
    signature: str, return_type: str,
) -> None:
    if not name or name in seen:
        return
    seen.add(name)
    out.append(Symbol(
        qualified_name=f"{scope}::{name}", name=name, kind=kind, signature=signature, path=path,
        start_line=stmt.start_point[0] + 1, end_line=stmt.end_point[0] + 1, has_body=False,
        return_type=return_type,
    ))


def class_members(
    body: Any, source: bytes, class_qname: str, path: str, taken: set[str], *, enum: bool,
) -> list[Symbol]:
    """The attributes a class declares, as `field` symbols: in the body,
    annotated (`x: int = 0`, a dataclass / pydantic / NamedTuple / TypedDict
    field) or plain (`x = 0`, an `Enum` member with its value); in its
    methods, `self.x = …` / `self.x: T = …` (the instance attributes an
    `__init__` sets — the compiler tier names them as members, and the
    receiver typer reads the same statements for their types), at the first
    assignment's line. Names the class's defs take (`taken`) are skipped, and the names
    added join `taken`."""
    out: list[Symbol] = []
    seen = taken
    if body is None:
        return out
    for stmt in body.named_children:
        if len(out) >= _MAX_CLASS_MEMBERS:
            break
        asg = _assignment_of(stmt)
        if asg is None:
            continue
        left = asg.child_by_field_name("left")
        annotation = asg.child_by_field_name("type")
        value = asg.child_by_field_name("right")
        if left is None:
            continue
        for nm in _targets(left, source):
            if enum and annotation is None and value is not None:
                sig = ("= " + node_text(value, source))[:_MAX_SIG]
            else:
                sig = node_text(annotation, source)[:_MAX_SIG] if annotation is not None else ""
            rt = type_name_of(annotation, source) if annotation is not None else ""
            _add(out, seen, nm, KIND_FIELD, stmt, class_qname, path, sig, rt)
    for stmt in body.named_children:
        fn = stmt.child_by_field_name("definition") if stmt.type == "decorated_definition" else stmt
        if fn is not None and fn.type == "function_definition":
            _self_attributes(fn.child_by_field_name("body"), source, class_qname, path, out, seen)
    return out


def _self_attributes(
    node: Any, source: bytes, class_qname: str, path: str, out: list[Symbol], seen: set[str],
) -> None:
    """`self.<name> = …` / `self.<name>: T = …` anywhere in a method body
    (nested defs and classes excluded: their `self` is another's)."""
    if node is None:
        return
    for child in node.named_children:
        if len(out) >= _MAX_CLASS_MEMBERS:
            return
        if child.type in ("function_definition", "class_definition", "decorated_definition"):
            continue
        asg = child if child.type == "assignment" else _assignment_of(child)
        if asg is not None:
            left = asg.child_by_field_name("left")
            annotation = asg.child_by_field_name("type")
            sig = node_text(annotation, source)[:_MAX_SIG] if annotation is not None else ""
            rt = type_name_of(annotation, source) if annotation is not None else ""
            for attr in _self_targets(left, source):
                _add(out, seen, attr, KIND_FIELD, child, class_qname, path, sig, rt)
        _self_attributes(child, source, class_qname, path, out, seen)


def _self_targets(left: Any, source: bytes) -> list[str]:
    """The attribute names `self.x = …` / `self.a, self.b = …` assign."""
    if left is None:
        return []
    nodes = [left] if left.type == "attribute" else (
        list(left.named_children) if left.type in {"pattern_list", "tuple_pattern"} else []
    )
    out: list[str] = []
    for n in nodes:
        if n.type != "attribute":
            continue
        obj, attr = n.child_by_field_name("object"), n.child_by_field_name("attribute")
        if obj is not None and attr is not None and node_text(obj, source) in ("self", "cls"):
            out.append(node_text(attr, source))
    return out


def is_stub_body(body: Any) -> bool:
    """A def whose body is only `...` (after an optional docstring) is a
    declaration — a Protocol / abstract / `@overload` signature, a `.pyi`
    stub — not a unit with code of its own (`has_body=False`, as the
    TypeScript lane marks an abstract method signature)."""
    if body is None:
        return False
    stmts = [c for c in body.named_children if c.type != "comment"]
    if stmts and stmts[0].type == "expression_statement" and stmts[0].named_children \
            and stmts[0].named_children[0].type == "string":
        stmts = stmts[1:]
    return len(stmts) == 1 and stmts[0].type == "expression_statement" and bool(
        stmts[0].named_children
    ) and stmts[0].named_children[0].type == "ellipsis"


def fold_declarations(symbols: list[Symbol]) -> list[Symbol]:
    """A bodiless def beside its implementation — the `@overload`
    signatures above the real def, an `if TYPE_CHECKING:` declaration with
    the runtime def below — is the same function: the implementation's row
    alone stays (the same rule as for TypeScript overloads)."""
    implemented = {
        s.qualified_name for s in symbols if s.kind in ("function", "fixture") and s.has_body
    }
    return [
        s for s in symbols
        if not (s.kind in ("function", "fixture") and not s.has_body
                and s.qualified_name in implemented)
    ]


__all__ = [
    "KIND_CONST", "KIND_FIELD", "KIND_TYPE", "class_members", "fold_declarations",
    "is_enum_class", "is_stub_body", "module_bindings",
]
