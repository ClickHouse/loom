"""Receiver types for Go member calls.

`s.Get(ctx, k)` names a method the parser tier can bind only when `Get` is unique in
the repository — and interface method signatures make every implemented method name
non-unique by construction. The tree does say what `s` is, almost always: the method's
own receiver (`func (a *app) run`), a parameter (`s *st.Store`), a short declaration
from a composite literal (`s := &Store{}`), from `new(T)`, from a type assertion (`c :=
x.(*Client)`), or from a call whose return type is known (`s := st.NewStore("x")`, the
callee's `return_type`); a `var r st.Reader` declaration; a struct field behind the
receiver (`a.store.Has()` through `store *st.Store` on `app`). This module reads those
bindings once per file and answers "what is the receiver written at this line" — the
shape of `ts_receivers.ReceiverTyper` / `py_receivers.PyReceiverTyper`, so the call
ladder's rung 2b narrows the candidates of `Get` to `…::Store::Get`.

Types are answered as written, qualifier kept: `st.Store` for a type
imported under `st`, `Store` for one of the file's own package — the
resolver turns the alias into a package directory through the module index.

Pure over the parse; no I/O. Imported by `edges_treesitter` at call time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lang_go import (
    _bare_type_name,
    _qualifier_of,
    go_import_aliases,
    go_struct_fields,
)
from server.code.domain.extract.treesitter import node_text

_FUNC_NODES: frozenset[str] = frozenset(
    {
        "function_declaration",
        "method_declaration",
        "func_literal",
    }
)
_CALL_PREFIX = "call:"
_CALL_NODES: frozenset[str] = frozenset({"call_expression", "type_conversion_expression"})
_REF_PREFIX = "ref:"
_CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "slice_type",
        "array_type",
        "map_type",
        "channel_type",
    }
)
_MAX_REF_HOPS = 4  # `a := b; b := c; …` — deeper than this is a cycle or noise


@dataclass
class _Scope:
    start: int
    end: int
    # name -> "Type" | "alias.Type" | "call:<callee>"
    bindings: dict[str, str] = field(default_factory=dict)


def _written(type_node: Any, source: bytes) -> str:
    """`alias.Type` / `Type` for a type node, '' for a builtin or anonymous."""
    if type_node is None:
        return ""
    bare = _bare_type_name(type_node, source)
    if not bare:
        return ""
    alias = _qualifier_of(type_node, source)
    return f"{alias}.{bare}" if alias else bare


def _callee_of(call: Any, source: bytes) -> str:
    """The callee as the resolver keys it: `Conn`, `st.NewStore`, `a.store.Get`,
    `ColStr{}.Array` for `(&ColStr{}).Array()`, `Conn().Do` for `Conn(t).Do()`,
    `new(T).M`; '' for a callee the tree cannot name."""
    if call.type == "type_conversion_expression":
        t = call.child_by_field_name("type")
        inner = t.child_by_field_name("type") if t is not None else None
        return node_text(inner, source) if inner is not None else ""
    fn = call.child_by_field_name("function")
    if fn is None:
        return ""
    if fn.type == "index_expression":  # `NewArray[T](…)`
        fn = fn.child_by_field_name("operand") or fn
    if fn.type == "identifier":
        return node_text(fn, source)
    if fn.type == "selector_expression":
        operand, field = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
        if operand is None or field is None:
            return ""
        head = go_value_spelling(operand, source)
        return f"{head}.{node_text(field, source)}" if head else ""
    if fn.type == "generic_function":
        inner = fn.child_by_field_name("function")
        return node_text(inner, source) if inner is not None else ""
    return ""


def go_value_spelling(node: Any, source: bytes) -> str:
    """How the typer names a value the tree can type: an identifier chain (`a.store`),
    an index (`c[]`), a literal or assertion (`T{}`, `st.T{}`), `new(T)`, or a call
    (`Conn()`, `table.Block()`) whose return the callee table answers; '' otherwise."""
    n = node
    if n.type == "parenthesized_expression":
        n = next(iter(n.named_children), n)
    if n.type == "unary_expression":
        n = n.child_by_field_name("operand") or n
    if n.type in ("composite_literal", "type_assertion_expression"):
        written = _written(n.child_by_field_name("type"), source)
        return f"{written}{{}}" if written else ""
    if n.type in ("call_expression", "type_conversion_expression"):
        callee = _callee_of(n, source)
        if callee == "new":
            args = n.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            inner = node_text(first, source).replace(" ", "") if first is not None else ""
            return f"new({inner})" if inner else ""
        return f"{callee}()" if callee else ""
    return go_receiver_chain(n, source)


def _value_binding(value: Any, source: bytes) -> str:
    """What a value expression types its target as: a composite literal (`Store{}`,
    `&st.Store{}`, `Pair[K, V]{}`), `new(T)`, a type assertion `x.(*T)`, `call:<callee>`
    for a call the return table may answer, or `ref:<name>` for a value that is another
    binding as written — `y := t`, `first := t.items[0]`, `z := *t`, `w := <-ch`, `it :=
    xs[i]` — whose type is the referenced binding's at the line (the bare type of a
    slice, map or channel is its element's, so an index or a range over a typed
    container types the element)."""
    v = value
    indexed = ""  # `xs[i]`: the element — `[]` asks type_of for it
    while v.type in (
        "unary_expression",
        "parenthesized_expression",
        "index_expression",
        "slice_expression",
    ):  # &T{...}, *p, <-ch, (x), xs[i], xs[i:j]
        if v.type == "index_expression":
            indexed = "[]"
        inner = v.child_by_field_name("operand")
        if inner is None:
            inner = next(iter(v.named_children), None)
        if inner is None:
            return ""
        v = inner
    if v.type == "identifier":
        return _REF_PREFIX + node_text(v, source) + indexed
    if v.type == "selector_expression":
        text = node_text(v, source)
        if not all(p.isidentifier() for p in text.split(".")):
            return ""
        return _REF_PREFIX + text + indexed
    if v.type == "composite_literal":
        return _written(v.child_by_field_name("type"), source)
    if v.type == "type_assertion_expression":
        return _written(v.child_by_field_name("type"), source)
    if v.type == "type_conversion_expression":
        return _written(v.child_by_field_name("type"), source)
    if v.type == "call_expression":
        callee = _callee_of(v, source)
        if callee == "new":
            args = v.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            return _written(first, source)
        if callee in ("make", "append", "len", "cap", "string", "errors.New", "fmt.Errorf"):
            return ""
        return _CALL_PREFIX + callee if callee else ""
    return ""


def _spec_nodes(decl: Any) -> list[Any]:
    out: list[Any] = []
    for c in decl.named_children:
        if c.type in ("const_spec", "var_spec"):
            out.append(c)
        elif c.type in ("var_spec_list", "const_spec_list"):
            out.extend(g for g in c.named_children if g.type in ("const_spec", "var_spec"))
    return out


def _param_bindings(params: Any, source: bytes, into: dict[str, str]) -> None:
    if params is None:
        return
    for p in params.named_children:
        if p.type not in ("parameter_declaration", "variadic_parameter_declaration"):
            continue
        tname = _written(p.child_by_field_name("type"), source)
        if not tname:
            continue
        for c in p.named_children:
            if c.type == "identifier":
                into[node_text(c, source)] = tname


def _scoped(node: Any, name: str, binding: str, scopes: list[_Scope]) -> None:
    """A binding that holds over one node's lines only: a type switch's alias in each
    single-type case, a range loop's element."""
    if name and binding:
        scopes.append(_Scope(node.start_point[0] + 1, node.end_point[0] + 1, {name: binding}))


