"""What a lane without a tree-sitter grammar shares.

The protobuf and Go-template lanes read their files with hand-written
scanners: `ParsedFile.tree` stays None, so `chunker.chunk_file` would fall
back to line windows and `edges_treesitter.extract_edges_and_misses` would
walk no call sites. The two helpers here give such a lane the shape the
grammar lanes get for free:

  * `span_chunks` — chunks cut on the lane's own symbol spans (one unit per
    top-level block, a file header, the leftovers as windows), in the
    chunker's `Chunk` shape and ordinal rule, so the chunk stage writes and
    key-diffs them like any other file's;
  * `bind_sites` — the reference binder: a lane lists (line, name, kind)
    sites, names a rule per kind (edge kind, target symbol kinds, whether the
    repository-unique rung is allowed), and the reference ladder
    binds them — same-file unique at 1.0, repository-unique at 0.6, nothing
    when ambiguous or unknown.

Outside the extractor stamp with the lane modules: neither `chunker.py` nor
`edges_treesitter.py` changes for a new lane.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from server.code.domain.extract.chunker import Chunk, _assign_ordinals, _split_oversized
from server.code.domain.extract.edges_treesitter import REPO_WIDE_CONFIDENCE, Edge
from server.code.domain.extract.treesitter import ParsedFile, Symbol

_MIN_CHUNK_CHARS = 40  # the chunker's floor for a body / leftover chunk
_HEADER_MIN_CHARS = 10  # and its lower floor for a header

Site = tuple[int, str, str]  # (1-indexed line, bare name, site kind)


@dataclass(frozen=True)
class SiteRule:
    """How one kind of site binds: the edge it records, the symbol kinds it
    may land on, and whether a repository-unique definition counts."""

    edge_kind: str
    target_kinds: frozenset[str]
    allow_repo_wide: bool = True


def _top_level(symbols: list[Symbol]) -> list[Symbol]:
    """The symbols no other symbol of the file encloses, in file order."""
    ordered = sorted(symbols, key=lambda s: (s.start_line, -s.end_line))
    out: list[Symbol] = []
    end = 0
    for s in ordered:
        if s.start_line <= end:
            continue  # inside the previous top-level block
        out.append(s)
        end = max(end, s.end_line)
    return out


def _comment_run_above(
    src_lines: list[str],
    start: int,
    leaders: tuple[str, ...],
    blocks: tuple[tuple[str, str], ...] = (),
) -> int:
    """First line (1-indexed) of the comment run directly above `start` —
    line comments by their leaders, block comments by their (opener,
    closer) pairs — or `start` when there is none."""
    i = start - 1  # 1-indexed line above
    while i >= 1:
        s = src_lines[i - 1].strip()
        if not s:
            break
        opener = next((o for o, c in blocks if s.endswith(c)), None)
        if opener is not None:
            j = i
            while j >= 1 and opener not in src_lines[j - 1]:
                j -= 1
            if j < 1:
                break
            i = j - 1
            continue
        if s.startswith(leaders):
            i -= 1
            continue
        break
    return i + 1


def span_chunks(
    pf: ParsedFile,
    max_chars: int = 6000,
    *,
    comment_leaders: tuple[str, ...] = ("//", "/*", "*"),
    comment_blocks: tuple[tuple[str, str], ...] = (("/*", "*/"),),
    header_prefixes: tuple[str, ...] = (),
) -> list[Chunk]:
    """Chunks for a tree-less lane file: one `function` chunk per top-level
    symbol block (its leading comment run attached, oversize blocks split
    on line boundaries), a `file_header` chunk for the leading run of
    header lines (`header_prefixes`, comments, blanks), and every leftover
    run of lines as an `other` chunk. Symbols that sit inside another
    symbol's span (a nested message, an rpc inside a service) ride the
    enclosing block's chunk and are listed in its `symbol_ids`. A block
    comment (`comment_blocks`: opener / closer pairs, `{{/* … */}}` for a
    template) directly above a unit rides the unit like a line comment."""
    text = pf.source.decode("utf-8", "replace")
    src_lines = text.splitlines(keepends=True)
    covered = [False] * (len(src_lines) + 2)
    leader = comment_leaders[0] if comment_leaders else "//"
    chunks = _unit_chunks(
        pf,
        src_lines,
        covered,
        max_chars,
        leader,
        comment_leaders,
        comment_blocks,
    )
    header = _header_chunk(pf, src_lines, covered, (*header_prefixes, *comment_leaders))
    if header is not None:
        chunks.append(header)
    chunks.extend(_leftover_chunks(pf, src_lines, covered, max_chars, leader))
    chunks.sort(key=lambda c: (c.start_line, c.end_line))
    _assign_ordinals(chunks)
    return chunks


def _unit_chunks(
    pf: ParsedFile,
    src_lines: list[str],
    covered: list[bool],
    max_chars: int,
    leader: str,
    comment_leaders: tuple[str, ...],
    comment_blocks: tuple[tuple[str, str], ...],
) -> list[Chunk]:
    """One `function` chunk per top-level symbol block, comment run attached."""
    n = len(src_lines)
    chunks: list[Chunk] = []
    for top in _top_level(pf.symbols):
        start = _comment_run_above(src_lines, top.start_line, comment_leaders, comment_blocks)
        start = max(1, min(start, top.start_line))
        end = min(top.end_line, n)
        if end < start:
            continue
        inner = [
            s.id
            for s in pf.symbols
            if s is not top and top.start_line <= s.start_line and s.end_line <= top.end_line
        ]
        ids = [top.id, *dict.fromkeys(inner)]
        content = "".join(src_lines[start - 1 : end])
        for i in range(start, end + 1):
            covered[i] = True
        header = src_lines[top.start_line - 1]
        pieces = (
            [(content, start, end)]
            if len(content) <= max_chars
            else _split_oversized(content, header, start, max_chars, leader=leader)
        )
        for piece, s_ln, e_ln in pieces:
            chunks.append(
                Chunk(
                    path=pf.path,
                    scope=top.qualified_name,
                    kind="function",
                    start_line=s_ln,
                    end_line=e_ln,
                    content=piece,
                    symbol_ids=ids,
                )
            )
    return chunks


def _header_chunk(
    pf: ParsedFile,
    src_lines: list[str],
    covered: list[bool],
    prefixes: tuple[str, ...],
) -> Chunk | None:
    """The leading run of header / comment / blank lines, stopping at the
    first line a unit already covers (a unit's doc comment is the unit's)."""
    header_end = 0
    for i, ln in enumerate(src_lines):
        s = ln.strip()
        if covered[i + 1]:
            break
        if s == "" or s.startswith(prefixes):
            header_end = i + 1
            if header_end > 200:
                break
        else:
            break
    if not header_end or covered[1]:
        return None
    head = "".join(src_lines[:header_end])
    if len(head.strip()) < _HEADER_MIN_CHARS:
        return None
    for i in range(1, header_end + 1):
        covered[i] = True
    return Chunk(
        path=pf.path, scope="", kind="file_header", start_line=1, end_line=header_end, content=head
    )


def _leftover_chunks(
    pf: ParsedFile,
    src_lines: list[str],
    covered: list[bool],
    max_chars: int,
    leader: str,
) -> list[Chunk]:
    """Every uncovered run of non-blank lines as an `other` chunk."""
    n = len(src_lines)
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for i in range(1, n + 1):
        uncovered = not covered[i] and src_lines[i - 1].strip() != ""
        if uncovered and run_start is None:
            run_start = i
        boundary = (not uncovered) or i == n
        if run_start is not None and boundary:
            runs.append((run_start, i if (uncovered and i == n) else i - 1))
            run_start = None
    chunks: list[Chunk] = []
    for a, b in runs:
        content = "".join(src_lines[a - 1 : b])
        if len(content.strip()) < _MIN_CHUNK_CHARS:
            continue
        pieces = (
            [(content, a, b)]
            if len(content) <= max_chars
            else _split_oversized(content, src_lines[a - 1], a, max_chars, leader=leader)
        )
        for piece, s_ln, e_ln in pieces:
            chunks.append(
                Chunk(
                    path=pf.path,
                    scope="",
                    kind="other",
                    start_line=s_ln,
                    end_line=e_ln,
                    content=piece,
                )
            )
    return chunks


def enclosing_symbol(pf: ParsedFile, line: int) -> Symbol | None:
    """The innermost symbol of any kind whose span holds `line`."""
    best: Symbol | None = None
    for s in pf.symbols:
        if s.start_line <= line <= s.end_line and (
            best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line)
        ):
            best = s
    return best


def bind_sites(
    pf: ParsedFile,
    sites: list[Site],
    name_table: dict[str, list[tuple[str, str, str]]],
    rules: dict[str, SiteRule],
) -> list[Edge]:
    """Edges for the sites under `rules`: from the innermost enclosing
    symbol to the same-file unique definition of a target kind at 1.0,
    else the repository-unique candidate (`name_table`: bare name →
    [(symbol_id, qualified_name, kind)], already gated to the lane's
    language class) at `REPO_WIDE_CONFIDENCE` when the rule allows it,
    else nothing. A site with no enclosing symbol, a name that stays
    ambiguous, a self-reference and a repeat of one (edge, source, target,
    line) record nothing."""
    local: dict[str, list[Symbol]] = {}
    for s in pf.symbols:
        local.setdefault(s.name, []).append(s)
    edges: list[Edge] = []
    seen: set[tuple[str, str, str, int]] = set()
    for line, name, site_kind in sites:
        rule = rules.get(site_kind)
        if rule is None:
            continue
        src = enclosing_symbol(pf, line)
        if src is None:
            continue
        mine = [
            s
            for s in local.get(name, [])
            if s.kind in rule.target_kinds and s.qualified_name != src.qualified_name
        ]
        if len(mine) == 1:
            to_id, to_qn, conf = mine[0].id, mine[0].qualified_name, 1.0
        elif mine:
            continue  # several same-file definitions: ambiguous
        elif rule.allow_repo_wide:
            cands = [c for c in name_table.get(name, []) if c[2] in rule.target_kinds]
            if len({c[1] for c in cands}) != 1:
                continue
            to_id, to_qn, conf = cands[0][0], cands[0][1], REPO_WIDE_CONFIDENCE
        else:
            continue
        if to_qn == src.qualified_name:
            continue
        key = (rule.edge_kind, src.id, to_id, line)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            Edge(rule.edge_kind, src.id, to_id, src.qualified_name, to_qn, pf.path, line, conf)
        )
    return edges


# not a setting: the directories a specifier is tried under, the file's own and its nearest
# ancestors, before the repository root; each adds one candidate per import, so a deep path
# multiplies a file's candidates
MAX_ANCESTOR_DIRS = 32


def ancestor_candidates(from_path: str, spec: str) -> list[str]:
    """The repository paths a root-relative specifier may name when the
    lane's include root is unknown (protobuf's `-I` roots, a chart's
    template directory): the importing file's directory + spec, then each
    ancestor directory + spec, `MAX_ANCESTOR_DIRS` directories in all, then the bare
    spec — nearest first, duplicates dropped."""
    spec = spec.strip().lstrip("/")
    if not spec:
        return []
    bases = (*ancestor_dirs(from_path), "")
    return list(dict.fromkeys(f"{base}/{spec}" if base else spec for base in bases))


@functools.lru_cache(maxsize=256)
def ancestor_dirs(from_path: str) -> tuple[str, ...]:
    """The file's directory and its ancestors, nearest first, `MAX_ANCESTOR_DIRS` of them
    at most counting the file's own, the root left out; read once per file."""
    parts = from_path.split("/")[:-1]
    depths = range(len(parts), max(len(parts) - MAX_ANCESTOR_DIRS, 0), -1)
    return tuple("/".join(parts[:depth]) for depth in depths)


__all__ = [
    "MAX_ANCESTOR_DIRS",
    "Site",
    "SiteRule",
    "ancestor_candidates",
    "ancestor_dirs",
    "bind_sites",
    "enclosing_symbol",
    "span_chunks",
]
