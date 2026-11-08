"""Jsonnet: top-level bindings and object members are symbols, imports are includes.

A file class a platform repository writes by hand: `.jsonnet` /
`.libsonnet` (Grafana dashboards, migration jobs). Jsonnet is a small
expression language; what an agent asks of it is where a helper is
defined, what a file imports and which files import a library — all of
which sit at the top level:

  * `local name = …;` / `local name(params) = …;` at column 0 → a `var`
    (a `function` when it takes parameters), qualified under the file's
    dotted path;
  * the root object's members `name: …`, `name:: …`, `name(params): …` →
    a `field` (a `function` when it takes parameters); hidden (`::`)
    members carry `hidden` in the signature; a member whose value is an
    object literal has its own members indexed as `outer.inner`, three
    levels deep — computed keys, object `local`s and
    comprehensions are skipped;
  * `import 'file.libsonnet'` / `importstr 'x.txt'` → `includes` to the
    repository file (relative to the importing file, else the same
    specifier from the tree root — jsonnet's `-J` roots are unknown to
    the index), settled against the namespace's live paths;
  * `alias.member(…)` where `alias` is `local alias = import '…'` → a
    `calls` site on `member`, bound by `lane_units.bind_sites` to a
    `function` / `field` of that bare name (same file 1.0, repository-
    unique 0.6).

Chunks come from the symbol spans (a top-level binding to its `;`, a member
to the next member). Outside the extractor stamp with the other lanes.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_JSONNET = "jsonnet"

SITE_CALL = "call"
SITE_RULES: dict[str, SiteRule] = {
    SITE_CALL: SiteRule("calls", frozenset({"function", "field"})),
}
CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("//", "#", "/*", "*"),
    "comment_blocks": (("/*", "*/"),),
    "header_prefixes": ("local ",),
}

_LOCAL_RE = re.compile(r"^local\s+([A-Za-z_][\w]*)\s*+(\([^)]*\))?\s*+=")
_IMPORT_RE = re.compile(r"import(?:str|bin)?\s*['\"]([^'\"\n]+)['\"]")
_ALIAS_IMPORT_RE = re.compile(r"^local\s+([A-Za-z_]\w*)\s*=\s*import\s*['\"]([^'\"\n]+)['\"]")
_MEMBER_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
# An unclosed `/*` runs to the end, as the lexer reads it, instead of being searched
# again from every later one.
_COMMENT_RE = re.compile(r"//[^\n]*|#[^\n]*|/\*.*?(?:\*/|\Z)", re.S)
# A string may hold a newline, and an unclosed one runs to the end, as the lexer reads
# it: retried from each quote it holds, it would scan the rest from every one.
_STRING_RE = re.compile(
    r"@'(?:[^']|'')*+'?|@\"(?:[^\"]|\"\")*+\"?"  # verbatim: a backslash is itself
    r"|'(?:[^'\\]|\\.)*+'?|\"(?:[^\"\\]|\\.)*+\"?|\|\|\|.*?\|\|\|",
    re.S,
)
# One pass, strings first: a `//` or `#` inside a string is not a comment, and a quote
# inside a comment opens no string.
_BLANK_RE = re.compile(_STRING_RE.pattern + "|" + _COMMENT_RE.pattern, re.S)


@dataclass
class JsonnetUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


def _blank_comments_and_strings(text: str) -> str:
    """The text with comments and string bodies blanked (newlines kept), so
    braces and keywords inside them do not count."""

    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return _BLANK_RE.sub(blank, text)


def _root_object_pos(code: str, from_line: int = 1) -> int | None:
    """Offset of the file's root `{` — the first depth-0 `{` at or after
    `from_line` (the line after the last top-level local, whose own braces
    are not the root's) — None without one."""
    depth = 0
    line = 1
    for i, ch in enumerate(code):
        if ch == "\n":
            line += 1
        elif line < from_line:
            continue
        elif ch in "{([":
            if depth == 0 and ch == "{":
                return i
            depth += 1
        elif ch in "})]":
            depth -= 1
    return None


def _binding_end(code_lines: list[str], start: int) -> int:
    """The line index where a top-level `local … = …;` ends: the first
    depth-0 `;` at or after its `=` (brackets opened by the value close
    first), else the last line."""
    depth = 0
    for j in range(start, len(code_lines)):
        text = code_lines[j].split("=", 1)[-1] if j == start else code_lines[j]
        for ch in text:
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
            elif ch == ";" and depth <= 0:
                return j
    return len(code_lines) - 1  # unterminated: it runs to the end, as the lexer reads it


def _top_level_locals(
    lines: list[str],
    code_lines: list[str],
    add: Any,
    aliases: dict[str, str],
) -> int:
    """Every column-0 `local … = …;` as a symbol; returns the line after the
    last one (where the root object may begin)."""
    depth = 0
    i = 0
    after_locals = 1
    while i < len(lines):
        cl = code_lines[i]
        m = _LOCAL_RE.match(cl) if depth == 0 else None
        if m:
            name, params = m.group(1), m.group(2)
            end = _binding_end(code_lines, i)
            real = _LOCAL_RE.match(lines[i])  # the params as written (strings intact)
            shown = real.group(2) if real and real.group(2) else params
            add(name, "function" if params else "var", f"local {name}{shown or ''}", i + 1, end + 1)
            am = _ALIAS_IMPORT_RE.match(lines[i].strip())
            if am:
                aliases[am.group(1)] = am.group(2)
            i = end + 1
            after_locals = i + 1
            continue
        for ch in cl:
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
        i += 1
    return after_locals


_KEY_RE = re.compile(r"((?<!\w)[A-Za-z_][\w]*|'[^']*'|\"[^\"]*\")\s*+(\([^()]*\))?\s*+(::?)(?!:)")
_MAX_MEMBER_DEPTH = 3


def _line_of(offset: int, line_starts: list[int]) -> int:
    """1-based line of a text offset (`line_starts`: the offset of each line)."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _object_members(
    text: str,
    lines: list[str],
    code: str,
    line_starts: list[int],
    open_pos: int,
    prefix: str,
    level: int,
    add: Any,
) -> int:
    """The members of the object whose `{` sits at `open_pos`, as symbols named
    `<prefix><key>`; a member whose value is an object literal has its members indexed
    too, up to `_MAX_MEMBER_DEPTH`. Computed keys, `local`s and comprehension clauses
    are skipped — a key is read only where the grammar puts one: after `{`, `,` or `;`
    at the object's own depth. Returns the offset of the closing `}`."""
    depth = 0
    expect_key = True
    i = open_pos
    members: list[tuple[int, str, str, str, int | None]] = []  # (pos, key, kind, sig, value_pos)
    bounds: list[int] = []  # every position a key could start at, member or not
    close = len(code)
    while i < len(code):
        ch = code[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
            if depth == 0:
                close = i
                break
        elif depth == 1 and ch in ",;":
            expect_key = True
        elif depth == 1 and expect_key and not ch.isspace():
            expect_key = False
            bounds.append(i)
            m = _KEY_RE.match(text, i)
            if m:
                key = m.group(1).strip("'\"")
                params, hidden = m.group(2), m.group(3) == "::"
                sig = f"{key}{params or ''}{' hidden' if hidden else ''}"
                j = m.end()
                while j < len(code) and code[j] in " \t":
                    j += 1
                value_pos = j if j < len(code) and code[j] == "{" else None
                members.append((i, key, "function" if params else "field", sig, value_pos))
                i = m.end()
                continue
        i += 1
    for pos, key, kind, sig, value_pos in members:
        start = _line_of(pos, line_starts)
        # the member ends before the next key position — a computed key or
        # an object `local` bounds it too — else before the object's closer
        k = bisect.bisect_right(bounds, pos)
        nxt = bounds[k] if k < len(bounds) else close
        end = _line_of(nxt, line_starts)
        if end > start:
            end -= 1  # the next member's line (or the closer's) is not this member's
            while end > start and (
                not lines[end - 1].strip() or lines[end - 1].lstrip().startswith(("//", "#"))
            ):
                end -= 1
        add(f"{prefix}{key}", kind, sig, start, max(end, start))
        if value_pos is not None and level < _MAX_MEMBER_DEPTH:
            _object_members(
                text, lines, code, line_starts, value_pos, f"{prefix}{key}.", level + 1, add
            )
    return close


def jsonnet_units(path: str, text: str) -> JsonnetUnits:
    units = JsonnetUnits()
    mod = module_scope(path)
    code = _blank_comments_and_strings(text)
    lines = text.split("\n")
    code_lines = code.split("\n")

    def add(name: str, kind: str, sig: str, start: int, end: int) -> None:
        units.symbols.append(
            Symbol(
                qualified_name=f"{mod}::{name}",
                name=name,
                kind=kind,
                signature=sig,
                path=path,
                start_line=start,
                end_line=max(end, start),
            )
        )

    aliases: dict[str, str] = {}
    after_locals = _top_level_locals(lines, code_lines, add, aliases)
    root = _root_object_pos(code, after_locals)
    if root is not None:
        line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        _object_members(text, lines, code, line_starts, root, "", 1, add)
    # imports (from the real text: the specifier is a string)
    for m in _IMPORT_RE.finditer(text):
        units.imports.append(m.group(1))
    # member calls through an import alias
    for j, cl in enumerate(code_lines):
        for m in _MEMBER_CALL_RE.finditer(cl):
            if m.group(1) in aliases:
                units.sites.append((j + 1, m.group(2), SITE_CALL))
    return units


def parse_jsonnet(path: str, source: bytes) -> ParsedFile:
    units = jsonnet_units(path, source.decode("utf-8", "replace"))
    pf = ParsedFile(path=path, lang=LANG_JSONNET, source=source)
    pf.symbols = units.symbols
    pf.includes = list(dict.fromkeys(units.imports))
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    return jsonnet_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """The importing file's directory first, then the tree root."""
    out: list[str] = []
    here = posixpath.dirname(from_path)
    for spec in specs:
        rel = posixpath.normpath(posixpath.join(here, spec)).strip("/")
        out.append(rel)
        out.append(spec.strip("/"))
    return list(dict.fromkeys(out))


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    out: list[str] = []
    here = posixpath.dirname(from_path)
    for spec in specs:
        for cand in (posixpath.normpath(posixpath.join(here, spec)).strip("/"), spec.strip("/")):
            if cand in present and cand != from_path:
                out.append(cand)
                break
    return list(dict.fromkeys(out))


__all__ = [
    "CHUNK_OPTIONS",
    "LANG_JSONNET",
    "SITE_RULES",
    "JsonnetUnits",
    "jsonnet_units",
    "parse_jsonnet",
    "reference_sites",
    "resolve_imports",
    "wanted_paths",
]