def _type_switch_bindings(stmt: Any, source: bytes, scopes: list[_Scope]) -> None:
    """`switch v := x.(type) { case *T: … }`: `v` is a `T` inside that case;
    a multi-type case (`case A, *B:`) leaves `v` the switched value."""
    alias = stmt.child_by_field_name("alias")
    names = [c for c in alias.named_children if c.type == "identifier"] if alias is not None else []
    if len(names) != 1:
        return
    for case in stmt.named_children:
        if case.type != "type_case":
            continue
        types = case.children_by_field_name("type")
        if len(types) == 1:
            _scoped(case, node_text(names[0], source), _written(types[0], source), scopes)


def _range_bindings(stmt: Any, source: bytes, scopes: list[_Scope]) -> None:
    """`for _, it := range xs` / `for k, v := range m`: the second name is
    the element (the container binding's bare type IS its element's); a
    single name is an index over a slice or an element over a channel, which
    the tree cannot tell apart — left untyped."""
    clause = next((c for c in stmt.named_children if c.type == "range_clause"), None)
    if clause is None:
        return
    left, right = clause.child_by_field_name("left"), clause.child_by_field_name("right")
    if left is None or right is None:
        return
    names = [c for c in left.named_children if c.type == "identifier"]
    if len(names) == 2:
        binding = _value_binding(right, source)
        if binding.startswith(_REF_PREFIX) and not binding.endswith("[]"):
            binding += "[]"  # the element of the ranged container
        _scoped(stmt, node_text(names[1], source), binding, scopes)


