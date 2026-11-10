"""Go: functions, methods, types, imports, and the SQL the file runs.

The fast-tier parse of a `.go` file — `function_declaration` and `method_declaration`
(`function` symbols; a method is qualified `<pkg path>::<Receiver>.<Name>`), `type_spec`
as `struct` / `interface` / `type`, import paths as raw include specifiers — plus the
SQL islands: the argument of a `database/sql`, `sqlx`, `pgx`, `gorm`, `bun` or
`clickhouse-go` client method (`GO_CLIENT_METHODS`; context-first signatures put the SQL
second, so the first string-bearing argument is taken), `fmt.Sprintf` formats with `%s`
/ `%d` holes, `+` concatenation, and bare SQL-shaped literals (`const q = "SELECT …"`).
Outside the extractor stamp with the other lane parsers: a namespace gains the rows by a
targeted backfill over its Go tree, never by a heal.

A repository holding many `go.mod` files (a Go monorepo) is read as a module index —
every module's path and directory, plus the relative `replace` directives — and an
import path resolves to the nearest module the way `go build` resolves it, so the
cross-module imports of a platform repository are file edges.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse
from server.code.domain.extract.lang_code_islands import CodeSqlUnits, IslandGrammar, code_sql_units
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

LANG_GO = "go"

# method name → dialect ('' = the text decides)
GO_CLIENT_METHODS: dict[str, str] = {
    # database/sql and its Context forms
    "Query": "",
    "QueryRow": "",
    "QueryContext": "",
    "QueryRowContext": "",
    "Exec": "",
    "ExecContext": "",
    "Prepare": "",
    "PrepareContext": "",
    # sqlx
    "Get": "",
    "Select": "",
    "GetContext": "",
    "SelectContext": "",
    "NamedExec": "",
    "NamedQuery": "",
    "NamedExecContext": "",
    "Queryx": "",
    "QueryRowx": "",
    "QueryxContext": "",
    "QueryRowxContext": "",
    "MustExec": "",
    "Preparex": "",
    "PrepareNamed": "",
    "In": "",
    # gorm / bun / ent
    "Raw": "",
    "NewRaw": "",
    "Where": "",
    "Having": "",
    "Order": "",
    "Joins": "",
    # clickhouse-go
    "AsyncInsert": "clickhouse",
    "PrepareBatch": "clickhouse",
}
GO_CLIENT_OBJECTS: dict[str, str] = {"ch": "clickhouse", "clickhouse": "clickhouse"}
_STRING_NODES = frozenset({"interpreted_string_literal", "raw_string_literal"})
_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\\\": "\\", '\\"': '"', "\\'": "'"}
_ESCAPE_RE = re.compile(r"\\[ntr\\\"']")
_IMPORT_RE = re.compile(r'"([^"\n]+)"')


@lru_cache(maxsize=1)
def _parser() -> Any:
    import tree_sitter
    import tree_sitter_go

    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_go.language()))


def go_callee(call: Any, source: bytes) -> tuple[str, str]:
    """(method, object) of a Go call: `db.QueryContext(...)` → ("QueryContext",
    "db"); `fmt.Sprintf(...)` → ("Sprintf", "fmt"); `f(...)` → ("f", "")."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return "", ""
    if fn.type == "selector_expression":
        fld = fn.child_by_field_name("field")
        obj = fn.child_by_field_name("operand")
        obj_name = node_text(obj, source).rsplit(".", 1)[-1] if obj is not None else ""
        return (node_text(fld, source) if fld is not None else ""), obj_name
    if fn.type == "identifier":
        return node_text(fn, source), ""
    return "", ""


def go_string_value(node: Any, source: bytes) -> tuple[str, bool]:
    """A Go string literal's text: raw (backtick) as written, interpreted
    with the common escapes decoded. Never interpolated (Go has none)."""
    raw = node_text(node, source)
    if node.type == "raw_string_literal":
        return raw[1:-1] if raw.startswith("`") and raw.endswith("`") else raw, False
    body = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(0), m.group(0)), body), False


