"""Each lookup the code index builds once per file answers what the scan it replaced
answered, on random inputs: same-named definitions under a scope, rung 3's narrowing and
the member and package-clause indexes, the span that holds a line, a Go import's module, a
tsconfig `paths` pattern and the ancestor ladders."""

from __future__ import annotations

import posixpath
import random

from server.code.app.index.ingest_headers import include_candidates
from server.code.domain.extract.edges_treesitter import (
    _CallResolver,
    _Namesakes,
    prefer_implementation,
)
from server.code.domain.extract.lane_units import ancestor_candidates
from server.code.domain.extract.lang_go import go_modules
from server.code.domain.extract.spans import first_span_at, last_span_at
from server.code.domain.extract.treesitter import Symbol
from server.code.domain.extract.ts_config import PathsTable


def _word(rnd: random.Random, alphabet: str, longest: int) -> str:
    return "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, longest)))


def test_namesakes_count_what_a_filter_keeps() -> None:
    rnd = random.Random(1)
    for _ in range(3000):
        syms = [
            Symbol(
                qualified_name=_word(rnd, "a:", 6),
                name="a",
                kind="function",
                signature="",
                path="p",
                start_line=1,
                end_line=1,
            )
            for _ in range(rnd.randint(0, 7))
        ]
        index = _Namesakes(syms)
        for _ in range(5):
            prefix = _word(rnd, "a:", 4)
            skip = rnd.choice([s.qualified_name for s in syms] + ["", "z"])
            kept = [
                s for s in syms if s.qualified_name.startswith(prefix) and s.qualified_name != skip
            ]
            n, one = index.one(prefix, skip)
            assert n == len(kept) and (one is kept[0] if n == 1 else one is None)


def test_span_lookups_answer_what_a_scan_answers() -> None:
    rnd = random.Random(2)
    for _ in range(3000):
        spans = [(s, s + rnd.randint(-2, 6), i) for i in range(8) for s in [rnd.randint(-1, 12)]]
        spans = spans[: rnd.randint(0, 8)]
        first = first_span_at(spans)
        for line in range(-3, 22):
            assert first(line) == next((v for s, e, v in spans if s <= line <= e), None)
        ordered = sorted(spans, key=lambda t: (t[0], -t[1]))
        last = last_span_at(ordered)
        for line in range(-3, 22):
            assert last(line) == next((v for s, e, v in reversed(ordered) if s <= line <= e), None)


def test_go_modules_answer_what_a_scan_answers() -> None:
    rnd = random.Random(3)
    for _ in range(3000):
        mods = {_word(rnd, "a/", 5) for _ in range(rnd.randint(1, 6))}
        index = sorted(((m, m.upper()) for m in mods), key=lambda kv: (-len(kv[0]), kv[0]))
        module_of = go_modules(index)
        for _ in range(5):
            spec = _word(rnd, "a/", 7)
            expected = next(
                ((m, d) for m, d in index if spec == m or spec.startswith(m + "/")), ("", "")
            )
            assert module_of(spec) == expected


def _scan_best(patterns: dict[str, list[str]], spec: str) -> tuple[str, str] | None:
    best: tuple[int, str, str] | None = None
    for pat in patterns:
        if "*" not in pat:
            bound = "" if pat == spec else None
        else:
            head, _, tail = pat.partition("*")
            fits = spec.startswith(head) and spec.endswith(tail)
            fits = fits and len(spec) >= len(head) + len(tail)
            bound = spec[len(head) : len(spec) - len(tail)] if fits else None
        if bound is None:
            continue
        length = len(pat.partition("*")[0]) if "*" in pat else len(pat) + 1
        if best is None or length > best[0]:
            best = (length, pat, bound)
    return None if best is None else (best[1], best[2])


def test_paths_best_answers_what_the_pattern_loop_answers() -> None:
    rnd = random.Random(4)
    for _ in range(3000):
        patterns = {_word(rnd, "ab*/", 5): ["x"] for _ in range(rnd.randint(1, 8))}
        table = PathsTable(base="", patterns=patterns)
        for _ in range(5):
            spec = _word(rnd, "ab*/", 6)
            assert table.best(spec) == _scan_best(patterns, spec)


