"""Go interfaces are satisfied structurally.

Nothing in a Go file says `Store implements Reader`; a type satisfies an interface when
its method set covers the interface's. The compiler tier (scip-go) writes that
relationship as an `inherits` edge where it has run; the fast tier derives it because
every interface's method signatures are `method` symbols and every concrete method a
`<pkg>::Type::Name` row, both in the repository's name table.

This module derives the same `inherits` edges from that table alone, in
both directions, so whichever file a pass re-ingests refreshes them:

- an **interface** this file declares → every type whose concrete methods
  cover its method names (repository-wide, the name table's candidates for
  each method name grouped by their `<pkg>::Type` prefix);
- a **struct** this file declares (with its methods in this file, and the
  methods of the types it embeds when those are in this file too) → every
  interface declared in this file whose method list — carried in the
  interface symbol's signature as `interface{Get,Has,+Embedded}` — its
  method set covers (an interface in another file is that file's to derive,
  through the first direction, when its own file is ingested).

Names, not signatures: a type whose `Get` takes other parameters than the
interface's still "satisfies" it here (confidence 0.8, below a compiler's
1.0). An interface with no methods (`any`, a marker) matches nothing.
Embedded interfaces (`+Reader`) are followed when the name table carries
the embedded interface's own signature row; an embedded interface from
another package is skipped (its methods are not in the file's table).
"""

from __future__ import annotations

import re
from typing import Any

from server.code.domain.extract.treesitter import ParsedFile, Symbol

IMPLEMENTS_CONFIDENCE = 0.8
KIND_METHOD = "method"
_METHOD_LIST_RE = re.compile(r"interface\{([^{}]*)\}")


def interface_method_names(signature: str) -> tuple[list[str], list[str]]:
    """(methods, embedded interface names) an interface symbol's signature
    lists — `type Reader interface{Get,Has,+Closer}` → (["Get","Has"],
    ["Closer"]); ([], []) for a signature without the list."""
    m = _METHOD_LIST_RE.search(signature or "")
    if m is None:
        return [], []
    methods: list[str] = []
    embedded: list[str] = []
    for part in m.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("+"):
            embedded.append(part[1:])
        else:
            methods.append(part)
    return methods, embedded


_VARIADIC = "..."


def _top_level_parts(text: str) -> list[str]:
    """Comma-split at depth 0 of `(`/`[`/`{`."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _shape(signature: str) -> tuple[int, bool, str]:
    """(parameter count, variadic, result types) of a Go signature as
    `lang_go._params` writes it — `[T any](ctx context.Context, keys
    ...string) ([]byte, error)`. Parameter names are dropped, so
    `Append(v any)` and `Append(value any)` agree; a parameter group
    `a, b int` counts two."""
    sig = signature.split(" [go:build", 1)[0].strip()
    if sig.startswith("["):
        depth = 0
        for i, ch in enumerate(sig):
            depth += (ch == "[") - (ch == "]")
            if depth == 0:
                sig = sig[i + 1 :].strip()
                break
    if not sig.startswith("("):
        return 0, False, ""
    depth = 0
    end = 0
    for i, ch in enumerate(sig):
        depth += (ch == "(") - (ch == ")")
        if depth == 0:
            end = i
            break
    params, results = sig[1:end], sig[end + 1 :].strip()
    parts = _top_level_parts(params)
    count = 0
    variadic = False
    for part in parts:
        if _VARIADIC in part:
            variadic = True
        # `a, b int` reaches here as `a` and `b int`; each is one parameter
        count += 1
    if results.startswith("(") and results.endswith(")"):
        results = results[1:-1]
    res_types = []
    for part in _top_level_parts(results):
        tokens = part.split(" ", 1)
        # a named result `n int` -> `int`; a type never starts with a lower-case
        # identifier followed by a space unless it is `func`/`map`/`chan`
        named = len(tokens) == 2 and tokens[0].isidentifier()
        if named and tokens[0] not in ("func", "map", "chan", "struct", "interface"):
            part = tokens[1]
        res_types.append(_unqualified(part.replace(" ", "")))
    return count, variadic, ",".join(res_types)


_QUALIFIER_RE = re.compile(r"\b[a-z_][A-Za-z0-9_]*\.(?=[A-Z_a-z])")


def _unqualified(type_text: str) -> str:
    """A result type without its package qualifiers — the interface names its own
    package's types bare (`(Rows, error)`), the implementor in another package writes
    them qualified (`(driver.Rows, error)`); both spell one type.
    `*driver.ServerVersion` → `*ServerVersion`, `map[string]driver.Stats` →
    `map[string]Stats`."""
    return _QUALIFIER_RE.sub("", type_text)


def compatible(iface_signature: str, method_signature: str) -> bool:
    """Whether a concrete method's signature can satisfy an interface
    method's: same parameter count, same variadic-ness, same result types
    (names dropped). Unknown ('' either side) is compatible — the check
    only refutes what it can see."""
    if not iface_signature or not method_signature:
        return True
    return _shape(iface_signature) == _shape(method_signature)


def _type_prefix(qualified: str) -> str:
    """`pkg::Type::Method` → `pkg::Type`; '' for a free function."""
    return qualified.rsplit("::", 1)[0] if qualified.count("::") >= 2 else ""


def _concrete_by_type(
    name_table: dict[str, list[tuple[str, str, str]]],
    method_names: list[str],
) -> dict[str, set[str]]:
    """{`pkg::Type`: set of method names it defines among `method_names`},
    from the name table's `function` rows shaped `pkg::Type::name`."""
    out: dict[str, set[str]] = {}
    for name in method_names:
        for _sid, qn, kind in name_table.get(name, []):
            if kind != "function":
                continue
            prefix = _type_prefix(qn)
            if prefix:
                out.setdefault(prefix, set()).add(name)
    return out


