"""Lua: functions are symbols, calls are reference sites, `require` is an include.

A Lua file holds a repository's scripts, plugin code or game logic. What an
agent asks of it is where a function is defined, who calls it and which
module a file requires; a token scanner with block tracking reads all three
without a tree-sitter grammar:

  * `function a.b()`, `function a:b()`, `function f()`, `local function f()`,
    `M.f = function()` and `local f = function()` → a `function` symbol whose
    bare name is the last part (`b`, `f`) and whose qualified name is the
    file's module path plus the dotted name (`<mod>::a.b`; a method's `:`
    reads as `.`); its span runs to the `end` that closes the function;
  * reference sites: `f(…)` and `a.b(…)` → a `call` on the bare name, bound by
    `lane_units.bind_sites` to a `function` of that name (same file 1.0,
    repository-unique 0.6). Lua's own functions (`print`, `ipairs`, …) and
    standard libraries (`string.format`, `table.insert`, …) name nothing; a
    method call `obj:m()` is not a site, the receiver's type being unknown;
  * `require("a.b")` / `require "a.b"` → an include of `a/b.lua` or
    `a/b/init.lua`. `package.path` is unknown to the index, so the candidates
    are the file's directory and its nearest ancestors, then the root
    (`lane_units.ancestor_dirs`), settled against the namespace's live paths.

Blocks open at `function`, `if`, `do` (which `for` and `while` use) and
`repeat`, and close at `end` / `until`. Strings, long brackets (`[[…]]`,
`[==[…]==]`) and comments (`--`, `--[[…]]`) are skipped, so a keyword inside
them moves nothing. `ParsedFile.tree` stays None: chunks come from
`lane_units.span_chunks`, edges from the sites. Outside the extractor stamp
with the other lanes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule, ancestor_dirs
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_LUA = "lua"

KIND_FUNCTION = "function"
SITE_CALL = "call"
SITE_RULES: dict[str, SiteRule] = {
    SITE_CALL: SiteRule("calls", frozenset({KIND_FUNCTION})),
}

# how `lane_units.span_chunks` cuts a Lua file
CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("--",),
    "comment_blocks": (("--[[", "]]"),),
    "header_prefixes": ("local ", "require"),
}

_KEYWORDS: frozenset[str] = frozenset(
    {
        "and",
        "break",
        "do",
        "else",
        "elseif",
        "end",
        "false",
        "for",
        "function",
        "goto",
        "if",
        "in",
        "local",
        "nil",
        "not",
        "or",
        "repeat",
        "return",
        "then",
        "true",
        "until",
        "while",
    }
)
_OPENERS: frozenset[str] = frozenset({"function", "if", "do", "repeat"})
_CLOSERS: frozenset[str] = frozenset({"end", "until"})

# Lua's base functions and standard library tables: a call to one names no repository
# symbol, even where a repository happens to define a function of the same name
BUILTIN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "assert",
        "collectgarbage",
        "dofile",
        "error",
        "getmetatable",
        "ipairs",
        "load",
        "loadfile",
        "loadstring",
        "module",
        "next",
        "pairs",
        "pcall",
        "print",
        "rawequal",
        "rawget",
        "rawlen",
        "rawset",
        "require",
        "select",
        "setmetatable",
        "tonumber",
        "tostring",
        "type",
        "unpack",
        "xpcall",
    }
)
BUILTIN_LIBRARIES: frozenset[str] = frozenset(
    {"coroutine", "debug", "io", "math", "os", "package", "string", "table", "utf8", "bit32"}
)

# A long bracket or string that never closes runs to the end of the file, as Lua's
# lexer reads it, instead of being searched again from every later opener.
_TOKEN_RE = re.compile(
    r"--\[(?P<cq>=*)\[.*?(?:\](?P=cq)\]|\Z)"  # long comment
    r"|--[^\n]*"  # line comment
    r"|\[(?P<sq>=*)\[.*?(?:\](?P=sq)\]|\Z)"  # long string
    r"|\"(?:[^\"\\\n]|\\.)*+\"?|'(?:[^'\\\n]|\\.)*+'?"  # short strings
    r"|[A-Za-z_][A-Za-z0-9_]*"
    r"|0[xX][0-9A-Fa-f.]+(?:[pP][+-]?\d+)?|\d[\d.]*(?:[eE][+-]?\d+)?"
    r"|::|\.\.\.?|[=~<>]=|\S",
    re.S,
)


@dataclass
class _Tok:
    text: str
    line: int


@dataclass
class LuaUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    line = 1
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        line += text.count("\n", pos, m.start())
        pos = m.start()
        t = m.group(0)
        if not t.startswith("--"):
            out.append(_Tok(t, line))
    return out


def _is_name(t: str) -> bool:
    return bool(t) and (t[0].isalpha() or t[0] == "_") and t not in _KEYWORDS


def _string_value(t: str) -> str | None:
    """The text a string token holds, None for anything else."""
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        return t[1:-1]
    m = re.fullmatch(r"\[(=*)\[\n?(.*)\]\1\]", t, re.S)
    return m.group(2) if m else None


def _block_ends(toks: list[_Tok]) -> dict[int, int]:
    """Each block opener's index → the index of the `end` / `until` that closes it."""
    ends: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(toks):
        if t.text in _OPENERS:
            stack.append(i)
        elif t.text in _CLOSERS and stack:
            ends[stack.pop()] = i
    return ends


