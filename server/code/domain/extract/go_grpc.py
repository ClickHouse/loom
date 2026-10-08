"""A Go type that carries a proto service implements it.

The generated `.pb.go` is declared, not searched, so the `<Svc>Server` interface protoc
writes never reaches the name table and the hop an agent asks for most in a service
monorepo — "who handles rpc X" — is lost with it. The contract survives in two places
the index does read: the proto lane's `service Svc { rpc M … }` rows (`interface`
`<mod>::Svc`, `function` `<mod>::Svc.M`) and the Go handler's own text, which embeds the
generated forward-compatibility type or registers itself.

Rungs, strongest first:

  * the handler struct embeds `Unimplemented<Svc>Server` (grpc-go),
    `Unsafe<Svc>Server`, or `Unimplemented<Svc>Handler` (connect-go) — the
    struct `inherits` the proto service at 0.9 and each of its methods named
    like one of the service's rpcs `overrides` that rpc at 0.9;
  * a call `Register<Svc>Server(x, &T{…})` / `Register<Svc>Server(x, T{…})` /
    `…(x, new(T))`, or connect's `New<Svc>Handler(&T{…})`, binds the literal's
    type the same way at 0.8; an identifier argument is left to the typer's
    line (declined here, no miss).

A name matches only when the name table holds exactly one proto service of
that bare name (kind `interface` whose qualified name ends `::Svc` and whose
scope is a `.proto` module — the proto lane's scopes are the dotted file
path, which ends with the file's stem); a Go interface that happens to share
the name is never taken. Outside the extractor stamp with the Go lane: a Go
namespace gains the edges by a targeted backfill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

_EMBED_RE = re.compile(
    r"^(?:\*)?(?:[A-Za-z_]\w*\.)?(?:Unimplemented|Unsafe)([A-Z]\w*?)(?:Server|Handler)$"
)
_BARE_IFACE_RE = re.compile(r"^(?:\*)?(?:[A-Za-z_]\w*\.)?([A-Z]\w*?)(?:Server|Handler)$")
_REGISTER_RE = re.compile(
    r"^(?:[A-Za-z_]\w*\.)?(?:Register([A-Z]\w*?)Server|New([A-Z]\w*?)Handler)$"
)

EMBED_CONFIDENCE = 0.9
REGISTER_CONFIDENCE = 0.8

NameTable = dict[str, list[tuple[str, str, str]]]


def _embeds_of(sig: str) -> list[str]:
    """The embedded types a struct signature lists (`type T struct{+a.B,+C}`)."""
    if "struct{" not in sig:
        return []
    inner = sig[sig.index("struct{") + len("struct{") :].rstrip("}")
    return [p[1:] for p in inner.split(",") if p.startswith("+")]


def _service_of_embed(embed: str, *, strict: bool) -> str:
    """`pb.UnimplementedFooServer` → `Foo`; with `strict` False a bare
    `FooServer` embed (the generated interface itself) also names `Foo`."""
    m = _EMBED_RE.match(embed)
    if m:
        return m.group(1)
    if strict:
        return ""
    m = _BARE_IFACE_RE.match(embed)
    return m.group(1) if m else ""


def implementing_structs(pf: ParsedFile) -> dict[str, tuple[str, float]]:
    """struct qualified name → (service bare name, confidence) for every
    struct of the file whose embeds or registration name a service."""
    return {qn: (svc, conf) for qn, (svc, conf, _q) in implementing_structs_qualified(pf).items()}


def _qualifier(text: str) -> str:
    """`pb.UnimplementedFooServer` / `querypb.RegisterFooServer` → the package
    alias the generated code is imported under, '' when unqualified."""
    head = text.lstrip("*").rsplit(".", 1)
    return head[0] if len(head) == 2 else ""


def implementing_structs_qualified(pf: ParsedFile) -> dict[str, tuple[str, float, str]]:
    """struct qualified name → (service bare name, confidence, the package alias the service's
    generated code is imported under — the alias's import path tells two same-named services apart).
    """
    out: dict[str, tuple[str, float, str]] = {}
    for s in pf.symbols:
        if s.kind != "struct":
            continue
        for strict in (True, False):
            for e in _embeds_of(s.signature):
                svc = _service_of_embed(e, strict=strict)
                if svc:
                    out[s.qualified_name] = (svc, EMBED_CONFIDENCE, _qualifier(e))
                    break
            if s.qualified_name in out:
                break
    for qn, (svc, qual) in _registered(pf).items():
        out.setdefault(qn, (svc, REGISTER_CONFIDENCE, qual))
    return out


def _literal_type(arg: Any, source: bytes) -> str:
    """`&T{}` / `T{}` / `new(T)` / `pkg.T{}` → bare `T`; '' otherwise."""
    n = arg
    if n.type == "unary_expression":
        n = n.child_by_field_name("operand") or (n.named_children[-1] if n.named_children else n)
    if n.type == "composite_literal":
        t = n.child_by_field_name("type")
        return node_text(t, source).rsplit(".", 1)[-1] if t is not None else ""
    if n.type == "call_expression":
        fn = n.child_by_field_name("function")
        args = n.child_by_field_name("arguments")
        if fn is not None and node_text(fn, source) == "new" and args is not None:
            named = args.named_children
            return node_text(named[0], source).rsplit(".", 1)[-1] if named else ""
    return ""


def _registered(pf: ParsedFile) -> dict[str, tuple[str, str]]:
    """struct qualified name → (service, alias) for `Register<Svc>Server(x, &T{})` and
    `New<Svc>Handler(&T{})` calls whose implementation is a literal — or an identifier
    the receiver typer types from its declaration in the same function (`api := &API{…}`
    … `RegisterQueryServiceServer(server, api)`)."""
    if pf.tree is None or pf.lang != "go":
        return {}
    structs = {s.name: s.qualified_name for s in pf.symbols if s.kind == "struct"}
    if not structs:
        return {}
    from server.code.domain.extract.go_receivers import GoReceiverTyper

    typer: GoReceiverTyper | None = None
    out: dict[str, tuple[str, str]] = {}
    stack = [pf.tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            fn_text = node_text(fn, pf.source) if fn is not None else ""
            m = _REGISTER_RE.match(fn_text)
            if m and args is not None:
                svc = m.group(1) or m.group(2)
                qual = _qualifier(fn_text)
                for arg in args.named_children:
                    t = _literal_type(arg, pf.source)
                    if not t and arg.type == "identifier":
                        typer = typer or GoReceiverTyper(pf)
                        typed = typer.type_of(arg.start_point[0] + 1, node_text(arg, pf.source))
                        t = typed.lstrip("*").rsplit(".", 1)[-1] if typed else ""
                    if t in structs:
                        out.setdefault(structs[t], (svc, qual))
        stack.extend(n.children)
    return out


@dataclass
class ExternalImpl:
    """`Register<Svc>Server(server, api)` where `api` is typed to a struct of ANOTHER
    package (`api, err := queries.New(cfg)` → `queries.Service`): the struct and its
    methods live in files this derivation cannot see, so the ingest reads their rows."""

    service: str
    service_alias: str
    type_alias: str  # the package alias the type is written under ('' = own package)
    type_name: str
    line: int


def external_registrations(
    pf: ParsedFile,
    callee_returns: dict[str, str] | None = None,
) -> list[ExternalImpl]:
    """The registrations whose implementation is an identifier typed by the
    receiver typer to a type this file does not declare."""
    if pf.tree is None or pf.lang != "go":
        return []
    from server.code.domain.extract.go_receivers import GoReceiverTyper

    own = {s.name for s in pf.symbols if s.kind in ("struct", "type")}
    typer: GoReceiverTyper | None = None
    out: list[ExternalImpl] = []
    stack = [pf.tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            fn_text = node_text(fn, pf.source) if fn is not None else ""
            m = _REGISTER_RE.match(fn_text)
            if m and args is not None:
                svc = m.group(1) or m.group(2)
                # the implementation: last argument of `Register<Svc>Server(server,
                # impl)`, first of `New<Svc>Handler(impl, opts…)` — the server /
                # mux argument is typed too and is never the implementor
                named = args.named_children
                picked = [named[-1]] if m.group(1) else named[:1]
                for arg in picked:
                    if arg.type != "identifier" or _literal_type(arg, pf.source):
                        continue
                    typer = typer or GoReceiverTyper(pf, callee_returns)
                    typed = typer.type_of(arg.start_point[0] + 1, node_text(arg, pf.source))
                    typed = typed.lstrip("*")
                    if not typed:
                        continue
                    alias, _, bare = typed.rpartition(".")
                    if bare and bare not in own:
                        out.append(
                            ExternalImpl(
                                svc, _qualifier(fn_text), alias, bare, arg.start_point[0] + 1
                            )
                        )
        stack.extend(n.children)
    return out


def has_identifier_registration(pf: ParsedFile) -> bool:
    """Whether the file registers a service with an identifier — the cheap gate for the
    cross-package read: typing the identifier needs the callees' return types, so the
    gate cannot be the typed result itself (an untyped read is empty for exactly the
    files it is for)."""
    if pf.tree is None or pf.lang != "go":
        return False
    if b"Register" not in pf.source and b"Handler(" not in pf.source:
        return False
    stack = [pf.tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if fn is not None and args is not None and _REGISTER_RE.match(node_text(fn, pf.source)):
                if any(
                    a.type == "identifier" and not _literal_type(a, pf.source)
                    for a in args.named_children
                ):
                    return True
        stack.extend(n.children)
    return False


def _scope_score(qn: str, import_path: str) -> int:
    """Segments the qualified name's scope shares with an import path."""
    want = {seg for seg in import_path.replace("-", "_").split("/") if seg}
    have = {seg.replace("-", "_") for seg in qn.split("::", 1)[0].split(".")}
    return len(want & have)


def external_struct(
    table: NameTable,
    impl: ExternalImpl,
    import_path: str,
) -> tuple[str, str] | None:
    """(symbol_id, qualified_name) of the one struct row the registration's
    identifier is typed to — by bare name, the import path of its package
    alias picking among namesakes (ties stay unbound)."""
    cands = list(
        {
            qn: sid
            for sid, qn, kind in table.get(impl.type_name, [])
            if kind in ("struct", "type", "class") and qn.endswith(f"::{impl.type_name}")
        }.items()
    )
    if len(cands) == 1:
        return cands[0][1], cands[0][0]
    if not cands or not import_path:
        return None
    scored = sorted(((_scope_score(qn, import_path), qn, sid) for qn, sid in cands), reverse=True)
    if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][2], scored[0][1]