class _Derivation:
    """The per-file state both directions share."""

    def __init__(
        self,
        pf: ParsedFile,
        name_table: dict[str, list[tuple[str, str, str]]],
        edge_cls: Any,
        method_sigs: dict[str, str] | None = None,
    ) -> None:
        self.pf, self.name_table, self.edge_cls = pf, name_table, edge_cls
        # symbol id -> signature for concrete methods (this file's, and the
        # rows the ingest read for the candidates elsewhere)
        self.sigs: dict[str, str] = dict(method_sigs or {})
        for s in pf.symbols:
            if s.kind in ("function", KIND_METHOD):
                self.sigs.setdefault(s.id, s.signature)
        self.iface_method_sig: dict[str, str] = {
            s.qualified_name: s.signature for s in pf.symbols if s.kind == KIND_METHOD
        }
        self.out: list[Any] = []
        self.seen: set[tuple[str, str]] = set()
        self.by_qn: dict[str, Symbol] = {s.qualified_name: s for s in pf.symbols}
        # method sets the file declares: `pkg::Type` -> names (own methods)
        self.own_methods: dict[str, set[str]] = {}
        for s in pf.symbols:
            if s.kind == "function" and s.qualified_name.count("::") >= 2:
                self.own_methods.setdefault(_type_prefix(s.qualified_name), set()).add(s.name)
        self.embeds: dict[str, list[str]] = {}
        for derived, base in pf.inherits:
            self.embeds.setdefault(derived, []).append(base)

    def emit(self, derived_id: str, derived_qn: str, base_id: str, base_qn: str, line: int) -> None:
        key = (derived_id, base_id)
        if key in self.seen or derived_id == base_id:
            return
        self.seen.add(key)
        self.out.append(
            self.edge_cls(
                "inherits",
                derived_id,
                base_id,
                derived_qn,
                base_qn,
                self.pf.path,
                line,
                IMPLEMENTS_CONFIDENCE,
            )
        )

    def method_set(self, type_qn: str, seen: frozenset[str] = frozenset()) -> set[str]:
        """Own methods plus those promoted from embedded types declared in
        this file (the embedding chain followed, cycles cut)."""
        names = set(self.own_methods.get(type_qn, set()))
        scope = type_qn.rsplit("::", 1)[0]
        for base in self.embeds.get(type_qn, []):
            base_qn = f"{scope}::{base}"
            sym = self.by_qn.get(base_qn)
            if sym is not None and sym.kind in ("struct", "type") and base_qn not in seen:
                names |= self.method_set(base_qn, seen | {type_qn})
        return names

    def wanted_of(self, iface: Symbol) -> set[str]:
        """The method names an interface declared here requires, its
        same-file embedded interfaces expanded (one from elsewhere adds
        nothing: its methods are not in this file's table)."""
        methods, embedded = interface_method_names(iface.signature)
        wanted = set(methods)
        scope = iface.qualified_name.rsplit("::", 1)[0]
        for emb in embedded:
            emb_sym = self.by_qn.get(f"{scope}::{emb}")
            if emb_sym is not None and emb_sym.kind == "interface":
                wanted |= set(interface_method_names(emb_sym.signature)[0])
        return wanted

    def _signatures_agree(self, iface_qn: str, type_qn: str, wanted: set[str]) -> bool:
        """Every wanted method the type defines is signature-compatible with the
        interface's; a signature the table does not carry cannot refute."""
        for name in wanted:
            want = self.iface_method_sig.get(f"{iface_qn}::{name}", "")
            if not want:
                continue
            for sid, qn, kind in self.name_table.get(name, []):
                if kind == "function" and _type_prefix(qn) == type_qn:
                    if not compatible(want, self.sigs.get(sid, "")):
                        return False
        return True

    def interface_side(self) -> None:
        """Interfaces this file declares -> their implementors, repository-wide."""
        for iface in (s for s in self.pf.symbols if s.kind == "interface"):
            wanted = self.wanted_of(iface)
            if not wanted:
                continue
            for type_qn, names in _concrete_by_type(self.name_table, sorted(wanted)).items():
                if names < wanted or type_qn == iface.qualified_name:
                    continue
                if not self._signatures_agree(iface.qualified_name, type_qn, wanted):
                    continue
                rows = [
                    c
                    for c in self.name_table.get(type_qn.rsplit("::", 1)[-1], [])
                    if c[1] == type_qn and c[2] in ("struct", "type", "class")
                ]
                if rows:
                    self.emit(rows[0][0], type_qn, iface.id, iface.qualified_name, iface.start_line)

    def type_side(self) -> None:
        """Types this file declares -> the same-file interfaces their method
        sets cover (an interface in another file is that file's to derive)."""
        for typ in (s for s in self.pf.symbols if s.kind in ("struct", "type")):
            names = self.method_set(typ.qualified_name)
            if not names:
                continue
            for iface in (s for s in self.pf.symbols if s.kind == "interface"):
                methods, embedded = interface_method_names(iface.signature)
                wanted = set(methods)
                if embedded:
                    wanted = self.wanted_of(iface)
                if (
                    wanted
                    and wanted <= names
                    and iface.qualified_name != typ.qualified_name
                    and self._signatures_agree(iface.qualified_name, typ.qualified_name, wanted)
                ):
                    self.emit(
                        typ.id, typ.qualified_name, iface.id, iface.qualified_name, typ.start_line
                    )


