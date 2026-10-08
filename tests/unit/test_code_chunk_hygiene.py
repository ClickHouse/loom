"""Chunk hygiene: every piece is bounded, cuts land between
statements, repeated content keeps its rows, fallback windows are capped.

Property tests use a seeded generator rather than a fixture corpus: the
failing shapes are ones nobody writes by hand (a 10-line, 9,337-char lookup
table; a 14,830-char Markdown window).
"""

from __future__ import annotations

import random
import re

import pytest

pytest.importorskip("tree_sitter", reason="code extras not installed")

from server.code.domain.extract import chunker
from server.code.domain.extract.chunker import Chunk, chunk_file
from server.code.domain.extract.treesitter import ParsedFile, parse_cpp

MAX = 1000


def _gen_function(rng: random.Random, *, n_stmts: int, nested: bool) -> str:
    """A C++ function with `n_stmts` top-level statements, some of them
    blocks (if/for) with their own statements, so the body has two depths."""
    out = ["void bigFn(int x)\n{\n"]
    for i in range(n_stmts):
        if nested and rng.random() < 0.3:
            out.append(f"    if (x > {i})\n    {{\n")
            for j in range(rng.randint(1, 6)):
                out.append(f"        inner_{i}_{j}(x, {'y' * rng.randint(1, 40)});\n")
            out.append("    }\n")
        else:
            out.append(f'    stmt_{i}(x, "{"z" * rng.randint(0, 60)}");\n')
    out.append("}\n")
    return "".join(out)


def _depth_at(lines: list[str], cut_row: int) -> int:
    """Brace depth of the source just before `cut_row` (0-based). The
    generator writes no braces inside strings, so counting is exact."""
    text = "".join(lines[:cut_row])
    return text.count("{") - text.count("}")


def _stmt_sizes(lines: list[str]) -> dict[int, int]:
    """Row -> size of the top-level statement (depth-1) starting there."""
    sizes: dict[int, int] = {}
    depth = 0
    start = None
    for i, ln in enumerate(lines):
        if depth == 1 and start is None and ln.strip() and ln.strip() != "}":
            start = i
        depth += ln.count("{") - ln.count("}")
        if start is not None and depth <= 1 and (ln.strip().endswith(";") or ln.strip() == "}"):
            sizes[start] = sum(len(x) for x in lines[start : i + 1])
            start = None
    return sizes


@pytest.mark.parametrize("seed", range(40))
def test_oversize_function_pieces_are_bounded_and_cut_between_statements(seed: int) -> None:
    rng = random.Random(seed)
    src = _gen_function(rng, n_stmts=rng.randint(20, 120), nested=True)
    lines = src.splitlines(keepends=True)
    pf = parse_cpp("src/big.cpp", src.encode())
    pieces = [c for c in chunk_file(pf, max_chars=MAX) if c.kind == "function"]
    assert len(pieces) > 1
    # hard bound on every piece
    assert all(len(c.content) <= MAX for c in pieces), [len(c.content) for c in pieces]
    # pieces tile the function: consecutive spans, and stripping the
    # continuation prefixes gives back the source
    for prev, nxt in zip(pieces, pieces[1:], strict=False):
        assert nxt.start_line == prev.end_line + 1
    rebuilt = pieces[0].content + "".join(c.content.split("\n", 1)[1] for c in pieces[1:])
    assert rebuilt == src
    # a cut lands at body depth (1) unless the top-level statement it falls
    # inside could not fit a piece by itself — then, and only then, one
    # level deeper
    stmt_sizes = _stmt_sizes(lines)
    stmt_rows = sorted(stmt_sizes)
    prefix_room = len("// … continued from void bigFn(int x)\n")
    for c in pieces[1:]:
        cut_row = c.start_line - 1
        depth = _depth_at(lines, cut_row)
        if depth == 1:
            continue
        enclosing = max(r for r in stmt_rows if r <= cut_row)
        assert stmt_sizes[enclosing] > MAX - prefix_room, (
            f"seed {seed}: cut at row {cut_row} depth {depth} inside a statement of "
            f"{stmt_sizes[enclosing]} chars that fit the budget"
        )
        assert depth == 2


