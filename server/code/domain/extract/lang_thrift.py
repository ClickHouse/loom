"""Thrift IDL: structs, unions, exceptions, enums, typedefs, services and their functions.

A `.thrift` file is the contract between the services a repository builds (the generated
code is excluded as generated); reading it gives the index the API's own names. The
language is small and regular, so a token scanner with brace tracking reads it without a
tree-sitter grammar, in the shape of `lang_proto.py`:

  * `struct X { … }`, `union X { … }` and `exception X { … }` → a `struct` symbol;
    `enum X { … }` → `enum`; `typedef T X` → `type`; `service S [extends B] { … }` →
    `interface`; each function in a service → a `function` symbol `<mod>::S.f` whose
    signature is the function as written. Qualified names follow the module path.
  * reference sites: a field's, an argument's, a return's or a `throws` entry's named
    type (`field_type`), and a service's `extends` (`extends`), bound by
    `lane_units.bind_sites`; base types (`i32`, `string`, …) and the element types of
    `list<>` / `set<>` / `map<>` are read through, so `list<User>` names `User`.
    A type qualified by an include (`shared.User`) binds by its last part.
  * `include "shared.thrift"` is an include specifier; Thrift resolves it against the
    including file's directory, then the `-I` paths, unknown to the index, so the
    candidates are the file's directory and its nearest ancestors, then the root
    (`lane_units.ancestor_candidates`), settled against the namespace's live paths.

`ParsedFile.tree` stays None: chunks come from `lane_units.span_chunks`, edges from the
sites. Outside the extractor stamp with the other lanes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule, ancestor_candidates
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_THRIFT = "thrift"

KIND_STRUCT = "struct"
KIND_ENUM = "enum"
KIND_TYPEDEF = "type"
KIND_SERVICE = "interface"
KIND_FUNCTION = "function"

SITE_FIELD_TYPE = "field_type"
SITE_EXTENDS = "extends"

SITE_RULES: dict[str, SiteRule] = {
    SITE_FIELD_TYPE: SiteRule("uses_type", frozenset({KIND_STRUCT, KIND_ENUM, KIND_TYPEDEF})),
    SITE_EXTENDS: SiteRule("inherits", frozenset({KIND_SERVICE})),
}

# how `lane_units.span_chunks` cuts a Thrift file
CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("//", "#", "/*", "*"),
    "comment_blocks": (("/*", "*/"),),
    "header_prefixes": ("namespace", "include", "cpp_include"),
}

BASE_TYPES: frozenset[str] = frozenset(
    {"bool", "byte", "i8", "i16", "i32", "i64", "double", "string", "binary", "uuid", "void"}
)
_CONTAINERS: frozenset[str] = frozenset({"list", "set", "map"})
_STRUCT_KEYWORDS: frozenset[str] = frozenset({"struct", "union", "exception"})
_FIELD_REQ: frozenset[str] = frozenset({"required", "optional"})
_XSD_FIELD_FLAGS: frozenset[str] = frozenset({"xsd_optional", "xsd_nillable"})
_PAIRS: dict[str, str] = {"(": ")", "[": "]", "{": "}", "<": ">"}

# An unclosed string is one token to the end of its line; an unclosed block comment runs
# to the end of the file, as Thrift's lexer reads them.
_TOKEN_RE = re.compile(
    r"//[^\n]*|#[^\n]*|/\*.*?(?:\*/|\Z)|\"(?:[^\"\\\n]|\\.)*+\"?|'(?:[^'\\\n]|\\.)*+'?"
    r"|[A-Za-z_][A-Za-z0-9_.]*|-?\d[\w.+-]*|[{}()<>,;:=\[\]]|\S",
    re.S,
)


@dataclass
class _Tok:
    text: str
    line: int


@dataclass
class ThriftUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        t = m.group(0)
        if not t.startswith(("//", "#", "/*")):
            out.append(_Tok(t, line))
    return out


def _is_name(t: str) -> bool:
    return bool(t) and (t[0].isalpha() or t[0] == "_")


def _is_literal(t: str) -> bool:
    """A closed, non-empty string literal: `"shared.thrift"` or `'shared.thrift'`."""
    return len(t) > 2 and t[0] in "\"'" and t[-1] == t[0]


def _bare_type(name: str) -> str:
    """`shared.User` / `User` → `User`; '' for a base type or a non-name."""
    if not _is_name(name) or name in BASE_TYPES or name in _CONTAINERS:
        return ""
    return name.rsplit(".", 1)[-1]


def _join(toks: list[_Tok]) -> str:
    """Tokens back to source-like text: `User get(1: UserId id) throws(1: NotFound nf)`."""
    out = ""
    for t in toks:
        x = t.text
        if out and x not in "(),:<>" and out[-1] not in "(<":
            out += " "
        out += x
        if x in ",:":
            out += " "
    return re.sub(r"  +", " ", out).strip()


class _Parser:
    def __init__(self, path: str, toks: list[_Tok]) -> None:
        self.path = path
        self.mod = module_scope(path)
        self.toks = toks
        self.i = 0
        self.units = ThriftUnits()
        # each `{` token's matching `}`, found in one pass
        self._closes: dict[int, int] = {}
        opens: list[int] = []
        for j, t in enumerate(toks):
            if t.text == "{":
                opens.append(j)
            elif t.text == "}" and opens:
                self._closes[opens.pop()] = j

    def _peek(self, k: int = 0) -> _Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _add(self, name: str, kind: str, sig: str, start: int, end: int, scope: str = "") -> None:
        self.units.symbols.append(
            Symbol(
                qualified_name=f"{self.mod}::{scope + '.' if scope else ''}{name}",
                name=name,
                kind=kind,
                signature=sig,
                path=self.path,
                start_line=start,
                end_line=max(end, start),
            )
        )

    def _site(self, line: int, type_name: str, kind: str = SITE_FIELD_TYPE) -> None:
        bare = _bare_type(type_name)
        if bare:
            self.units.sites.append((line, bare, kind))

    def _type(self, j: int, stop: int) -> int:
        """Read a type at `j` (a name, or a container with `<…>`), recording its named
        types; returns the index after it."""
        if j >= stop:
            return j
        t = self.toks[j]
        if t.text in _CONTAINERS and j + 1 < stop and self.toks[j + 1].text == "<":
            depth = 0
            k = j + 1
            while k < stop:
                tk = self.toks[k].text
                if tk == "<":
                    depth += 1
                elif tk == ">":
                    depth -= 1
                    if depth == 0:
                        return k + 1
                elif _is_name(tk):
                    self._site(self.toks[k].line, tk)
                k += 1
            return k
        if _is_name(t.text):
            self._site(t.line, t.text)
        return j + 1

    def _group_end(self, j: int, stop: int) -> int:
        """At an opening bracket, the index after its match (or `stop`); else `j + 1`."""
        close = _PAIRS.get(self.toks[j].text)
        if close is None:
            return j + 1
        opener = self.toks[j].text
        depth = 0
        while j < stop:
            t = self.toks[j].text
            if t == opener:
                depth += 1
            elif t == close:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return stop

    def _field(self, j: int, stop: int) -> int:
        """One field, `[1:] [required|optional] Type [&] name [= value] [xsd options]
        [(annotations)]`, as in the IDL grammar, where the numeric ID is optional: record
        its type and return the index after it."""
        t = self.toks[j].text
        if t.lstrip("-").isdigit() and j + 1 < stop and self.toks[j + 1].text == ":":
            j += 2
        if j < stop and self.toks[j].text in _FIELD_REQ:
            j += 1
        j = self._type(j, stop)
        for skip in ("(", "&"):  # type annotations, then a C++ reference marker
            if j < stop and self.toks[j].text == skip:
                j = self._group_end(j, stop)
        if j < stop and _is_name(self.toks[j].text):
            j += 1
        if j < stop and self.toks[j].text == "=" and j + 1 < stop:
            j = self._group_end(j + 1, stop)
        while j < stop and self.toks[j].text in _XSD_FIELD_FLAGS:
            j += 1
        if j + 1 < stop and self.toks[j].text == "xsd_attrs" and self.toks[j + 1].text == "{":
            attrs_end = self._group_end(j + 1, stop)
            self._fields(j + 2, attrs_end - 1)
            j = attrs_end
        if j < stop and self.toks[j].text == "(":
            j = self._group_end(j, stop)
        return j

    def _fields(self, j: int, stop: int) -> None:
        """Field lists between `j` and `stop`, with or without numeric IDs and separators:
        record each field's type."""
        while j < stop:
            if self.toks[j].text in ",;":
                j += 1
                continue
            j = max(self._field(j, stop), j + 1)

    def _block_end(self, open_idx: int) -> int:
        j = self._closes.get(open_idx)
        return j if j is not None else len(self.toks) - 1

    def _service(self, start: int, name: str, open_idx: int) -> None:
        close = self._block_end(open_idx)
        self._add(
            name, KIND_SERVICE, f"service {name}", self.toks[start].line, self.toks[close].line
        )
        j = open_idx + 1
        while j < close:
            # [oneway] ReturnType name ( args ) [throws ( … )] [, ;]
            fstart = j
            if self.toks[j].text == "oneway":
                j += 1
            ret_end = self._type(j, close)
            if (
                ret_end < close
                and _is_name(self.toks[ret_end].text)
                and ret_end + 1 < close
                and self.toks[ret_end + 1].text == "("
            ):
                fname = self.toks[ret_end].text
                args_open = ret_end + 1
                k = args_open
                depth = 0
                while k < close:
                    if self.toks[k].text == "(":
                        depth += 1
                    elif self.toks[k].text == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                self._fields(args_open + 1, k)
                end = k
                if (
                    k + 1 < close
                    and self.toks[k + 1].text == "throws"
                    and k + 2 < close
                    and self.toks[k + 2].text == "("
                ):
                    m = k + 2
                    depth = 0
                    while m < close:
                        if self.toks[m].text == "(":
                            depth += 1
                        elif self.toks[m].text == ")":
                            depth -= 1
                            if depth == 0:
                                break
                        m += 1
                    self._fields(k + 3, m)
                    end = m
                sig = _join(self.toks[fstart : end + 1])
                self._add(
                    fname, KIND_FUNCTION, sig, self.toks[fstart].line, self.toks[end].line, name
                )
                j = end + 1
            else:
                j = max(ret_end, j + 1)
        self.i = close + 1

    def parse(self) -> ThriftUnits:
        toks = self.toks
        n = len(toks)
        while self.i < n:
            t = toks[self.i].text
            nxt = toks[self.i + 1].text if self.i + 1 < n else ""
            if t in _STRUCT_KEYWORDS and _is_name(nxt):
                start = self.i
                j = self.i + 2
                if j < n and toks[j].text == "xsd_all":
                    j += 1
                if j < n and toks[j].text == "{":
                    close = self._block_end(j)
                    self._add(nxt, KIND_STRUCT, f"{t} {nxt}", toks[start].line, toks[close].line)
                    self._fields(j + 1, close)
                    self.i = close + 1
                    continue
            elif t == "include" and _is_literal(nxt):
                # read from the tokens, so an include inside a comment is not one
                self.units.includes.append(nxt[1:-1])
                self.i += 2
                continue
            elif t == "enum" and _is_name(nxt):
                start = self.i
                j = self.i + 2
                if j < n and toks[j].text == "{":
                    close = self._block_end(j)
                    self._add(nxt, KIND_ENUM, f"enum {nxt}", toks[start].line, toks[close].line)
                    self.i = close + 1
                    continue
            elif t == "typedef":
                start = self.i
                j = self._type(self.i + 1, n)
                if j < n and _is_name(toks[j].text):
                    self._add(
                        toks[j].text,
                        KIND_TYPEDEF,
                        " ".join(x.text for x in toks[start : j + 1]),
                        toks[start].line,
                        toks[j].line,
                    )
                    self.i = j + 1
                    continue
                self.i = max(self.i + 1, j)
                continue
            elif t == "service" and _is_name(nxt):
                start = self.i
                j = self.i + 2
                if j + 1 < n and toks[j].text == "extends":
                    self._site(toks[j + 1].line, toks[j + 1].text, SITE_EXTENDS)
                    j += 2
                if j < n and toks[j].text == "{":
                    self._service(start, nxt, j)
                    continue
            self.i += 1
        return self.units