def _body_bindings(node: Any, source: bytes, into: dict[str, str], scopes: list[_Scope]) -> None:
    """Declarations inside a body, without entering nested func literals
    (their scopes are their own)."""
    for child in node.named_children:
        if child.type == "func_literal":
            continue
        if child.type == "type_switch_statement":
            _type_switch_bindings(child, source, scopes)
        elif child.type == "for_statement":
            _range_bindings(child, source, scopes)
        if child.type == "short_var_declaration":
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if left is not None and right is not None:
                names = [c for c in left.named_children if c.type == "identifier"]
                values = list(right.named_children)
                if len(names) == len(values):
                    for n, v in zip(names, values, strict=True):
                        b = _value_binding(v, source)
                        if b:
                            into[node_text(n, source)] = b
                elif names and len(values) == 1:
                    # `v, err := f()` / `c, ok := x.(*T)`: the first name takes the value
                    b = _value_binding(values[0], source)
                    if b:
                        into[node_text(names[0], source)] = b
        elif child.type in ("var_declaration", "const_declaration"):
            # a typed const/var, and the untyped consts that follow one in a
            # group (`KindA Kind = iota; KindB` — KindB is a Kind too)
            group_type = ""
            for spec in _spec_nodes(child):
                explicit = _written(spec.child_by_field_name("type"), source)
                if spec.type == "const_spec":
                    if explicit:
                        group_type = explicit
                    elif spec.child_by_field_name("value") is not None:
                        group_type = ""  # a new untyped value starts a new implicit type
                    tname = explicit or group_type
                else:
                    tname = explicit
                if not tname:
                    value = spec.child_by_field_name("value")
                    first = (
                        value.named_children[0]
                        if value is not None and value.named_children
                        else None
                    )
                    tname = _value_binding(first, source) if first is not None else ""
                if tname:
                    for c in spec.named_children:
                        if c.type == "identifier":
                            into[node_text(c, source)] = tname
        elif child.type == "assignment_statement":
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if (
                left is not None
                and right is not None
                and len(left.named_children) == len(right.named_children)
            ):
                for n, v in zip(left.named_children, right.named_children, strict=True):
                    if n.type == "identifier":
                        b = _value_binding(v, source)
                        if b:
                            into.setdefault(node_text(n, source), b)
        _body_bindings(child, source, into, scopes)