GO_ISLANDS = IslandGrammar(
    lang=LANG_GO,
    call_node="call_expression",
    string_nodes=_STRING_NODES,
    methods=GO_CLIENT_METHODS,
    callee=go_callee,
    string_value=go_string_value,
    objects=GO_CLIENT_OBJECTS,
    format_methods=frozenset({"Sprintf", "Sprint", "Sprintln"}),
    assign_nodes=(
        ("short_var_declaration", "left", "right"),
        ("assignment_statement", "left", "right"),
        ("var_spec", "name", "value"),
        ("const_spec", "name", "value"),
    ),
    aug_assign_node=("assignment_statement", "left", "operator", "right"),
)


_BUILTIN_TYPES = frozenset(
    {
        "string",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uintptr",
        "byte",
        "rune",
        "float32",
        "float64",
        "complex64",
        "complex128",
        "bool",
        "error",
        "any",
        "comparable",
    }
)
_GO_BUILD_RE = re.compile(r"(?m)^//go:build\s++(.*\S)\s*$")
KIND_METHOD = "method"  # an interface's method signature: body-less
KIND_FIELD = "field"  # a struct field: body-less, signature = its type
KIND_CONST = "const"
KIND_VAR = "var"


def go_dir_scope(directory: str) -> str:
    """The dotted scope of a repository directory: `svc/internal/handler` →
    `svc.internal.handler`; the root directory → `root`. Dotted, never a path separator,
    because the name gate classes a Go definition by the shape of its scope — a `/`
    would make it native."""
    return directory.strip("/").replace("/", ".") if directory.strip("/") else "root"


def go_scope(path: str, package_name: str = "") -> str:
    """A Go symbol is qualified by its PACKAGE — the dotted repository directory plus
    the package clause's name (`svc.internal.handler.handler`, `root.main`) — never by
    its file. The methods of a type split across files, its `_test.go` and its build-tag
    variants fold under one scope, the way `go build` sees them; an external test
    package (`package store_test`) is its own scope, as in Go. The compiler tier's
    `<module>/<pkg>::Name` folds with it by path and line."""
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return f"{go_dir_scope(directory)}.{package_name or 'main'}"


def go_build_constraint(source: bytes) -> str:
    """The file's `//go:build` expression ('' without one): two files of one package defining the
    same name under different tags are two definitions, so the constraint joins the signature."""
    head = source[:2048].decode("utf-8", "replace")
    m = _GO_BUILD_RE.search(head)
    return m.group(1).strip() if m else ""


def _doc_above(node: Any, source: bytes) -> str:
    """The `//` comment block directly above a declaration, as Go's doc
    convention reads it: contiguous line comments ending on the line before."""
    lines: list[str] = []
    prev = node.prev_sibling
    expect = node.start_point[0] - 1
    while prev is not None and prev.type == "comment" and prev.end_point[0] == expect:
        text = node_text(prev, source)
        if text.startswith("//go:") or text.startswith("//nolint") or text.startswith("//lint:"):
            break  # a directive, not documentation
        lines.append(text[2:].strip() if text.startswith("//") else text)
        expect = prev.start_point[0] - 1
        prev = prev.prev_sibling
    return "\n".join(reversed(lines)).strip()


def _bare_type_name(node: Any, source: bytes) -> str:
    """The named type a type node denotes, bare: `*st.Store` → Store,
    `[]Foo` → Foo, `Pair[K, V]` → Pair, `map[string]Foo` → Foo (the value),
    `chan Foo` → Foo; '' for a builtin, a func type or an anonymous struct."""
    n = node
    while n is not None:
        t = n.type
        if t in ("pointer_type", "slice_type", "array_type", "channel_type", "parenthesized_type"):
            n = next((c for c in n.named_children if c.type != "array_length"), None)
            continue
        if t == "map_type":
            n = n.child_by_field_name("value")
            continue
        if t == "generic_type":
            n = n.child_by_field_name("type")
            continue
        if t == "qualified_type":
            n = n.child_by_field_name("name")
            continue
        if t == "type_identifier":
            name = node_text(n, source)
            return "" if name in _BUILTIN_TYPES else name
        return ""
    return ""