def thrift_units(path: str, text: str) -> ThriftUnits:
    units = _Parser(path, _tokens(text)).parse()
    units.includes = list(dict.fromkeys(units.includes))
    return units


def parse_thrift(path: str, source: bytes) -> ParsedFile:
    units = thrift_units(path, source.decode("utf-8", "replace"))
    pf = ParsedFile(path=path, lang=LANG_THRIFT, source=source)
    pf.symbols = units.symbols
    pf.includes = units.includes
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    """(line, bare type name, `field_type` | `extends`) for every named type a field,
    argument, return, `throws` entry or `extends` names, in source order."""
    return thrift_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """Every repository path the includes may name, for one membership read."""
    out: dict[str, None] = {}
    for spec in specs:
        out.update(dict.fromkeys(ancestor_candidates(from_path, spec)))
    return list(out)


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    """One indexed file per include: the nearest candidate `present` holds."""
    out: dict[str, None] = {}
    for spec in specs:
        hit = next((c for c in ancestor_candidates(from_path, spec) if c in present), None)
        if hit and hit != from_path:
            out[hit] = None
    return list(out)


__all__ = [
    "BASE_TYPES",
    "CHUNK_OPTIONS",
    "KIND_ENUM",
    "KIND_FUNCTION",
    "KIND_SERVICE",
    "KIND_STRUCT",
    "KIND_TYPEDEF",
    "LANG_THRIFT",
    "SITE_EXTENDS",
    "SITE_FIELD_TYPE",
    "SITE_RULES",
    "ThriftUnits",
    "parse_thrift",
    "reference_sites",
    "resolve_imports",
    "thrift_units",
    "wanted_paths",
]