class GoReceiverTyper:
    """Answers `type_of(line, receiver)` for one parsed Go file."""

    def __init__(self, pf: Any, callee_returns: dict[str, str] | None = None) -> None:
        self._scopes: list[_Scope] = []
        self._chains: set[str] = set()
        self._module = _Scope(1, 1 << 30)
        self._returns: dict[str, str] = dict(callee_returns or {})
        self._fields: dict[str, dict[str, str]] = {}  # struct name -> field -> written type
        # (type written, field) -> type written, answered from the repository's `field`
        # rows by the resolver; '' when unknown
        self.field_lookup: Any = None
        self._same_file: dict[str, str] = {
            s.name: s.return_type
            for s in pf.symbols
            if s.kind == "function" and s.return_type and "::" not in s.name
        }
        # a same-file callee's return type needs no read; the resolver's `bind_returns`
        # adds the repository's
        for k, v in self._same_file.items():
            self._returns.setdefault(k, v)
        # the file's own type names — `Kind(3)` is a conversion to Kind, not a call,
        # when Kind is a type declared here
        self._types: set[str] = set()
        # Named container types → their element as written (`type ColDate32 []Date32`),
        # this file's here, the repository's rows through `type_elems` (set by the
        # resolver from the lane context)
        self._elems: dict[str, str] = {}
        self.type_elems: dict[str, str] = {}
        self._receiver_calls: set[str] = set()
        self._import_aliases: set[str] = (
            set(go_import_aliases(pf)) if pf.tree is not None else set()
        )
        if pf.tree is not None:
            self._collect(pf.tree.root_node, pf.source)
            _body_bindings(pf.tree.root_node, pf.source, self._module.bindings, self._scopes)
        self._scopes.sort(key=lambda s: s.end - s.start)

    def _collect(self, node: Any, source: bytes) -> None:
        for child in node.named_children:
            if child.type == "call_expression":
                # `Conn(t).Do()` — the receiver is a call whose return types the member
                # call; its callee joins the read
                fn = child.child_by_field_name("function")
                operand = None
                if fn is not None and fn.type == "selector_expression":
                    operand = fn.child_by_field_name("operand")
                if operand is not None and operand.type in _CALL_NODES:
                    inner = _callee_of(operand, source)
                    if inner and inner != "new":
                        self._receiver_calls.add(inner)
            if child.type == "selector_expression":
                operand = child.child_by_field_name("operand")
                if operand is not None and operand.type == "selector_expression":
                    text = node_text(operand, source)
                    if all(p.isidentifier() for p in text.split(".")):
                        self._chains.add(text)
            if child.type == "type_declaration":
                for spec in child.named_children:
                    if spec.type != "type_spec":
                        continue
                    name, typ = spec.child_by_field_name("name"), spec.child_by_field_name("type")
                    if name is not None:
                        self._types.add(node_text(name, source))
                    if name is not None and typ is not None and typ.type in _CONTAINER_TYPES:
                        elem = _written(typ, source)
                        if elem:
                            self._elems[node_text(name, source)] = elem
                    if name is not None and typ is not None and typ.type == "struct_type":
                        self._fields[node_text(name, source)] = {
                            f: (f"{alias}.{t}" if alias else t)
                            for f, (t, alias) in go_struct_fields(typ, source).items()
                            if t
                        }
                continue
            if child.type in _FUNC_NODES:
                scope = _Scope(child.start_point[0] + 1, child.end_point[0] + 1)
                if child.type == "method_declaration":
                    _param_bindings(child.child_by_field_name("receiver"), source, scope.bindings)
                _param_bindings(child.child_by_field_name("parameters"), source, scope.bindings)
                body = child.child_by_field_name("body")
                self._scopes.append(scope)
                if body is not None:
                    _body_bindings(body, source, scope.bindings, self._scopes)
            self._collect(child, source)

    def same_file_returns(self) -> dict[str, str]:
        return dict(self._same_file)

    def type_of(self, line: int, receiver: str, _hops: int = 0) -> str:
        """The type of `receiver` (`s`, `a.store`, `a.store.inner`) as written
        at `line` — `Type` or `alias.Type` — '' when the tree does not say."""
        key = receiver.strip()
        if not key or _hops > _MAX_REF_HOPS:
            return ""
        if key.endswith("{}"):
            # `T{}`: a composite literal or an assertion
            inner = key[:-2]
            return inner if all(p.isidentifier() for p in inner.split(".")) else ""
        if key.endswith("()"):
            # `Conn()` / `table.Block()`: the value a call returns
            return self._resolve(_CALL_PREFIX + key[:-2], line, _hops + 1)
        if key.startswith("new(") and key.endswith(")"):
            # `new(proto.ColStr).LowCardinality()`: the receiver is the type
            inner = key[4:-1].strip().lstrip("*")
            return inner if all(p.isidentifier() for p in inner.split(".")) else ""
        parts = key.split(".")
        # `c[].Time()` / `t.items[]`: a part written with `[]` is indexed — its type is
        # the container's element
        names = [p[:-2] if p.endswith("[]") else p for p in parts]
        indexed = [p.endswith("[]") for p in parts]
        if not all(n.isidentifier() for n in names):
            return ""
        head = self._lookup(line, names[0], _hops)
        if not head and len(parts) > 1:
            # `pkg.Var.Method()`: a package-level var of an imported package
            # is beyond the tree; a local package var is in the module scope
            return ""
        current = self._element_of(head) if indexed[0] and head else head
        for fld, idx in zip(names[1:], indexed[1:], strict=True):
            if not current:
                return ""
            bare = current.rsplit(".", 1)[-1]
            fields = self._fields.get(bare)
            nxt = fields.get(fld, "") if fields else ""
            if not nxt and self.field_lookup is not None:
                nxt = self.field_lookup(current, fld) or ""
            current = self._element_of(nxt) if idx and nxt else nxt
        return current

    def _element_of(self, tname: str) -> str:
        """The element type of a named container (`ColDate32` → `Date32`), from this
        file's declarations or the repository's `type` rows; a type that is not a named
        container is its own element — a binding already carries a slice's or map's
        element."""
        alias, _, bare = tname.rpartition(".")
        elem = (self._elems.get(bare, "") if not alias else "") or self.type_elems.get(bare, "")
        if not elem:
            return tname
        if alias and "." not in elem and elem[:1].isupper():
            return f"{alias}.{elem}"  # the element is spelled in the type's own package
        return elem

    def bind_values(self, values: dict[str, str]) -> None:
        """Package-level consts and vars declared in OTHER files of this package, typed
        from their rows (`const KindA Kind`); this file's own declarations win."""
        for name, tname in values.items():
            if tname:
                self._module.bindings.setdefault(name, tname)

    def chain_fields(self) -> set[tuple[str, str]]:
        """Every (receiver head, field) a selector chain in this file walks — the field
        rows the ingest reads for `field_lookup`."""
        out: set[tuple[str, str]] = set()
        for name in self._chains:
            parts = name.split(".")
            for i in range(1, len(parts)):
                out.add((parts[0], parts[i]))
        return out

    def _lookup(self, line: int, name: str, hops: int = 0) -> str:
        for scope in self._scopes:  # innermost first
            if scope.start <= line <= scope.end and name in scope.bindings:
                return self._resolve(scope.bindings[name], line, hops)
        return self._resolve(self._module.bindings.get(name, ""), line, hops)

    def _resolve(self, binding: str, line: int = 0, hops: int = 0) -> str:
        if binding.startswith(_REF_PREFIX):
            # `y := t` / `it := xs[i]`: the referenced binding's type at this line, a
            # bounded number of hops away
            return self.type_of(line, binding[len(_REF_PREFIX) :], hops + 1)
        if not binding.startswith(_CALL_PREFIX):
            return binding
        callee = binding[len(_CALL_PREFIX) :]
        if "." not in callee and callee in self._types and callee not in self._returns:
            return callee  # `Kind(3)`: a conversion to this file's own type
        # a call written `x.F()` types its result only when THAT callee was bound (the
        # return table keys it as written); a bare `F()` may also be this file's own F.
        # Never the return of some repository namesake: `rows, _ := db.Query()` on a
        # *sql.DB must not become our Rows.
        found = self._returns.get(callee, "")
        if not found and "." not in callee:
            found = self._same_file.get(callee, "")
        if found:
            # a return type declared in another package keeps that package's qualifier:
            # `st.NewStore` returns st's Store; a method's return
            # (`new(proto.Col).WithPrecision()`, `a.store.Get()`) is spelled in the
            # RECEIVER TYPE's package, never the receiver text's
            if "." in callee and "." not in found and found[:1].isupper():
                head = callee.rsplit(".", 1)[0]
                if head in self._import_aliases:
                    return f"{head}.{found}"
                rtype = self.type_of(line, head, hops + 1)
                ralias = rtype.rpartition(".")[0]
                return f"{ralias}.{found}" if ralias else found
            return found
        return ""

    def type_names(self) -> set[str]:
        """Every bare type name a binding in this file is written as (`Store` of `s
        *st.Store`, `box` of `&box{}`) — the struct rows the ingest reads so a method
        the type does not define can be looked up on the types it embeds in ANOTHER file
        (cross-file promotion)."""
        out: set[str] = set()
        for scope in (*self._scopes, self._module):
            for v in scope.bindings.values():
                if not v.startswith(_REF_PREFIX):
                    out.add(self._resolve(v).rsplit(".", 1)[-1])
        for fields in self._fields.values():
            out.update(t.rsplit(".", 1)[-1] for t in fields.values())
        return {t for t in out if t.isidentifier()}

    def callees(self) -> set[str]:
        """Every callee some binding's type waits on."""
        out: set[str] = set(self._receiver_calls)
        for scope in (*self._scopes, self._module):
            for v in scope.bindings.values():
                if v.startswith(_CALL_PREFIX):
                    out.add(v[len(_CALL_PREFIX) :])
        return out

    def bind_returns(self, returns: dict[str, str]) -> None:
        self._returns.update({k: v for k, v in returns.items() if v})


