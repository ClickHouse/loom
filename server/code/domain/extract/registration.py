"""Registration edges for the fast tier.

A factory-registered class has no static caller: `factory.registerFunction
<FunctionArrayConcat>()` hands the class to a registry that constructs it and
drives its interface later, from library code no TU spells. Every method of
such a class would answer `no_static_callers`, and function-registration
files are where most such empty impact answers come from. The registration
statement is the static evidence that the class is reached, so it becomes the
caller:

  registerFunction<T>(...)             the class T (an alias `using T =
                                       Tmpl<Impl, Name>` is followed to
                                       `Tmpl`, and `Impl` joins the set)
  registerFunction(name, createX)      the creator function handed over by
  registerFunction(name, {createX, …}) name — a function pointer the
  registerFunction(name, createT<Q>)   registry calls later (a template
                                       creator's type arguments are classes too)
  registerFunction(name, [](…){ … })   a lambda: the classes it constructs
                                       (`std::make_shared<T>`, `T::create`)
                                       are registered, as are the classes a
                                       named creator constructs in this file

For a registered class the edges go from the enclosing registration function
(`REGISTER_FUNCTION(X)`, `registerAggregateFunctionX`) to every method of the
class this file defines (1.0: the file binds them exactly), else to its
`create` / `executeImpl` where the repo-wide table holds exactly one (0.6),
and to its constructor when no `create` binds (the registry constructs it).
A creator identifier binds by the ordinary ladder (same-file unique 1.0,
repo-wide unique 0.6). Nothing here emits a miss: a registered type nothing
indexes is a library or template type, not a call anyone asks about.

The edges are `calls` rows — the read side derives `via: registration` from
the caller's shape (`is_registration_caller`), so every consumer that counts
callers counts them and every consumer that lists them can tell them apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from server.code.domain.extract.treesitter import ParsedFile, Symbol, _declarator_name, node_text

# The bare name of a registration function: the `REGISTER_FUNCTION(X)` macro
# (tree-sitter parses it as a function named REGISTER_FUNCTION with the
# parameter list `(X)`) and the hand-written `registerAggregateFunctionX` /
# `registerStorageFoo` / `registerFunctionsArithmetic` family.
_REGISTRAR_NAME = re.compile(r"^(?:REGISTER_[A-Z0-9_]+|register[A-Z]\w*)$")
# A registry method: `registerFunction`, `registerStorage`, `registerInputFormat`,
# `registerDataType`, `registerCombinator`, …
_REGISTRY_METHOD = re.compile(r"^register[A-Z]\w*$")
# Registry methods whose arguments name nothing the registry constructs — an
# alias of a name registered elsewhere.
_NOT_REGISTRATIONS = {"registerAlias", "registerAliasUnchecked"}
# The interface the registry drives when the class is defined elsewhere.
INTERFACE_METHODS: tuple[str, ...] = ("create", "executeImpl")
REPO_WIDE_CONFIDENCE = 0.6


def is_registration_caller(qualified_name: str) -> bool:
    """Is this caller a registration function? `DB::REGISTER_FUNCTION`,
    `DB::registerAggregateFunctionUniq`, `registerStorageMergeTree`; a
    trailing parameter list is ignored so a scip rendering qualifies too."""
    head = qualified_name.split("(", 1)[0].strip()
    bare = head.split("::")[-1]
    return bool(bare) and _REGISTRAR_NAME.match(bare) is not None


@dataclass(frozen=True)
class RegistrationSite:
    line: int  # 1-indexed
    end_line: int  # the call's last line (a lambda argument spans several)
    method: str  # registerFunction
    types: tuple[str, ...]  # template argument types as written
    creators: tuple[str, ...]  # identifier arguments, locals of the registrar excluded
    fn_node: Any  # the enclosing function_definition node, or None


@dataclass(frozen=True)
class RegistrationEdge:
    line: int
    caller: Symbol
    to_symbol_id: str
    to_qualified_name: str
    confidence: float
    rule: str  # local | repo | ctor | impl | creator | constructed


def _registry_method(call: Any, source: bytes) -> tuple[str, Any] | None:
    """(method name, template_argument_list or None) when `call` is a
    registry call — `f.registerX<T>(…)`, `f->registerX(…)`, a bare
    `registerX<T>(…)` inside the factory itself; None otherwise."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "field_expression":
        fn = fn.child_by_field_name("field")
        if fn is None:
            return None
    if fn.type == "qualified_identifier":
        fn = fn.child_by_field_name("name")
        if fn is None:
            return None
    targs = None
    if fn.type in {"template_method", "template_function"}:
        targs = fn.child_by_field_name("arguments")
        fn = fn.child_by_field_name("name")
        if fn is None:
            return None
    if fn.type not in {"identifier", "field_identifier"}:
        return None
    method = node_text(fn, source)
    if _REGISTRY_METHOD.match(method) is None or method in _NOT_REGISTRATIONS:
        return None
    return method, targs