def grpc_wanted_names(pf: ParsedFile) -> list[str]:
    """The bare names the derivation looks up: every implementing struct's
    service and the names of its methods (the rpc rows share them)."""
    impls = implementing_structs(pf)
    if not impls:
        return []
    names: set[str] = {svc for svc, _c in impls.values()}
    for s in pf.symbols:
        if s.kind == "function" and "::" in s.qualified_name:
            owner = s.qualified_name.rsplit("::", 2)
            if len(owner) == 3 and f"{owner[0]}::{owner[1]}" in impls:
                names.add(s.name)
    return sorted(names)


def _proto_service(
    table: NameTable,
    svc: str,
    import_path: str = "",
) -> tuple[str, str] | None:
    """(symbol_id, qualified_name) of the one proto service named `svc`. When several
    proto files declare a service of that name (a monorepo may declare `QueryService`
    in three), the import path of the generated package the handler names (`pb` →
    `…/internal-api/api/query/v1`) picks the proto file whose dotted path
    shares the most segments with it; a tie stays unbound."""
    cands = list(
        {
            qn: sid
            for sid, qn, kind in table.get(svc, [])
            if kind == "interface" and qn.endswith(f"::{svc}") and _is_proto_scope(qn)
        }.items()
    )
    if len(cands) == 1:
        return cands[0][1], cands[0][0]
    if not cands or not import_path:
        return None
    want = {seg for seg in import_path.replace("-", "_").split("/") if seg}
    scored = sorted(
        (
            (
                len(want & {seg.replace("-", "_") for seg in qn.split("::", 1)[0].split(".")}),
                qn,
                sid,
            )
            for qn, sid in cands
        ),
        reverse=True,
    )
    if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][2], scored[0][1]