def go_receiver_chain(node: Any, source: bytes) -> str:
    """A member call's receiver as the typer reads it: `c`, `a.store.inner`, `c[]` for
    `c[i]` (an index is the container's element); '' for anything the tree cannot name
    (a call, a literal, an assertion)."""
    if node.type == "identifier":
        return node_text(node, source)
    if node.type == "parenthesized_expression":
        inner = next(iter(node.named_children), None)
        return go_receiver_chain(inner, source) if inner is not None else ""
    operand = node.child_by_field_name("operand")
    if operand is None:
        return ""
    if node.type == "selector_expression":
        field = node.child_by_field_name("field")
        head = go_receiver_chain(operand, source)
        return f"{head}.{node_text(field, source)}" if head and field is not None else ""
    if node.type == "index_expression":
        head = go_receiver_chain(operand, source)
        return f"{head}[]" if head else ""
    return ""


def unbound_receiver_heads(pf: Any, typer: GoReceiverTyper) -> set[str]:
    """The heads of member-call receivers this file's tree does not type — `ColumnTypeInt8` of
    `ColumnTypeInt8.Array()` — that may be package-level consts or vars declared in another file:
    the ingest reads their rows. Import aliases are packages, not values."""
    out: set[str] = set()
    if pf.tree is None:
        return out
    aliases = set(go_import_aliases(pf))

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None and fn.type == "selector_expression":
                    operand = fn.child_by_field_name("operand")
                    chain = go_receiver_chain(operand, pf.source) if operand is not None else ""
                    head = chain.split(".", 1)[0].removesuffix("[]")
                    if (
                        head.isidentifier()
                        and head not in aliases
                        and not typer.type_of(child.start_point[0] + 1, head)
                    ):
                        out.add(head)
            walk(child)

    walk(pf.tree.root_node)
    return out


__all__ = ["GoReceiverTyper", "go_receiver_chain", "go_value_spelling", "unbound_receiver_heads"]
