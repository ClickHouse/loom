"""AST-aligned chunking: whole semantic units, never a cut window.

Chunk rules (quality lever: a chunk is always a syntactically complete unit
the model can reason about, plus a file:line pointer to verify):

  * every function definition  -> one `function` chunk (whole body)
  * every class/struct         -> one `class_skeleton` chunk: the class text
    with method BODIES elided to `{ ... }` (methods get their own chunks)
  * leading comments+includes  -> one `file_header` chunk
  * anything left at top level -> grouped `other` chunks
  * a function larger than max_chars splits at top-level statement
    boundaries inside its body (the AST's fresh-line child starts), each
    piece re-prefixed with the signature header so no piece ever floats
    without its declaration. A single statement that is itself over the
    budget is refined the same way one level down; only a run with no
    finer structure falls back to lines, and only a single over-budget
    LINE is cut mid-line (with an elision marker). Every piece is
    therefore at most `max_chars` long; a plain line-count split lets a
    10-line lookup table reach 9,337 chars against a 6,000-char cap.

Unparsed files (configs, docs) fall back to line-window chunks —
still whole lines, kind='other', cut early once a window reaches
`max_chars` (a 120-line window can otherwise reach 14,830 chars).

chunk identity: uuid5("loom://code/{namespace}/{path}#{scope}@{sha16}") —
deterministic and content-addressed, so an unchanged chunk re-ingests to
the same id+hash and the embed step skips it without a lookup. Two chunks
of one file with identical scope AND content (a repeated `#undef` block, a
copy-pasted `other` run) would collapse into one row under the RMT key, so
the second and later occurrences carry an ordinal suffix `#n` in the key.
Ordinal 0 has no suffix.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.tokenize import lexemes_text
from server.code.domain.extract.treesitter import ParsedFile, Symbol

_FALLBACK_WINDOW_LINES = 120
_MIN_CHUNK_CHARS = 40
# Comment leader per language for the `continued from` / elision markers.
_COMMENT_LEADER = {
    "cpp": "//", "python": "#", "shell": "#",
    "typescript": "//", "javascript": "//", "rust": "//", "prisma": "//", "css": "/*",
    "go": "//", "java": "//",
}
# Symbol kinds chunked as skeletons (bodies of inner functions elided)
# BESIDE class / struct, per language. C++ and Python have none here: a C++
# `enum` or a Python module constant is not chunked as a container.
_LANE_CONTAINER_KINDS: dict[str, frozenset[str]] = {
    "typescript": frozenset({"interface", "type", "enum", "const", "module", "suite"}),
    "javascript": frozenset({"const", "module", "suite"}),
    "rust": frozenset({"enum", "trait", "impl", "type", "const", "module", "macro"}),
    "prisma": frozenset({"model", "enum"}),
    "go": frozenset({"interface", "type"}),  # struct is a base kind
    "java": frozenset({"interface", "enum", "record"}),
}
_BASE_CONTAINER_KINDS: frozenset[str] = frozenset({"class", "struct"})


def container_kinds(lang: str) -> frozenset[str]:
    return _BASE_CONTAINER_KINDS | _LANE_CONTAINER_KINDS.get(lang, frozenset())


# Per-language test for a line that belongs to the file header (the leading
# run of comments, includes, imports and `use` lines), for the languages
# whose header lines C++ and the script languages' tuples do not cover.
_TS_HEADER_PREFIXES: tuple[str, ...] = ("//", "/*", "*", "import ", "'use ", '"use ')
_RUST_HEADER_PREFIXES: tuple[str, ...] = (
    "//", "/*", "*", "use ", "pub use ", "pub(crate) use ", "pub(super) use ",
    "extern crate ", "#[", "#!",
)
_CSS_HEADER_PREFIXES: tuple[str, ...] = (
    "/*", "*", "@import", "@tailwind", "@use", "@layer", "@plugin",
)


def _is_header_line(lang: str, s: str) -> bool:
    if lang in {"typescript", "javascript"}:
        return s.startswith(_TS_HEADER_PREFIXES) or (s.startswith("export ") and " from " in s)
    if lang == "rust":
        return s.startswith(_RUST_HEADER_PREFIXES) or (
            s.startswith(("mod ", "pub mod ", "pub(crate) mod ")) and s.endswith(";")
        )
    if lang == "css":
        return s.startswith(_CSS_HEADER_PREFIXES)
    return False


@dataclass
class Chunk:
    path: str
    scope: str  # enclosing qualified name ('' for file-level)
    kind: str  # function | class_skeleton | comment_block | file_header | other
    start_line: int  # 1-indexed inclusive
    end_line: int
    content: str
    symbol_ids: list[str] = field(default_factory=list)
    # 0 for the first chunk with this (scope, content) in the file (no key
    # suffix); n >= 1 for later identical chunks so they survive the RMT key
    # as their own rows.
    ordinal: int = 0
    # Space-joined names the lexical leg should see that the body does not
    # carry (the callee outline's caller names). Empty by default; placed
    # before the content so the 512-token cap never drops them on a long unit.
    extra_lexemes: str = ""

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8", "replace")).hexdigest()

    def chunk_id(self, namespace: str) -> str:
        key = f"loom://code/{namespace}/{self.path}#{self.scope}@{self.content_hash[:16]}"
        if self.ordinal:
            key += f"#{self.ordinal}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    @property
    def lexemes(self) -> str:
        if self.extra_lexemes:
            return lexemes_text(f"{self.path} {self.scope} {self.extra_lexemes} {self.content}")
        return lexemes_text(f"{self.path} {self.scope} {self.content}")


def _split_oversized(
    content: str, header: str, start_line: int, max_chars: int, *,
    node: Any = None, doc: str = "", leader: str = "//",
    cut_rows: set[int] | None = None,
) -> list[tuple[str, int, int]]:
    """Split an oversized unit into pieces of at most `max_chars`, cutting
    only between whole statements where the AST allows it.

    `content` is the unit's own text (NOT including `doc`); its first line is
    absolute line `start_line`. `node` is the tree-sitter node of the unit
    (or its body); cuts are allowed at rows where one of the node's children
    starts a fresh line. A run between two allowed cuts that does not fit an
    empty piece is refined through the child covering it; a run with no
    finer structure is cut by lines; a single line longer than the budget is
    cut mid-line with an elision marker. `cut_rows` (absolute 0-based rows)
    is the same information precomputed for text whose lines no longer map
    to the AST one-to-one (class skeletons).

    Every piece after the first is re-prefixed with the unit's header so no
    piece floats context-free; `doc` is prepended to the first. Returns
    (text, start_line, end_line) tuples covering `content` in order.
    """
    lines = content.splitlines(keepends=True)
    if not lines:
        return []
    prefix = f"{leader} … continued from {header.strip()[:160]}\n"
    doc_prefix = doc + "\n" if doc else ""
    budget = max(max_chars - len(prefix), max_chars // 2, 1)
    # The doc comment rides the first piece; a doc that would leave that
    # piece less than a quarter of the cap is dropped from the pieces (it
    # stays on the symbol row) rather than starving them.
    first_budget = max_chars - len(doc_prefix)
    if first_budget < max_chars // 4:
        doc_prefix = ""
        first_budget = max_chars
    base_row = start_line - 1

    def seg_fn(n: Any, lo: int, hi: int) -> list[tuple[int, int, Any]]:
        if n is None:
            if cut_rows is None:
                return []
            rows = sorted(r - base_row for r in cut_rows if lo < r - base_row < hi)
            return _runs(lo, hi, [(r, None) for r in rows])
        return _child_segments(n, lo, hi, base_row)

    ranges = _pack_rows(lines, 0, len(lines), node, budget, seg_fn)
    if ranges and ranges[0][0] == "rows" and first_budget < budget:
        _kind, a, b = ranges[0]
        if _size(lines, a, b) > first_budget:
            # re-pack only the first piece's rows under the doc-reduced budget
            ranges = _pack_rows(lines, a, b, node, first_budget, seg_fn) + ranges[1:]
    pieces: list[tuple[str, int, int]] = []
    for kind, a, b in ranges:
        if kind == "rows":
            texts = ["".join(lines[a:b])]
        else:  # one over-budget line, cut mid-line
            texts = _cut_line(lines[a], budget, leader)
        for text in texts:
            if not pieces:
                text = doc_prefix + text
            else:
                text = prefix + text
            pieces.append((text, start_line + a, start_line + b - 1))
    return pieces


def _runs(
    lo: int, hi: int, starts: list[tuple[int, Any]]
) -> list[tuple[int, int, Any]]:
    """[lo, hi) cut at the given (row, child) starts -> (a, b, child) runs.
    The first run carries no child unless a start sits exactly at `lo`."""
    out: list[tuple[int, int, Any]] = []
    cur, cur_child = lo, None
    for row, child in starts:
        if row <= lo or row >= hi:
            if row == lo:
                cur_child = child
            continue
        if row > cur:
            out.append((cur, row, cur_child))
        cur, cur_child = row, child
    if cur < hi:
        out.append((cur, hi, cur_child))
    return out


def _child_segments(
    node: Any, lo: int, hi: int, base_row: int
) -> list[tuple[int, int, Any]]:
    """Rows in [lo, hi) (relative to `base_row`) cut where a child of `node`
    starts a fresh line — i.e. the previous sibling ended on an earlier row,
    so the cut never lands inside another construct on the same line."""
    starts: list[tuple[int, Any]] = []
    prev_end = -1
    for child in node.children:
        r = child.start_point[0] - base_row
        if r > prev_end and lo <= r < hi:
            starts.append((r, child))
        prev_end = max(prev_end, child.end_point[0] - base_row)
    return _runs(lo, hi, starts)


def _size(lines: list[str], a: int, b: int) -> int:
    return sum(len(ln) for ln in lines[a:b])


def _pack_rows(
    lines: list[str], lo: int, hi: int, node: Any, budget: int, seg_fn: Any,
) -> list[tuple[str, int, int]]:
    """Cut [lo, hi) into pieces of at most `budget` chars.

    Two passes. First the rows are flattened into atoms: the runs
    `seg_fn(node, lo, hi)` yields, except that a run over the budget is
    refined one AST level down (through the child that starts it) and a run
    with no finer structure becomes single lines. Atoms deeper than the body
    therefore exist only inside a statement that could not fit a piece on
    its own. Then one greedy pass packs consecutive atoms while they fit —
    so a signature line joins the first sub-piece of a huge initializer
    instead of becoming a 60-char piece of its own. Returns ("rows", a, b)
    row ranges and ("line", a, a+1) for single lines that must be cut
    mid-line."""
    atoms = _atoms(lines, lo, hi, node, budget, seg_fn)
    out: list[tuple[str, int, int]] = []
    cur_a: int | None = None
    cur_size = 0
    for kind, a, b in atoms:
        if kind == "line":
            if cur_a is not None:
                out.append(("rows", cur_a, a))
                cur_a, cur_size = None, 0
            out.append(("line", a, b))
            continue
        sz = _size(lines, a, b)
        if cur_a is None:
            cur_a, cur_size = a, sz
        elif cur_size + sz <= budget:
            cur_size += sz
        else:
            out.append(("rows", cur_a, a))
            cur_a, cur_size = a, sz
    if cur_a is not None:
        out.append(("rows", cur_a, hi))
    return out


def _atoms(
    lines: list[str], lo: int, hi: int, node: Any, budget: int, seg_fn: Any,
) -> list[tuple[str, int, int]]:
    """The indivisible row runs of [lo, hi): see `_pack_rows`."""
    segs = seg_fn(node, lo, hi) if node is not None or seg_fn is not None else []
    if len(segs) <= 1:
        return _line_ranges(lines, lo, hi, budget)
    out: list[tuple[str, int, int]] = []
    for a, b, child in segs:
        if _size(lines, a, b) <= budget:
            out.append(("rows", a, b))
            continue
        deeper = child if (child is not None and child.child_count > 0) else None
        if deeper is not None and deeper.end_point[0] > deeper.start_point[0]:
            out.extend(_atoms(lines, a, b, deeper, budget, seg_fn))
        else:
            out.extend(_line_ranges(lines, a, b, budget))
    return out


def _line_ranges(
    lines: list[str], lo: int, hi: int, budget: int
) -> list[tuple[str, int, int]]:
    """Bottom of the recursion: every line is its own atom; an over-budget
    line is a ("line", …) atom for the mid-line cut."""
    return [
        ("line" if len(lines[i]) > budget else "rows", i, i + 1) for i in range(lo, hi)
    ]


def _cut_line(line: str, budget: int, leader: str) -> list[str]:
    """A single line longer than the budget (a generated lookup table, a
    minified blob): fixed-width windows, each ending in an elision marker
    so the reader knows the line continues in the next piece."""
    marker = f" {leader} … line continues\n"
    width = max(budget - len(marker), 1)
    body = line.rstrip("\n")
    out: list[str] = []
    for start in range(0, len(body), width):
        window = body[start : start + width]
        if start + width < len(body):
            out.append(window + marker)
        else:
            out.append(window + "\n")
    return out


def _unit_node(pf: ParsedFile, start_line: int, end_line: int, kinds: set[str]) -> Any:
    """The tree-sitter node of a symbol by its line span: the smallest node
    covering the span, then down through same-span wrappers
    (template_declaration, decorated_definition) to the unit itself."""
    tree = pf.tree
    if tree is None:
        return None
    root = getattr(tree, "root_node", None)
    if root is None:
        return None
    text = pf.source.decode("utf-8", "replace").splitlines()
    row = start_line - 1
    if row >= len(text):
        return None
    col = len(text[row]) - len(text[row].lstrip())
    end_row = end_line - 1
    try:
        node = root.descendant_for_point_range((row, col), (end_row, 0))
    except Exception:  # noqa: BLE001 — a grammar without the API: no AST cuts
        return None
    for _ in range(6):
        if node is None:
            return None
        if node.type in kinds and node.start_point[0] == row:
            return node
        nxt = next(
            (
                c for c in node.children
                if c.start_point[0] == row and c.end_point[0] >= end_row - 1
            ),
            None,
        )
        if nxt is None:
            return next(
                (c for c in node.children if c.type in kinds and c.start_point[0] == row),
                None,
            )
        node = nxt
    return None


def _body_node(node: Any) -> Any:
    if node is None:
        return None
    body = node.child_by_field_name("body")
    return body if body is not None else node


_FUNCTION_NODE_TYPES = {
    "function_definition", "lambda_expression",
    # The unit node of an oversized TypeScript or Rust function, for
    # statement-boundary cuts
    "function_declaration", "generator_function_declaration", "method_definition",
    "arrow_function", "function_expression", "function_item",
}
_CLASS_NODE_TYPES = {
    "class_specifier", "struct_specifier", "class_definition",
    "class_declaration", "abstract_class_declaration", "interface_declaration",
    "enum_declaration", "internal_module", "object",
    "struct_item", "enum_item", "trait_item", "impl_item", "mod_item",
}


_ELIDE_MARKER = {
    "cpp": "    /* … body elided (indexed as its own chunk) … */\n",
    "typescript": "    // … body elided (indexed as its own chunk) …\n",
    "javascript": "    // … body elided (indexed as its own chunk) …\n",
    "rust": "    // … body elided (indexed as its own chunk) …\n",
}
_ELIDE_DEFAULT = "    # … body elided (indexed as its own chunk) …\n"


def _elide_bodies_rows(
    src_lines: list[str], sym: Symbol, inner_fns: list[Symbol], lang: str = "cpp"
) -> tuple[list[str], list[int]]:
    """`_elide_bodies` as lines, plus the 0-based source row each output
    line came from (-1 for a marker line) — the map the statement-boundary
    split needs, since elision makes skeleton lines and AST rows diverge."""
    marker = _ELIDE_MARKER.get(lang, _ELIDE_DEFAULT)
    keep = [True] * len(src_lines)
    for fn in inner_fns:
        # elide everything after the function's first line through its last
        for i in range(fn.start_line, fn.end_line):  # 0-indexed interior lines
            if 0 <= i < len(keep):
                keep[i] = False
    out: list[str] = []
    rows: list[int] = []
    skipping = False
    for i in range(sym.start_line - 1, min(sym.end_line, len(src_lines))):
        if keep[i]:
            out.append(src_lines[i])
            rows.append(i)
            skipping = False
        elif not skipping:
            out.append(marker)
            rows.append(-1)
            skipping = True
    return out, rows


def _fresh_line_child_rows(node: Any) -> set[int]:
    """Absolute 0-based rows at which a child of `node` starts a fresh line."""
    rows: set[int] = set()
    prev_end = -1
    for child in node.children:
        r = child.start_point[0]
        if r > prev_end:
            rows.add(r)
        prev_end = max(prev_end, child.end_point[0])
    return rows


def _assign_ordinals(chunks: list[Chunk]) -> None:
    """Second and later chunks with the same (scope, content) in one file get
    ordinal 1, 2, … so they keep their own rows (and line spans) instead of
    collapsing under one content-addressed key. Chunks must be in file order
    so the ordinal is stable across re-ingests of unchanged text."""
    seen: dict[tuple[str, str], int] = {}
    for c in chunks:
        key = (c.scope, c.content_hash)
        n = seen.get(key, 0)
        c.ordinal = n
        seen[key] = n + 1


def _file_header_chunk(
    pf: ParsedFile, src_lines: list[str], covered: list[bool], max_chars: int,
) -> Chunk | None:
    """The leading run of comments / includes / imports / `use` lines as
    one `file_header` chunk (marks its lines covered), or None when line 1
    is already inside a unit or the run is too short to say anything."""
    header_prefixes: tuple[str, ...] = (
        ("//", "/*", "*", "#include", "#pragma")
        if pf.lang == "cpp"
        else ("#", "import ", "from ", '"""', "'''", "source ", ". ")
    )
    header_end = 0
    for i, ln in enumerate(src_lines):
        s = ln.strip()
        if s == "" or s.startswith(header_prefixes) or _is_header_line(pf.lang, s):
            header_end = i + 1
            if header_end > 200:
                break
        else:
            break
    if header_end == 0 or covered[1]:
        return None
    content = "".join(src_lines[:header_end])
    for i in range(1, header_end + 1):
        if i < len(covered):
            covered[i] = True
    # Headers are meaningful even when short (a lone include tells you
    # the dependency) — use a lower floor than body chunks.
    if len(content.strip()) < 10:
        return None
    return Chunk(
        path=pf.path, scope="", kind="file_header", start_line=1, end_line=header_end,
        content=content[:max_chars],
    )


