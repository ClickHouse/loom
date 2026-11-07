"""Code.grep's chunk statements scan without FINAL and without
the n-gram index, picking each key's newest version by LIMIT 1 BY.

Two layers. The builders' SQL is pinned (no FINAL, no index guard, the pick
before the tombstone filter, the files total as a window). And the statements
the repository issues are answered twice over the same multi-version fixture:
once with the scan's own semantics (filter, pick the newest admitted version,
drop tombstones) and once with FINAL's (collapse each key to its newest
version, then filter), and the results of `grep_chunks`, `grep_files` and
`grep_message_chunks` must be equal. The fixture carries what makes the two
differ when the version invariant breaks: a tombstone newer than a live copy,
a live copy newer than a tombstone, a moved span, a chunk under two embedding
labels.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from server.code.domain.index import grep_sql
from server.code.store.index import CodeRepo
from server.settings import settings

ORG, NS = "o", "n"
DEAD = "2026-09-25 00:00:00"

# (embedding_model, path, chunk_id, version, start_line, end_line, scope, content, tombstoned_at)
FIXTURE: list[tuple[str, str, str, int, int, int, str, str, str | None]] = [
    # newest version live, older version tombstoned: live
    ("m1", "src/A.cpp", "a1", 1, 10, 20, "A::f", 'throw Exception("Cannot parse input")', DEAD),
    ("m1", "src/A.cpp", "a1", 2, 12, 22, "A::f", 'throw Exception("Cannot parse input")', None),
    # newest version a tombstone over a live copy: gone
    ("m1", "src/A.cpp", "a2", 1, 30, 40, "A::g", 'LOG_ERROR(log, "Cannot parse input")', None),
    ("m1", "src/A.cpp", "a2", 5, 30, 40, "A::g", 'LOG_ERROR(log, "Cannot parse input")', DEAD),
    # a moved span: the newest span is the one served
    ("m1", "src/B.cpp", "b1", 3, 5, 9, "B::h", "parseInput(x); // cannot parse input", None),
    ("m1", "src/B.cpp", "b1", 7, 105, 109, "B::h", "parseInput(x); // cannot parse input", None),
    # one chunk under two labels: two rows, as under FINAL
    ("m1", "src/C.cpp", "c1", 1, 1, 3, "", "PARSEINPUT macro", None),
    ("m2", "src/C.cpp", "c1", 2, 1, 3, "", "PARSEINPUT macro", None),
    # no match
    ("m1", "src/D.cpp", "d1", 1, 1, 2, "", "unrelated code", None),
    ("m1", "tests/t.sql", "t1", 1, 1, 1, "", "SELECT parseInput(1)", None),
]


def _row(t: tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "embedding_model",
        "path",
        "chunk_id",
        "version",
        "start_line",
        "end_line",
        "scope",
        "content",
        "tombstoned_at",
    )
    r = dict(zip(keys, t, strict=True))
    r["symbol_ids"] = [f"{r['path']}#{r['scope']}"] if r["scope"] else []
    return r


def _admits(sql: str, params: dict[str, Any], content: str) -> bool:
    if "multiSearchAnyCaseInsensitive" in sql:
        low = content.lower()
        return any(n.lower() in low for n in params["needles"]) and any(
            k in content for k in params["keywords"]
        )
    ok = True
    for name, like in params.items():  # the index guard, when present
        if name.startswith(("gp_", "gn_")):
            ok = ok and like.strip("%").replace("\\_", "_") in content.lower()
    if "match(content" in sql:
        return ok and re.search(params["pat"], content) is not None
    if "positionCaseInsensitive" in sql:
        return ok and params["pat"].lower() in content.lower()
    return ok and params["pat"] in content


def _newest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["embedding_model"], r["path"], r["chunk_id"])
        if k not in best or r["version"] > best[k]["version"]:
            best[k] = r
    return list(best.values())


class _CH:
    """Answers the scan statements on FIXTURE with the scan's semantics."""

    def __init__(self, fixture: list[tuple[Any, ...]] = FIXTURE) -> None:
        self.rows = [_row(t) for t in fixture]
        self.statements: list[str] = []

    def _live(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        scoped = [
            r
            for r in self.rows
            if not params.get("prefix") or r["path"].startswith(params["prefix"])
        ]
        assert f"LIMIT 1 BY {grep_sql.NEWEST_BY}" in sql and "ORDER BY version DESC" in sql
        picked = _newest([r for r in scoped if _admits(sql, params, r["content"])])
        return [r for r in picked if r["tombstoned_at"] is None]

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(sql)
        live = self._live(sql, params)
        lim = int(params["limit"])
        if "count() AS chunks" in sql:
            by: dict[str, list[int]] = {}
            for r in live:
                by.setdefault(r["path"], []).append(r["start_line"])
            out = [
                {"path": p, "chunks": len(v), "first_line": min(v), "total": len(by)}
                for p, v in sorted(by.items())
            ]
            return out[:lim]
        if "shingles" in sql:
            rows = [
                r
                | {
                    "shingles": sum(
                        1 for n in params["needles"] if n.lower() in r["content"].lower()
                    ),
                    "admitted_files": len({x["path"] for x in live}),
                }
                for r in live
            ]
            rows.sort(key=lambda r: (-r["shingles"], r["path"], r["start_line"], r["chunk_id"]))
            return [_public(r, message=True) for r in rows[:lim]]
        live.sort(key=lambda r: (r["path"], r["start_line"], r["chunk_id"]))
        return [_public(r) for r in live[:lim]]


class _FinalCH(_CH):
    """The same statements answered with FINAL's semantics: the oracle."""

    def _live(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        scoped = [
            r
            for r in self.rows
            if not params.get("prefix") or r["path"].startswith(params["prefix"])
        ]
        picked = [r for r in _newest(scoped) if _admits(sql, params, r["content"])]
        return [r for r in picked if r["tombstoned_at"] is None]


def _public(r: dict[str, Any], *, message: bool = False) -> dict[str, Any]:
    cols = ["path", "scope", "symbol_ids", "start_line", "end_line", "content"]
    if message:
        cols += ["shingles", "admitted_files"]
    return {c: r[c] for c in cols}


def _both(call: Any, fixture=FIXTURE) -> tuple[Any, Any, _CH]:
    """(FINAL oracle's answer, the scan's answer, the scan's stub)."""
    final_ch, scan_ch = _FinalCH(fixture), _CH(fixture)
    oracle = asyncio.run(call(CodeRepo(final_ch)))  # type: ignore[arg-type]
    got = asyncio.run(call(CodeRepo(scan_ch)))  # type: ignore[arg-type]
    return oracle, got, scan_ch


CASES = [
    dict(pattern="Cannot parse input"),
    dict(pattern="cannot parse input", case_sensitive=False),
    dict(pattern="parseInput"),
    dict(pattern="parse[A-Z]nput", regex=True),
    dict(pattern="PARSEINPUT"),
    dict(pattern="parseInput", path_prefix="src/"),
    dict(pattern="absent literal"),
]


@pytest.mark.parametrize("kw", CASES)
def test_matches_agree_with_final(kw) -> None:
    old, new, new_ch = _both(lambda repo: repo.grep_chunks(ORG, NS, limit=60, **kw))
    assert old == new
    assert "FINAL" not in new_ch.statements[0]


@pytest.mark.parametrize("kw", CASES)
def test_files_agree_with_final(kw) -> None:
    old, new, new_ch = _both(lambda repo: repo.grep_files(ORG, NS, limit=500, **kw))
    assert old == new
    assert len(new_ch.statements) == 1


def test_files_total_counts_past_the_limit() -> None:
    old, new, _ = _both(
        lambda repo: repo.grep_files(ORG, NS, "parse", case_sensitive=False, limit=1)
    )
    assert old == new
    files, total = new
    assert len(files) == 1 and total == 4  # src/A, src/B, src/C (PARSEINPUT), tests/t.sql


@pytest.mark.parametrize(
    "needles", [["cannot parse input"], ["parse input", "no such"], ["zzz qqq"]]
)
def test_message_agrees_with_final(needles) -> None:
    old, new, new_ch = _both(lambda repo: repo.grep_message_chunks(ORG, NS, needles, limit=60))
    assert old == new
    assert "FINAL" not in new_ch.statements[0]


def test_the_fixture_discriminates_the_pick_order() -> None:
    """The newest version is picked before the tombstone filter: a live copy
    under a newer tombstone (a2) is gone, a tombstone under a newer live copy
    (a1) is served at its newest span — filtering first would get both wrong."""
    _, new, _ = _both(lambda repo: repo.grep_chunks(ORG, NS, "Cannot parse input", limit=60))
    assert [(r["path"], r["start_line"]) for r in new] == [("src/A.cpp", 12)]
    _, moved, _ = _both(lambda repo: repo.grep_chunks(ORG, NS, "parseInput(x)", limit=60))
    assert [(r["path"], r["start_line"]) for r in moved] == [("src/B.cpp", 105)]


def test_parity_rests_on_the_version_invariant() -> None:
    """If a key's versions carried different content, filter-then-pick and
    FINAL would disagree — the version-invariant columns are what make the
    scan exact (VERSION_INVARIANT_COLS)."""
    broken = [
        ("m1", "src/X.cpp", "x1", 1, 1, 2, "", "needle here", None),
        ("m1", "src/X.cpp", "x1", 2, 1, 2, "", "content changed", None),
    ]
    old, new, _ = _both(lambda repo: repo.grep_chunks(ORG, NS, "needle", limit=60), fixture=broken)
    assert old == [] and len(new) == 1


# --------------------------------------------------------------------- SQL


def test_scan_statements_carry_no_final_no_guard_and_the_pick() -> None:
    for sql in (
        grep_sql.grep_chunks_sql("position(content, {pat:String}) > 0", "", ""),
        grep_sql.grep_files_sql("position(content, {pat:String}) > 0", "", ""),
        grep_sql.grep_message_sql("", ""),
    ):
        flat = " ".join(sql.split())
        assert "FINAL" not in flat
        assert "LIKE" not in flat
        assert "ORDER BY version DESC LIMIT 1 BY embedding_model, path, chunk_id" in flat
        # the tombstone filter sits OUTSIDE the pick
        assert flat.index("LIMIT 1 BY") < flat.index("WHERE tombstoned_at IS NULL")
        assert flat.endswith(grep_sql.GREP_SCAN_SETTINGS)


def test_files_statement_carries_the_total_as_a_window() -> None:
    flat = " ".join(grep_sql.grep_files_sql("1", "", "").split())
    assert "count() OVER () AS total" in flat
    assert "uniqExact" not in flat


def test_page_order_is_deterministic() -> None:
    assert "ORDER BY path, start_line, chunk_id" in grep_sql.grep_chunks_sql("1", "", "")
    assert "ORDER BY shingles DESC, path, start_line, chunk_id" in grep_sql.grep_message_sql("", "")


def test_scan_predicate_has_no_index_guard() -> None:
    ch = _CH()
    asyncio.run(CodeRepo(ch).grep_chunks(ORG, NS, "parseInput", limit=5))  # type: ignore[arg-type]
    assert "lower(content)" not in ch.statements[0]


def test_empty_files_answer_is_zero_total() -> None:
    files, total = asyncio.run(
        CodeRepo(_CH()).grep_files(ORG, NS, "absent literal", limit=5)  # type: ignore[arg-type]
    )
    assert files == [] and total == 0


# ------------------------------------------------------------------ budgets


def test_code_grep_prices_files_and_message() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["code.grep"]
    assert op.budget_p99_ms == 600
    assert op.shape_budgets_p99_ms == {"files": 600, "message": 2000}


@pytest.mark.parametrize("mode,shape", [("files", "files"), ("matches", "")])
def test_the_handler_notes_the_mode_it_served(monkeypatch, mode, shape) -> None:
    from unittest.mock import patch

    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep
    from server.observability.budget_shape import current_budget_shape, note_budget_shape

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", NS)

    async def _empty(*a, **k):
        return {}

    async def go() -> str:
        note_budget_shape("stale")
        with (
            patch.object(grep, "CodeRepo", return_value=CodeRepo(_CH())),
            patch.object(grep, "index_as_of", new=_empty),
        ):
            await grep.handle_code_grep(
                CodeGrepInput(org=ORG, namespace=NS, pattern="parseInput", mode=mode)
            )
        return current_budget_shape()

    assert asyncio.run(go()) == shape


# ------------------------------------------------ handler: spans and as_of


class _SpanCH:
    """Answers the span read; records statements."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        rows = [
            {
                "tier": "treesitter",
                "qualified_name": "A::f",
                "symbol_id": "s1",
                "name": "f",
                "path": "src/A.cpp",
                "start_line": 10,
                "end_line": 90,
            },
            {
                "tier": "scip",
                "qualified_name": "A::f",
                "symbol_id": "s1",
                "name": "f",
                "path": "src/A.cpp",
                "start_line": 10,
                "end_line": 10,
            },
        ]
        return [r for r in rows if r["symbol_id"] in params["ids"]]


def test_span_read_is_one_statement_with_the_key_discovery_as_a_subquery() -> None:
    ch = _SpanCH()
    rows = asyncio.run(CodeRepo(ch).symbol_spans_by_ids(ORG, NS, ["s1"]))  # type: ignore[arg-type]
    assert len(ch.statements) == 1
    flat = ch.statements[0]
    assert "IN ( SELECT tier, qualified_name, symbol_id FROM loom.code_symbol_ids" in flat
    assert "SELECT *" not in flat
    # the same collapse as symbols_by_ids: the compiler row keeps the parser extent
    assert [(r["tier"], r["start_line"], r["end_line"]) for r in rows] == [("scip", 10, 90)]
    assert asyncio.run(CodeRepo(ch).symbol_spans_by_ids(ORG, NS, [])) == []  # type: ignore[arg-type]


def test_as_of_runs_beside_the_chunk_read(monkeypatch) -> None:
    """The freshness block is started before the grep statement and awaited
    at the end: a slow as_of and a slow grep overlap instead of adding."""
    from unittest.mock import patch

    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    events: list[str] = []

    async def slow_as_of(*a, **k):
        events.append("as_of:start")
        await asyncio.sleep(0.2)
        events.append("as_of:end")
        return {"commit": "c"}

    class _Repo(CodeRepo):
        async def grep_chunks(self, *a, **k):
            events.append("chunks:start")
            await asyncio.sleep(0.2)
            events.append("chunks:end")
            return [
                {
                    "path": "src/B.cpp",
                    "scope": "",
                    "symbol_ids": [],
                    "start_line": 1,
                    "end_line": 1,
                    "content": "parseInput(x)",
                }
            ]

    async def go() -> Any:
        with (
            patch.object(grep, "CodeRepo", return_value=_Repo(_CH())),
            patch.object(grep, "index_as_of", new=slow_as_of),
        ):
            return await grep.handle_code_grep(
                CodeGrepInput(org=ORG, namespace=NS, pattern="parseInput")
            )

    out = asyncio.run(go())
    assert out.as_of == {"commit": "c"} and len(out.matches) == 1
    # Each read starts before the other ends: overlapped, not one after the other.
    assert events.index("as_of:start") < events.index("chunks:end")
    assert events.index("chunks:start") < events.index("as_of:end")
