"""Receiver types for TypeScript / JavaScript member calls.

Without a receiver type, `client.query(sql)` names a method the parser tier
can only bind when `query` is unique in the repository (rung 3) or a method
of the caller's own class (rung 2). The compiler tier resolves it exactly,
but only where a checkout is installed.

The tree does say what `client` is, often: a parameter annotation
(`client: ClickHouseClient`), a typed declaration (`const r: Reply = …`), a
construction (`const reply = new Reply(…)`), an assertion (`x as Reply`), a
class field (`private client: ClickHouseClient`, or a constructor parameter
property) behind `this.client`, or `this` itself inside a class. This module
reads those bindings once per file and answers "what is the receiver written
at this line" so the call ladder can narrow the candidates of `query` to
`…::ClickHouseClient::query` — the same candidate set, one more fact.

Pure over the parse; no I/O. Kept apart from lang_typescript.py because
edges_treesitter imports this at call time and lang_typescript imports
edges_treesitter at module load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.treesitter import ParsedFile, node_text

_FUNCTION_NODES: frozenset[str] = frozenset({
    "function_declaration", "function_expression", "function", "arrow_function",
    "method_definition", "generator_function_declaration", "generator_function",
})
_PARAM_NODES: frozenset[str] = frozenset({"required_parameter", "optional_parameter"})
_SKIP_TYPES: frozenset[str] = frozenset({
    "undefined", "null", "void", "never", "unknown", "any", "string", "number", "boolean",
    "object", "symbol", "bigint", "this",
})
_NAME_RE = re.compile(r"^[A-Za-z_$][\w$]*")
# A binding whose type is what a callee returns — resolved
# once the callee's return type is known (same file, or the repository's row)
_CALL_PREFIX = "call:"
_AWAIT_PREFIX = "await:"  # `await f()`: the callee's Promise<T> is T here
_PROMISE_RE = re.compile(r"^(?:Promise|Awaited)<(.+)>$")


def _is_awaited(value: Any) -> bool:
    node = value
    while node is not None and node.type == "parenthesized_expression":
        inner = node.named_children
        node = inner[0] if inner else None
    return node is not None and node.type == "await_expression"
_JSDOC_PARAM_RE = re.compile(r"@(?:param|arg|argument)\s*\{([^}]+)\}\s*\[?([A-Za-z_$][\w$]*)")
_JSDOC_TYPE_RE = re.compile(r"@type\s*\{([^}]+)\}")


def _jsdoc_name(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^Promise<(.+)>$", r"\1", text)
    text = text.rstrip("=").strip()  # Closure's optional marker
    return text if re.fullmatch(r"[A-Za-z_$][\w$]*", text) and text not in _SKIP_TYPES else ""


def _doc_above(node: Any, source: bytes) -> str:
    """The JSDoc block right above a node (its previous sibling comment)."""
    prev = node.prev_sibling
    while (
        prev is not None and prev.type == "comment"
        and prev.end_point[0] + 1 >= node.start_point[0] - 1
    ):
        text = node_text(prev, source)
        if text.startswith("/**"):
            return text
        prev = prev.prev_sibling
    return ""


def _called(value: Any, source: bytes) -> str:
    """`f(…)` / `await f(…)` / `ns.f(…)` → the callee as written, '' else."""
    node = value
    while node is not None and node.type in {"await_expression", "parenthesized_expression"}:
        inner = node.named_children
        node = inner[0] if inner else None
    if node is None or node.type != "call_expression":
        return ""
    fn = node.child_by_field_name("function")
    if fn is None or fn.type not in {"identifier", "member_expression"}:
        return ""
    text = node_text(fn, source)
    return text if all(p.isidentifier() for p in text.split(".")) else ""


@dataclass
class _Scope:
    start: int  # 1-indexed, inclusive
    end: int
    bindings: dict[str, str] = field(default_factory=dict)  # receiver text -> type name


def type_name_of(annotation: Any, source: bytes) -> str:
    """The one named type an annotation / type node denotes, '' when it is a
    primitive, a union of several named types, an array, a literal or a
    function type. `Foo<T>` → Foo, `Foo | undefined` → Foo, `ns.Foo` → Foo,
    `Foo[]` → '' (the receiver is the element, not the array)."""
    node = annotation
    if node is None:
        return ""
    if node.type == "type_annotation":
        inner = [c for c in node.named_children]
        if not inner:
            return ""
        node = inner[0]
    t = node.type
    if t == "union_type":
        names = {type_name_of(c, source) for c in node.named_children}
        names.discard("")
        return names.pop() if len(names) == 1 else ""
    if t == "generic_type":
        name = node.child_by_field_name("name")
        return type_name_of(name, source) if name is not None else ""
    if t == "nested_type_identifier":
        name = node.child_by_field_name("name")
        return node_text(name, source) if name is not None else ""
    if t == "type_identifier":
        text = node_text(node, source)
        return "" if text in _SKIP_TYPES else text
    if t == "parenthesized_type" and node.named_children:
        return type_name_of(node.named_children[0], source)
    return ""


def _constructed(value: Any, source: bytes) -> str:
    """`new Foo(…)` / `new ns.Foo(…)` / `await new Foo(…)` / `x as Foo` /
    `<Foo>x` → Foo."""
    node = value
    while node is not None and node.type in {"await_expression", "parenthesized_expression"}:
        inner = node.named_children
        node = inner[0] if inner else None
    if node is not None and node.type == "binary_expression":
        # `opts.schemas || new Schemas()` / `x ?? new T()`: the construction
        # is the fallback and names the type either way
        for c in node.named_children:
            found = _constructed(c, source)
            if found:
                return found
        return ""
    if node is None:
        return ""
    if node.type == "new_expression":
        ctor = node.child_by_field_name("constructor")
        if ctor is None:
            return ""
        if ctor.type == "member_expression":
            prop = ctor.child_by_field_name("property")
            return node_text(prop, source) if prop is not None else ""
        return node_text(ctor, source) if ctor.type == "identifier" else ""
    if node.type in {"as_expression", "satisfies_expression", "type_assertion"}:
        for c in node.named_children:
            if c.type.endswith("type") or c.type in {"type_identifier", "generic_type"}:
                return type_name_of(c, source)
        return ""
    return ""


def _param_bindings(params: Any, source: bytes, into: dict[str, str], doc: str = "") -> None:
    if params is None:
        return
    # JavaScript states its parameter types in JSDoc
    documented = {m.group(2): _jsdoc_name(m.group(1)) for m in _JSDOC_PARAM_RE.finditer(doc)}
    for p in params.named_children:
        if p.type not in _PARAM_NODES and p.type != "identifier":
            continue
        pattern = p if p.type == "identifier" else p.child_by_field_name("pattern")
        ann = p.child_by_field_name("type") if p.type in _PARAM_NODES else None
        if pattern is None or pattern.type != "identifier":
            continue
        pname = node_text(pattern, source)
        name = type_name_of(ann, source) if ann is not None else ""
        if not name:
            value = p.child_by_field_name("value") if p.type in _PARAM_NODES else None
            name = _constructed(value, source) if value is not None else ""
        if not name:
            name = documented.get(pname, "")
        if name:
            into[pname] = name


def _declarator_bindings(node: Any, source: bytes, into: dict[str, str]) -> None:
    """`const x: T = …` / `const x = new T()` / `let x = y as T` inside a
    function body — walked without entering nested functions (their scopes
    are their own)."""
    for child in node.named_children:
        if child.type in _FUNCTION_NODES or child.type == "class_declaration":
            continue
        if child.type == "variable_declarator":
            name = child.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                ann = child.child_by_field_name("type")
                tname = type_name_of(ann, source) if ann is not None else ""
                value = child.child_by_field_name("value")
                if not tname and value is not None:
                    tname = _constructed(value, source)
                if not tname and child.parent is not None:
                    # `/** @type {Reply} */ const r = …`
                    m = _JSDOC_TYPE_RE.search(_doc_above(child.parent, source))
                    tname = _jsdoc_name(m.group(1)) if m else ""
                if not tname and value is not None:
                    callee = _called(value, source)
                    if callee:
                        prefix = _AWAIT_PREFIX if _is_awaited(value) else _CALL_PREFIX
                        tname = prefix + callee  # typed once the callee's return is known
                if tname:
                    into[node_text(name, source)] = tname
        _declarator_bindings(child, source, into)


def _class_bindings(class_node: Any, source: bytes) -> dict[str, str]:
    """`this.<field>` → type, from field declarations and constructor
    parameter properties; `this` → the class's own name."""
    out: dict[str, str] = {}
    name = class_node.child_by_field_name("name")
    if name is not None:
        out["this"] = node_text(name, source)
    body = class_node.child_by_field_name("body")
    if body is None:
        return out
    for member in body.named_children:
        if member.type == "public_field_definition":
            fname = member.child_by_field_name("name")
            ann = member.child_by_field_name("type")
            if fname is None:
                continue
            tname = type_name_of(ann, source) if ann is not None else ""
            if not tname:
                value = member.child_by_field_name("value")
                tname = _constructed(value, source) if value is not None else ""
            if tname:
                out["this." + node_text(fname, source)] = tname
        elif member.type == "method_definition":
            mname = member.child_by_field_name("name")
            if mname is not None and node_text(mname, source) == "constructor":
                params = member.child_by_field_name("parameters")
                for p in (params.named_children if params is not None else []):
                    if p.type not in _PARAM_NODES:
                        continue
                    has_modifier = any(
                        c.type == "accessibility_modifier" or node_text(c, source) in {"readonly"}
                        for c in p.children
                    )
                    pattern = p.child_by_field_name("pattern")
                    ann = p.child_by_field_name("type")
                    typed_prop = (
                        pattern is not None and pattern.type == "identifier" and ann is not None
                    )
                    if has_modifier and typed_prop:
                        tname = type_name_of(ann, source)
                        if tname:
                            out["this." + node_text(pattern, source)] = tname
                # `this.x = new T()` in the constructor body
                cbody = member.child_by_field_name("body")
                if cbody is not None:
                    _this_assignments(cbody, source, out)
    return out