def chunk_file(
    pf: ParsedFile, max_chars: int = 6000, merge_target_chars: int = 0
) -> list[Chunk]:
    """Chunk a parsed file. The AST path is language-agnostic — it works
    off symbol line spans, so any parsed language (cpp/python/shell) gets
    semantic chunks; an unparsed file falls back to line windows.

    `merge_target_chars` > 0 turns on the leftover merge: consecutive
    uncovered runs separated only by blank lines accumulate into one `other`
    chunk up to that size, and sub-40-char declarations inside a merged
    group are kept instead of dropped. 0 keeps every run its own chunk.
    """
    text = pf.source.decode("utf-8", "replace")
    src_lines = text.splitlines(keepends=True)
    leader = _COMMENT_LEADER.get(pf.lang, "//")

    if pf.tree is None:
        return _fallback_chunks(pf.path, src_lines, max_chars)

    chunks: list[Chunk] = []
    fn_syms = [s for s in pf.symbols if s.kind == "function" and s.has_body]
    decl_syms = [s for s in pf.symbols if s.kind == "function" and not s.has_body]
    kinds = container_kinds(pf.lang)
    cls_syms = [s for s in pf.symbols if s.kind in kinds]

    covered = [False] * (len(src_lines) + 2)

    # function chunks (whole definitions, with the doc comment attached)
    for fn in sorted(fn_syms, key=lambda s: (s.start_line, -s.end_line)):
        if covered[fn.start_line]:
            continue  # nested/lambda duplication guard
        start = fn.start_line
        doc = fn.doc_comment
        body = "".join(src_lines[start - 1 : fn.end_line])
        content = (doc + "\n" if doc else "") + body
        header = src_lines[start - 1] if start - 1 < len(src_lines) else fn.qualified_name
        for i in range(start, fn.end_line + 1):
            if i < len(covered):
                covered[i] = True
        if len(content) <= max_chars:
            chunks.append(
                Chunk(
                    path=pf.path,
                    scope=fn.qualified_name,
                    kind="function",
                    start_line=start,
                    end_line=fn.end_line,
                    content=content,
                    symbol_ids=[fn.id],
                )
            )
        else:
            node = _body_node(_unit_node(pf, start, fn.end_line, _FUNCTION_NODE_TYPES))
            for piece, s_ln, e_ln in _split_oversized(
                body, header, start, max_chars, node=node, doc=doc, leader=leader
            ):
                chunks.append(
                    Chunk(
                        path=pf.path,
                        scope=fn.qualified_name,
                        kind="function",
                        start_line=s_ln,
                        end_line=e_ln,
                        content=piece,
                        symbol_ids=[fn.id],
                    )
                )

    # class skeletons: class text minus already-chunked method bodies
    for cls in cls_syms:
        inner = [f for f in fn_syms if cls.start_line < f.start_line and f.end_line <= cls.end_line]
        skel_lines, skel_rows = _elide_bodies_rows(src_lines, cls, inner, lang=pf.lang)
        skel = "".join(skel_lines)
        if not skel.strip():
            continue
        if cls.kind not in _BASE_CONTAINER_KINDS and len(skel.strip()) < _MIN_CHUNK_CHARS:
            continue  # a one-line `export const X = 5` rides the leftovers, not a row of its own
        decl_ids = [
            d.id for d in decl_syms if cls.start_line <= d.start_line <= cls.end_line
        ]
        content = (cls.doc_comment + "\n" if cls.doc_comment else "") + skel
        if len(content) <= max_chars:
            pieces = [(content, cls.start_line, cls.end_line)]
        else:
            # Skeleton lines no longer map to AST rows one-to-one, so the
            # allowed cuts are the body's fresh-line member starts mapped
            # onto skeleton line offsets.
            body_node = _body_node(
                _unit_node(pf, cls.start_line, cls.end_line, _CLASS_NODE_TYPES)
            )
            member_rows = _fresh_line_child_rows(body_node) if body_node is not None else set()
            cut_rows = {
                cls.start_line - 1 + i for i, r in enumerate(skel_rows) if r in member_rows
            }
            header = (
                src_lines[cls.start_line - 1]
                if cls.start_line - 1 < len(src_lines)
                else cls.qualified_name
            )
            raw_pieces = _split_oversized(
                skel, header, cls.start_line, max_chars,
                doc=cls.doc_comment, leader=leader, cut_rows=cut_rows,
            )
            # piece spans are skeleton-relative; map them back to source rows
            pieces = []
            for piece, s_ln, e_ln in raw_pieces:
                a = s_ln - cls.start_line
                b = e_ln - cls.start_line
                rows_in = [r for r in skel_rows[a : b + 1] if r >= 0]
                if rows_in:
                    pieces.append((piece, rows_in[0] + 1, rows_in[-1] + 1))
                else:
                    pieces.append((piece, cls.start_line, cls.end_line))
        for piece, s_ln, e_ln in pieces:
            chunks.append(
                Chunk(
                    path=pf.path,
                    scope=cls.qualified_name,
                    kind="class_skeleton",
                    start_line=s_ln,
                    end_line=e_ln,
                    content=piece,
                    symbol_ids=[cls.id, *decl_ids],
                )
            )
        for i in range(cls.start_line, cls.end_line + 1):
            if i < len(covered):
                covered[i] = True

    # file header: leading run of comments/includes/imports/blank lines
    header_chunk = _file_header_chunk(pf, src_lines, covered, max_chars)
    if header_chunk is not None:
        chunks.append(header_chunk)

    # leftovers: uncovered top-level runs (blank lines and covered lines end
    # a run), optionally merged across blank lines up to merge_target_chars
    runs: list[tuple[int, int]] = []
    run_start = None
    for i in range(1, len(src_lines) + 1):
        uncovered = not covered[i] and src_lines[i - 1].strip() != ""
        if uncovered and run_start is None:
            run_start = i
        boundary = (not uncovered) or i == len(src_lines)
        if run_start is not None and boundary:
            run_end = i if (uncovered and i == len(src_lines)) else i - 1
            runs.append((run_start, run_end))
            run_start = None
    for run_start, run_end in _merge_runs(runs, src_lines, covered, merge_target_chars):
        content = "".join(src_lines[run_start - 1 : run_end])
        if len(content.strip()) < _MIN_CHUNK_CHARS:
            continue
        for piece, s_ln, e_ln in (
            [(content, run_start, run_end)]
            if len(content) <= max_chars
            else _split_oversized(
                content, src_lines[run_start - 1], run_start, max_chars, leader=leader
            )
        ):
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

    chunks.sort(key=lambda c: (c.start_line, c.end_line))
    _assign_ordinals(chunks)
    return chunks


