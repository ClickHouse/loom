"""code.grep: per-line extraction, enclosing scope, window dedupe, floors."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.core.errors import ValidationError
from server.settings import settings


class _FakeRepo:
    def __init__(
        self, chunks: list[dict[str, Any]], symbols: list[dict[str, Any]] | None = None
    ) -> None:
        self._chunks = chunks
        self._symbols = symbols or []
        self.symbol_lookups: list[list[str]] = []

    async def grep_chunks(self, org, ns, pattern, **kw):
        return self._chunks

    async def symbols_by_ids(self, org, ns, ids):
        self.symbol_lookups.append(list(ids))
        return [r for r in self._symbols if r["symbol_id"] in ids]

    # The unit spans read only where each symbol is
    symbol_spans_by_ids = symbols_by_ids

    async def all_state(self, org, ns):
        return {}


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def _run(chunks, *, symbols=None, repo_out=None, **kw):
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    repo = _FakeRepo(chunks, symbols)
    if repo_out is not None:
        repo_out.append(repo)

    async def go():
        with (
            patch.object(grep, "CodeRepo", return_value=repo),
            patch.object(grep, "index_as_of", new=lambda *a, **k: _empty()),
        ):
            return await grep.handle_code_grep(CodeGrepInput(org="o", namespace="n", **kw))

    async def _empty():
        return {}

    return asyncio.run(go())


def test_lines_scope_and_window_dedupe() -> None:
    chunk = {
        "path": "src/A.cpp",
        "scope": "DB::A::f",
        "start_line": 10,
        "end_line": 14,
        "content": "int x;\nthrow MAGIC_ERR;\nint y;",
    }
    overlap = dict(chunk)  # an oversized-unit window repeating the same span
    out = _run([chunk, overlap], pattern="MAGIC_ERR")
    assert len(out.matches) == 1  # deduped by (path, line)
    m = out.matches[0]
    assert m == {
        "path": "src/A.cpp",
        "line": 11,
        "text": "throw MAGIC_ERR;",
        "scope": "DB::A::f",
        "scope_start_line": 10,
        "scope_end_line": 14,
    }
    assert out.coverage_floor is True


def test_hit_carries_enclosing_unit_span_without_a_symbol_lookup() -> None:
    """ClickGap ask: a hit says how big the function around it is, so the
    client can decide about (or skip) code.enclosing. A chunk that IS the
    unit answers from its own row — no extra query for the common grep."""
    repos: list[Any] = []
    chunks = [
        {
            "path": "src/A.cpp",
            "scope": "DB::A::f",
            "start_line": 10,
            "end_line": 40,
            "symbol_ids": ["sym-f"],
            "content": "int x;\nhit();",
        },
        # a comment block outside any unit: no scope, so no span to claim
        {
            "path": "src/A.cpp",
            "scope": "",
            "start_line": 1,
            "end_line": 3,
            "symbol_ids": [],
            "content": "// hit() is documented here",
        },
    ]
    out = _run(chunks, pattern="hit()", repo_out=repos)
    assert [(m["line"], m["scope_start_line"], m["scope_end_line"]) for m in out.matches] == [
        (11, 10, 40),
        (1, 0, 0),
    ]
    assert repos[0].symbol_lookups == []


def test_windowed_unit_resolves_the_full_span_and_exact_lines() -> None:
    """An oversized function is indexed as windows; each window row only
    knows its own span, and every window after the first opens with a
    synthetic 'continued from' header line that is not in the file."""
    header = "void bigFn(int x)"
    w1 = {
        "path": "src/big.cpp",
        "scope": "bigFn",
        "start_line": 1,
        "end_line": 3,
        "symbol_ids": ["sym-big"],
        "content": f"{header}\n{{\n    a(); // hit",
    }
    w2 = {
        "path": "src/big.cpp",
        "scope": "bigFn",
        "start_line": 4,
        "end_line": 6,
        "symbol_ids": ["sym-big"],
        "content": f"// … continued from {header}\n    b(); // hit\n    c();\n}}",
    }
    sym = {"symbol_id": "sym-big", "path": "src/big.cpp", "start_line": 1, "end_line": 6}
    repos: list[Any] = []
    out = _run([w1, w2], pattern="hit", symbols=[sym], repo_out=repos)
    # line 4 is `b();` — the synthetic header at content line 0 shifted nothing
    assert [(m["line"], m["text"]) for m in out.matches] == [(3, "a(); // hit"), (4, "b(); // hit")]
    assert {(m["scope_start_line"], m["scope_end_line"]) for m in out.matches} == {(1, 6)}
    assert repos[0].symbol_lookups == [["sym-big"]]
    # the header pattern itself must not re-match on the synthetic line
    assert [m["line"] for m in _run([w1, w2], pattern="bigFn", symbols=[sym]).matches] == [1]


def test_windowed_unit_falls_back_to_the_union_of_fetched_windows() -> None:
    w2 = {
        "path": "src/big.cpp",
        "scope": "bigFn",
        "start_line": 4,
        "end_line": 6,
        "symbol_ids": ["sym-gone"],
        "content": "// … continued from void bigFn()\n    b(); // hit",
    }
    w3 = {
        "path": "src/big.cpp",
        "scope": "bigFn",
        "start_line": 7,
        "end_line": 9,
        "symbol_ids": ["sym-gone"],
        "content": "// … continued from void bigFn()\n    c(); // hit",
    }
    out = _run([w2, w3], pattern="hit")  # symbol row tombstoned / not yet indexed
    assert [(m["line"], m["scope_start_line"], m["scope_end_line"]) for m in out.matches] == [
        (4, 4, 9),
        (7, 4, 9),
    ]


def test_case_insensitive_and_regex() -> None:
    chunk = {
        "path": "a.py",
        "scope": "m::f",
        "start_line": 1,
        "end_line": 2,
        "content": "raise MagicErr('x')\nok",
    }
    assert _run([chunk], pattern="magicerr", case_sensitive=False).matches
    assert _run([chunk], pattern=r"Magic\w+", regex=True).matches
    with pytest.raises(ValidationError):
        _run([chunk], pattern="[unclosed", regex=True)


def test_limit_marks_truncated() -> None:
    chunk = {
        "path": "a.py",
        "scope": "",
        "start_line": 1,
        "end_line": 9,
        "content": "\n".join(f"hit {i}" for i in range(9)),
    }
    out = _run([chunk], pattern="hit", limit=3)
    assert len(out.matches) == 3 and out.truncated


def test_a_literal_with_more_matches_than_the_limit_serves_the_limit_never_zero() -> None:
    """The wrong-abstain shape: the chunk statement returned
    the over-fetch cap (200 chunks for `Alias`) — the answer is `limit`
    lines, truncated, never an abstain. Pins the handler half of the path."""
    chunks = [
        {
            "path": f"src/{i:04d}.cpp",
            "scope": f"DB::f{i}",
            "start_line": 1,
            "end_line": 2,
            "content": f"int Alias{i};\nAlias other{i};",
        }
        for i in range(200)
    ]
    out = _run(chunks, pattern="Alias", limit=200)
    assert len(out.matches) == 200 and out.truncated and out.chunks_matched == 200
    assert out.matches[0]["path"] == "src/0000.cpp" and out.matches[0]["line"] == 1
    out_small = _run(chunks, pattern="Alias", limit=20)
    assert len(out_small.matches) == 20 and out_small.truncated and out_small.chunks_matched == 200


def test_the_trace_row_carries_chunks_matched(monkeypatch) -> None:
    """The ledger says what the chunk
    statement returned, so `abstain = 1 AND chunks_matched = 0` is readable
    without the span; matches mode counts fetched chunks, files mode the
    matching chunks of the listed files."""
    from server.code.app import grep as grep_mod

    traced: list[dict[str, Any]] = []
    monkeypatch.setattr(grep_mod, "schedule_code_trace", lambda **kw: traced.append(kw))
    chunk = {"path": "a.py", "scope": "", "start_line": 1, "end_line": 2, "content": "hit\nmiss"}
    _run([chunk, dict(chunk, path="b.py")], pattern="hit")
    assert traced[-1]["chunks_matched"] == 2 and traced[-1]["abstain"] is False
    _run([], pattern="absent_literal")
    assert traced[-1]["chunks_matched"] == 0 and traced[-1]["abstain"] is True
    assert traced[-1]["path_prefix"] == "" and traced[-1]["route"] == "grep_literal"
    # a scoped grep: the route says so and the row carries the scope, so a
    # recount reads the abstain against the prefix it ran under
    _run([], pattern="Alias", path_prefix="src/Storages/StorageFactory.cpp")
    assert traced[-1]["route"] == "grep_literal+prefix"
    assert traced[-1]["path_prefix"] == "src/Storages/StorageFactory.cpp"
    assert traced[-1]["abstain"] is True and traced[-1]["chunks_matched"] == 0


class _FilesRepo(_FakeRepo):
    async def grep_files(self, org, ns, pattern, **kw):
        self.kw = kw
        return (
            [
                {"path": "tests/queries/0_stateless/00001.sql", "chunks": 2, "first_line": 3},
                {"path": "tests/queries/0_stateless/00002.sql", "chunks": 1, "first_line": 9},
            ],
            57,
        )


def test_files_mode_trace_row_carries_the_matching_chunk_count(monkeypatch) -> None:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    traced: list[dict[str, Any]] = []
    monkeypatch.setattr(grep, "schedule_code_trace", lambda **kw: traced.append(kw))
    repo = _FilesRepo([])

    async def go():
        async def _empty(*a, **k):
            return {}

        with (
            patch.object(grep, "CodeRepo", return_value=repo),
            patch.object(grep, "index_as_of", new=_empty),
        ):
            return await grep.handle_code_grep(
                CodeGrepInput(org="o", namespace="n", pattern="insert_deduplicate", mode="files")
            )

    out = asyncio.run(go())
    assert out.chunks_matched == 3 and traced[-1]["chunks_matched"] == 3
    assert traced[-1]["route"] == "grep_literal_files" and traced[-1]["abstain"] is False


def test_files_mode_lists_paths_and_total() -> None:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    repo = _FilesRepo([])

    async def go():
        async def _empty(*a, **k):
            return {}

        with (
            patch.object(grep, "CodeRepo", return_value=repo),
            patch.object(grep, "index_as_of", new=_empty),
        ):
            return await grep.handle_code_grep(
                CodeGrepInput(
                    org="o",
                    namespace="n",
                    pattern="insert_deduplicate",
                    mode="files",
                    limit=2,
                    path_prefix="tests/queries/0_stateless/",
                )
            )

    out = asyncio.run(go())
    assert out.matches == []
    assert [f["path"] for f in out.files] == [
        "tests/queries/0_stateless/00001.sql",
        "tests/queries/0_stateless/00002.sql",
    ]
    assert out.files_total == 57 and out.truncated is True
    assert out.chunks_matched == 3
    assert repo.kw["limit"] == 2 and repo.kw["path_prefix"] == "tests/queries/0_stateless/"


def test_files_mode_limit_can_exceed_the_line_cap() -> None:
    from server.code.api.models_grep import CodeGrepInput

    assert CodeGrepInput(org="o", namespace="n", pattern="xx", mode="files", limit=500).limit == 500


def test_limit_defaults_per_mode_and_clamps_lines() -> None:
    from server.code.api.models_grep import CodeGrepInput

    assert CodeGrepInput(org="o", namespace="n", pattern="xx").effective_limit == 20
    assert CodeGrepInput(org="o", namespace="n", pattern="xx", mode="files").effective_limit == 500
    assert CodeGrepInput(org="o", namespace="n", pattern="xx", limit=400).effective_limit == 200
    assert (
        CodeGrepInput(org="o", namespace="n", pattern="xx", mode="files", limit=7).effective_limit
        == 7
    )


def test_a_case_insensitive_regex_matches_lines_the_sql_step_matched() -> None:
    chunk = {"path": "a.py", "scope": "", "start_line": 1, "end_line": 1, "content": "MagicErr"}
    # ClickHouse is sent `(?i)magic\w+`; the line step must read it the same way
    assert _run([chunk], pattern=r"magic\w+", regex=True, case_sensitive=False).matches
    assert not _run([chunk], pattern=r"magic\w+", regex=True, case_sensitive=True).matches


def test_a_catastrophic_pattern_returns_quickly() -> None:
    import time

    chunk = {"path": "a.py", "scope": "", "start_line": 1, "end_line": 1}
    chunk["content"] = "a" * 50_000 + "!"
    started = time.monotonic()
    out = _run([chunk], pattern="(a+)+$", regex=True)
    assert not out.matches
    assert time.monotonic() - started < 2.0  # Python's re takes minutes here


@pytest.mark.parametrize("pattern", [r"(a)\1", r"foo(?=bar)", r"(?<!x)foo"])
def test_constructs_re2_lacks_are_refused_naming_re2(pattern: str) -> None:
    chunk = {"path": "a.py", "scope": "", "start_line": 1, "end_line": 1, "content": "aa"}
    with pytest.raises(ValidationError, match="RE2"):
        _run([chunk], pattern=pattern, regex=True)


def test_the_line_step_stops_at_its_scan_cap(monkeypatch) -> None:
    from server.code.app import grep as grep_mod

    monkeypatch.setattr(grep_mod, "_LINE_SCAN_MAX_CHARS", 10)
    chunks = [
        {"path": f"{i}.py", "scope": "", "start_line": 1, "end_line": 1, "content": "hit"}
        for i in range(5)
    ]
    out = _run(chunks, pattern="hit")
    assert len(out.matches) == 3 and out.truncated
