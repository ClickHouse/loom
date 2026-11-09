"""Design documents in a code repository, read by their headings.

A repository's Markdown — `docs/architecture.md`, `commit-protocol.md`, `recovery.md`, a
glossary whose naming invariants govern the codebase, proposals and reviews — says what
the code is for. Chunked as line windows with no symbols, an agent asking "where is the
durable-LSN watermark advanced" cannot be pointed at the section that defines it, and
a function's blast radius never names the document that specifies it. Here a Markdown
file is:

* a ``section`` symbol per heading, qualified name `<module>::<H1>::<H2>…`
  from the heading text (the file's `module_scope`, headings as written),
  signature the `#` line, span from the heading to the next heading of the
  same or a higher level, doc the first paragraph; a file with a single H1
  is that section's parent;
* one chunk per section (`comment_block` — the kind the enum has for
  prose, with the section's symbol id), the front matter / preamble before
  the first heading as `file_header`; a long section is split at its
  paragraph boundaries under `max_chars`, every piece carrying the heading
  path as its first line so no piece floats context-free (the same rule
  as code chunks);
* relative links to other repository files (`[x](../src/foo.cpp)`,
  `(recovery.md#replay)`) → include targets made repository-relative; the
  anchor dropped;
* ``reads`` edges section → code symbol for every identifier a section
  names in backticks (`` `durableLsn` ``, `` `resolveCommitTime` ``,
  `` `RowBlockEntry` ``) that the namespace defines exactly once, through
  the name table (`ingest._doc_reference_edges`) — the edge kind the enum
  has for "consumes without calling"; `code.callers(X)` and the blast
  radius then answer "documented in commit-protocol.md § Prepare" beside
  the code callers.

Fenced code blocks stay inside their section's chunk (their language is
noted in the piece); `.rst` keeps the line-window reading. Outside the
extractor stamp with the other lane modules.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_DOC = "doc"
KIND_SECTION = "section"
EDGE_READS = "reads"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE = re.compile(r"^(```|~~~)")
# A section repeats its ancestors' titles in its qualified name and heading path: the
# titles and both are cut, so a section costs at most this much whatever its depth.
_MAX_PATH = 1000
_LINK_RE = re.compile(r"\]\(((?:(?!\]\()[^)\s])++)(?:\s+\"[^\"]*\")?\)")
_CODE_REF_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)(?:\(\))?`")
_NOISE_REFS: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "null",
        "int",
        "uint64_t",
        "int64_t",
        "size_t",
        "void",
        "bool",
        "char",
        "auto",
        "std",
        "const",
        "static",
        "struct",
        "class",
        "enum",
        "namespace",
        "template",
        "return",
        "if",
        "else",
        "for",
        "while",
        "TODO",
        "FIXME",
        "NOTE",
        "master",
        "main",
        "src",
        "docs",
        "tla",
        "build",
        "cmake",
        "ninja",
        "clang",
        "gtest",
        "ctest",
        "git",
        "bb",
        "debug",
        "release",
        "TLC",
        "TLA",
    }
)


def is_markdown_path(path: str) -> bool:
    # `.mdc`: an editor rule file is Markdown with a front-matter block (the language
    # detection table names it a doc)
    return path.lower().endswith((".md", ".markdown", ".mdc"))


@dataclass
class Section:
    level: int
    title: str
    start_line: int
    end_line: int
    path_titles: list[str] = field(default_factory=list)  # ancestors + self
    first_paragraph: str = ""


def sections_of(text: str) -> tuple[list[Section], int]:
    """(sections, preamble_end) — headings outside fenced blocks; a section
    runs to the line before the next heading of the same or a higher level;
    `preamble_end` is the last line before the first heading (0 if none)."""
    lines = text.splitlines()
    heads: list[tuple[int, int, str]] = []
    fence = ""
    for i, raw in enumerate(lines, start=1):
        m = _FENCE_RE.match(raw)
        if m:
            fence = "" if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            continue
        h = _HEADING_RE.match(raw)
        if h:
            # The closing `#`s off in Python: a pattern that matches them after a lazy
            # title backtracks cubically on a run of spaces.
            rest = h.group(2)
            # `or`: a rest of only `#`s keeps its first, as the old pattern's title did
            title = rest.rstrip().rstrip("#").rstrip() or rest[:1]
            heads.append((i, len(h.group(1)), title.strip()[:_MAX_PATH].rstrip()))
    out: list[Section] = []
    stack: list[str] = []
    for n, (line, level, title) in enumerate(heads):
        end = len(lines)
        for k in range(n + 1, len(heads)):  # by index: a slice per heading is quadratic
            if heads[k][1] <= level:
                end = heads[k][0] - 1
                break
        stack = stack[: level - 1] + [""] * max(0, level - 1 - len(stack)) + [title]
        # first paragraph: the first non-blank, non-fence run after the heading
        para: list[str] = []
        for raw in lines[line:end]:
            s = raw.strip()
            if not s:
                if para:
                    break
                continue
            if _FENCE_RE.match(s) or _HEADING_RE.match(raw):
                break
            para.append(s)
        out.append(Section(level, title, line, end, [t for t in stack if t], " ".join(para)[:600]))
    preamble_end = heads[0][0] - 1 if heads else 0
    return out, preamble_end


def _qname(mod: str, sec: Section) -> str:
    name = "::".join([mod, *sec.path_titles])
    if len(name) <= _MAX_PATH:
        return name
    # cut, with a hash of the whole so two cut names stay distinct
    return name[: _MAX_PATH - 17] + "~" + hashlib.sha1(name.encode()).hexdigest()[:16]


def markdown_units(
    path: str, text: str
) -> tuple[list[Symbol], list[str], list[tuple[str, str, int]], int]:
    """(symbols, includes, refs, preamble_end); refs are (section qualified
    name, backticked identifier, line)."""
    mod = module_scope(path)
    secs, preamble_end = sections_of(text)
    syms = [
        Symbol(
            _qname(mod, s),
            s.title,
            KIND_SECTION,
            "#" * s.level + " " + s.title,
            path,
            s.start_line,
            s.end_line,
            doc_comment=s.first_paragraph,
        )
        for s in secs
    ]
    includes: list[str] = []
    seen_includes: set[str] = set()
    refs: list[tuple[str, str, int]] = []
    lines = text.splitlines()
    d = posixpath.dirname(path)
    fence = ""
    by_start = {s.start_line: i for i, s in enumerate(secs)}
    current_q = ""
    for i, raw in enumerate(lines, start=1):
        if i in by_start:
            current_q = syms[by_start[i]].qualified_name
        m = _FENCE_RE.match(raw)
        if m:
            fence = "" if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            continue
        for lm in _LINK_RE.finditer(raw):
            target = lm.group(1).split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            rel = posixpath.normpath(posixpath.join(d, target)) if d else posixpath.normpath(target)
            if not rel.startswith("..") and rel not in seen_includes:
                seen_includes.add(rel)
                includes.append(rel)
        if current_q:
            seen: set[str] = set()
            for rm in _CODE_REF_RE.finditer(raw):
                name = rm.group(1)
                bare = name.rsplit("::", 1)[-1]
                if bare in _NOISE_REFS or len(bare) < 3 or bare in seen:
                    continue
                seen.add(bare)
                refs.append((current_q, name, i))
    return syms, includes, refs, preamble_end


def parse_markdown(path: str, source: bytes) -> ParsedFile:
    pf = ParsedFile(path=path, lang=LANG_DOC, tree=None, source=source)
    syms, includes, _refs, _pre = markdown_units(path, source.decode("utf-8", "replace"))
    pf.symbols = syms
    pf.includes = includes
    return pf


def markdown_chunks(pf: ParsedFile, max_chars: int = 6000) -> list[Chunk]:
    text = pf.source.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    secs, preamble_end = sections_of(text)
    mod = module_scope(pf.path)
    by_q = {s.qualified_name: s for s in pf.symbols}
    out: list[Chunk] = []
    if preamble_end >= 1 and "".join(lines[:preamble_end]).strip():
        out.append(
            Chunk(pf.path, "", "file_header", 1, preamble_end, "".join(lines[:preamble_end]))
        )
    # leaf-level pieces: a section's own lines up to its first child heading
    for n, s in enumerate(secs):
        own_end = s.end_line
        if n + 1 < len(secs) and secs[n + 1].start_line <= s.end_line:
            own_end = secs[n + 1].start_line - 1
        body = lines[s.start_line - 1 : own_end]
        sym = by_q.get(_qname(mod, s))
        heading_path = " › ".join(s.path_titles)[:_MAX_PATH]
        pieces = _split_paragraphs(body, max_chars, s.start_line)
        for k, (p_start, p_end, p_text) in enumerate(pieces):
            content = p_text if k == 0 else f"<!-- {heading_path} (cont.) -->\n" + p_text
            out.append(
                Chunk(
                    pf.path,
                    sym.qualified_name if sym else "",
                    "comment_block",
                    p_start,
                    p_end,
                    content,
                    [sym.id] if sym and k == 0 else [],
                )
            )
    _assign_ordinals(out)
    return out


def _split_paragraphs(
    body: list[str], max_chars: int, first_line: int
) -> list[tuple[int, int, str]]:
    """Split a section's lines at blank lines so no piece exceeds
    `max_chars`; a single over-long paragraph stays one piece."""
    text = "".join(body)
    if len(text) <= max_chars or len(body) <= 1:
        return [(first_line, first_line + len(body) - 1, text)]
    out: list[tuple[int, int, str]] = []
    cur: list[str] = []
    cur_start = first_line
    size = 0
    for i, ln in enumerate(body):
        if size + len(ln) > max_chars and cur and not ln.strip():
            out.append((cur_start, first_line + i - 1, "".join(cur)))
            cur, size, cur_start = [], 0, first_line + i
        cur.append(ln)
        size += len(ln)
    if cur:
        out.append((cur_start, first_line + len(body) - 1, "".join(cur)))
    # a paragraph, table or list longer than the cap on its own: split at lines
    final: list[tuple[int, int, str]] = []
    for a, b, t in out:
        if len(t) <= max_chars * 1.5 or b <= a:
            final.append((a, b, t))
            continue
        seg = body[a - first_line : b - first_line + 1]
        acc: list[str] = []
        acc_start = a
        size = 0
        for k, ln in enumerate(seg):
            if size + len(ln) > max_chars and acc:
                final.append((acc_start, a + k - 1, "".join(acc)))
                acc, size, acc_start = [], 0, a + k
            acc.append(ln)
            size += len(ln)
        if acc:
            final.append((acc_start, b, "".join(acc)))
    return final


def doc_reference_sites(pf: ParsedFile) -> list[tuple[str, str, int]]:
    _syms, _inc, refs, _pre = markdown_units(pf.path, pf.source.decode("utf-8", "replace"))
    return refs


_CODE_KINDS: frozenset[str] = frozenset(
    {
        "function",
        "class",
        "struct",
        "enum",
        "union",
        "operator",
        "constant",
        "variable",
        "module",
        "target",
        "type",
        "trait",
        "const",
        "macro",
        "service",
    }
)


def doc_reference_edges(
    pf: ParsedFile,
    sites: list[tuple[str, str, int]],
    table: dict[str, list[tuple[str, str, str]]],
) -> list[Edge]:
    """`reads` edges section → definition for the backticked names the
    namespace defines exactly once among code kinds; a qualified mention
    (`A::b`) must match the candidate's qualified name suffix."""
    by_q = {s.qualified_name: s for s in pf.symbols}
    out: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for from_q, name, line in sites:
        a = by_q.get(from_q)
        if a is None or (from_q, name) in seen:
            continue
        bare = name.rsplit("::", 1)[-1]
        cands = [c for c in table.get(bare, ()) if c[2] in _CODE_KINDS]
        if "::" in name:
            cands = [c for c in cands if c[1].endswith(name)]
        if len(cands) != 1:
            continue
        seen.add((from_q, name))
        sid, qname, _k = cands[0]
        out.append(Edge(EDGE_READS, a.id, sid, a.qualified_name, qname, pf.path, line, 0.7))
    return out


__all__ = [
    "EDGE_READS",
    "KIND_SECTION",
    "LANG_DOC",
    "Section",
    "doc_reference_edges",
    "doc_reference_sites",
    "is_markdown_path",
    "markdown_chunks",
    "markdown_units",
    "parse_markdown",
    "sections_of",
]