@pytest.mark.parametrize("seed", range(10))
def test_flat_function_cuts_only_at_depth_one(seed: int) -> None:
    rng = random.Random(1000 + seed)
    src = _gen_function(rng, n_stmts=rng.randint(30, 90), nested=False)
    lines = src.splitlines(keepends=True)
    pf = parse_cpp("src/flat.cpp", src.encode())
    pieces = [c for c in chunk_file(pf, max_chars=MAX) if c.kind == "function"]
    assert len(pieces) > 1
    for c in pieces[1:]:
        assert _depth_at(lines, c.start_line - 1) == 1
        assert re.match(r"^// … continued from void bigFn", c.content)


def test_single_giant_line_is_cut_mid_line_with_marker() -> None:
    table = ", ".join(f"0x{i:08x}" for i in range(400))  # ~4,800 chars on one line
    src = f"void f()\n{{\n    static const int t[] = {{{table}}};\n    use(t);\n}}\n"
    pf = parse_cpp("src/table.cpp", src.encode())
    pieces = [c for c in chunk_file(pf, max_chars=MAX) if c.kind == "function"]
    assert all(len(c.content) <= MAX for c in pieces)
    cut = [c for c in pieces if "line continues" in c.content]
    assert len(cut) >= 4
    # every mid-line piece points at the same source line
    assert {(c.start_line, c.end_line) for c in cut} == {(3, 3)}
    joined = "".join(
        c.content.split("\n", 1)[1] if c.content.startswith("//") else c.content for c in pieces
    )
    assert table in joined.replace(" // … line continues\n", "")


def test_doc_comment_counts_toward_the_first_piece_bound() -> None:
    doc = "/// " + "d" * 700 + "\n"
    body = "".join(f"    stmt_{i}(x);\n" for i in range(60))
    src = f"{doc}void f(int x)\n{{\n{body}}}\n"
    pf = parse_cpp("src/doc.cpp", src.encode())
    pieces = [c for c in chunk_file(pf, max_chars=MAX) if c.kind == "function"]
    assert pieces[0].content.startswith("/// ddd")
    assert all(len(c.content) <= MAX for c in pieces)


def test_class_skeleton_split_is_bounded_and_cuts_between_members() -> None:
    members = "".join(
        f"    int member_{i} = {i}; // {'c' * 30}\n    void m{i}() {{ work_{i}(); }}\n"
        for i in range(80)
    )
    src = f"class Big\n{{\npublic:\n{members}}};\n"
    pf = parse_cpp("src/Big.h", src.encode())
    skel = [c for c in chunk_file(pf, max_chars=MAX) if c.kind == "class_skeleton"]
    assert len(skel) > 1
    assert all(len(c.content) <= MAX for c in skel)
    for c in skel[1:]:
        first = c.content.split("\n", 2)[1]
        assert first.lstrip().startswith(("int member_", "void m", "/* … body elided"))
        assert c.start_line >= 1 and c.end_line <= src.count("\n")


def test_no_chunk_exceeds_max_chars_on_python() -> None:
    body = "".join(f"    value_{i} = compute({i}, '{'q' * 50}')\n" for i in range(80))
    src = f"def big(x):\n{body}    return x\n"
    from server.code.domain.extract.lang_python import parse_python

    pf = parse_python("tests/big.py", src.encode())
    chunks = chunk_file(pf, max_chars=MAX)
    assert chunks
    assert all(len(c.content) <= MAX for c in chunks)
    fn = [c for c in chunks if c.kind == "function"]
    assert len(fn) > 1
    assert fn[1].content.startswith("# … continued from def big")


# ---- ordinals ------------------------------------------------------------


def test_identical_other_chunks_at_different_lines_are_two_rows() -> None:
    block = "#undef VERY_LONG_MACRO_NAME_ONE\n#undef VERY_LONG_MACRO_NAME_TWO\n"
    src = f"int a;\n\n{block}\nvoid f() {{}}\n\n{block}\nint b;\n"
    pf = parse_cpp("src/dup.cpp", src.encode())
    chunks = chunk_file(pf)
    same = [c for c in chunks if c.content == block]
    assert len(same) == 2
    assert same[0].ordinal == 0 and same[1].ordinal == 1
    assert same[0].chunk_id("ns") != same[1].chunk_id("ns")
    assert same[0].start_line < same[1].start_line


def test_ordinal_zero_key_is_byte_identical_to_the_pre_ordinal_key() -> None:
    import uuid

    c = Chunk(
        path="src/a.cpp",
        scope="DB::f",
        kind="function",
        start_line=1,
        end_line=2,
        content="void f() {}\n",
    )
    legacy = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"loom://code/ns/src/a.cpp#DB::f@{c.content_hash[:16]}")
    )
    assert c.chunk_id("ns") == legacy
    c.ordinal = 1
    assert c.chunk_id("ns") != legacy