def _dotted(toks: list[_Tok], i: int) -> tuple[list[str], int]:
    """At a name: the `a.b:c` path it starts and the index after it."""
    parts = [toks[i].text]
    j = i + 1
    while j + 1 < len(toks) and toks[j].text in (".", ":") and _is_name(toks[j + 1].text):
        parts.append(toks[j + 1].text)
        j += 2
    return parts, j


def _params(toks: list[_Tok], i: int) -> tuple[str, int]:
    """At `(`: the parameter list as written and the index after `)`."""
    j = i + 1
    names: list[str] = []
    # only names, commas and `...` belong in a parameter list: stop at anything else,
    # so an unclosed list costs its own length, not the rest of the file
    while j < len(toks) and (_is_name(toks[j].text) or toks[j].text in (",", "...")):
        if toks[j].text != ",":
            names.append(toks[j].text)
        j += 1
    return "(" + ", ".join(names) + ")", j + 1


def _call_sites(toks: list[_Tok], header: set[int]) -> list[tuple[int, str, str]]:
    """`f(…)` and `a.b(…)` call sites, past definitions' own names, method calls on an
    unknown receiver, and Lua's base functions and standard libraries."""
    n = len(toks)
    sites: list[tuple[int, str, str]] = []
    for i, tok in enumerate(toks):
        name = tok.text
        if i in header or not _is_name(name) or i + 1 >= n or toks[i + 1].text != "(":
            continue
        prev = toks[i - 1].text if i > 0 else ""
        if prev == ":" or prev == "function":
            continue  # a method call on an unknown receiver; an anonymous function
        if prev == ".":
            receiver = toks[i - 2].text if i >= 2 else ""
            if receiver in BUILTIN_LIBRARIES and (i < 3 or toks[i - 3].text not in (".", ":")):
                continue
        elif name in BUILTIN_FUNCTIONS:
            continue
        sites.append((tok.line, name, SITE_CALL))
    return sites


def _requires(toks: list[_Tok]) -> list[str]:
    """The module names `require "a.b"` / `require("a.b")` name, as written."""
    n = len(toks)
    out: list[str] = []
    for i, tok in enumerate(toks):
        if tok.text != "require" or (i > 0 and toks[i - 1].text in (".", ":")):
            continue
        j = i + 1
        if j < n and toks[j].text == "(":
            j += 1
        spec = _string_value(toks[j].text) if j < n else None
        if spec:
            out.append(spec.strip())
    return out