def _qualifier_of(node: Any, source: bytes) -> str:
    """The package alias a type node is written under (`st.Store` → st), ''."""
    n = node
    while n is not None and n.type in (
        "pointer_type",
        "slice_type",
        "array_type",
        "channel_type",
        "generic_type",
        "map_type",
        "parenthesized_type",
    ):
        n = (
            n.child_by_field_name("value")
            if n.type == "map_type"
            else (
                n.child_by_field_name("type")
                if n.type == "generic_type"
                else next((c for c in n.named_children if c.type != "array_length"), None)
            )
        )
    if n is not None and n.type == "qualified_type":
        pkg = n.child_by_field_name("package")
        return node_text(pkg, source) if pkg is not None else ""
    return ""


def _result_type(node: Any, source: bytes) -> str:
    """The one named type a Go function returns — a single result, or the first named
    non-error type of a result list. `(int, error)` → ''; `(*Store, error)` → Store. A
    type written under a package alias keeps it — `(driver.Conn, error)` → `driver.Conn`
    — so a caller in another file resolves the receiver through its own import of that
    package (`conn, err := GetConn()`; `conn.Exec(…)` binds to `driver.Conn::Exec`, not
    to an own-package `Conn` that does not exist); a bare result stays bare."""
    res = node.child_by_field_name("result")
    if res is None:
        return ""
    if res.type == "parameter_list":
        for p in res.named_children:
            t = p.child_by_field_name("type") or p
            name = _bare_type_name(t, source)
            if name:
                return _qualified_result(t, name, source)
        return ""
    return _qualified_result(res, _bare_type_name(res, source), source)


def _qualified_result(type_node: Any, bare: str, source: bytes) -> str:
    if not bare:
        return ""
    qual = _qualifier_of(type_node, source)
    return f"{qual}.{bare}" if qual else bare


def _receiver_type(method: Any, source: bytes) -> str:
    recv = method.child_by_field_name("receiver")
    if recv is None:
        return ""
    for p in recv.named_children:
        t = p.child_by_field_name("type")
        if t is not None:
            return node_text(t, source).lstrip("*").split("[", 1)[0]
    return ""


def _params(node: Any, source: bytes) -> str:
    """`[T any](xs []T) []T` — type parameters, parameters and results as written (the
    results tell an interface's `Append(any) error` from a column's `Append(any)
    ([]uint8, error)`)."""
    p = node.child_by_field_name("parameters")
    tp = node.child_by_field_name("type_parameters")
    res = node.child_by_field_name("result")
    params = node_text(p, source) if p is not None else "()"
    sig = (node_text(tp, source) + params) if tp is not None else params
    return f"{sig} {node_text(res, source)}" if res is not None else sig


def _written_type(node: Any, source: bytes) -> str:
    """`alias.Type` / `Type` for a type node — the qualifier kept so the resolver can
    tell an imported type (`net.Conn`, `io.Closer`) from a repository namesake; '' for a
    builtin or anonymous."""
    bare = _bare_type_name(node, source)
    if not bare:
        return ""
    alias = _qualifier_of(node, source)
    return f"{alias}.{bare}" if alias else bare


def _interface_members(iface: Any, source: bytes) -> tuple[list[Any], list[str]]:
    """(method_elem nodes, embedded interface names as written) of an
    interface_type."""
    methods: list[Any] = []
    embedded: list[str] = []
    for el in iface.named_children:
        if el.type in ("method_elem", "method_spec"):
            methods.append(el)
        elif el.type == "type_elem":
            for t in el.named_children:
                name = _written_type(t, source) if t.type != "negated_type" else ""
                if name:
                    embedded.append(name)
    return methods, embedded


def _struct_embedded(struct: Any, source: bytes) -> list[str]:
    """Embedded field types of a struct (a field with no name): the types whose methods
    are promoted (the embedding is recorded)."""
    out: list[str] = []
    fields = struct.child_by_field_name("field_declaration_list") or next(
        (c for c in struct.named_children if c.type == "field_declaration_list"), None
    )
    if fields is None:
        return out
    for f in fields.named_children:
        if f.type != "field_declaration" or f.child_by_field_name("name") is not None:
            continue
        t = f.child_by_field_name("type")
        name = _written_type(t, source) if t is not None else ""
        if name:
            out.append(name)
    return out


