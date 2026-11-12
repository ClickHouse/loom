"""Each reader of a statement's leading `--` and `/* */` comments
(`statement_class`, `is_read_statement`, `looks_like_sql`, `_sql_head`) skips them
in time linear in the text: `looks_like_sql` reads template literals from indexed
repositories. A comment body that may hold `*/` makes a
failed match backtrack exponentially in the number of `*/` runs."""

from __future__ import annotations

import time

import pytest

from server.code.domain.extract.lang_py_islands import _sql_head
from server.code.domain.extract.lang_ts_islands import looks_like_sql
from server.core.ch import statement_class
from server.core.ch_class_settings import is_read_statement

# 26 runs take seconds under an exponential pattern and microseconds under a linear one.
_HOSTILE = ["/**/" * 26 + "!", "/*" + "*/ /*" * 26 + "!"]


def test_leading_comments_are_skipped() -> None:
    sql = "-- a\n/* b */ /* c * / d */\nINSERT INTO t VALUES (1)"
    assert statement_class(sql) == "write"
    assert is_read_statement("/* x */ -- y\n  select 1")
    assert not is_read_statement("/* SELECT */ INSERT INTO t VALUES (1)")
    assert looks_like_sql("/* q */ SELECT a FROM t", inside_client=True)
    # A `*` just before the close still closes the comment.
    assert statement_class("/** doc **/ /***/ INSERT INTO t VALUES (1)") == "write"
    assert is_read_statement("/** doc **/ SELECT 1")
    assert looks_like_sql("/***/ SELECT a FROM t", inside_client=True)
    assert _sql_head("-- name: Get :one\n/* a */ /** b **/ SELECT 1") == "SELECT 1"


def test_a_comment_ends_at_its_first_close() -> None:
    # A known limit, not wanted behaviour: ClickHouse nests `/* */`, these readers do not,
    # so the text after the first `*/` is read as the statement's start.
    assert statement_class("/* a /* b */ (c) */ INSERT INTO t VALUES (1)") == "read"
    assert not is_read_statement("/* a /* b */ (c) */ SELECT 1")
    assert not looks_like_sql("/* a /* b */ (c) */ SELECT a FROM t", inside_client=True)
    assert not is_read_statement("/* a /* b */ x */ SELECT 1")
    assert not looks_like_sql("/* a /* b */ x */ SELECT a FROM t", inside_client=True)


@pytest.mark.parametrize("text", _HOSTILE)
def test_comment_runs_before_a_non_keyword_finish_quickly(text: str) -> None:
    started = time.monotonic()
    statement_class(text)
    is_read_statement(text)
    looks_like_sql(text, inside_client=True)
    _sql_head(text)
    assert time.monotonic() - started < 1.0
