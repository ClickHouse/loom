"""CMake: the targets a tree builds, what each is built from, what it links.

A C++ repository's `CMakeLists.txt` files are the one place that says which
sources form which library or executable, which test binaries exist, and
what depends on what, and they register the gtest binaries with
`gtest_discover_tests`. Here they are symbols and edges:

* ``target`` symbols for `add_library(name …)` / `add_executable(name …)`
  (qualified name = the target's own name — CMake target names are global,
  so `target_link_libraries` in another directory binds by that name),
  signature `library` / `executable` (+ `INTERFACE` / `STATIC` / `SHARED`
  / `ALIAS`), doc the `#` comment run above;
* ``function`` symbols for `function(name …)` / `macro(name …)`;
* ``option`` symbols for `option(NAME "doc" default)`;
* ``test`` symbols for `add_test(NAME x …)`;
* a ``builds`` facet per source of a target — from the `add_*` argument
  list, `target_sources(name … a.cpp b.cpp)`, and `${VAR}` arguments that a
  `file(GLOB VAR *.cpp)` / `set(VAR …)` in the same file defined (a glob
  stays a glob: `src/storage/*.cpp`, confidence 0.9) — the path made
  repository-relative from the CMakeLists' directory, so a reader learns
  which binary a file belongs to by equality or by `fnmatch`;
* a ``tests`` facet on a target named by `gtest_discover_tests(target)` /
  `add_test(… COMMAND target …)`;
* ``uses_type`` edges target → target for `target_link_libraries(A PUBLIC
  B C)` (the dependency graph, `PUBLIC`/`PRIVATE`/`INTERFACE` keywords
  skipped, `Boost::headers`-style imported targets and plain libraries
  left unresolved), cross-file through the namespace name table
  (`ingest._cmake_reference_edges`);
* `add_subdirectory(dir)` → an include target `dir/CMakeLists.txt`;
* one chunk per command that declares something (`class_skeleton` for a
  target with its comment, `function` for a function / macro body, `other`
  for the runs between), the leading `cmake_minimum_required` / `project`
  block as `file_header`.

The reader is a tokenizer for CMake's `command(args)` syntax — quoted
strings, `${VAR}` kept as written, `#` comments, bracket comments dropped.
`.cmake` module files get the same reading. Outside the extractor stamp
with the other lane modules.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile, Symbol

# `\b` changes no match here (a command starts after a `)`, a blank or a comment); it keeps
# the pattern linear when searched from every offset, as the pattern cost test does
_COMMAND_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*+)\s*+\(")

LANG_CMAKE = "cmake"
KIND_TARGET = "target"
KIND_TEST = "test"
KIND_OPTION = "option"
FACET_BUILDS = "builds"
FACET_TESTS = "tests"
EDGE_USES_TYPE = "uses_type"

_TARGET_TYPE_WORDS: frozenset[str] = frozenset(
    {
        "STATIC",
        "SHARED",
        "MODULE",
        "OBJECT",
        "INTERFACE",
        "IMPORTED",
        "GLOBAL",
        "ALIAS",
        "EXCLUDE_FROM_ALL",
        "WIN32",
        "MACOSX_BUNDLE",
    }
)
_VISIBILITY: frozenset[str] = frozenset(
    {"PUBLIC", "PRIVATE", "INTERFACE", "LINK_PUBLIC", "LINK_PRIVATE"}
)
_SOURCE_EXTS: tuple[str, ...] = (
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".hh",
    ".ipp",
    ".inl",
    ".S",
    ".s",
    ".asm",
    ".fbs",
    ".proto",
    ".tla",
    ".cfg",
)
_DECLARING = {"add_library", "add_executable", "function", "macro", "option", "add_test"}
# A `${VAR}` use repeats every entry of VAR: without a cap, uses times entries facets.
# Sources stop being added once a file has this many facets.
_MAX_FACETS = 10_000
_SOURCE_SKIP = frozenset({"TYPE", "BASE_DIRS", "FILES", "HEADERS"})


def is_cmake_path(path: str) -> bool:
    lower = path.lower()
    return lower.endswith("cmakelists.txt") or lower.endswith(".cmake")


@dataclass
class Command:
    name: str
    args: list[str]
    start_line: int
    end_line: int
    doc: str = ""


def _skip_comment(text: str, i: int, doc: list[str]) -> tuple[int, int]:
    """Consume a `#` line comment (recorded as doc) or a `#[[ … ]]` bracket
    comment (dropped) starting at `i`; returns (new index, newlines seen)."""
    n = len(text)
    if text.startswith("#[[", i) or text.startswith("#[=", i):
        end = text.find("]]", i)
        end = n if end < 0 else end + 2
        return end, text.count("\n", i, end)
    end = text.find("\n", i)
    end = n if end < 0 else end
    doc.append(text[i + 1 : end].strip())
    return end, 0


def _read_quoted(text: str, j: int) -> tuple[str, int, int]:
    """A `"…"` argument from the opening quote at `j`; returns (value, index
    after the closing quote, newlines inside)."""
    n = len(text)
    k = j + 1
    buf: list[str] = []
    newlines = 0
    while k < n and text[k] != '"':
        if text[k] == "\\" and k + 1 < n:
            buf.append(text[k + 1])
            k += 2
            continue
        if text[k] == "\n":
            newlines += 1
        buf.append(text[k])
        k += 1
    return "".join(buf), k + 1, newlines


def _read_args(text: str, j: int) -> tuple[list[str], int, int]:
    """The argument tokens of a command whose `(` was just consumed at
    `j - 1`; returns (args, index after the closing `)`, newlines seen)."""
    n = len(text)
    depth = 1
    args: list[str] = []
    tok: list[str] = []
    newlines = 0
    while j < n and depth > 0:
        c = text[j]
        if c == '"':
            val, j, nl = _read_quoted(text, j)
            args.append(val)
            newlines += nl
            continue
        if c == "#":
            k = text.find("\n", j)
            j = n if k < 0 else k
            continue
        if c == "[" and text.startswith("[[", j):
            k = text.find("]]", j)
            j = n if k < 0 else k + 2
            continue
        if c == "\n":
            newlines += 1
        if c == "(":
            depth += 1
            tok.append(c)
        elif c == ")":
            depth -= 1
            if depth == 0:
                if tok:
                    args.append("".join(tok))
                j += 1
                break
            tok.append(c)
        elif c in " \t\r\n":
            if tok:
                args.append("".join(tok))
                tok = []
        else:
            tok.append(c)
        j += 1
    return args, j, newlines


def commands(text: str) -> list[Command]:
    """Every `name(args…)` invocation with its argument tokens (quoted
    strings unquoted, `${VAR}` and generator expressions kept as written),
    the `#` comment run above it as doc. Bracket comments `#[[ … ]]` and
    bracket arguments `[[ … ]]` are skipped."""
    out: list[Command] = []
    i, n, line = 0, len(text), 1
    doc: list[str] = []
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        if ch == "#":
            i, nl = _skip_comment(text, i, doc)
            line += nl
            continue
        m = _COMMAND_RE.match(text, i)  # at i: a slice from i per row is quadratic
        if not m:
            j = text.find("\n", i)
            i = n if j < 0 else j
            doc = []
            continue
        start_line = line
        line += text.count("\n", i, m.end())  # `\s*+` may cross lines before the `(`
        args, j, nl = _read_args(text, m.end())
        line += nl
        out.append(
            Command(
                m.group(1).lower(), args, start_line, line, "\n".join(x for x in doc if x)[:2000]
            )
        )
        doc = []
        i = j
    return out


@dataclass
class CmakeUnits:
    symbols: list[Symbol]
    facets: list[Facet]
    includes: list[str]
    links: list[tuple[str, str, int]]  # (target, linked name, line)
    commands: list[Command]
    header_end: int


def _rel(cm_path: str, arg: str) -> str:
    """A source argument made repository-relative: relative to the
    CMakeLists' directory; `${CMAKE_CURRENT_SOURCE_DIR}/x` the same;
    anything else with `${…}` left as written."""
    a = arg.replace("${CMAKE_CURRENT_SOURCE_DIR}/", "").replace("${CMAKE_CURRENT_LIST_DIR}/", "")
    if "${" in a or a.startswith("$<"):
        return a
    d = posixpath.dirname(cm_path)
    return posixpath.normpath(posixpath.join(d, a)) if d else posixpath.normpath(a)


def _expand(args: list[str], variables: dict[str, list[str]]) -> Iterator[str]:
    """`${VAR}` arguments replaced by what `file(GLOB VAR …)` / `set(VAR …)`
    in the same file gave them (a glob stays a glob, made directory-relative
    by the caller); other arguments pass through. Lazy, so the caller's cap
    stops the expansion too."""
    for a in args:
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", a)
        if m and m.group(1) in variables:
            yield from variables[m.group(1)]
        else:
            yield a


def _is_source(arg: str) -> bool:
    return arg.lower().endswith(tuple(e.lower() for e in _SOURCE_EXTS)) and "$<" not in arg


def _builds(arg: str) -> bool:
    """Whether a source argument gives a `builds` facet: a variable keeps only these, so
    every entry a `${VAR}` use expands to counts toward the cap."""
    if arg in _VISIBILITY or arg.startswith("FILE_SET") or arg in _SOURCE_SKIP:
        return False
    return _is_source(arg) or "*" in arg


@dataclass
class _Acc:
    path: str
    syms: list[Symbol] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    links: list[tuple[str, str, int]] = field(default_factory=list)
    ordinal_of: dict[str, int] = field(default_factory=dict)
    variables: dict[str, list[str]] = field(default_factory=dict)
    # `endfunction` / `endmacro` -> (start line, end line) of each, in file order
    block_ends: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


def _remember_variable(c: Command, acc: _Acc) -> bool:
    """`file(GLOB VAR …)` / `set(VAR …)` → the sources or globs a `${VAR}`
    argument later expands to; True when the command was one."""
    if c.name == "file" and len(c.args) >= 3 and c.args[0] in ("GLOB", "GLOB_RECURSE"):
        recursive = c.args[0] == "GLOB_RECURSE"
        skip = {
            "CONFIGURE_DEPENDS",
            "LIST_DIRECTORIES",
            "RELATIVE",
            "FOLLOW_SYMLINKS",
            "true",
            "false",
        }
        pats = [a for a in c.args[2:] if a not in skip and not a.startswith("${")]
        globs = [("**/" + p if recursive and "/" not in p else p) for p in pats]
        acc.variables[c.args[1]] = [g for g in globs if _builds(g)]
        return True
    if c.name == "set" and len(c.args) >= 2 and not c.args[0].startswith("CMAKE_"):
        acc.variables[c.args[0]] = [a for a in c.args[1:] if _builds(a)]
        return True
    return False


def _add_target(c: Command, acc: _Acc) -> None:
    name = c.args[0]
    words = [a for a in c.args[1:] if a in _TARGET_TYPE_WORDS]
    kind_word = "library" if c.name == "add_library" else "executable"
    sig = " ".join([kind_word, *words]).strip()
    if "ALIAS" in words and len(c.args) >= 3:
        sig = f"{kind_word} ALIAS {c.args[2]}"
        acc.links.append((name, c.args[2], c.start_line))
    acc.syms.append(
        Symbol(name, name, KIND_TARGET, sig, acc.path, c.start_line, c.end_line, doc_comment=c.doc)
    )
    acc.ordinal_of[name] = len(acc.syms)
    _add_sources(name, c.args[1:], c.start_line, acc)


def _add_sources(target: str, args: list[str], line: int, acc: _Acc) -> None:
    for a in _expand(args, acc.variables):
        if len(acc.facets) >= _MAX_FACETS:
            return
        if _builds(a):
            conf = 1.0 if "*" not in a else 0.9
            acc.facets.append(
                Facet(FACET_BUILDS, _rel(acc.path, a), acc.ordinal_of.get(target, 0), line, conf)
            )


def _add_test_symbol(c: Command, acc: _Acc) -> None:
    name = c.args[0]
    if "NAME" in c.args and c.args.index("NAME") + 1 < len(c.args):
        name = c.args[c.args.index("NAME") + 1]
    acc.syms.append(
        Symbol(
            name, name, KIND_TEST, "add_test", acc.path, c.start_line, c.end_line, doc_comment=c.doc
        )
    )
    if "COMMAND" in c.args and c.args.index("COMMAND") + 1 < len(c.args):
        tgt = c.args[c.args.index("COMMAND") + 1]
        if tgt in acc.ordinal_of:
            acc.facets.append(Facet(FACET_TESTS, tgt, acc.ordinal_of[tgt], c.start_line, 1.0))


def _handle(c: Command, acc: _Acc) -> None:
    """One declaring / linking / including command into the accumulator."""
    if c.name in ("add_library", "add_executable") and c.args:
        _add_target(c, acc)
    elif c.name == "target_sources" and c.args:
        _add_sources(c.args[0], c.args[1:], c.start_line, acc)
    elif c.name == "target_link_libraries" and len(c.args) >= 2:
        acc.links.extend(
            (c.args[0], a, c.start_line)
            for a in c.args[1:]
            if a not in _VISIBILITY and "$<" not in a and not a.startswith("-")
        )
    elif c.name in ("function", "macro") and c.args:
        # the first `end…` on a later line; a scan of every later command per block is quadratic
        ends = acc.block_ends.get("end" + c.name, [])
        i = bisect.bisect_right(ends, c.start_line, key=lambda e: e[0])
        end = ends[i][1] if i < len(ends) else c.end_line
        sig = f"{c.name}({' '.join(c.args)})"
        acc.syms.append(
            Symbol(
                c.args[0],
                c.args[0],
                "function",
                sig,
                acc.path,
                c.start_line,
                end,
                doc_comment=c.doc,
            )
        )
    elif c.name == "option" and c.args:
        desc = c.args[1] if len(c.args) > 1 else ""
        default = c.args[2] if len(c.args) > 2 else ""
        acc.syms.append(
            Symbol(
                c.args[0],
                c.args[0],
                KIND_OPTION,
                f"option default {default}".strip(),
                acc.path,
                c.start_line,
                c.end_line,
                doc_comment=(desc or c.doc)[:2000],
            )
        )
    elif c.name == "add_test" and c.args:
        _add_test_symbol(c, acc)
    elif c.name in ("gtest_discover_tests", "gtest_add_tests", "catch_discover_tests") and c.args:
        acc.facets.append(
            Facet(FACET_TESTS, c.args[0], acc.ordinal_of.get(c.args[0], 0), c.start_line, 1.0)
        )
    elif c.name == "add_subdirectory" and c.args and "${" not in c.args[0]:
        acc.includes.append(_rel(acc.path, c.args[0].rstrip("/") + "/CMakeLists.txt"))
    elif (
        c.name == "include"
        and c.args
        and c.args[0].lower().endswith(".cmake")
        and "${" not in c.args[0]
    ):
        acc.includes.append(_rel(acc.path, c.args[0]))


def cmake_units(path: str, text: str) -> CmakeUnits:
    cmds = commands(text)
    acc = _Acc(path)
    header_end = 0
    saw_declaring = False
    src_lines = text.splitlines()
    for c in cmds:
        if c.name in ("endfunction", "endmacro"):
            acc.block_ends.setdefault(c.name, []).append((c.start_line, c.end_line))
    for c in cmds:
        if _remember_variable(c, acc):
            continue
        if not saw_declaring and c.name in _DECLARING:
            saw_declaring = True
            header_end = c.start_line - 1
            # the comment run right above the first declaration is its doc, not header
            while header_end >= 1 and src_lines[header_end - 1].lstrip().startswith("#"):
                header_end -= 1
        _handle(c, acc)
    return CmakeUnits(
        acc.syms, acc.facets, acc.includes, acc.links, cmds, header_end if saw_declaring else 0
    )


def parse_cmake(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_CMAKE, tree=None, source=source)
    units = cmake_units(path, source.decode("utf-8", "replace"))
    pf.symbols = units.symbols
    pf.includes = units.includes
    return pf


def cmake_chunks(pf: ParsedFile) -> list[Chunk]:
    """One chunk per declaring command (with its comment), the runs of
    other commands between them as `other`, the block before the first
    declaration as `file_header`."""
    text = pf.source.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    units = cmake_units(pf.path, text)
    out: list[Chunk] = []
    cursor = 1
    if units.header_end >= 1 and "".join(lines[: units.header_end]).strip():
        out.append(
            Chunk(
                pf.path, "", "file_header", 1, units.header_end, "".join(lines[: units.header_end])
            )
        )
        cursor = units.header_end + 1
    for s in sorted(pf.symbols, key=lambda x: x.start_line):
        start = s.start_line
        # pull the comment run above into the chunk
        while start - 1 >= cursor and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        if start > cursor:
            gap = "".join(lines[cursor - 1 : start - 1])
            if gap.strip():
                out.append(Chunk(pf.path, "", "other", cursor, start - 1, gap))
        if s.end_line < cursor:
            continue
        kind = "function" if s.kind == "function" else "class_skeleton"
        out.append(
            Chunk(
                pf.path,
                s.qualified_name,
                kind,
                max(start, cursor),
                s.end_line,
                "".join(lines[max(start, cursor) - 1 : s.end_line]),
                [s.id],
            )
        )
        cursor = s.end_line + 1
    if cursor <= len(lines) and "".join(lines[cursor - 1 :]).strip():
        out.append(Chunk(pf.path, "", "other", cursor, len(lines), "".join(lines[cursor - 1 :])))
    _assign_ordinals(out)
    return out


def cmake_local_edges(pf: ParsedFile) -> tuple[list[Edge], list[tuple[str, str, int]]]:
    """`uses_type` edges target → target for links resolved in this file;
    the rest (targets declared elsewhere, imported targets, system
    libraries) go to the name table."""
    units = cmake_units(pf.path, pf.source.decode("utf-8", "replace"))
    by_name = {s.name: s for s in pf.symbols if s.kind == KIND_TARGET}
    edges: list[Edge] = []
    external: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for tgt, dep, line in units.links:
        if (tgt, dep) in seen:
            continue
        seen.add((tgt, dep))
        a = by_name.get(tgt)
        b = by_name.get(dep)
        if a is None:
            external.append(
                (tgt, dep, line)
            )  # `target_link_libraries` for a target declared elsewhere
            continue
        if b is None:
            external.append((a.qualified_name, dep, line))
            continue
        edges.append(
            Edge(EDGE_USES_TYPE, a.id, b.id, a.qualified_name, b.qualified_name, pf.path, line, 1.0)
        )
    return edges, external


def cmake_external_edges(
    pf: ParsedFile,
    external: list[tuple[str, str, int]],
    table: dict[str, list[tuple[str, str, str]]],
) -> list[Edge]:
    """Cross-file link edges through the name table: both ends bound by
    target name (exactly one `target` candidate each); the from-target may
    itself live in another CMakeLists (`target_link_libraries` after
    `add_subdirectory`)."""
    by_name = {s.name: s for s in pf.symbols if s.kind == KIND_TARGET}
    out: list[Edge] = []

    def cand(name: str) -> tuple[str, str] | None:
        local = by_name.get(name)
        if local is not None:
            return local.id, local.qualified_name
        c = [x for x in table.get(name, ()) if x[2] == KIND_TARGET]
        return (c[0][0], c[0][1]) if len(c) == 1 else None

    for tgt, dep, line in external:
        a, b = cand(tgt), cand(dep)
        if a is None or b is None or a == b:
            continue
        out.append(Edge(EDGE_USES_TYPE, a[0], b[0], a[1], b[1], pf.path, line, 0.9))
    return out


__all__ = [
    "EDGE_USES_TYPE",
    "FACET_BUILDS",
    "FACET_TESTS",
    "KIND_OPTION",
    "KIND_TARGET",
    "KIND_TEST",
    "LANG_CMAKE",
    "CmakeUnits",
    "Command",
    "cmake_chunks",
    "cmake_external_edges",
    "cmake_local_edges",
    "cmake_units",
    "commands",
    "is_cmake_path",
    "parse_cmake",
]
