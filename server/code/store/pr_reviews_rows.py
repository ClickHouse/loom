"""`loom.code_pr_reviews` rows from the review comments the GitHub client parsed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from server.core.datetimes import naive_utc_now


class ReviewComment(Protocol):
    """The fields of a parsed GitHub review comment the rows carry."""

    comment_id: int
    path: str
    line: int
    author: str
    is_bot: bool
    body: str
    in_reply_to: int
    created_at: datetime


FETCHED_MARKER_ID = 0


def fetched_marker_row(org: str, ns: str, pr_number: int) -> dict[str, Any]:
    """The row that says "this PR was fetched and had no inline comments".
    Without it the backfill's anti-join would re-fetch every comment-less PR
    on every run. comment_id 0 never occurs on GitHub; readers already require body length >= 40, so
    the marker is invisible to packs and to owners scoring."""
    return {
        "org": org,
        "namespace": ns,
        "pr_number": pr_number,
        "comment_id": FETCHED_MARKER_ID,
        "path": "",
        "line": 0,
        "author": "",
        "is_bot": False,
        "body": "",
        "in_reply_to": 0,
        "comment_created_at": naive_utc_now(),
    }


def review_rows(
    org: str, ns: str, pr_number: int, comments: Sequence[ReviewComment]
) -> list[dict[str, Any]]:
    """code_pr_reviews rows for one PR's comments; a fetched marker when
    there are none, so the PR is never fetched again."""
    if not comments:
        return [fetched_marker_row(org, ns, pr_number)]
    return [
        {
            "org": org,
            "namespace": ns,
            "pr_number": pr_number,
            "comment_id": c.comment_id,
            "path": c.path,
            "line": c.line,
            "author": c.author,
            "is_bot": c.is_bot,
            "body": c.body,
            "in_reply_to": c.in_reply_to,
            "comment_created_at": c.created_at,
        }
        for c in comments
    ]
