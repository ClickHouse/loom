"""GraphQL schemas: object, interface, input, enum, union and scalar types, and
the fields of the root operation types.

A schema file (`.graphql`, `.gql`) is the contract a GraphQL API serves. Its
grammar is small and regular, so a token scanner with brace tracking reads it
without a tree-sitter grammar:

  * `type X` and `input X` → a `struct` symbol, `interface X` → `interface`,
    `enum X` → `enum`, `union X` → `union`, `scalar X` → `type`, qualified
    `<mod>::X`;
  * each field of a root operation type (`Query`, `Mutation`, `Subscription`,
    or the type a `schema { … }` block names in its place), in a type or an
    `extend type` → a `function` symbol `<mod>::Query.field` whose signature is
    its arguments and type;
  * reference sites: a field's named type (`field_type`) and each name of an
    `implements` list (`implements`), bound by `lane_units.bind_sites`; the
    built-in scalars name nothing;
  * a description (`"…"` or `\"\"\"…\"\"\"`) is part of the definition it
    precedes, so the symbol's span starts on its first line;
  * operations and fragments define nothing and are skipped.

`ParsedFile.tree` stays None: chunks come from `lane_units.span_chunks`,
edges from the sites. Outside the extractor stamp with the other lanes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_GRAPHQL = "graphql"

KIND_OBJECT = "struct"
KIND_INPUT = "struct"
KIND_INTERFACE = "interface"
KIND_ENUM = "enum"
KIND_UNION = "union"
KIND_SCALAR = "type"
KIND_FIELD = "function"

SITE_FIELD_TYPE = "field_type"
SITE_IMPLEMENTS = "implements"

_TYPE_KINDS = frozenset({KIND_OBJECT, KIND_INTERFACE, KIND_ENUM, KIND_UNION, KIND_SCALAR})

SITE_RULES: dict[str, SiteRule] = {
    SITE_FIELD_TYPE: SiteRule("uses_type", _TYPE_KINDS),
    SITE_IMPLEMENTS: SiteRule("inherits", frozenset({KIND_INTERFACE})),
}

# how `lane_units.span_chunks` cuts a schema file
CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("#",),
    "comment_blocks": (),
    "header_prefixes": ("schema", "extend schema", "directive"),
}

BUILTIN_SCALARS: frozenset[str] = frozenset({"Int", "Float", "String", "Boolean", "ID"})
# the root type of each operation unless a `schema` block names another
ROOT_TYPES: dict[str, str] = {
    "query": "Query",
    "mutation": "Mutation",
    "subscription": "Subscription",
}

_KINDS_BY_KEYWORD: dict[str, str] = {
    "type": KIND_OBJECT,
    "input": KIND_INPUT,
    "interface": KIND_INTERFACE,
    "enum": KIND_ENUM,
    "union": KIND_UNION,
    "scalar": KIND_SCALAR,
}
_OPERATIONS: frozenset[str] = frozenset({"query", "mutation", "subscription"})
# A name in an `implements` list never reads as one of these, so a type
# without a body ends where the next definition starts.
_DEFINITION_WORDS: frozenset[str] = frozenset(
    {*_KINDS_BY_KEYWORD, *_OPERATIONS, "schema", "directive", "extend", "fragment"}
)

# Commas are insignificant in GraphQL, so they match nothing. An unclosed
# string is one token to its end, or each quote in it would rescan the rest.
_TOKEN_RE = re.compile(
    r"#[^\n]*"
    r'|"""(?:[^"\\]|\\.|"(?!""))*+(?:"""|\Z)'
    r'|"(?:[^"\\\n]|\\.)*+"?'
    r"|[_A-Za-z][_0-9A-Za-z]*"
    r"|-?\d[\w.+-]*"
    r"|\.\.\."
    r"|[^\s,]",
    re.S,
)
_NAME_RE = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")


@dataclass
class _Tok:
    text: str
    line: int


@dataclass
class GraphqlUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        t = m.group(0)
        if not t.startswith("#"):
            out.append(_Tok(t, line))
    return out


def _is_name(t: _Tok | None) -> bool:
    return t is not None and _NAME_RE.fullmatch(t.text) is not None


def _is_string(t: _Tok | None) -> bool:
    return t is not None and t.text[:1] == '"'


class _Parser:
    def __init__(self, path: str, toks: list[_Tok]) -> None:
        self.path = path
        self.mod = module_scope(path)
        self.toks = toks
        self.i = 0
        self.last_line = toks[0].line if toks else 0
        # root types named by `schema` and `extend schema` blocks; the default
        # names count only in a file without a `schema` definition
        self.named_roots: dict[str, str] = {}
        self.schema_defined = False
        self.units = GraphqlUnits()
        # (symbol, owner): a field's owner decides at the end whether it is a root
        # field, since a `schema` block may come after the types it names
        self._items: list[tuple[Symbol, str]] = []

    # ---------------------------------------------------------------- tokens
    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _peek_text(self) -> str:
        t = self._peek()
        return t.text if t is not None else ""

    def _next(self) -> _Tok | None:
        t = self._peek()
        if t is not None:
            self.i += 1
            self.last_line = t.line
        return t

    def _skip_balanced(self) -> None:
        """At `{`, `[` or `(`: consume through its matching closer."""
        depth = 0
        while (t := self._next()) is not None:
            if t.text in ("{", "[", "("):
                depth += 1
            elif t.text in ("}", "]", ")"):
                depth -= 1
                if depth <= 0:
                    return

    def _directives(self) -> None:
        while self._peek_text() == "@":
            self._next()
            self._next()
            if self._peek_text() == "(":
                self._skip_balanced()

    def _skip_value(self) -> None:
        if self._peek_text() in ("{", "["):
            self._skip_balanced()
        else:
            self._next()

    def _type_ref(self) -> tuple[str, str, int]:
        """`[Name!]!`: (its text, the named type, the name's line); ('', '', 0)
        when no name follows."""
        parts: list[str] = []
        depth = 0
        while self._peek_text() == "[":
            self._next()
            parts.append("[")
            depth += 1
        t = self._peek()
        if not _is_name(t):
            return "".join(parts), "", 0
        assert t is not None
        self._next()
        parts.append(t.text)
        if self._peek_text() == "!":
            self._next()
            parts.append("!")
        while depth and self._peek_text() == "]":
            self._next()
            parts.append("]")
            depth -= 1
            if self._peek_text() == "!":
                self._next()
                parts.append("!")
        return "".join(parts), t.text, t.line

    # --------------------------------------------------------------- symbols
    def _add(
        self, name: str, kind: str, sig: str, start: int, end: int, owner: str, at: int
    ) -> None:
        qn = f"{self.mod}::{owner + '.' if owner else ''}{name}"
        sym = Symbol(
            qualified_name=qn,
            name=name,
            kind=kind,
            signature=sig,
            path=self.path,
            start_line=start,
            end_line=end,
        )
        self._items.insert(at, (sym, owner))

    def _site(self, line: int, name: str, kind: str) -> None:
        if name and name not in BUILTIN_SCALARS:
            self.units.sites.append((line, name, kind))

    # ------------------------------------------------------------- document
    def parse(self) -> GraphqlUnits:
        while (t := self._peek()) is not None:
            start = t.line
            if _is_string(t):  # a description
                self._next()
                t = self._peek()
                if t is None:
                    break
            if t.text == "extend":
                self._next()
                self._definition(start, extend=True)
            elif t.text in _KINDS_BY_KEYWORD or t.text in ("schema", "directive"):
                self._definition(start, extend=False)
            elif t.text in _OPERATIONS or t.text == "fragment" or t.text == "{":
                self._skip_operation()
            else:
                self._next()
        defaults = {} if self.schema_defined else ROOT_TYPES
        roots = set({**defaults, **self.named_roots}.values())
        self.units.symbols = [s for s, owner in self._items if not owner or owner in roots]
        return self.units

    def _skip_operation(self) -> None:
        """An operation or a fragment, through its selection set."""
        while (t := self._peek()) is not None and t.text != "{":
            if t.text == "(":
                self._skip_balanced()
            else:
                self._next()
        if t is not None:
            self._skip_balanced()

    def _definition(self, start: int, *, extend: bool) -> None:
        kw_tok = self._next()
        if kw_tok is None:
            return
        kw = kw_tok.text
        if kw == "schema":
            self._directives()
            if self._peek_text() == "{":
                self.schema_defined = self.schema_defined or not extend
                self._schema_block()
            return
        if kw == "directive":
            self._directive_definition()
            return
        if kw not in _KINDS_BY_KEYWORD:
            return
        name_tok = self._peek()
        if not _is_name(name_tok):
            return
        assert name_tok is not None
        self._next()
        name = name_tok.text
        at = len(self._items)
        if kw in ("type", "interface") and self._peek_text() == "implements":
            self._implements()
        self._directives()
        if kw == "union" and self._peek_text() == "=":
            self._union_members()
        elif kw in ("type", "interface", "input") and self._peek_text() == "{":
            self._fields(name if kw == "type" else "")
        elif kw == "enum" and self._peek_text() == "{":
            self._skip_balanced()
        if not extend:
            sig = f"{kw} {name}"
            self._add(name, _KINDS_BY_KEYWORD[kw], sig, start, self.last_line, "", at)

    def _schema_block(self) -> None:
        """`{ query: Q mutation: M }`: the root type names."""
        self._next()
        while (t := self._next()) is not None and t.text != "}":
            if t.text in _OPERATIONS and self._peek_text() == ":":
                self._next()
                if _is_name(name := self._peek()) and name is not None:
                    self._next()
                    self.named_roots[t.text] = name.text

    def _directive_definition(self) -> None:
        """`@name(args) repeatable on A | B`: defines no type, names none."""
        if self._peek_text() == "@":
            self._next()
            self._next()
        if self._peek_text() == "(":
            self._skip_balanced()
        if self._peek_text() == "repeatable":
            self._next()
        if self._peek_text() == "on":
            self._next()
            self._name_list("|", site_kind="")

    def _implements(self) -> None:
        self._next()
        self._name_list("&", site_kind=SITE_IMPLEMENTS)

    def _union_members(self) -> None:
        self._next()
        self._name_list("|", site_kind="")

    def _name_list(self, sep: str, *, site_kind: str) -> None:
        """Names joined by `sep` (a leading one allowed, and the legacy
        comma or space form), stopping at the next definition."""
        if self._peek_text() == sep:
            self._next()
        while _is_name(t := self._peek()) and t is not None and t.text not in _DEFINITION_WORDS:
            self._next()
            if site_kind:
                self._site(t.line, t.text, site_kind)
            if self._peek_text() == sep:
                self._next()

    def _fields(self, owner: str) -> None:
        """`{ "desc" name(args): Type = default @dir … }`: each field's type is a
        site; an object type's field is a `function` symbol when `owner` turns out
        to be a root type."""
        self._next()
        while (t := self._peek()) is not None and t.text != "}":
            start = t.line
            if _is_string(t):
                self._next()
                t = self._peek()
                if t is None or t.text == "}":
                    break
            if not _is_name(t):
                self._next()
                continue
            self._next()
            args = self._arguments() if self._peek_text() == "(" else ""
            if self._peek_text() != ":":
                continue
            self._next()
            type_text, named, named_line = self._type_ref()
            if self._peek_text() == "=":
                self._next()
                self._skip_value()
            self._directives()
            self._site(named_line, named, SITE_FIELD_TYPE)
            if owner and type_text:
                sig = f"{t.text}{args}: {type_text}"
                self._add(t.text, KIND_FIELD, sig, start, self.last_line, owner, len(self._items))
        self._next()

    def _arguments(self) -> str:
        """`(a: T = v @dir, …)` read for the signature: `(a: T, …)`."""
        self._next()
        out: list[str] = []
        while (t := self._peek()) is not None and t.text != ")":
            if not _is_name(t):
                self._next()  # a description or a stray token
                continue
            self._next()
            if self._peek_text() != ":":
                continue
            self._next()
            type_text, _named, _line = self._type_ref()
            out.append(f"{t.text}: {type_text}")
            if self._peek_text() == "=":
                self._next()
                self._skip_value()
            self._directives()
        self._next()
        return "(" + ", ".join(out) + ")"


def graphql_units(path: str, text: str) -> GraphqlUnits:
    return _Parser(path, _tokens(text)).parse()


def parse_graphql(path: str, source: bytes) -> ParsedFile:
    """One schema file: its type and root field symbols; the reference sites
    are re-read from the text by `reference_sites`."""
    text = source.decode("utf-8", "replace")
    pf = ParsedFile(path=path, lang=LANG_GRAPHQL, source=source)
    pf.symbols = graphql_units(path, text).symbols
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    """(line, bare type name, `field_type` | `implements`) for every type a
    field or an `implements` list names, in source order."""
    return graphql_units(pf.path, pf.source.decode("utf-8", "replace")).sites


__all__ = [
    "BUILTIN_SCALARS",
    "CHUNK_OPTIONS",
    "KIND_ENUM",
    "KIND_FIELD",
    "KIND_INPUT",
    "KIND_INTERFACE",
    "KIND_OBJECT",
    "KIND_SCALAR",
    "KIND_UNION",
    "LANG_GRAPHQL",
    "ROOT_TYPES",
    "SITE_FIELD_TYPE",
    "SITE_IMPLEMENTS",
    "SITE_RULES",
    "GraphqlUnits",
    "graphql_units",
    "parse_graphql",
    "reference_sites",
]
