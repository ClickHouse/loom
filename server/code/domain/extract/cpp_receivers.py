"""The named type of a C++ member call's receiver, read off
the tree — a parameter (`const IVolume & volume`), a local (`MergeTreeData
data;`, `auto r = std::make_shared<Reader>(…)`, `auto q = Reader(…)`,
`auto s = new Sink()`), a range-for variable with a written type, or a
member of the enclosing class declared in this file (`IVolume * volume;`,
`this->volume`). Pointers, references, `const` and the smart-pointer
wrappers (`shared_ptr<T>`, `unique_ptr<T>`, `intrusive_ptr<T>`) fold to
`T`; a same-file `using X = T;` / `typedef T X;` folds `X` to `T`; a
qualified `DB::IVolume` folds to its last segment (the resolver matches
`…::IVolume::getDisk` by suffix). A standard-library container or a type
the file does not name yields '' — the resolver then guesses repo-wide at
0.6, or, for a `std::` receiver, abstains.

Mirrors py_receivers.PyReceiverTyper's `type_of(line, receiver)`; the
header's class members join through `bind_header` (the `HeaderDecls` the
ingest resolved)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.treesitter import node_text

_SMART = frozenset({
    "shared_ptr", "unique_ptr", "weak_ptr", "intrusive_ptr", "boost::intrusive_ptr",
    "COWPtr", "COW", "make_shared", "make_unique", "make_intrusive",
})
_STD_CONTAINERS = frozenset({
    "vector", "map", "unordered_map", "set", "unordered_set", "string", "string_view",
    "optional", "variant", "array", "deque", "list", "pair", "tuple", "span", "function",
    "atomic", "mutex", "queue", "stack",
})
_FUNCTION_NODES = frozenset({"function_definition", "lambda_expression"})
_DECLARATOR_WRAPPERS = frozenset({
    "pointer_declarator", "reference_declarator", "init_declarator", "parenthesized_declarator",
})


def _last_segment(name: str) -> str:
    return name.rsplit("::", 1)[-1].strip()


def type_name_of(node: Any, source: bytes) -> str:
    """The class a type node names, folded as the module docstring says;
    '' when the node is `auto`, a standard container or unreadable."""
    if node is None:
        return ""
    t = node.type
    if t in ("placeholder_type_specifier", "primitive_type", "auto"):
        return ""
    if t == "type_identifier":
        name = node_text(node, source)
        return "std::" + name if name in _STD_CONTAINERS else name
    if t == "qualified_identifier":
        scope = node.child_by_field_name("scope")
        name = node.child_by_field_name("name")
        if scope is not None and node_text(scope, source) == "std" and name is not None:
            inner = name
            if inner.type == "template_type":
                tname = inner.child_by_field_name("name")
                if tname is not None and node_text(tname, source) in _SMART:
                    return _first_template_arg(inner, source)
            return "std::" + (node_text(name, source).split("<", 1)[0])  # the library's
        return type_name_of(name, source)
    if t == "template_type":
        tname = node.child_by_field_name("name")
        base = node_text(tname, source) if tname is not None else ""
        if base in _SMART:
            return _first_template_arg(node, source)
        return "std::" + base if base in _STD_CONTAINERS else base
    if t in ("sized_type_specifier", "struct_specifier", "class_specifier"):
        name = node.child_by_field_name("name")
        return node_text(name, source) if name is not None else ""
    return ""


def _first_template_arg(template_type: Any, source: bytes) -> str:
    args = template_type.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.named_children:
        if c.type == "type_descriptor":
            return type_name_of(c.child_by_field_name("type"), source)
        return type_name_of(c, source)
    return ""


def _declared_name(declarator: Any, source: bytes) -> str:
    """The variable a declarator introduces: `volume` in `IVolume * volume`,
    `data` in `data`, `r` in `r = …`; '' for a function declarator."""
    cur = declarator
    while cur is not None and cur.type in _DECLARATOR_WRAPPERS:
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            # a `reference_declarator` (`& vol`) keeps its name as an unnamed
            # child; take the last named one
            named = [c for c in cur.named_children]
            nxt = named[-1] if named else None
        cur = nxt
    if cur is None:
        return ""
    if cur.type in ("identifier", "field_identifier"):
        return node_text(cur, source)
    return ""


def _value_type(value: Any, source: bytes) -> str:
    """`auto x = <value>`: `std::make_shared<T>(…)` / `make_unique<T>(…)` →
    T; `T(…)` / `T{…}` with an upper-case T → T; `new T(…)` → T; else ''."""
    if value is None:
        return ""
    if value.type == "new_expression":
        return type_name_of(value.child_by_field_name("type"), source)
    if value.type == "call_expression":
        fn = value.child_by_field_name("function")
        if fn is None:
            return ""
        if fn.type == "qualified_identifier":
            fn = fn.child_by_field_name("name") or fn
        if fn.type == "template_function":
            name = fn.child_by_field_name("name")
            if name is not None and node_text(name, source) in _SMART:
                args = fn.child_by_field_name("arguments")
                if args is not None:
                    for c in args.named_children:
                        inner = c.child_by_field_name("type") if c.type == "type_descriptor" else c
                        return type_name_of(inner, source)
            return ""
        if fn.type in ("identifier", "type_identifier"):
            name = node_text(fn, source)
            return name if name[:1].isupper() else ""
        return ""
    if value.type in ("compound_literal_expression",):
        return type_name_of(value.child_by_field_name("type"), source)
    return ""


_ALIAS_WRAP = re.compile(r"^(?:std::|boost::)?(\w+)\s*<\s*(.+)\s*>\s*$", re.S)


def fold_type_text(text: str) -> str:
    """The class a type spelled as text names, by the module's folding rules
    (a header alias's right-hand side: `std::shared_ptr<Reader>` → `Reader`,
    `std::vector<Part>` → `std::vector`, `DB::IVolume *` → `IVolume`)."""
    t = " ".join(text.split()).replace("const ", "").replace("volatile ", "")
    t = t.rstrip(" *&")
    m = _ALIAS_WRAP.match(t)
    if m:
        outer, inner = m.group(1), m.group(2)
        if outer in _SMART:
            return fold_type_text(inner.split(",")[0])
        return "std::" + outer if outer in _STD_CONTAINERS else outer
    if t.startswith("std::"):
        return "std::" + t[len("std::"):].split("<", 1)[0]
    bare = _last_segment(t.split("<", 1)[0])
    return "std::" + bare if bare in _STD_CONTAINERS else bare


@dataclass
class _Scope:
    start: int
    end: int
    bindings: dict[str, str] = field(default_factory=dict)


class CppReceiverTyper:
    """Answers `type_of(line, receiver)` for one parsed C++ file."""

    def __init__(self, pf: Any, callee_returns: dict[str, str] | None = None) -> None:
        self._scopes: list[_Scope] = []
        self._members: dict[str, dict[str, str]] = {}  # class bare name -> field -> type
        self._aliases: dict[str, str] = {}
        self._returns: dict[str, str] = dict(callee_returns or {})
        if pf.tree is not None:
            self._collect_aliases(pf.tree.root_node, pf.source)
            self._collect_members(pf.tree.root_node, pf.source)
            self._collect_functions(pf.tree.root_node, pf.source, owner="")
        self._scopes.sort(key=lambda s: s.end - s.start)

    # ---- collection ---------------------------------------------------------

    def _collect_aliases(self, node: Any, source: bytes) -> None:
        for child in node.named_children:
            if child.type == "alias_declaration":
                name, typ = child.child_by_field_name("name"), child.child_by_field_name("type")
                if name is not None and typ is not None:
                    self._aliases[node_text(name, source)] = type_name_of(
                        typ.child_by_field_name("type") if typ.type == "type_descriptor" else typ,
                        source,
                    )
            elif child.type == "type_definition":
                typ = child.child_by_field_name("type")
                for d in child.children_by_field_name("declarator"):
                    if d.type == "type_identifier" and typ is not None:
                        self._aliases[node_text(d, source)] = type_name_of(typ, source)
            else:
                self._collect_aliases(child, source)

    def _collect_members(self, node: Any, source: bytes) -> None:
        for child in node.named_children:
            if child.type in ("class_specifier", "struct_specifier"):
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name is not None and body is not None:
                    fields = self._members.setdefault(_last_segment(node_text(name, source)), {})
                    for m in body.named_children:
                        if m.type == "field_declaration":
                            typ = type_name_of(m.child_by_field_name("type"), source)
                            for d in m.children_by_field_name("declarator"):
                                var = _declared_name(d, source)
                                if var and typ:
                                    fields[var] = typ
            self._collect_members(child, source)

    def _collect_functions(self, node: Any, source: bytes, owner: str) -> None:
        for child in node.named_children:
            if child.type in ("class_specifier", "struct_specifier"):
                name = child.child_by_field_name("name")
                self._collect_functions(
                    child, source,
                    _last_segment(node_text(name, source)) if name is not None else owner,
                )
                continue
            if child.type == "function_definition":
                scope = _Scope(child.start_point[0] + 1, child.end_point[0] + 1)
                decl = child.child_by_field_name("declarator")
                cls = owner
                fn_decl = decl
                while fn_decl is not None and fn_decl.type in _DECLARATOR_WRAPPERS:
                    fn_decl = fn_decl.child_by_field_name("declarator")
                if fn_decl is not None and fn_decl.type == "function_declarator":
                    name_node = fn_decl.child_by_field_name("declarator")
                    if name_node is not None and name_node.type == "qualified_identifier":
                        qn = node_text(name_node, source)
                        if "::" in qn:
                            cls = qn.rsplit("::", 2)[-2] if qn.count("::") >= 1 else owner
                    params = fn_decl.child_by_field_name("parameters")
                    if params is not None:
                        for p in params.named_children:
                            if p.type in (
                                "parameter_declaration", "optional_parameter_declaration",
                            ):
                                typ = type_name_of(p.child_by_field_name("type"), source)
                                var = _declared_name(p.child_by_field_name("declarator"), source)
                                if var and typ:
                                    scope.bindings[var] = typ
                scope.bindings["__owner__"] = cls
                for var, typ in self._members.get(cls, {}).items():
                    scope.bindings.setdefault(var, typ)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect_locals(body, source, scope.bindings)
                self._scopes.append(scope)
                continue
            self._collect_functions(child, source, owner)

    def _collect_locals(self, node: Any, source: bytes, bindings: dict[str, str]) -> None:
        for child in node.named_children:
            if child.type in _FUNCTION_NODES:
                continue  # a nested lambda's locals are its own
            if child.type == "declaration":
                typ = type_name_of(child.child_by_field_name("type"), source)
                for d in child.children_by_field_name("declarator"):
                    var = _declared_name(d, source)
                    if not var:
                        continue
                    if not typ and d.type == "init_declarator":
                        typ = _value_type(d.child_by_field_name("value"), source)
                    if typ:
                        bindings.setdefault(var, typ)
            elif child.type == "for_range_loop":
                typ = type_name_of(child.child_by_field_name("type"), source)
                var = _declared_name(child.child_by_field_name("declarator"), source)
                if var and typ:
                    bindings.setdefault(var, typ)
            self._collect_locals(child, source, bindings)

    # ---- queries ------------------------------------------------------------

    def bind_header(self, header: Any) -> None:
        """The fields the headers declare for each class
        (`HeaderDecls.classes`: bare name -> [(qualified, {field: _Decl}, …)]),
        folded like a same-file field: a smart-pointer container yields its
        argument, `written` the class. The file's own declarations win."""
        table = getattr(getattr(header, "aliases", None), "table", None) or {}
        for name, text in table.items():
            if text and name not in self._aliases:
                self._aliases[name] = fold_type_text(text)
        classes = getattr(header, "classes", None) or {}
        for bare, entries in classes.items():
            fields = self._members.setdefault(bare, {})
            for _qn, decls, _sigs in entries:
                for var, d in (decls or {}).items():
                    if d is None or var in fields:
                        continue
                    typ = ""
                    if d.container in _SMART and d.targs:
                        typ = _last_segment(d.targs[0])
                    elif d.written:
                        typ = _last_segment(d.written)
                    elif d.container in _STD_CONTAINERS:
                        typ = "std::" + d.container
                    if typ:
                        fields[var] = typ
        # the members reach the method scopes already built
        for scope in self._scopes:
            cls = scope.bindings.get("__owner__", "")
            for var, typ in self._members.get(cls, {}).items():
                scope.bindings.setdefault(var, typ)

    def is_library(self, line: int, receiver: str) -> bool:
        """The receiver's declared type is the standard
        library's (`std::vector`, `std::optional`) — its methods are nobody's
        in the repository, so the call abstains instead of guessing."""
        return self._raw(line, receiver).startswith("std::")

    def _raw(self, line: int, receiver: str) -> str:
        key = receiver.strip()
        if key.startswith("this->"):
            key = key[len("this->"):]
        for scope in self._scopes:
            if scope.start <= line <= scope.end and key in scope.bindings:
                return scope.bindings[key]
        return ""

    def type_of(self, line: int, receiver: str) -> str:
        key = receiver.strip()
        if key.startswith("this->"):
            key = key[len("this->"):]
        elif key.startswith("this."):
            key = key[len("this."):]
        if not key or key == "this" or not key.replace("_", "a").isalnum():
            return ""
        for scope in self._scopes:  # innermost first
            if scope.start <= line <= scope.end and key in scope.bindings:
                t = self._fold(scope.bindings[key])
                return "" if t.startswith("std::") else t
        return ""

    def _fold(self, tname: str) -> str:
        seen: set[str] = set()
        while tname in self._aliases and tname not in seen:
            seen.add(tname)
            tname = self._aliases[tname] or tname
        return _last_segment(tname)

    def callees(self) -> set[str]:
        return set()

    def bind_returns(self, returns: dict[str, str]) -> None:
        self._returns.update({k: v for k, v in returns.items() if v})