def _enclosing_function_node(node: Any) -> Any:
    cur = node.parent
    while cur is not None:
        if cur.type == "function_definition":
            return cur
        if cur.type in {"translation_unit", "class_specifier", "struct_specifier"}:
            return None
        cur = cur.parent
    return None


def _declared_names(fn_node: Any, source: bytes) -> set[str]:
    """Identifiers the registration function declares — its parameters and
    the locals of its body (`FunctionDocumentation documentation{…}`,
    `AggregateFunctionProperties properties = …`) — which are values handed
    to the registry, never functions it calls."""
    out: set[str] = set()
    if fn_node is None:
        return out
    stack = [fn_node]
    while stack:
        cur = stack.pop()
        if cur.type in {"init_declarator", "parameter_declaration", "declaration"}:
            for d in cur.children_by_field_name("declarator"):
                name = _declarator_name(d, source).split("::")[-1]
                if name:
                    out.add(name)
        if cur.type == "lambda_expression":
            continue
        stack.extend(cur.named_children)
    return out


def _creator_args(
    args: Any, source: bytes, declared: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(creators, types): bare identifiers among the arguments, one brace
    level deep (`{createX, documentation, properties}`), minus the
    registrar's own declarations; a template creator
    (`createAggregateFunctionQuantile<FuncQuantile>`) is a creator whose
    type arguments are registered classes. A string, a lambda (its
    constructions are read from the call's span), a qualified constant
    (`Case::Insensitive`) and a call are not creators."""
    if args is None:
        return (), ()
    names: list[str] = []
    types: list[str] = []
    for a in args.named_children:
        nodes = a.named_children if a.type == "initializer_list" else [a]
        for n in nodes:
            if n.type == "identifier":
                name = node_text(n, source)
                if name and name not in declared:
                    names.append(name)
            elif n.type == "template_function":
                nm = n.child_by_field_name("name")
                if nm is not None and nm.type == "identifier":
                    names.append(node_text(nm, source))
                types.extend(_type_args(n.child_by_field_name("arguments"), source))
    return tuple(dict.fromkeys(names)), tuple(dict.fromkeys(types))


def _type_args(targs: Any, source: bytes) -> tuple[str, ...]:
    out: list[str] = []
    if targs is None:
        return ()
    for c in targs.named_children:
        if c.type == "type_descriptor":
            out.append(node_text(c, source))
    return tuple(out)


def registration_sites(pf: ParsedFile) -> list[RegistrationSite]:
    """Every registry call of a C++ parse, in source order."""
    out: list[RegistrationSite] = []
    if pf.tree is None or pf.lang != "cpp":
        return out
    source = pf.source

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == "call_expression":
                hit = _registry_method(child, source)
                if hit is not None:
                    method, targs = hit
                    fn_node = _enclosing_function_node(child)
                    declared = _declared_names(fn_node, source)
                    creators, ctypes = _creator_args(
                        child.child_by_field_name("arguments"), source, declared,
                    )
                    out.append(RegistrationSite(
                        line=child.start_point[0] + 1, end_line=child.end_point[0] + 1,
                        method=method, types=_type_args(targs, source) + ctypes,
                        creators=creators, fn_node=fn_node,
                    ))
            walk(child)

    walk(pf.tree.root_node)
    return out


def _resolve_type(written: str, aliases: Any) -> tuple[str, list[str]]:
    """(class the written type finally names, template-argument class names)
    — `FunctionAbs` with `using FunctionAbs = FunctionUnaryArithmetic<AbsImpl,
    NameAbs, false>` is (`FunctionUnaryArithmetic`, [`AbsImpl`, `NameAbs`]).
    The head keeps its qualification as written; the arguments are bare."""
    from server.code.domain.extract.edges_treesitter import _type_parts_of_text

    head, args = _type_parts_of_text(written)
    if not head:
        return "", []
    head_text = written.split("<", 1)[0].strip()
    resolved = aliases.resolve_text(head) if aliases is not None else None
    if resolved:
        r_head, r_args = _type_parts_of_text(resolved)
        if r_head:
            head_text = resolved.split("<", 1)[0].strip()
            args = [a for a in r_args if a] + [a for a in args if a]
    return head_text, [a for a in args if a]


def _constructions(pf: ParsedFile) -> list[tuple[int, str]]:
    """(line, class) for every construction of the file — the type sites
    of a constructor call (`std::make_shared<T>`, `new T`, `T x(…)`) and the
    static factory form `T::create(…)`."""
    from server.code.domain.extract.edges_treesitter import ctor_sites

    out: list[tuple[int, str]] = [(ln, t) for ln, t, _argc in ctor_sites(pf)]
    if pf.tree is None:
        return out
    source = pf.source

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is not None and fn.type == "qualified_identifier":
                    text = node_text(fn, source)
                    scope, _, name = text.rpartition("::")
                    if name == "create" and scope:
                        out.append((child.start_point[0] + 1, scope))
            walk(child)

    walk(pf.tree.root_node)
    return out


def _is_method_of(qualified_name: str, cls: str) -> str:
    """The method name when `qualified_name` is `…::cls::m` (or `cls::m`
    for a bare `cls` written unqualified); "" otherwise."""
    head = qualified_name.split("(", 1)[0].strip()
    prefix, _, method = head.rpartition("::")
    if not method or not prefix:
        return ""
    if prefix == cls or prefix.endswith("::" + cls):
        return method
    return ""


def registered_names(pf: ParsedFile, aliases: Any = None) -> set[str]:
    """Bare names the registration edges resolve through the repo-wide
    table: each registered class, the interface methods, the creators."""
    names: set[str] = set()
    for site in registration_sites(pf):
        for t in site.types:
            head, args = _resolve_type(t, aliases)
            if head:
                names.add(head.split("::")[-1])
                names.update(INTERFACE_METHODS)
                names.update(a.split("::")[-1] for a in args)
        names.update(c.split("::")[-1] for c in site.creators)
    return names


class _Binder:
    def __init__(
        self, pf: ParsedFile, name_table: dict[str, list[tuple[str, str, str]]],
    ) -> None:
        self.pf = pf
        self.table = name_table
        self.local_fns = [s for s in pf.symbols if s.kind == "function"]
        self.local_classes = {
            s.name for s in pf.symbols if s.kind in {"class", "struct"}
        }

    def local_methods(self, cls: str) -> dict[str, list[Symbol]]:
        out: dict[str, list[Symbol]] = {}
        for s in self.local_fns:
            m = _is_method_of(s.qualified_name, cls)
            if m:
                out.setdefault(m, []).append(s)
        return out

    def repo_unique(self, cls: str, method: str) -> tuple[str, str] | None:
        """(symbol_id, qualified_name) of the one repo-wide function
        `…::cls::method`, or None."""
        cands = [
            c for c in self.table.get(method, [])
            if c[2] == "function" and _is_method_of(c[1], cls) == method
        ]
        ids = {c[0] for c in cands}
        if len(ids) != 1:
            return None
        return cands[0][0], cands[0][1]

    def class_edges(
        self, site: RegistrationSite, caller: Symbol, cls: str, impls: list[str],
        *, rule: str = "local", skip_ctor: bool = False,
    ) -> list[RegistrationEdge]:
        out: list[RegistrationEdge] = []
        bare = cls.split("::")[-1]
        local = self.local_methods(bare)
        bound: set[str] = set()
        for method, syms in local.items():
            if skip_ctor and method == bare:
                bound.add(method)
                continue
            for s in syms:
                if s.id != caller.id:
                    out.append(self._edge(site, caller, s.id, s.qualified_name, 1.0, rule))
                    bound.add(method)
        for method in INTERFACE_METHODS:
            if method in bound:
                continue
            hit = self.repo_unique(cls, method)
            if hit is not None:
                out.append(self._edge(site, caller, hit[0], hit[1], REPO_WIDE_CONFIDENCE, "repo"))
                bound.add(method)
        if "create" not in bound and bare not in bound and not skip_ctor:
            hit = self.repo_unique(cls, bare)
            if hit is not None:
                out.append(self._edge(site, caller, hit[0], hit[1], REPO_WIDE_CONFIDENCE, "ctor"))
        for impl in impls:
            if impl in self.local_classes:
                for syms in self.local_methods(impl).values():
                    for s in syms:
                        if s.id != caller.id:
                            out.append(
                                self._edge(site, caller, s.id, s.qualified_name, 1.0, "impl")
                            )
        return out

    @staticmethod
    def _edge(
        site: RegistrationSite, caller: Symbol, to_id: str, to_qname: str, conf: float, rule: str,
    ) -> RegistrationEdge:
        return RegistrationEdge(site.line, caller, to_id, to_qname, conf, rule)

    def creator_edge(
        self, site: RegistrationSite, caller: Symbol, name: str,
    ) -> RegistrationEdge | None:
        local = [
            s for s in self.local_fns
            if s.name == name and s.id != caller.id and s.has_body
        ]
        if len({s.id for s in local}) == 1:
            s = local[0]
            return RegistrationEdge(site.line, caller, s.id, s.qualified_name, 1.0, "creator")
        if local:
            return None  # several same-file overloads: the pointer picks one we cannot
        cands = [c for c in self.table.get(name, []) if c[2] == "function"]
        if len({c[0] for c in cands}) == 1:
            return RegistrationEdge(
                site.line, caller, cands[0][0], cands[0][1], REPO_WIDE_CONFIDENCE, "creator",
            )
        return None


def registration_edges(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    *,
    enclosing: Any,
    aliases: Any = None,
) -> list[RegistrationEdge]:
    """The registration edges of one parse. `enclosing(line)` is the
    walker's innermost-function lookup; `aliases` the file's alias table
    (and its headers') for `using T = Tmpl<…>`."""
    sites = registration_sites(pf)
    if not sites:
        return []
    binder = _Binder(pf, name_table)
    constructions = _constructions(pf)
    out: list[RegistrationEdge] = []
    for site in sites:
        caller = enclosing(site.line)
        if caller is None:
            continue
        named: set[str] = set()
        for t in site.types:
            head, impls = _resolve_type(t, aliases)
            if head:
                named.add(head.split("::")[-1])
                out.extend(binder.class_edges(site, caller, head, impls))
        spans = [(site.line, site.end_line)]
        for name in site.creators:
            e = binder.creator_edge(site, caller, name)
            if e is not None:
                out.append(e)
                if e.rule == "creator" and e.confidence == 1.0:
                    spans.extend(
                        (s.start_line, s.end_line) for s in binder.local_fns
                        if s.id == e.to_symbol_id
                    )
        # the classes a lambda argument or a same-file creator constructs are
        # registered too; their constructor already has its edge
        for ln, cls in constructions:
            bare = cls.split("::")[-1]
            if bare in named or not any(a <= ln <= b for a, b in spans):
                continue
            head, impls = _resolve_type(cls, aliases)
            if head and head.split("::")[-1] not in named:
                named.add(head.split("::")[-1])
                out.extend(binder.class_edges(
                    site, caller, head, impls, rule="constructed", skip_ctor=True,
                ))
    return out
