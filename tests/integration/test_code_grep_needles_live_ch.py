"""code.grep's alternation needles never change an answer.

`content_predicate` puts `multiSearchAnyCaseInsensitive(content, needles)` in front
of `match()` for a case-sensitive alternation. The needles are planned in Python
while RE2 does the matching, so this test runs both statements on a real server: the
exact matcher alone (the statement code.grep ran before) and the guarded one, over
a fixed corpus that carries what the two engines read differently (Unicode case
folding, POSIX classes, escapes, empty branches). Every pattern must return the
same rows in both modes, and enough of them must actually be guarded.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from server.code.domain.index import grep_sql
from server.code.domain.index.grep_plan import content_predicate
from tests.integration._resources import unique_org

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


def _rows(ch_client: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in ch_client.query(sql, parameters=params).result_rows]


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
    ch_client: Any, corpus: tuple[str, str]
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
                before = _rows(ch_client, build(exact, "", ""), params)
                after = _rows(ch_client, build(new, "", ""), params)
                assert after == before, (pattern, case_sensitive, new)
                matched += bool(before)
    assert guarded >= 10  # the guard ran, the comparison is not vacuous
    assert matched >= 20


def test_the_engines_differ_where_the_planner_says_they_do(ch_client: Any) -> None:
    """The reasons the planner scans case-insensitive regexes and POSIX classes."""
    row = ch_client.query(
        "SELECT match({kelvin:String}, '(?i)kelvin'), match('xabc', '[[:alpha:]]abc'), "
        "multiSearchAnyCaseInsensitive({kelvin:String}, ['kelvin'])",
        parameters={"kelvin": "Kelvin"},
    ).result_rows[0]
    assert tuple(row) == (1, 1, 0)
