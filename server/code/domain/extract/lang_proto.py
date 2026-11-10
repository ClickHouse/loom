"""Protocol Buffers: messages, enums, services and rpcs.

A `.proto` file is the contract between the services a repository builds
(the generated `.pb.go` / `_pb2.py` code is excluded as generated); reading
it gives the index the API's own names. The grammar is small and regular —
a token scanner with brace tracking reads it without a tree-sitter grammar:

  * `message X { … }` → a `struct` symbol (nested `Outer.Inner`, qualified
    `<mod>::Outer.Inner`), `enum X { … }` → `enum`, `service S { … }` →
    `interface`, `rpc M (Req) returns (stream Resp)` → a `function` symbol
    `<mod>::S.M` whose signature is the rpc's own parenthesised shape;
  * reference sites: an rpc's request / response types (`rpc_type`) and a
    field's message / enum type (`field_type`), bound by `lane_units.
    bind_sites` — scalars, `map<>` keys and `google.protobuf.*` name nothing;
  * `import "a/b.proto"` is a raw include specifier; the import root is
    protoc's `-I`, unknown to the index, so the candidates are the file's
    directory and its nearest ancestors, then the root, + the specifier
    (`lane_units.ancestor_candidates`), settled against the namespace's live paths.

`ParsedFile.tree` stays None: chunks come from `lane_units.span_chunks`,
edges from the sites. Outside the extractor stamp with the other lanes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule, ancestor_candidates
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_PROTO = "proto"

KIND_MESSAGE = "struct"
KIND_ENUM = "enum"
KIND_SERVICE = "interface"
KIND_RPC = "function"

SITE_RPC_TYPE = "rpc_type"
SITE_FIELD_TYPE = "field_type"

SITE_RULES: dict[str, SiteRule] = {
    SITE_RPC_TYPE: SiteRule("uses_type", frozenset({KIND_MESSAGE})),
    SITE_FIELD_TYPE: SiteRule("uses_type", frozenset({KIND_MESSAGE, KIND_ENUM})),
}

# how `lane_units.span_chunks` cuts a proto file
CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("//", "/*", "*"),
    "comment_blocks": (("/*", "*/"),),
    "header_prefixes": ("syntax", "edition", "package", "import", "option"),
}

SCALARS: frozenset[str] = frozenset(
    {
        "double",
        "float",
        "int32",
        "int64",
        "uint32",
        "uint64",
        "sint32",
        "sint64",
        "fixed32",
        "fixed64",
        "sfixed32",
        "sfixed64",
        "bool",
        "string",
        "bytes",
    }
)
_FIELD_LABELS: frozenset[str] = frozenset({"repeated", "optional", "required"})
_WELL_KNOWN_PREFIX = "google.protobuf."
_EXTERNAL_IMPORT_PREFIXES: tuple[str, ...] = ("google/",)

# An unclosed string is one token to the end of its line, or each quote it holds would
# scan the rest of the line again.
_TOKEN_RE = re.compile(
    r"//[^\n]*|/\*.*?(?:\*/|\Z)|\"(?:[^\"\\\n]|\\.)*+\"?|'(?:[^'\\\n]|\\.)*+'?"
    r"|[A-Za-z_][A-Za-z0-9_.]*|\.[A-Za-z_][A-Za-z0-9_.]*|-?\d[\w.+-]*|[{}();=<>,\[\]:]|\S",
    re.S,
)
_PACKAGE_RE = re.compile(r"(?m)^[^\S\n]*package\s+([A-Za-z_][\w.]*)\s*;")
_GO_PACKAGE_RE = re.compile(r"(?m)^[^\S\n]*option\s+go_package\s*=\s*\"([^\"]+)\"")
_IMPORT_RE = re.compile(r"(?m)^[^\S\n]*import\s+(?:public\s+|weak\s+)?\"([^\"]+)\"\s*;")


@dataclass
class _Tok:
    text: str
    line: int


@dataclass
class ProtoUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        t = m.group(0)
        if not t.startswith(("//", "/*")):
            out.append(_Tok(t, line))
    return out


def _bare_type(name: str) -> str:
    """`.pkg.Outer.Inner` / `Outer.Inner` → `Inner`; '' for a scalar, a
    well-known type or a non-identifier."""
    if name in SCALARS or name.startswith("-") or name[:1].isdigit():
        return ""
    stripped = name.lstrip(".")
    if stripped.startswith(_WELL_KNOWN_PREFIX):
        return ""
    return stripped.rsplit(".", 1)[-1]


class _Parser:
    def __init__(self, path: str, toks: list[_Tok]) -> None:
        self.path = path
        self.mod = module_scope(path)
        self.toks = toks
        self.i = 0
        self.units = ProtoUnits()
        # each `{` token's matching `}`, found in one pass: a scan per block from its
        # `{` costs depth times tokens in a deep nest
        self._closes: dict[int, int] = {}
        opens: list[int] = []
        for j, t in enumerate(toks):
            if t.text == "{":
                opens.append(j)
            elif t.text == "}" and opens:
                self._closes[opens.pop()] = j

    # ---------------------------------------------------------------- tokens
    def _peek(self, k: int = 0) -> _Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _next(self) -> _Tok | None:
        t = self._peek()
        if t is not None:
            self.i += 1
        return t

    def _skip_statement(self) -> int:
        """Consume to the end of the statement: past `;`, or past a balanced
        `{ … }` block. Returns the last line consumed."""
        depth = 0
        head = self._peek()
        last = head.line if head is not None else 0
        while (t := self._next()) is not None:
            last = t.line
            if t.text == "{":
                depth += 1
            elif t.text == "}":
                depth -= 1
                if depth <= 0:
                    return last
            elif t.text == ";" and depth == 0:
                return last
        return last

    def _block_end(self) -> int:
        """At `{`: the line of the matching `}`, else the last token's (consumes
        nothing)."""
        j = self._closes.get(self.i)
        if j is not None:
            return self.toks[j].line
        return self.toks[-1].line if self.toks else 0

    # --------------------------------------------------------------- symbols
    def _add(self, name: str, kind: str, sig: str, start: int, end: int, scope: str) -> None:
        qn = f"{self.mod}::{scope + '.' if scope else ''}{name}"
        self.units.symbols.append(
            Symbol(
                qualified_name=qn,
                name=name,
                kind=kind,
                signature=sig,
                path=self.path,
                start_line=start,
                end_line=end,
            )
        )

    def parse(self) -> ProtoUnits:
        self._body(scope="", enclosing_kind="")
        return self.units

    def _body(self, scope: str, enclosing_kind: str) -> None:
        """Statements until the closing `}` of the current block (or EOF)."""
        while (t := self._peek()) is not None:
            if t.text == "}":
                self._next()
                return
            kw = t.text
            if kw in ("message", "enum", "service", "extend", "oneof", "group"):
                self._block_decl(kw, scope, enclosing_kind)
            elif kw == "rpc" and enclosing_kind == KIND_SERVICE:
                self._rpc(scope)
            elif kw in ("syntax", "package", "option", "reserved", "extensions", "edition"):
                self._skip_statement()
            elif kw == "import":
                self._import()
            elif kw == ";":
                self._next()
            elif enclosing_kind in (KIND_MESSAGE, "extend"):
                self._field(scope)
            else:
                self._skip_statement()  # an enum value, or a top-level statement we do not read

    def _import(self) -> None:
        self._next()
        t = self._next()
        if t is not None and t.text in ("public", "weak"):
            t = self._next()
        if t is not None and t.text[:1] == '"':
            self.units.imports.append(t.text[1:-1])
        self._skip_statement()

    def _block_decl(self, kw: str, scope: str, enclosing_kind: str) -> None:
        start_tok = self._next()
        assert start_tok is not None
        name_tok = self._next()
        if name_tok is None:
            return
        name = name_tok.text
        # `group Name = 1 {` (proto2) / `oneof name {` / `extend Name {`
        while (t := self._peek()) is not None and t.text != "{":
            if t.text == ";":
                self._next()
                return
            self._next()
        if self._peek() is None:
            return
        end = self._block_end()
        self._next()  # `{`
        if kw == "message":
            self._add(name, KIND_MESSAGE, f"message {name}", start_tok.line, end, scope)
            self._body(f"{scope}.{name}" if scope else name, KIND_MESSAGE)
        elif kw == "enum":
            self._add(name, KIND_ENUM, f"enum {name}", start_tok.line, end, scope)
            self._body(f"{scope}.{name}" if scope else name, KIND_ENUM)
        elif kw == "service":
            self._add(name, KIND_SERVICE, f"service {name}", start_tok.line, end, scope)
            self._body(f"{scope}.{name}" if scope else name, KIND_SERVICE)
        elif kw in ("oneof", "group"):
            # fields inside belong to the enclosing message
            self._body(scope, enclosing_kind or KIND_MESSAGE)
        else:  # extend: fields of someone else's message; no symbol, no sites
            self._body(scope, "extend")

    def _rpc(self, scope: str) -> None:
        start_tok = self._next()
        assert start_tok is not None
        name_tok = self._next()
        if name_tok is None:
            return
        parts: list[str] = []
        types: list[tuple[int, str]] = []
        depth = 0
        while (t := self._peek()) is not None and t.text not in (";", "{"):
            self._next()
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
            elif depth == 1 and t.text != "stream":
                types.append((t.line, t.text))
            parts.append(t.text)
        sig = " ".join(parts).replace("( ", "(").replace(" )", ")")
        end = self._skip_statement()
        self._add(name_tok.text, KIND_RPC, sig, start_tok.line, end, scope)
        for line, typ in types:
            bare = _bare_type(typ)
            if bare:
                self.units.sites.append((line, bare, SITE_RPC_TYPE))

    def _field(self, scope: str) -> None:
        """`[label] Type name = N [opts];` or `map<K, V> name = N;` in a
        message / oneof body; an enum value (`NAME = N;`) has no type."""
        t = self._next()
        if t is None:
            return
        if t.text in _FIELD_LABELS:
            t = self._next()
            if t is None:
                return
        if t.text == "map":
            # map < K , V >
            vals: list[_Tok] = []
            while (u := self._next()) is not None and u.text != ">":
                if u.text not in ("<", ","):
                    vals.append(u)
            if len(vals) >= 2:
                bare = _bare_type(vals[-1].text)
                if bare:
                    self.units.sites.append((vals[-1].line, bare, SITE_FIELD_TYPE))
        else:
            nxt = self._peek()
            # `Type name = N`: an enum value is `NAME = N` (next token is `=`)
            if nxt is not None and nxt.text not in ("=", ";", "{"):
                bare = _bare_type(t.text)
                if bare:
                    self.units.sites.append((t.line, bare, SITE_FIELD_TYPE))
        self._skip_statement()


def proto_units(path: str, text: str) -> ProtoUnits:
    return _Parser(path, _tokens(text)).parse()


def parse_proto(path: str, source: bytes) -> ParsedFile:
    """One `.proto` file: its symbols and raw import specifiers; the
    reference sites are re-read from the text by `reference_sites`."""
    text = source.decode("utf-8", "replace")
    units = proto_units(path, text)
    pf = ParsedFile(path=path, lang=LANG_PROTO, source=source)
    pf.symbols = units.symbols
    pf.includes = list(dict.fromkeys(units.imports))
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    """(line, bare type name, `rpc_type` | `field_type`) for every message /
    enum type an rpc or a field names, in source order."""
    return proto_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def proto_package(pf: ParsedFile) -> str:
    m = _PACKAGE_RE.search(pf.source.decode("utf-8", "replace"))
    return m.group(1) if m else ""


def go_package(pf: ParsedFile) -> str:
    """The `option go_package` import path (before any `;alias`), '' when
    absent — the Go package the generated code lands in."""
    m = _GO_PACKAGE_RE.search(pf.source.decode("utf-8", "replace"))
    return m.group(1).split(";", 1)[0] if m else ""


def import_specs(text: str) -> list[str]:
    return list(dict.fromkeys(_IMPORT_RE.findall(text)))


def is_external_import(spec: str) -> bool:
    """`google/protobuf/timestamp.proto` and the other well-known imports
    name files protoc ships, not repository files."""
    return spec.startswith(_EXTERNAL_IMPORT_PREFIXES)


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """Every repository path the imports may name, for one membership read."""
    out: dict[str, None] = {}
    for spec in specs:
        if is_external_import(spec):
            continue
        out.update(dict.fromkeys(ancestor_candidates(from_path, spec)))
    return list(out)


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    """One indexed file per import: the nearest candidate `present` holds."""
    out: dict[str, None] = {}
    for spec in specs:
        if is_external_import(spec):
            continue
        hit = next((c for c in ancestor_candidates(from_path, spec) if c in present), None)
        if hit and hit != from_path:
            out[hit] = None
    return list(out)


__all__ = [
    "CHUNK_OPTIONS",
    "KIND_ENUM",
    "KIND_MESSAGE",
    "KIND_RPC",
    "KIND_SERVICE",
    "LANG_PROTO",
    "SCALARS",
    "SITE_FIELD_TYPE",
    "SITE_RPC_TYPE",
    "SITE_RULES",
    "ProtoUnits",
    "go_package",
    "import_specs",
    "is_external_import",
    "parse_proto",
    "proto_package",
    "proto_units",
    "reference_sites",
    "resolve_imports",
    "wanted_paths",
]