def _is_proto_scope(qn: str) -> bool:
    """The proto lane's scope is the dotted path of a `.proto` file, so a Go
    row (`store.store::X`, package clause last) never has a stem the file
    system would spell with `proto` or `v1`… we accept a scope whose last
    segment is not a Go package clause of the same file — decided by the
    rpc rows: a service row is proto's when a `Svc.M` function shares its
    scope (checked by the caller); here only the shape gate applies."""
    return "::" in qn and "." in qn.split("::", 1)[0]


def _rpcs_of(table: NameTable, svc: str, svc_qn: str) -> dict[str, str]:
    """rpc bare name → symbol id for the service's `Svc.M` function rows."""
    rpc_scope = svc_qn.rsplit("::", 1)[0]
    return {
        qn.rsplit(".", 1)[-1]: sid
        for cands in table.values()
        for sid, qn, kind in cands
        if kind == "function" and qn.startswith(f"{rpc_scope}::{svc}.")
    }


def _service_edges(
    edge_cls: Any,
    path: str,
    struct_id: str,
    struct_qn: str,
    struct_line: int,
    svc: str,
    svc_id: str,
    svc_qn: str,
    rpcs: dict[str, str],
    methods: list[tuple[str, str, str, int]],
    conf: float,
) -> list[Any]:
    """One struct → service `inherits` edge and a method → rpc `overrides`
    edge per method whose name is one of the service's rpcs."""
    rpc_scope = svc_qn.rsplit("::", 1)[0]
    out = [
        edge_cls(
            edge_kind="inherits",
            from_symbol_id=struct_id,
            to_symbol_id=svc_id,
            from_qualified_name=struct_qn,
            to_qualified_name=svc_qn,
            path=path,
            line=struct_line,
            confidence=conf,
        )
    ]
    for name, mid, mqn, mline in methods:
        rpc_id = rpcs.get(name)
        if rpc_id is None:
            continue
        out.append(
            edge_cls(
                edge_kind="overrides",
                from_symbol_id=mid,
                to_symbol_id=rpc_id,
                from_qualified_name=mqn,
                to_qualified_name=f"{rpc_scope}::{svc}.{name}",
                path=path,
                line=mline,
                confidence=conf,
            )
        )
    return out


