"""Signals read off merged-PR metadata that the history table already holds.

A revert is the one kind of history that says what NOT to do: the change
landed, broke something, and was undone. It is invisible to a symbol lookup
(the code is back to how it was) and to co-change (the revert touches the
same files as the original). Surfacing "this area was reverted before" on
code.symbol history and code.impact is cheap because the PR rows are
already fetched; the detection is a title/body pattern mirrored in SQL
(`CodeRepo.pr_history_for` computes `is_revert` the same way) so the two
never disagree.
"""

from __future__ import annotations

import re

# GitHub's own revert title ("Revert \"...\"") and the conventional-commit
# form ("revert: ..."), plus the trailer GitHub writes into the body.
_REVERT_TITLE = re.compile(r"^\s*revert\b", re.IGNORECASE)
_REVERT_BODY = re.compile(r"this reverts commit", re.IGNORECASE)


def is_revert_pr(title: str, body_excerpt: str = "") -> bool:
    return bool(_REVERT_TITLE.search(title or "") or _REVERT_BODY.search(body_excerpt or ""))
