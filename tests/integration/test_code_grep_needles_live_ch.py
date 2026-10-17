"""The grep plan's guards never change an answer.

Two guards come from a plan made in Python while RE2 does the matching:
code.grep's `multiSearchAnyCaseInsensitive(content, needles)` in front of `match()`
for an alternation, and code.search's `lower(content) LIKE` terms. This test runs
each guarded statement and the exact matcher alone on a real server, as the reader
user both ops run as, over a fixed corpus that carries what the two engines read
differently (Unicode case folding, POSIX classes, escapes, empty branches). Every
pattern must return the same rows both ways, and enough of them must be guarded.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from server.code.domain.index import grep_sql
from server.code.domain.index.grep_plan import GREP_FINAL_SETTINGS, content_predicate
from server.code.domain.search_scope import build_scope
from tests.integration._resources import ch_client_from_env, unique_org

CORPUS = [
    'throw Exception(ErrorCodes::NOT_IMPLEMENTED, "WITH TOTALS is not supported")',
    "if (join.supportsTotals()) setTotals(block);",
    'auto totals = getTotals();\nLOG_DEBUG(log, "grace_hash totals");',
    "// TODO: remove\n// FIXME later",
    "todo and fixme in lower case",
    "Kelvin units",  # KELVIN SIGN: RE2 folds it to 'k' under (?i)
    "ſtd::string",  # LATIN SMALL LETTER LONG S: folds to 's'
    "café naïve CAFÉ NAÏVE",
    "x[[:alpha:]]abc and xabc",
    "50% of a_b\\c",
    "getQuota()->used(1)",
    "line one\nline two\n\nTODO at the start\n",
    "",
    "unrelated content",
]

PATTERNS = [
    r"WITH TOTALS.*not supported|totals.*not supported.*join",
    r"getTotals\(\)|setTotals\(",
    r"TODO|FIXME",
    r"^TODO|^// FIXME",
    r"\bTODO\b|\bfixme\b",
    r"(?i)kelvin|zzzz",
    r"(?i:kelvin)|qqqq",
    r"kelvin|std::string",
    r"café|naïve",
    r"CAFÉ|NAÏVE",
    r"[[:alpha:]]abc|zzzz",
    r"50% of|a_b\\c",
    r"getQuota\(\)->used|quota->used\(",
    r"(|abc)",
    r"abc|",
    r"\x41BC|totals",
    r"(?s)line one.line two|nothing",
    r"line (one|two)",
    r"[0-9]+|\s+",
]


def _rows(client: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in client.query(sql, parameters=params).result_rows]


@pytest.fixture
def reader() -> Iterator[Any]:
    user = os.environ.get("LOOM_CH_READER_USER") or pytest.skip("needs LOOM_CH_READER_USER")
    client = ch_client_from_env(username=user)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def corpus(ch_client: Any) -> tuple[str, str]:
    org, ns = unique_org("grep-needles"), "code-needles"
    ch_client.insert(
        "loom.code_chunks",
        [
            # a full-width vector: the column's quantize codec refuses an empty one
            [org, ns, "m", f"src/f{i}.cpp", uuid.uuid4(), i + 1, i + 1, text, [0.0] * 3072, 1]
            for i, text in enumerate(CORPUS)
        ],
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "path",
            "chunk_id",
            "start_line",
            "end_line",
            "content",
            "embedding",
            "version",
        ],
    )
    return org, ns


def test_needles_return_exactly_what_the_exact_matcher_returns(
    reader: Any, corpus: tuple[str, str]
) -> None:
    org, ns = corpus
    guarded = matched = 0
    for pattern in PATTERNS:
        for case_sensitive in (True, False):
            exact = "match(content, {pat:String})"
            new, params = content_predicate(
                pattern, regex=True, case_sensitive=case_sensitive, index_guard=False
            )
            params.update(org=org, ns=ns, limit=100)
            guarded += new != exact
            for build in (grep_sql.grep_chunks_sql, grep_sql.grep_files_sql):
                before = _rows(reader, build(exact, "", ""), params)
                after = _rows(reader, build(new, "", ""), params)
                assert after == before, (pattern, case_sensitive, new)
                matched += bool(before)
    assert guarded >= 10  # the guard ran, the comparison is not vacuous
    assert matched >= 20


def test_search_like_guard_returns_exactly_what_the_exact_matcher_returns(
    reader: Any, corpus: tuple[str, str]
) -> None:
    org, ns = corpus
    statement = (
        "SELECT path FROM loom.code_chunks WHERE org = {org:String} AND namespace = {ns:String} "
        "AND {cond} ORDER BY path"
    )
    guarded = 0
    for pattern in PATTERNS:
        for case_sensitive in (True, False):
            cond, params = build_scope(
                pattern=pattern, regex=True, case_sensitive=case_sensitive
            ).content_condition()
            params.update(org=org, ns=ns)
            exact = "match(content, {sc_pat:String})"
            guarded += cond != exact
            before = _rows(reader, statement.replace("{cond}", exact), params)
            after = _rows(reader, statement.replace("{cond}", cond), params)
            assert after == before, (pattern, case_sensitive, cond)
    assert guarded >= 20


def test_the_engines_differ_where_the_planner_says_they_do(ch_client: Any) -> None:
    """The reasons the planner scans case-insensitive regexes and POSIX classes."""
    row = ch_client.query(
        "SELECT match({kelvin:String}, '(?i)kelvin'), match('xabc', '[[:alpha:]]abc'), "
        "multiSearchAnyCaseInsensitive({kelvin:String}, ['kelvin'])",
        parameters={"kelvin": "Kelvin"},
    ).result_rows[0]
    assert tuple(row) == (1, 1, 0)


def _granules(reader: Any, sql: str, params: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """(kept, total) granules per EXPLAIN index block: `PrimaryKey` and the text index."""
    lines = [
        r[0].strip()
        for r in reader.query("EXPLAIN indexes = 1 " + sql, parameters=params).result_rows
    ]
    assert "Name: idx_code_chunks_content_ng" in lines, "\n".join(lines)
    out = {}
    for key, header in (("pk", "PrimaryKey"), ("text", "Name: idx_code_chunks_content_ng")):
        at = lines.index(header)
        granules = next(line for line in lines[at:] if line.startswith("Granules:"))
        kept, total = granules.removeprefix("Granules:").strip().split("/")
        out[key] = (int(kept), int(total))
    return out


def test_a_case_insensitive_search_regex_still_prunes_on_the_text_index(
    ch_client: Any, reader: Any
) -> None:
    """code.search's guard for a case-insensitive regex stays index-visible and
    selective: EXPLAIN shows the n-gram index keeping only the granule with the
    match, and the chunk that matches only through the Kelvin sign comes back."""
    org, ns = unique_org("grep-explain"), "code-explain"
    rows = [
        [org, ns, "m", f"src/f{i:05d}.cpp", uuid.uuid4(), 1, 1, f"filler body {i}", [0.0] * 3072, 1]
        for i in range(3072)  # paths sort in insert order
    ]
    # far enough apart to land in different granules however the table splits them
    # (by row count, or by bytes: each row carries a 12 KB vector)
    match, near_misses = 1664, (128, 2944)
    # matches (?i)checkStackSize only through the Kelvin sign, which lower() leaves alone
    rows[match][7] = "void chec\u212aStackSize();"
    # every piece of the identifier, never the whole of it
    rows[near_misses[0]][7] = "check the stack size"
    rows[near_misses[1]][7] = "stackSize; check"
    ch_client.insert(
        "loom.code_chunks",
        rows,
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "path",
            "chunk_id",
            "start_line",
            "end_line",
            "content",
            "embedding",
            "version",
        ],
    )
    for pattern in ("checkStackSize", r"getTotals\(\)|checkStackSize"):
        cond, params = build_scope(pattern=pattern, regex=True).content_condition()
        params.update(org=org, ns=ns)
        sql = (
            "SELECT path FROM loom.code_chunks FINAL WHERE org = {org:String} "
            f"AND namespace = {{ns:String}} AND tombstoned_at IS NULL AND {cond} "
            f"{GREP_FINAL_SETTINGS}"
        )
        granules = _granules(reader, sql, params)
        _, total = granules["pk"]
        assert total >= 3, granules  # the match and the near misses sit apart
        assert granules["text"] == (1, total), (pattern, cond, granules)
        assert _rows(reader, sql, params) == [(f"src/f{match:05d}.cpp",)], pattern
