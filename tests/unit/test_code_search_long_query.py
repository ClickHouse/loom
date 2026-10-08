"""A long code.search query (a pasted issue body) is clipped, never refused:
the search trims it to its rarest terms itself, and a 422 lost the search."""

from __future__ import annotations

from server.code.api.models_search import QUERY_MAX_CHARS, CodeSearchInput


def test_a_long_issue_body_is_accepted_and_clipped() -> None:
    body = "Traceback (most recent call last): " + "frame line " * 3000
    q = CodeSearchInput(org="o", namespace="code-x", query=body)
    assert q.query == body[:QUERY_MAX_CHARS]


def test_a_query_within_the_bound_is_unchanged() -> None:
    body = "x" * 4000
    assert CodeSearchInput(org="o", namespace="code-x", query=body).query == body