def grpc_edges(
    pf: ParsedFile,
    table: NameTable,
    edge_cls: Any,
    external: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """The `inherits` (struct → service) and `overrides` (method → rpc) edges of one Go file;
    `table` is the bare-name table `grpc_wanted_names` asked for, merged with the file's ordinary
    one. `external`: the ingest's resolved cross-package registrations — dicts with `service`,
    `service_alias`, `struct_id`, `struct_qn`, `line` and `methods` [(name, id, qualified_name,
    line)]."""
    from server.code.domain.extract.lang_go import go_import_aliases

    aliases = go_import_aliases(pf)
    by_qn: dict[str, Symbol] = {s.qualified_name: s for s in pf.symbols}
    edges: list[Any] = []
    for struct_qn, (svc, conf, qual) in implementing_structs_qualified(pf).items():
        struct = by_qn.get(struct_qn)
        found = _proto_service(table, svc, aliases.get(qual, ""))
        if struct is None or found is None:
            continue
        rpcs = _rpcs_of(table, svc, found[1])
        if not rpcs:
            continue  # a Go interface of that name, or a service without rpcs
        methods = [
            (s.name, s.id, s.qualified_name, s.start_line)
            for s in pf.symbols
            if s.kind == "function" and s.qualified_name.startswith(f"{struct_qn}::")
        ]
        edges.extend(
            _service_edges(
                edge_cls,
                pf.path,
                struct.id,
                struct_qn,
                struct.start_line,
                svc,
                found[0],
                found[1],
                rpcs,
                methods,
                conf,
            )
        )
    for ext in external or []:
        found = _proto_service(table, ext["service"], aliases.get(ext.get("service_alias", ""), ""))
        if found is None:
            continue
        rpcs = _rpcs_of(table, ext["service"], found[1])
        if not rpcs:
            continue
        edges.extend(
            _service_edges(
                edge_cls,
                pf.path,
                ext["struct_id"],
                ext["struct_qn"],
                int(ext.get("line") or 0),
                ext["service"],
                found[0],
                found[1],
                rpcs,
                [tuple(m) for m in ext.get("methods", [])],
                REGISTER_CONFIDENCE,
            )
        )
    return edges


__all__ = [
    "EMBED_CONFIDENCE",
    "REGISTER_CONFIDENCE",
    "grpc_edges",
    "grpc_wanted_names",
    "ExternalImpl",
    "external_registrations",
    "external_struct",
    "has_identifier_registration",
    "implementing_structs",
    "implementing_structs_qualified",
]