def test_ordinals_are_stable_across_rechunk() -> None:
    block = "#undef X_ONE_LONG_NAME_FOR_THE_FLOOR\n#undef X_TWO_LONG_NAME_FOR_THE_FLOOR\n"
    src = f"int a;\n\n{block}\n{block}\n{block}\n"
    pf = parse_cpp("src/dup.cpp", src.encode())
    ids_a = [c.chunk_id("ns") for c in chunk_file(pf)]
    ids_b = [c.chunk_id("ns") for c in chunk_file(parse_cpp("src/dup.cpp", src.encode()))]
    assert ids_a == ids_b
    assert len(set(ids_a)) == len(ids_a)


# ---- fallback windows -----------------------------------------------------


def test_fallback_windows_are_capped_at_max_chars_with_marker() -> None:
    lines = "".join(f"line {i}: {'w' * 90}\n" for i in range(300))  # ~100 chars/line
    pf = ParsedFile(path="docs/big.md", lang="doc", source=lines.encode())
    chunks = chunk_file(pf, max_chars=MAX)
    assert chunks
    assert all(len(c.content) <= MAX for c in chunks)
    # windows closed early by the char cap say where the text continues
    assert all("… continued at line" in c.content for c in chunks[:-1])
    assert all(c.end_line - c.start_line + 1 < 120 for c in chunks)
    # spans tile the file
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start_line == prev.end_line + 1
    assert chunks[-1].end_line == 300


def test_fallback_windows_unchanged_when_under_the_cap() -> None:
    lines = "".join(f"line {i} of a config file with words\n" for i in range(300))
    pf = ParsedFile(path="tests/config.xml", lang="config", source=lines.encode())
    chunks = chunk_file(pf)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 120), (121, 240), (241, 300)]
    assert not any("… continued" in c.content for c in chunks)


def test_fallback_giant_line_is_cut_mid_line() -> None:
    src = "short\n" + "x" * 5000 + "\n" + "tail line with enough words to keep it\n" * 3
    pf = ParsedFile(path="q.sql", lang="sql", source=src.encode())
    chunks = chunk_file(pf, max_chars=MAX)
    assert all(len(c.content) <= MAX for c in chunks)
    mid = [c for c in chunks if c.start_line == c.end_line == 2]
    assert len(mid) >= 5


# ---- leftover merge (off by default) ---------------------------------------


_LEFTOVERS = (
    "int alpha_value = 1;\n\n"
    "int beta_value = 2;\n\n"
    "using T = int;\n\n"
    "void f() { a(); }\n\n"
    "int gamma_value = 3;\n\n"
    "int delta_value = 4;\n"
)


def test_leftover_merge_off_keeps_today_rows() -> None:
    pf = parse_cpp("src/l.cpp", _LEFTOVERS.encode())
    others = [c for c in chunk_file(pf) if c.kind == "other"]
    # sub-40-char runs are dropped as before
    assert others == []


def test_leftover_merge_joins_runs_across_blank_lines_but_not_functions() -> None:
    pf = parse_cpp("src/l.cpp", _LEFTOVERS.encode())
    others = [c for c in chunk_file(pf, merge_target_chars=1500) if c.kind == "other"]
    spans = [(c.start_line, c.end_line) for c in others]
    # a, b, using T merge (lines 1-5); c, d merge (9-11); f() is never inside
    assert spans == [(1, 5), (9, 11)]
    assert "void f()" not in others[0].content and "void f()" not in others[1].content
    # the merged group indexes declarations that alone were under the floor
    assert "int alpha_value = 1;" in others[0].content
    assert "using T = int;" in others[0].content


def test_leftover_merge_respects_target() -> None:
    src = "".join(f"int v{i} = {i}; // {'m' * 40}\n\n" for i in range(40))
    pf = parse_cpp("src/m.cpp", src.encode())
    others = [c for c in chunk_file(pf, merge_target_chars=300) if c.kind == "other"]
    assert len(others) > 1
    assert all(len(c.content) <= 300 for c in others)


def test_merge_runs_helper_never_crosses_covered_lines() -> None:
    src_lines = ["int a;\n", "\n", "int b;\n"]
    covered = [False, False, True, False]  # line 2 (blank) is covered
    assert chunker._merge_runs([(1, 1), (3, 3)], src_lines, covered, 10_000) == [(1, 1), (3, 3)]
