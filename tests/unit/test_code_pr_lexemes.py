"""code/domain/pr_lexemes.py — the PR text the history leg scores
: template boilerplate stripped, linked issues parsed, the
row builder writes every persisted column."""

from __future__ import annotations

from datetime import UTC, datetime

from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.pr_lexemes import (
    linked_issues,
    pr_lexemes,
    strip_pr_template,
)
from server.code.store.index import CODE_PR_COLS
from server.code.store.index.rows import pr_history_row

_BODY = """Closes: https://github.com/ClickHouse/ClickHouse/issues/116268
Fixes #118729 and resolves: #118729 (dup). Related: #114783

`system.asynchronous_insert_log.query` reported the unmasked query text.
One branch reused the queue's batching key instead of calling `serializeQuery`.

### Changelog category (leave one):
- Bug Fix (user-visible misbehavior in an official stable release)

### Changelog entry (a [user-readable short description](https://github.com/x/y) of the changes):
Fixed cleartext credentials in `system.asynchronous_insert_log.query`.

### Modify your CI run
- [ ] Allow: Integration Tests
- [x] Exclude: Stateless tests

<!-- CI automatic block start :ci_links: -->
---
Workflow [[PR](https://s3.amazonaws.com/clickhouse-test-reports/praktika.html?PR=1)]
Sync PR [[sync-upstream/pr/1](https://github.com/search?q=1)]
<!-- CI automatic block end -->
Improvement of the masking also covers `system.processes`.
"""


def test_linked_issues_accepts_hash_and_url_forms_and_dedupes() -> None:
    assert linked_issues(_BODY) == [116268, 118729]
    # "Related:" is not a closing keyword; a bare #N without one is not linked
    assert linked_issues("Related: #5\nsee #6") == []
    assert linked_issues("fix:#7 CLOSES #8 Resolved #9") == [7, 8, 9]
    assert linked_issues("") == []


def test_strip_pr_template_keeps_prose_and_drops_boilerplate() -> None:
    kept = strip_pr_template(_BODY)
    assert "batching key" in kept and "serializeQuery" in kept
    assert "Fixed cleartext credentials" in kept
    # prose beginning with a category word survives; the category bullet does not
    assert "Improvement of the masking" in kept
    assert "Bug Fix (user-visible" not in kept
    for gone in (
        "Changelog category",
        "Changelog entry",
        "Modify your CI run",
        "Allow: Integration",
        "Exclude: Stateless",
        "Workflow [[PR",
        "Sync PR",
        "https://",
        "ci_links",
    ):
        assert gone not in kept, gone


def test_pr_lexemes_uses_the_chunk_tokenizer_and_folds_issue_titles() -> None:
    lex = pr_lexemes(
        "Mask secrets in `system.asynchronous_insert_log.query`",
        _BODY,
        issue_titles=["Credentials leak via async insert log"],
    )
    toks = lex.split(" ")
    assert toks == code_tokens(
        "\n".join(
            [
                "Mask secrets in `system.asynchronous_insert_log.query`",
                strip_pr_template(_BODY),
                "Credentials leak via async insert log",
            ]
        )
    )
    assert "asynchronous_insert_log" in toks and "asynchronous" in toks
    assert "credentials" in toks and "leak" in toks
    assert "changelog" not in toks and "https" not in toks
    # deterministic + idempotent: the same input stores the same string
    assert lex == pr_lexemes(
        "Mask secrets in `system.asynchronous_insert_log.query`",
        _BODY,
        issue_titles=["Credentials leak via async insert log"],
    )


def test_pr_history_row_writes_every_persisted_column() -> None:
    row = pr_history_row(
        org="o",
        namespace="code-x",
        pr_number=7,
        title="Fix crash in Foo::bar",
        body_excerpt=_BODY,
        author="alice",
        files=["src/Foo.cpp", "src/Foo.h"],
        merged_at=datetime(2026, 9, 1, tzinfo=UTC),
        linked=[116268],
    )
    assert set(row) == set(CODE_PR_COLS)
    assert row["linked_issues"] == [116268]  # the fetch path's full-body parse wins
    assert "foo" in row["lexemes"].split(" ") and "crash" in row["lexemes"].split(" ")
    assert row["version"] > 0 and row["files"] == ["src/Foo.cpp", "src/Foo.h"]
    # without an explicit list the numbers come from the stored excerpt
    assert pr_history_row(
        org="o",
        namespace="n",
        pr_number=1,
        title="t",
        body_excerpt=_BODY,
        author="a",
        files=["src/a.cpp"],
        merged_at=datetime(2026, 9, 1, tzinfo=UTC),
    )["linked_issues"] == [116268, 118729]


def test_html_comments_are_dropped_as_the_lazy_pattern_did() -> None:
    import re

    lazy = re.compile(r"<!--.*?-->", re.S)
    for body in (
        "a<!--x-->b<!--y-->c",
        "a<!-->b-->c",
        "a<!---->b",
        "a<!---b",
        "a<!--x-->b<!--open",
    ):
        assert strip_pr_template(body) == "\n".join(
            s for s in lazy.sub(" ", body).splitlines() if s.strip()
        ), body


def test_the_linked_issue_pattern_reads_what_the_greedy_runs_did() -> None:
    assert linked_issues("fix:#1 fix : #2 fix  #3 fixes https://github.com/a/b/issues/4") == [
        1,
        2,
        3,
        4,
    ]
