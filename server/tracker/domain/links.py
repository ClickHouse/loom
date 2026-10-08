"""Issue–PR link rules the readers share with the pass: the closing-mention
pattern an upstream text match tests, and the "latest pass only" filter a
reader of `loom.qa_link_candidates` applies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def closes_pattern(number: int, repo: str = "") -> str:
    """The RE2 pattern an upstream `match(concat(title, ' ', body), …)`
    tests: a closing keyword (closes / fixes / resolves and their forms)
    followed by `#N`, the bare number or the issue's URL — GitHub's own
    linking grammar, the one the QA `fixes` edges were made from."""
    url = f"(https://github\\.com/{repo}/issues/)?" if repo else ""
    return rf"(?i)(clos|fix|resolv)\w*[: ]+{url}#?{int(number)}\b"


def latest_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows of each issue's latest pass: a pair that left the top list
    keeps its old row under ReplacingMergeTree, and must not be served
    beside the new list."""
    latest: dict[int, datetime] = {}
    for r in rows:
        n = int(r.get("issue_number") or 0)
        t = r.get("computed_at")
        if isinstance(t, datetime) and (n not in latest or t > latest[n]):
            latest[n] = t
    return [r for r in rows if r.get("computed_at") == latest.get(int(r.get("issue_number") or 0))]