def test_a_deep_file_tries_the_nearest_ancestors_then_the_root() -> None:
    from server.code.domain.extract.lane_units import MAX_ANCESTOR_DIRS

    from_path = "/".join(f"d{i}" for i in range(MAX_ANCESTOR_DIRS + 8)) + "/x.proto"
    ladder = ancestor_candidates(from_path, "p.proto")
    dirs = from_path.split("/")[:-1]
    assert len(ladder) == MAX_ANCESTOR_DIRS + 1 and ladder[-1] == "p.proto"
    assert ladder[0] == "/".join(dirs) + "/p.proto"
    assert ladder[-2] == "/".join(dirs[:9]) + "/p.proto"
    assert include_candidates(from_path.replace(".proto", ".c"), "h.h")[-1] == "h.h"


def test_the_ladders_answer_what_the_loops_answered_within_the_cap() -> None:
    rnd = random.Random(5)
    for _ in range(3000):
        from_path = "/".join(rnd.choice("abc") for _ in range(rnd.randint(1, 6))) + "/f.c"
        include = "/".join(rnd.choice(["a", "b", "..", ".", "", ".h"]) for _ in range(3))
        expected: list[str] = []
        d = posixpath.dirname(from_path)
        while True:
            cand = posixpath.normpath(posixpath.join(d, include.lstrip("/")))
            if include.strip("/") and cand and not cand.startswith("..") and cand not in expected:
                expected.append(cand)
            if not d:
                break
            d = posixpath.dirname(d)
        assert include_candidates(from_path, include) == expected
        spec = include.strip().lstrip("/")
        parts = from_path.split("/")[:-1]
        bases = ["/".join(parts[:depth]) for depth in range(len(parts), -1, -1)]
        ladder = list(dict.fromkeys(f"{b}/{spec}" if b else spec for b in bases)) if spec else []
        assert ancestor_candidates(from_path, include) == ladder


def _bare_resolver(go: bool = False, script: bool = False) -> _CallResolver:
    resolver = _CallResolver.__new__(_CallResolver)
    resolver._memo, resolver.go, resolver.script = {}, go, script
    return resolver


def _old_narrowed(
    resolver: _CallResolver, rows: list[tuple[str, str, str]], skip: str
) -> list[tuple[str, str, str]]:
    rows = [c for c in rows if c[1] != skip] if skip else rows
    if resolver.go and len(rows) > 1:
        concrete = [c for c in rows if c[2] not in ("method", "field")]
        rows = concrete if len(concrete) == 1 else rows
    if resolver.script and len(rows) > 1:
        rows = prefer_implementation(rows)
    return rows


def test_rung_three_answers_what_the_narrowing_filters_answered() -> None:
    rnd = random.Random(6)
    for _ in range(3000):
        resolver = _bare_resolver(go=rnd.random() < 0.5, script=rnd.random() < 0.5)
        names = {f"{rnd.choice(['m', 'm.d', 'n'])}::{_word(rnd, 'ab', 2)}a" for _ in range(6)}
        pool = [
            (f"s{i}", qn, rnd.choice(["function", "method", "field"])) for i, qn in enumerate(names)
        ]
        for skip in ["", *names, "z::a"]:
            old = _old_narrowed(resolver, pool, skip)
            assert resolver._repo_wide_one(pool, skip) == (old[0] if len(old) == 1 else None)


def test_member_and_clause_indexes_keep_what_their_filters_kept() -> None:
    rnd = random.Random(7)
    for _ in range(3000):
        resolver = _bare_resolver()
        rows = [
            (f"s{i}", "::".join(_word(rnd, "ab.", 3) for _ in range(rnd.randint(1, 4))), "function")
            for i in range(rnd.randint(0, 8))
        ]
        owner, name = _word(rnd, "ab", 2), _word(rnd, "ab", 2)
        assert resolver._member_rows(rows, owner, name) == [
            c for c in rows if c[1].endswith(f"::{owner}::{name}")
        ]
        receiver = _word(rnd, "ab", 2)
        assert resolver._by_clause(rows, name).get(receiver, []) == [
            c
            for c in rows
            if c[1].count("::") == 1
            and c[1].endswith("::" + name)
            and c[1].partition("::")[0].endswith("." + receiver)
        ]