def implementor_type_names(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
) -> list[str]:
    """The bare names of the concrete types whose method rows cover an interface this
    file declares but whose OWN row the table does not hold — a type another package
    declares is named in this file by none of its call names, so `interface_side` finds
    its methods and then nothing to draw the edge from. The ingest reads these names' rows once and
    merges them into the table the derivation gets."""
    if pf.lang != "go":
        return []
    d = _Derivation(pf, name_table, edge_cls=None)
    out: list[str] = []
    for iface in (s for s in pf.symbols if s.kind == "interface"):
        wanted = d.wanted_of(iface)
        if not wanted:
            continue
        for type_qn, names in _concrete_by_type(name_table, sorted(wanted)).items():
            if names < wanted or type_qn == iface.qualified_name:
                continue
            bare = type_qn.rsplit("::", 1)[-1]
            present = any(
                c[1] == type_qn and c[2] in ("struct", "type", "class")
                for c in name_table.get(bare, [])
            )
            if not present and bare not in out:
                out.append(bare)
    return out


def candidate_method_ids(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
) -> list[str]:
    """The concrete methods whose signatures the interface side wants to check: every
    `function` row of a method name one of this file's interfaces requires, outside this
    file. The ingest reads their rows once and hands the signatures in as
    `go_method_sigs`."""
    if pf.lang != "go":
        return []
    local = {s.id for s in pf.symbols}
    d = _Derivation(pf, name_table, edge_cls=None)
    ids: list[str] = []
    for iface in (s for s in pf.symbols if s.kind == "interface"):
        for name in d.wanted_of(iface):
            for sid, qn, kind in name_table.get(name, []):
                if kind == "function" and _type_prefix(qn) and sid not in local:
                    ids.append(sid)
    # The field rows a receiver chain of this file may type through
    # (`c.compression.String()` with `c`'s struct elsewhere)
    from server.code.domain.extract.go_receivers import GoReceiverTyper

    typer = GoReceiverTyper(pf)
    for _head, field in typer.chain_fields():
        for sid, _qn, kind in name_table.get(field, []):
            if kind == "field" and sid not in local:
                ids.append(sid)
    # The struct rows of the bound types, for the embeds their signatures list (`type T
    # struct{+Base}`) — cross-file promotion; Their `type` rows too, for a named
    # container's element
    for tname in typer.type_names():
        for sid, _qn, kind in name_table.get(tname, []):
            if kind in ("struct", "type") and sid not in local:
                ids.append(sid)
    # Package-level consts / vars the file's receivers name but the tree does not type —
    # their rows carry the declared type
    from server.code.domain.extract.go_receivers import unbound_receiver_heads

    for head in unbound_receiver_heads(pf, typer):
        for sid, _qn, kind in name_table.get(head, []):
            if kind in ("const", "var") and sid not in local:
                ids.append(sid)
    return list(dict.fromkeys(ids))