def go_struct_fields(struct: Any, source: bytes) -> dict[str, tuple[str, str]]:
    """{field name: (bare type, package alias)} of a struct's named fields — the
    receiver typer's fact for `a.store.Has()`."""
    out: dict[str, tuple[str, str]] = {}
    fields = next((c for c in struct.named_children if c.type == "field_declaration_list"), None)
    if fields is None:
        return out
    for f in fields.named_children:
        if f.type != "field_declaration":
            continue
        t = f.child_by_field_name("type")
        if t is None:
            continue
        tname, alias = _bare_type_name(t, source), _qualifier_of(t, source)
        for c in f.named_children:
            if c.type == "field_identifier":
                out[node_text(c, source)] = (tname, alias)
    return out


def _specs(decl: Any) -> list[Any]:
    """The `const_spec` / `var_spec` nodes of a declaration, grouped
    (`var ( a = 1; b = 2 )`, a `var_spec_list`) or single."""
    out: list[Any] = []
    for c in decl.named_children:
        if c.type in ("const_spec", "var_spec"):
            out.append(c)
        elif c.type in ("var_spec_list", "const_spec_list"):
            out.extend(g for g in c.named_children if g.type in ("const_spec", "var_spec"))
    return out


class _GoFile:
    """The per-file state the declaration readers share: the parsed file,
    its package scope, the `//go:build` suffix and the source bytes."""

    def __init__(self, pf: ParsedFile, scope: str, suffix: str) -> None:
        self.pf, self.scope, self.suffix, self.source = pf, scope, suffix, pf.source

    def add(
        self,
        name: str,
        kind: str,
        sig: str,
        node: Any,
        owner: str = "",
        *,
        doc_node: Any = None,
        has_body: bool = True,
        return_type: str = "",
    ) -> None:
        qn = f"{self.scope}::{owner + '::' if owner else ''}{name}"
        self.pf.symbols.append(
            Symbol(
                qualified_name=qn,
                name=name,
                kind=kind,
                signature=sig + self.suffix,
                path=self.pf.path,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                doc_comment=_doc_above(doc_node if doc_node is not None else node, self.source),
                has_body=has_body,
                return_type=return_type,
            )
        )


def _read_type_spec(f: _GoFile, n: Any, decl: Any) -> None:
    """One `type_spec` / `type_alias`: struct, interface (with its method
    signatures as body-less `method` symbols and its embedded interfaces
    as `inherits`), alias, or defined type."""
    source = f.source
    name_node, typ = n.child_by_field_name("name"), n.child_by_field_name("type")
    if name_node is None:
        return
    name = node_text(name_node, source)
    tparams = n.child_by_field_name("type_parameters")
    head = f"type {name}{node_text(tparams, source) if tparams is not None else ''}"
    doc_node = decl if len(decl.named_children) == 1 else n
    if n.type == "type_alias":
        f.add(
            name,
            "type",
            f"{head} = {node_text(typ, source) if typ is not None else ''}",
            n,
            doc_node=doc_node,
        )
        return
    kind = {"struct_type": "struct", "interface_type": "interface"}.get(
        typ.type if typ is not None else "", "type"
    )
    qn = f"{f.scope}::{name}"
    if kind == "interface" and typ is not None:
        methods, embedded = _interface_members(typ, source)
        names = [
            node_text(m.child_by_field_name("name"), source)
            for m in methods
            if m.child_by_field_name("name") is not None
        ]
        listed = ",".join([*names, *(f"+{e}" for e in embedded)])
        f.add(name, kind, f"{head} interface{{{listed}}}", n, doc_node=doc_node)
        for m in methods:
            mname = m.child_by_field_name("name")
            if mname is not None:
                f.add(
                    node_text(mname, source),
                    KIND_METHOD,
                    _params(m, source),
                    m,
                    name,
                    has_body=False,
                    return_type=_result_type(m, source),
                )
        f.pf.inherits.extend((qn, e) for e in embedded)
    elif kind == "struct" and typ is not None:
        # The signature lists the embedded types (`type T struct{+Base,+io.Closer}`) so
        # a file elsewhere can promote a method through them from the repository's row
        # alone
        embedded = _struct_embedded(typ, source)
        listed = f"{{{','.join(f'+{e}' for e in embedded)}}}" if embedded else ""
        f.add(name, kind, f"{head} struct{listed}" if listed else head, n, doc_node=doc_node)
        f.pf.inherits.extend((qn, e) for e in embedded)
        # A named field is a body-less `field` symbol whose signature is its type as
        # written (`*st.Store`), so a receiver chain through a struct declared in
        # another file (`c.compression`) can be typed from the repository's rows; the
        # scip converter's Go fields carry the same kind
        fields = next((c for c in typ.named_children if c.type == "field_declaration_list"), None)
        for fd in fields.named_children if fields is not None else []:
            if fd.type != "field_declaration":
                continue
            ftype = fd.child_by_field_name("type")
            written = _written_type(ftype, source) if ftype is not None else ""
            named = [c for c in fd.named_children if c.type == "field_identifier"]
            if not named and written:
                # An embedded field is a field named by its type's bare name
                # (`t.Base.Describe()` is how Go spells it)
                f.add(written.rsplit(".", 1)[-1], KIND_FIELD, written, fd, name, has_body=False)
            for c in named:
                f.add(
                    node_text(c, source),
                    KIND_FIELD,
                    written or (node_text(ftype, source) if ftype is not None else ""),
                    fd,
                    name,
                    has_body=False,
                )
    else:
        body = node_text(typ, source) if typ is not None else ""
        f.add(name, kind, f"{head} {body}".rstrip(), n, doc_node=doc_node)