def _merge_runs(
    runs: list[tuple[int, int]], src_lines: list[str], covered: list[bool], target: int
) -> list[tuple[int, int]]:
    """Leftover merge (0 = off): consecutive runs with only blank, uncovered
    lines between them join into one span while the joined text stays
    within `target` chars. A run is never merged across a covered line — a
    merged `other` chunk must not swallow a function's span, or
    code.enclosing would answer with the wrong unit."""
    if target <= 0 or len(runs) <= 1:
        return runs
    out: list[tuple[int, int]] = []
    cur_s, cur_e = runs[0]
    cur_size = sum(len(ln) for ln in src_lines[cur_s - 1 : cur_e])
    for s, e in runs[1:]:
        gap_blank = all(
            not covered[i] and src_lines[i - 1].strip() == "" for i in range(cur_e + 1, s)
        )
        gap_size = sum(len(ln) for ln in src_lines[cur_e : s - 1])
        size = sum(len(ln) for ln in src_lines[s - 1 : e])
        if gap_blank and cur_size + gap_size + size <= target:
            cur_e = e
            cur_size += gap_size + size
        else:
            out.append((cur_s, cur_e))
            cur_s, cur_e, cur_size = s, e, size
    out.append((cur_s, cur_e))
    return out


def _fallback_chunks(path: str, src_lines: list[str], max_chars: int = 6000) -> list[Chunk]:
    """Line windows for unparsed files: at most `_FALLBACK_WINDOW_LINES`
    lines AND at most `max_chars` chars. A window that would overflow the
    char cap closes early with an elision marker naming the next line, and a
    single over-cap line is cut mid-line (`_cut_line`): a 120-line window of
    docs or SQL can otherwise reach 14,830 chars."""
    chunks: list[Chunk] = []
    n = len(src_lines)
    marker_room = 48

    def emit(content: str, start: int, end: int) -> None:
        if len(content.strip()) < _MIN_CHUNK_CHARS:
            return
        chunks.append(
            Chunk(path=path, scope="", kind="other", start_line=start, end_line=end,
                  content=content)
        )

    i = 0
    while i < n:
        buf: list[str] = []
        size = 0
        start = i
        while i < n and len(buf) < _FALLBACK_WINDOW_LINES:
            ln = src_lines[i]
            if len(ln) > max_chars:
                if buf:
                    break
                for piece in _cut_line(ln, max_chars, "…"):
                    emit(piece, i + 1, i + 1)
                i += 1
                start = i
                continue
            if buf and size + len(ln) > max_chars - marker_room:
                break
            buf.append(ln)
            size += len(ln)
            i += 1
        if not buf:
            continue
        # A tail too short to be a chunk of its own (a manifest's
        # closing braces past the 120th line) rides with this window rather
        # than being dropped — the reassembled file must be the whole file
        tail = src_lines[i:]
        short = tail and len("".join(tail).strip()) < _MIN_CHUNK_CHARS
        if short and all(len(t) <= max_chars for t in tail):
            buf.extend(tail)
            i = n
        content = "".join(buf)
        if i < n and len(buf) < _FALLBACK_WINDOW_LINES and start + len(buf) == i:
            content += f"… continued at line {i + 1}\n"
        emit(content, start + 1, start + len(buf))
    return chunks
