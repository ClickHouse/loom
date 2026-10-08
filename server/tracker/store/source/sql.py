"""Statement fragments more than one reader family shares: where an edge's
other end lives, the original-PR predicate, and the token predicates the
upstream text index serves in any case.
"""

from __future__ import annotations

from typing import Any

# where an edge's other end lives, by its `to_type` / `from_type`
KIND_TABLES: dict[str, str] = {
    "github_issue": "github_issues",
    "github_issue_private": "github_issues",
    "github_pr": "github_prs",
    "github_pr_private": "github_prs",
    "core_incident": "core_incidents",
    "support_case": "support_cases",
    "slack_thread": "slack_threads",
}
RESTRICTED_TABLES = frozenset({"core_incidents", "support_cases", "slack_threads"})
# an original PR, not its derivatives: sync, backport and cherry-pick PRs repeat a
# change already counted (the flags lag the titles upstream — verified live)
ORIGINAL_PR_PRED = (
    "is_sync_pr = 0 AND is_backport_pr = 0 AND is_cherrypick_pr = 0"
    " AND position(title, '[Sync]') = 0 AND NOT startsWith(title, 'Backport')"
    " AND NOT startsWith(title, 'Cherry pick')"
)


def text_tokens(text: str) -> list[str]:
    """splitByNonAlpha-compatible tokens of a test name (`02345_keeper_x` →
    02345 keeper x), case kept: the upstream text index is case-sensitive
    (verified live: 'Keeper' 167 titles, 'keeper' 202, either 370)
    and `_case_variants` supplies the spellings."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text or "":
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _case_variants(tok: str) -> list[str]:
    """The spellings a token takes in titles and bodies: as typed, lower,
    Capitalised and UPPER."""
    return sorted({tok, tok.lower(), tok.upper(), tok.capitalize()})


def all_tokens(col: str, tokens: list[str], params: dict[str, Any], *, key: str = "t") -> str:
    """`col` carries every token in any case: one `hasAnyTokens` per token,
    each served by the upstream text index. `hasAllTokens(lower(col), …)`
    reads the same rows but bypasses the index (EXPLAIN indexes = 1, verified
    live); this form keeps the PREWHERE on the index."""
    parts: list[str] = []
    for i, tok in enumerate(t for t in tokens if t):
        name = f"{key}{i}"
        params[name] = _case_variants(tok)
        parts.append(f"hasAnyTokens({col}, {{{name}:Array(String)}})")
    return "(" + " AND ".join(parts) + ")" if parts else "1"


def any_tokens(col: str, tokens: list[str], params: dict[str, Any], *, key: str = "ta") -> str:
    """`col` carries at least one of the tokens, in any case."""
    params[key] = sorted({v for t in tokens if t for v in _case_variants(t)})
    return f"hasAnyTokens({col}, {{{key}:Array(String)}})"


def text_match(tokens: list[str], params: dict[str, Any]) -> str:
    """Title or body carries every token (the two indexed columns share the
    token parameters)."""
    return f"({all_tokens('title', tokens, params)} OR {all_tokens('body', tokens, params)})"