def lua_units(path: str, text: str) -> LuaUnits:
    units = LuaUnits()
    mod = module_scope(path)
    toks = _tokens(text)
    ends = _block_ends(toks)
    header: set[int] = set()  # name tokens that are a definition's own name

    def add(parts: list[str], method: bool, fn_index: int, start_line: int, params: str) -> None:
        end_index = ends.get(fn_index)
        end_line = toks[end_index].line if end_index is not None else toks[-1].line
        shown = (
            ".".join(parts[:-1]) + (":" if method else ".") + parts[-1]
            if len(parts) > 1
            else parts[0]
        )
        units.symbols.append(
            Symbol(
                qualified_name=f"{mod}::{'.'.join(parts)}",
                name=parts[-1],
                kind=KIND_FUNCTION,
                signature=f"function {shown}{params}",
                path=path,
                start_line=start_line,
                end_line=max(end_line, start_line),
            )
        )

    n = len(toks)
    i = 0
    while i < n:
        t = toks[i].text
        # `function a.b:c(...)` and `local function f(...)`
        if t == "function" and i + 1 < n and _is_name(toks[i + 1].text):
            start = toks[i - 1].line if i > 0 and toks[i - 1].text == "local" else toks[i].line
            parts, j = _dotted(toks, i + 1)
            method = j >= 2 and toks[j - 2].text == ":"
            if j < n and toks[j].text == "(":
                header.update(range(i + 1, j))
                params, _ = _params(toks, j)
                add(parts, method, i, start, params)
        # `local f = function(...)` and `M.f = function(...)`
        elif _is_name(t) and (i == 0 or toks[i - 1].text not in (".", ":")):
            parts, j = _dotted(toks, i)
            if (
                j + 2 < n
                and toks[j].text == "="
                and toks[j + 1].text == "function"
                and toks[j + 2].text == "("
                and ":" not in [toks[k].text for k in range(i, j)]
            ):
                start = toks[i - 1].line if i > 0 and toks[i - 1].text == "local" else toks[i].line
                header.update(range(i, j))
                params, _ = _params(toks, j + 2)
                add(parts, False, j + 1, start, params)
        i += 1

    units.sites = _call_sites(toks, header)
    units.requires = _requires(toks)
    return units


def parse_lua(path: str, source: bytes) -> ParsedFile:
    units = lua_units(path, source.decode("utf-8", "replace"))
    pf = ParsedFile(path=path, lang=LANG_LUA, source=source)
    pf.symbols = units.symbols
    pf.includes = list(dict.fromkeys(units.requires))
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    """(line, bare function name, `call`) for every `f(…)` / `a.b(…)`, in source order."""
    return lua_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def _module_paths(spec: str) -> list[str]:
    """`a.b` → `a/b.lua`, `a/b/init.lua`; '' parts and absolute paths name nothing."""
    rel = spec[:-4] if spec.endswith(".lua") else spec
    rel = rel.replace(".", "/") if "/" not in rel else rel
    if not rel or rel.startswith("/") or "//" in rel or ".." in rel.split("/"):
        return []
    return [f"{rel}.lua", f"{rel}/init.lua"]


def _candidates(from_path: str, spec: str) -> list[str]:
    bases = (*ancestor_dirs(from_path), "")
    out: dict[str, None] = {}
    for base in bases:
        for rel in _module_paths(spec):
            out[f"{base}/{rel}" if base else rel] = None
    return list(out)


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """Every repository path the requires may name, for one membership read."""
    out: dict[str, None] = {}
    for spec in specs:
        out.update(dict.fromkeys(_candidates(from_path, spec)))
    return list(out)


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    """One indexed file per require: the nearest candidate `present` holds; a module
    the repository does not hold (a rock, a C module) resolves to nothing."""
    out: dict[str, None] = {}
    for spec in specs:
        hit = next((c for c in _candidates(from_path, spec) if c in present), None)
        if hit and hit != from_path:
            out[hit] = None
    return list(out)


__all__ = [
    "BUILTIN_FUNCTIONS",
    "BUILTIN_LIBRARIES",
    "CHUNK_OPTIONS",
    "KIND_FUNCTION",
    "LANG_LUA",
    "SITE_CALL",
    "SITE_RULES",
    "LuaUnits",
    "lua_units",
    "parse_lua",
    "reference_sites",
    "resolve_imports",
    "wanted_paths",
]