def _read_value_spec(f: _GoFile, n: Any, decl: Any, kind: str, implied_type: str = "") -> None:
    """One `const_spec` / `var_spec`: a symbol per name (`_` skipped); a `var f = func(...) {...}`
    is a function, so the calls in its body have a caller and the name resolves as a callee. The
    signature carries the declared type (`const KindA Kind`) — or the group's implied one — so a
    file elsewhere can type `KindA.String()`."""
    source = f.source
    typ, value = n.child_by_field_name("type"), n.child_by_field_name("value")
    doc_node = decl if len(decl.named_children) == 1 else n
    names = [c for c in n.named_children if c.type == "identifier"]
    for ident in names:
        name = node_text(ident, source)
        if name == "_":
            continue
        lit = None
        if value is not None and len(names) == 1:
            lit = next((c for c in value.named_children if c.type == "func_literal"), None)
        if lit is not None:
            f.add(
                name,
                "function",
                _params(lit, source),
                n,
                doc_node=doc_node,
                return_type=_result_type(lit, source),
            )
            continue
        tsig = node_text(typ, source) if typ is not None else implied_type
        f.add(name, kind, f"{kind} {name}{(' ' + tsig) if tsig else ''}", n, doc_node=doc_node)


def _read_function(f: _GoFile, n: Any) -> None:
    """A `function_declaration` or `method_declaration`; every `init` is its
    own function (the line joins the signature)."""
    source = f.source
    name_node = n.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(name_node, source)
    sig = _params(n, source)
    if n.type == "method_declaration":
        f.add(
            name, "function", sig, n, _receiver_type(n, source), return_type=_result_type(n, source)
        )
        return
    if name == "init":
        sig = f"(init@L{n.start_point[0] + 1})"
    f.add(name, "function", sig, n, return_type=_result_type(n, source))


