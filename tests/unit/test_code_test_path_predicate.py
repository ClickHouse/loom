"""The shared test-path predicate the source-only card read carries, and its
Python twin: the two name exactly the same files."""

from __future__ import annotations

import re

import pytest

from server.code.store.index import is_test_path, skip_tests_sql


def test_python_twin_matches_the_sql_shape() -> None:
    assert is_test_path("tests/queries/0_stateless/01.sql") and is_test_path(
        "src/x/tests/gtest_a.cpp"
    )
    assert (
        is_test_path("pkg/a_test.go")
        and is_test_path("ui/a.test.ts")
        and not is_test_path("src/Common/MemoryTracker.cpp")
    )


def test_predicate_names_the_column() -> None:
    p = skip_tests_sql("scope_path")
    assert "startsWith(scope_path, 'tests/')" in p and "scope_path LIKE '%\\\\_test.%'" in p
    assert p.startswith(" AND NOT (")


def _like(value: str, pattern: str) -> bool:
    """ClickHouse LIKE: `%` any run, `_` any one character, `\\` escapes."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out, i = out + re.escape(pattern[i + 1]), i + 2
            continue
        out += ".*" if c == "%" else "." if c == "_" else re.escape(c)
        i += 1
    return re.fullmatch(out, value, re.DOTALL) is not None


def _sql_skips(path: str) -> bool:
    """Evaluate the rendered predicate for one path: the SQL literals are
    unescaped the way the ClickHouse parser reads them (`\\\\` -> `\\`)."""
    sql = skip_tests_sql("path")
    prefixes = re.findall(r"startsWith\(path, '((?:[^'\\\\]|\\\\.)*)'\)", sql)
    likes = re.findall(r"path LIKE '((?:[^'\\\\]|\\\\.)*)'", sql)
    return any(path.startswith(_unescape(x)) for x in prefixes) or any(
        _like(path, _unescape(x)) for x in likes
    )


def _unescape(literal: str) -> str:
    return literal.replace("\\\\", "\\")


@pytest.mark.parametrize(
    "path",
    [
        "src/latest.cpp",
        "contest.py",
        "pkg/attest.go",
        "src/test.py",
        "tests/a.py",
        "src/x/tests/gtest_a.cpp",
        "lib/test/a.c",
        "pkg/a_test.go",
        "ui/a.test.ts",
        "ui/a.spec.ts",
        "src/Common/MemoryTracker.cpp",
    ],
)
def test_the_sql_predicate_skips_exactly_the_python_twins_files(path: str) -> None:
    assert _sql_skips(path) is is_test_path(path)
