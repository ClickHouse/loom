"""What reviewers said about this code, on the PR rows of `code.history` and
`code.blame_range`.

`loom.code_pr_reviews` mirrors every merged PR's inline review comments (tens of
thousands for a large repository); the review brief's reviewer ranking reads it too. A
PR row carries `reviews` — up to `reviews_per_pr` human comments on the queried file
(or, for blame, on a line inside the queried range), newest first, each a reviewer, a
~300-char excerpt, path, line and time — and `review_count`, the PR's total on the file,
so a client knows when there are more. One read per request covers every PR of the
answer (`CodeRepo.review_comments_on_prs`).

A comment's `line` is the line in the PR's version of the file; later edits
move code, so the blame overlap is a pointer, not a proof. Humans only, by
the same rule as the reviewer ranking (`owners.is_bot` plus the ledger's
`is_bot` flag).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

MAX_PER_PR = 10
EXCERPT_CHARS = 300
# rows one read may return: per_pr in-range rows + one count row per PR
MAX_ROWS = 600


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).isoformat()
    return str(v or "")


def _excerpt(body: str) -> str:
    text = " ".join(str(body or "").split())
    return text if len(text) < EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


def fold(
    rows: list[dict[str, Any]], pr_numbers: list[int], per_pr: int
) -> dict[int, tuple[list[dict[str, Any]], int]]:
    """pr_number -> (reviews, review_count) for every asked PR (a PR with no
    row gets `([], 0)`). Rows arrive from `review_comments_on_prs`; an
    `in_range = 0` row only carries the count. Bots are dropped again here
    (the SQL filter mirrors `owners.is_bot`; this keeps the two in step)."""
    from server.code.domain.index.owners import is_bot

    out: dict[int, tuple[list[dict[str, Any]], int]] = {int(n): ([], 0) for n in pr_numbers if n}
    for r in rows:
        n = int(r.get("pr_number") or 0)
        if n not in out:
            continue
        reviews, count = out[n]
        count = max(count, int(r.get("on_file") or 0))
        author = str(r.get("author") or "")
        if r.get("in_range") and not is_bot(author) and len(reviews) < per_pr:
            reviews.append(
                {
                    "reviewer": author,
                    "excerpt": _excerpt(str(r.get("excerpt") or "")),
                    "path": str(r.get("path") or ""),
                    "line": int(r.get("line") or 0),
                    "created_at": _iso(r.get("comment_created_at")),
                }
            )
        out[n] = (reviews, count)
    return out


async def reviews_for(
    repo: Any,
    org: str,
    ns: str,
    pr_numbers: list[int],
    paths: list[str],
    *,
    per_pr: int,
    lines: tuple[int, int] = (0, 0),
) -> dict[int, tuple[list[dict[str, Any]], int]] | None:
    """The one batched read plus `fold`; `{}` when nothing was asked
    (`per_pr = 0`, no PR, no path), None when the read failed (the rows are
    returned without reviews rather than the answer failing)."""
    from server.code.domain.index.owners import _BOT_NAMES, _BOT_SUFFIXES

    per = max(0, min(int(per_pr), MAX_PER_PR))
    nums = [int(n) for n in pr_numbers if n]
    if not per or not nums or not paths:
        return {}
    try:
        rows = await repo.review_comments_on_prs(
            org,
            ns,
            nums,
            paths,
            lines=lines,
            per_pr=per,
            bots=sorted(_BOT_NAMES),
            bot_suffixes=list(_BOT_SUFFIXES),
            excerpt_chars=EXCERPT_CHARS + 1,
            limit=MAX_ROWS,
        )
    except Exception as e:  # noqa: BLE001 — reviews enrich the rows, never gate them
        logger.info("pr_reviews: read failed for %s: %s", ns, e)
        return None
    return fold(list(rows), nums, per)


__all__ = ["EXCERPT_CHARS", "MAX_PER_PR", "fold", "reviews_for"]