def parse_go(path: str, source: bytes) -> ParsedFile:
    """Every declaration of one Go file: functions and methods (`<pkg>::Recv::Name`,
    generic type parameters in the signature, the one named result as `return_type`, the
    doc block), types (struct / interface / alias / defined type, type parameters in the
    signature), an interface's method signatures as body-less `method` symbols, package
    consts and vars (a `var f = func…` is a function), embedded struct and interface
    types as `inherits`, two `init` per file kept apart by line, and a `//go:build`
    constraint joined to every signature. Import paths stay raw for the resolver; the
    SQL islands are read by `go_sql_units` from the same tree."""
    tree = bounded_parse.parse(_parser(), source)
    pf = ParsedFile(path=path, lang=LANG_GO, tree=tree, source=source)
    root = tree.root_node
    pkg_node = next((c for c in root.named_children if c.type == "package_clause"), None)
    pkg_name = ""
    if pkg_node is not None:
        ident = next((c for c in pkg_node.named_children if c.type == "package_identifier"), None)
        pkg_name = node_text(ident, source) if ident is not None else ""
    tag = go_build_constraint(source)
    f = _GoFile(pf, go_scope(path, pkg_name), f" [go:build {tag}]" if tag else "")
    if pkg_node is not None and pkg_name:
        # The package comment (`// Package proto …`, by convention in one file, doc.go)
        # is the package's `module` symbol — the row `code.symbol proto` and
        # `code.search` answer with
        pkg_doc = _doc_above(pkg_node, source)
        if pkg_doc:
            f.add(pkg_name, "module", f"package {pkg_name}", pkg_node, doc_node=pkg_node)
    for n in root.named_children:
        t = n.type
        if t in ("function_declaration", "method_declaration"):
            _read_function(f, n)
        elif t == "type_declaration":
            for spec in n.named_children:
                if spec.type in ("type_spec", "type_alias"):
                    _read_type_spec(f, spec, n)
        elif t in ("const_declaration", "var_declaration"):
            kind = KIND_CONST if t == "const_declaration" else KIND_VAR
            group_type = ""  # `KindA Kind = iota; KindB` — KindB is a Kind
            for spec in _specs(n):
                explicit = spec.child_by_field_name("type")
                if kind == KIND_CONST:
                    if explicit is not None:
                        group_type = node_text(explicit, f.source)
                    elif spec.child_by_field_name("value") is not None:
                        group_type = ""
                _read_value_spec(f, spec, n, kind, "" if explicit is not None else group_type)
    # import paths are module paths, not repository files: no include edge here (the ingest resolves
    # them through the module index); the dialect reads them from the tree
    pf.includes = []
    return pf


def go_import_aliases(pf: ParsedFile) -> dict[str, str]:
    """{name a file refers to a package by: import path} — the explicit alias or the
    path's last element (`v5` of `…/pgx/v5` → `pgx`); dot and blank imports name
    nothing."""
    out: dict[str, str] = {}
    if pf.tree is None:
        return out

    def visit(n: Any) -> None:
        if n.type == "import_spec":
            spec = _IMPORT_RE.search(node_text(n, pf.source))
            if spec is None:
                return
            path = spec.group(1)
            alias_node = n.child_by_field_name("name")
            if alias_node is not None and alias_node.type in ("dot", "blank_identifier"):
                return
            alias = node_text(alias_node, pf.source) if alias_node is not None else ""
            if not alias:
                parts = path.rsplit("/", 2)
                last = parts[-1]
                alias = parts[-2] if re.fullmatch(r"v\d+", last) and len(parts) > 1 else last
                alias = alias.replace("-", "_").replace(".", "_")
            out[alias] = path
            return
        for c in n.children:
            visit(c)

    visit(pf.tree.root_node)
    return out


def go_imports(pf: ParsedFile) -> list[str]:
    """The file's import paths, from the tree."""
    out: list[str] = []
    if pf.tree is None:
        return out

    def visit(n: Any) -> None:
        if n.type == "import_spec":
            out.extend(_IMPORT_RE.findall(node_text(n, pf.source)))
        for c in n.children:
            visit(c)

    visit(pf.tree.root_node)
    return out


def go_file_dialect(pf: ParsedFile) -> str:
    """What the imports say the file talks to: clickhouse-go → clickhouse,
    pgx / lib/pq → postgres, go-sql-driver/mysql → mysql, sqlite drivers →
    sqlite; '' otherwise."""
    for spec in go_imports(pf):
        low = spec.lower()
        if "clickhouse" in low:
            return "clickhouse"
        if "pgx" in low or "lib/pq" in low or "postgres" in low:
            return "postgres"
        if "mysql" in low:
            return "mysql"
        if "sqlite" in low:
            return "sqlite"
    return ""


_GO_MOD_MODULE_RE = re.compile(r"(?m)^module\s+(\S+)")


def go_module_path(go_mod_text: str) -> str:
    """The `module` line of a go.mod, '' when absent."""
    m = _GO_MOD_MODULE_RE.search(go_mod_text or "")
    return m.group(1).strip() if m else ""