def value_type(signature: str) -> str:
    """The declared type in a const / var row's signature: `const KindA Kind` → `Kind`,
    `var Default` → ''."""
    parts = signature.split(" ", 2)
    if len(parts) != 3 or parts[0] not in ("const", "var"):
        return ""
    tname = parts[2].strip().lstrip("*")
    return tname if all(p.isidentifier() for p in tname.split(".")) else ""


_TYPE_HEAD = re.compile(r"^type \w+(?:\[[^\]]*\])? (.+)$")


def type_alias_target(signature: str) -> str:
    """The type a `type` row aliases: `type Dynamic = Variant` → `Variant`, `type D =
    chcol.Dynamic` → `chcol.Dynamic`; '' for a defined type (`type Kind int`) or
    anything that is not a plain named target."""
    m = _TYPE_HEAD.match(signature)
    if m is None or not m.group(1).startswith("= "):
        return ""
    target = m.group(1)[2:].strip().lstrip("*")
    return target if target and all(p.isidentifier() for p in target.split(".")) else ""


def type_element(signature: str) -> str:
    """The element type a named container's row declares: `type ColDate32 []Date32` →
    `Date32`, `type Set map[string]*Item` → `Item`, `type Kind int` → '' (not a
    container)."""
    m = _TYPE_HEAD.match(signature)
    if m is None:
        return ""
    under = m.group(1).strip()
    peeled = under
    while True:
        if peeled.startswith("[]"):
            peeled = peeled[2:]
        elif peeled.startswith("[") and "]" in peeled:  # [N]T
            peeled = peeled[peeled.index("]") + 1 :]
        elif peeled.startswith("map["):
            depth, i = 0, 3
            while i < len(peeled):
                depth += peeled[i] == "["
                depth -= peeled[i] == "]"
                i += 1
                if depth == 0:
                    break
            peeled = peeled[i:]
        elif peeled.startswith("<-chan "):
            peeled = peeled[7:]
        elif peeled.startswith("chan<- "):
            peeled = peeled[7:]
        elif peeled.startswith("chan "):
            peeled = peeled[5:]
        else:
            break
    if peeled == under:
        return ""
    peeled = peeled.strip().lstrip("*")
    return peeled if peeled and all(p.isidentifier() for p in peeled.split(".")) else ""


def struct_embeds(signature: str) -> list[str]:
    """The embedded types a struct row's signature lists: `type T
    struct{+Base,+io.Closer}` → ['Base', 'io.Closer']; [] otherwise."""
    if "{+" not in signature or not signature.endswith("}"):
        return []
    inner = signature[signature.index("{") + 1 : -1]
    return [part[1:] for part in inner.split(",") if part.startswith("+")]


def implementor_edges(
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    edge_cls: Any,
    method_sigs: dict[str, str] | None = None,
) -> list[Any]:
    """The `inherits` edges derived for one Go file; `edge_cls` is
    `edges_treesitter.Edge` (passed in to keep this module import-free of the walker,
    which imports it at call time); `method_sigs` the signatures of the candidate
    methods outside this file."""
    if pf.lang != "go":
        return []
    d = _Derivation(pf, name_table, edge_cls, method_sigs)
    d.interface_side()
    d.type_side()
    return d.out


__all__ = [
    "IMPLEMENTS_CONFIDENCE",
    "candidate_method_ids",
    "compatible",
    "implementor_edges",
    "implementor_type_names",
    "interface_method_names",
]


def go_clauses_of(
    go_packages: dict[str, str],
    name_table: dict[str, list[tuple[str, str, str]]],
) -> dict[str, str]:
    """{package clause: directory scope} for the repository directories a file imports,
    read off the name table's qualified names (`root.ch::Client` → clause `ch` for
    directory `root`). A clause equal to an existing alias adds nothing."""
    dirs = {d for d in go_packages.values() if d}
    if not dirs:
        return {}
    out: dict[str, str] = {}
    for rows in name_table.values():
        for _sid, qn, _kind in rows:
            scope = qn.partition("::")[0]
            d, _, clause = scope.rpartition(".")
            if d in dirs and clause.isidentifier() and clause not in go_packages:
                out.setdefault(clause, d)
    return out