def _this_assignments(node: Any, source: bytes, into: dict[str, str]) -> None:
    for child in node.named_children:
        if child.type in _FUNCTION_NODES:
            continue
        if child.type == "assignment_expression":
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if left is not None and right is not None and left.type == "member_expression":
                text = node_text(left, source)
                if text.startswith("this.") and text.count(".") == 1:
                    tname = _constructed(right, source)
                    if not tname:
                        callee = _called(right, source)
                        tname = (
                            (_AWAIT_PREFIX if _is_awaited(right) else _CALL_PREFIX) + callee
                            if callee else ""
                        )
                    if tname:
                        into.setdefault(text, tname)
        _this_assignments(child, source, into)


class ReceiverTyper:
    """Answers `type_of(line, receiver)` for one parsed file."""

    def __init__(self, pf: ParsedFile, callee_returns: dict[str, str] | None = None) -> None:
        self._scopes: list[_Scope] = []
        self._module = _Scope(1, 1 << 30)
        # callee as written -> the type it returns
        self._returns: dict[str, str] = dict(callee_returns or {})
        if pf.tree is not None:
            self._collect(pf.tree.root_node, pf.source, class_bindings=None)
            _declarator_bindings(pf.tree.root_node, pf.source, self._module.bindings)
        # innermost first: sort by span length ascending
        self._scopes.sort(key=lambda s: s.end - s.start)

    def _collect(self, node: Any, source: bytes, class_bindings: dict[str, str] | None) -> None:
        for child in node.named_children:
            if child.type in {"class_declaration", "class", "abstract_class_declaration"}:
                self._collect(child, source, _class_bindings(child, source))
                continue
            if child.type in _FUNCTION_NODES:
                scope = _Scope(child.start_point[0] + 1, child.end_point[0] + 1)
                if class_bindings:
                    scope.bindings.update(class_bindings)
                doc = _doc_above(child, source) or (
                    _doc_above(child.parent, source) if child.parent is not None else ""
                )
                params = child.child_by_field_name("parameters")
                _param_bindings(params, source, scope.bindings, doc)
                body = child.child_by_field_name("body")
                if body is not None:
                    _declarator_bindings(body, source, scope.bindings)
                self._scopes.append(scope)
            self._collect(child, source, class_bindings)

    def type_of(self, line: int, receiver: str) -> str:
        """The named type of `receiver` (`client`, `this.client`, `this`) as
        written at `line`, '' when the tree does not say."""
        key = receiver.strip()
        if not key or key.startswith(("(", "[", "\"", "'", "`")):
            return ""
        if not _NAME_RE.match(key) and not key.startswith("this"):
            return ""
        for scope in self._scopes:  # innermost first
            if scope.start <= line <= scope.end and key in scope.bindings:
                return self._resolve(scope.bindings[key])
        return self._resolve(self._module.bindings.get(key, ""))

    def _resolve(self, binding: str) -> str:
        awaited = binding.startswith(_AWAIT_PREFIX)
        if not awaited and not binding.startswith(_CALL_PREFIX):
            return binding
        callee = binding.split(":", 1)[1]
        got = self._returns.get(callee, "") or self._returns.get(callee.rsplit(".", 1)[-1], "")
        if awaited:
            # `const a = await getFoo()` with `getFoo(): Promise<Foo>` is a Foo
            got = _PROMISE_RE.sub(r"\1", got.strip())
        return got

    def callees(self) -> set[str]:
        """Every callee (as written) some binding's type waits on — what the
        resolver looks up before the ladder runs."""
        out: set[str] = set()
        for scope in (*self._scopes, self._module):
            for v in scope.bindings.values():
                if v.startswith((_CALL_PREFIX, _AWAIT_PREFIX)):
                    out.add(v.split(":", 1)[1])
        return out

    def bind_returns(self, returns: dict[str, str]) -> None:
        self._returns.update({k: v for k, v in returns.items() if v})