def go_import_candidates(spec: str, module_path: str, module_dir: str = "") -> list[str]:
    """Repository files an import path inside the module may name: the package directory's
    `<base>.go`, `doc.go`, `main.go` — the files a Go package conventionally opens with. A path
    outside the module (`fmt`, `github.com/jackc/pgx/v5`) names nothing in the tree."""
    if not module_path or not (spec == module_path or spec.startswith(module_path + "/")):
        return []
    rel = spec[len(module_path) :].lstrip("/")
    pkg_dir = "/".join(p for p in (module_dir.strip("/"), rel) if p)
    if not pkg_dir:
        return []
    base = pkg_dir.rsplit("/", 1)[-1]
    return [
        f"{pkg_dir}/{base}.go",
        f"{pkg_dir}/doc.go",
        f"{pkg_dir}/main.go",
        f"{pkg_dir}/{base}s.go",
    ]


# `[^\S\n]`: whitespace within the line; `\s` would scan on through every blank line
# from each line start, quadratic in a go.mod of them. The replaced path stops at the
# first `=>` (a module path holds none) and the runs are possessive: a `\S+` that could
# end at every `=>` of one long token is quadratic in it.
_GO_MOD_REPLACE_RE = re.compile(
    r"(?m)^[^\S\n]*(?:replace[^\S\n]+)?((?:(?!=>)\S)++)(?:[^\S\n]+v(?:(?!=>)\S)*+)?"
    r"[^\S\n]*=>[^\S\n]*(\S++)(?:[^\S\n]+v\S++)?[^\S\n]*$"
)


def go_module_index(texts: dict[str, str]) -> list[tuple[str, str]]:
    """{go.mod path: text} for the go.mod files read (the resolver passes the shallowest
    `GO_MODS_MAX`, each capped) → [(module path, module directory)], longest module path
    first, so `go_modules` picks the nearest module for an import path the way
    `go build` does across a multi-module repository. A `replace` directive whose target
    is a relative directory (`=> ../lib`) maps the replaced path onto that directory too
    — the repository's own statement of where a module lives — unless a go.mod already
    names it. A replace onto a remote module names nothing in the tree."""
    seen: dict[str, str] = {}
    replaces: list[tuple[str, str]] = []
    for mod_path in sorted(texts):
        text = texts[mod_path] or ""
        module_dir = mod_path.rsplit("/", 1)[0] if "/" in mod_path else ""
        module = go_module_path(text)
        if module and module not in seen:
            seen[module] = module_dir
        for m in _GO_MOD_REPLACE_RE.finditer(text):
            target = m.group(2)
            if not target.startswith(("./", "../")) and target != ".":
                continue
            resolved = _norm_join(module_dir, target)
            if resolved is not None:
                replaces.append((m.group(1), resolved))
    for path, directory in replaces:
        seen.setdefault(path, directory)
    return sorted(seen.items(), key=lambda kv: (-len(kv[0]), kv[0]))


def _norm_join(base: str, rel: str) -> str | None:
    """`base/rel` normalised to a repository-relative directory; None when
    the target climbs above the repository root."""
    import posixpath

    joined = posixpath.normpath(posixpath.join(base, rel)) if base else posixpath.normpath(rel)
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return "" if joined == "." else joined


def go_modules(index: list[tuple[str, str]]) -> Callable[[str], tuple[str, str]]:
    """An import path -> (module path, module directory) of the indexed module it
    belongs to: the longest module path that is the spec or a prefix of it at a `/`
    boundary; ("", "") when the spec is outside every module. The module paths are read
    into a tree of their `/` segments once, so a lookup walks the spec's segments instead
    of every module of the index."""
    root: dict[str | None, Any] = {}  # segment -> subtree; None -> the module ending here
    for module, directory in index:
        node = root
        for part in module.split("/"):
            node = node.setdefault(part, {})
        node[None] = (module, directory)
    return lambda spec: _deepest_module(root, spec)


def _deepest_module(root: dict[str | None, Any], spec: str) -> tuple[str, str]:
    found: tuple[str, str] = ("", "")
    node = root
    for part in spec.split("/"):
        if part not in node:
            break
        node = node[part]
        found = node.get(None, found)
    return found


def go_resolve_imports_indexed(
    specs: list[str], index: list[tuple[str, str]], present: set[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """`go_resolve_imports` across a module index. Returns the resolved files and the
    (spec, package directory) pairs of the in-repository imports no conventional file
    name satisfied — the listing rung's input."""
    out: dict[str, None] = {}
    unresolved: list[tuple[str, str]] = []
    module_of = go_modules(index)
    for spec in specs:
        module, directory = module_of(spec)
        if not module:
            continue
        cands = go_import_candidates(spec, module, directory)
        hit = next((c for c in cands if c in present), None)
        if hit:
            out[hit] = None
            continue
        pkg_dir = go_package_dir(spec, module, directory)
        if pkg_dir:
            unresolved.append((spec, pkg_dir))
    return list(out), unresolved


def go_wanted_paths(specs: list[str], index: list[tuple[str, str]]) -> list[str]:
    """Every candidate file the specs may name across the index; the one
    membership probe's input."""
    out: dict[str, None] = {}
    module_of = go_modules(index)
    for spec in specs:
        module, directory = module_of(spec)
        if module:
            out.update(dict.fromkeys(go_import_candidates(spec, module, directory)))
    return list(out)


def go_package_context(pf: ParsedFile, index: list[tuple[str, str]]) -> dict[str, str]:
    """{alias the file uses: dotted directory scope of the package (`go_dir_scope`)} for
    every import, '' for a package the module index does not place in the repository
    (the standard library, a dependency) — the resolver's `go_packages`, computed once
    per file by the ingest and the corpus alike."""
    out: dict[str, str] = {}
    module_of = go_modules(index)
    for alias, spec in go_import_aliases(pf).items():
        module, directory = module_of(spec)
        pkg_dir = go_package_dir(spec, module, directory) if module else ""
        out[alias] = go_dir_scope(pkg_dir) if module else ""
    return out


def go_package_dir(spec: str, module_path: str, module_dir: str = "") -> str:
    """The repository directory an in-module import path names, '' otherwise."""
    if not module_path or not (spec == module_path or spec.startswith(module_path + "/")):
        return ""
    rel = spec[len(module_path) :].lstrip("/")
    return "/".join(p for p in (module_dir.strip("/"), rel) if p)


def go_package_file(listing: list[str], pkg_dir: str) -> str:
    """The file that stands for a package among its directory's listing: the first `.go` directly in
    the directory that is not a test, '' when none."""
    prefix = pkg_dir.rstrip("/") + "/"
    for p in sorted(listing):
        if not p.startswith(prefix) or "/" in p[len(prefix) :]:
            continue
        if p.endswith(".go") and not p.endswith("_test.go"):
            return p
    return ""


def go_resolve_imports(
    specs: list[str], module_path: str, present: set[str], module_dir: str = ""
) -> list[str]:
    """One indexed file per import path inside the module, the first
    candidate `present` holds; pure."""
    out: list[str] = []
    for spec in specs:
        cands = go_import_candidates(spec, module_path, module_dir)
        hit = next((c for c in cands if c in present), None)
        if hit and hit not in out:
            out.append(hit)
    return out


def go_sql_units(pf: ParsedFile) -> CodeSqlUnits:
    return code_sql_units(pf, GO_ISLANDS, go_file_dialect(pf))


__all__ = [
    "GO_CLIENT_METHODS",
    "GO_CLIENT_OBJECTS",
    "GO_ISLANDS",
    "KIND_CONST",
    "KIND_FIELD",
    "KIND_METHOD",
    "KIND_VAR",
    "LANG_GO",
    "go_build_constraint",
    "go_callee",
    "go_file_dialect",
    "go_import_aliases",
    "go_import_candidates",
    "go_imports",
    "go_module_index",
    "go_modules",
    "go_module_path",
    "go_package_context",
    "go_package_dir",
    "go_package_file",
    "go_resolve_imports",
    "go_resolve_imports_indexed",
    "go_dir_scope",
    "go_scope",
    "go_sql_units",
    "go_string_value",
    "go_struct_fields",
    "go_wanted_paths",
    "parse_go",
]
