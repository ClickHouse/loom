"""code.pr_check — does a PR like this draft already exist?

The code-plane sibling of `tracker.duplicate_check`: an agent about to open a pull
request passes the draft — title, body, the paths it changes, the symbols it touches,
the issues it closes — and gets back either "an open PR is already doing this", "a
merged PR already did this", a `related` list, or `none`.

Candidates come from loom's own PR ledgers, four legs:

* open PRs of the repository (`loom.code_gh_items`, `state = 'open'`) — the
  whole set, title + the files the mirror's `refs/pull/N/head` touched; the
  ledger holds no body for these, so their text is the title alone and the
  evidence says so;
* PRs closed without merging in the last `closed_days` (same ledger) —
  "someone tried this and it did not land" is worth a line before a second
  attempt; capped at `related`;
* merged PRs that touched the draft's paths (`loom.code_pr_history`,
  `hasAny(files, paths)` under its bloom index);
* merged PRs whose text scores against the draft's title tokens — the
  history leg of code.search (idf over the PR corpus) — and
  merged PRs that close the same issues (`linked_issues`).

Every candidate is scored by the SAME pure function, `features` →
`score`, from what the two ledgers hold:

* `text`   — idf-weighted share of the draft's title tokens (and symbols)
             found in the candidate's tokens (title for open / closed PRs,
             the stored lexemes for merged ones);
* `body`   — the same share over the draft's body tokens, merged PRs only
             (the only rows with a body), weight small: bodies repeat the
             template;
* `paths`  — the draft's paths the candidate touched, as a share of the
             draft's paths and of the candidate's files (mean of the two);
             a candidate touching more than `mass_files` does not vote;
* `symbols`— share of the draft's symbols named in the candidate's tokens;
* `issue`  — the candidate closes an issue the draft closes / names.

The weights are a rule, not a fit — there is no gold of duplicate PRs to
calibrate on (a merged duplicate PR is closed, not merged) — so the op says
`epistemic: heuristic`, and the rule is shaped so that a title alone never
reaches the duplicate line: `duplicate_of` needs shared files or a shared
issue beside the words. Thresholds live in settings
(`code_pr_check_duplicate_threshold`, `_related_threshold`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.pr_lexemes import linked_issues, strip_pr_template
from server.core import bm25

W_TEXT = 0.40
W_BODY = 0.10
W_PATHS = 0.30
W_SYMBOLS = 0.15
W_ISSUE = 0.30
# The draft's words found only in a PR's description count this fraction of the
# same words in its title.
BODY_SHARE = 0.6

# `#123` anywhere in the draft (beside the closing-keyword form pr_lexemes reads)
_MENTION_RE = re.compile(r"(?<![\w/])#(\d{2,7})\b")
_BODY_TOKENS_MAX = 200
_FILES_LISTED = 5
# "Backport #118927 to 26.3: …" / "Cherry pick #114873 to 26.3: …" — the same
# change carried to a release branch by the backport bot (ClickHouse's shape)
BACKPORT_RE = re.compile(r"(?i)^\s*(?:backport|cherry[ -]?pick)\s+#(\d+)\s+to\s+([\w./-]+)\s*:")


def backport_of(title: str) -> tuple[int, str]:
    """(source PR number, target branch) when `title` is a backport / cherry
    pick of another PR, else (0, '')."""
    m = BACKPORT_RE.match(title or "")
    return (int(m.group(1)), m.group(2)) if m else (0, "")


@dataclass
class Draft:
    title: str
    body: str
    paths: list[str]
    symbols: list[str]
    issues: list[int]
    title_tokens: list[str] = field(default_factory=list)
    body_tokens: list[str] = field(default_factory=list)
    # the PR already opened for this draft (excluded from the pool; its
    # backports read as the same change)
    pr_number: int = 0

    @classmethod
    def build(
        cls,
        *,
        title: str,
        body: str,
        paths: list[str],
        symbols: list[str],
        issues: list[int],
        pr_number: int = 0,
    ) -> Draft:
        clean_body = strip_pr_template(body or "")
        nums = list(
            dict.fromkeys(
                [int(n) for n in issues if int(n) > 0]
                + linked_issues(body or "")
                + [int(m.group(1)) for m in _MENTION_RE.finditer(f"{title}\n{body or ''}")]
            )
        )
        symbol_tokens = code_tokens(" ".join(symbols))
        title_tokens = list(dict.fromkeys(code_tokens(title or "") + symbol_tokens))
        body_tokens = [
            t for t in code_tokens(clean_body)[:_BODY_TOKENS_MAX] if t not in set(title_tokens)
        ]
        return cls(
            title=title or "",
            body=clean_body,
            paths=list(dict.fromkeys(p.strip() for p in paths if p and p.strip())),
            symbols=list(dict.fromkeys(s.strip() for s in symbols if s and s.strip())),
            issues=nums,
            title_tokens=title_tokens,
            body_tokens=body_tokens,
            pr_number=int(pr_number or 0),
        )

    def empty(self) -> bool:
        return not (self.title_tokens or self.paths or self.issues)


@dataclass
class Candidate:
    number: int
    state: str  # open | merged | closed
    title: str
    author: str = ""
    files: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    linked_issues: list[int] = field(default_factory=list)
    opened_at: Any = None
    merged_at: Any = None
    closed_at: Any = None
    labels: list[str] = field(default_factory=list)
    has_body: bool = False
    legs: set[str] = field(default_factory=set)

    def backport(self) -> tuple[int, str]:
        return backport_of(self.title)


def idf_table(tokens: list[str], dfs: list[int], n_prs: int) -> dict[str, float]:
    """BM25-style idf per token over the PR corpus; every token 1.0 when the
    corpus is empty (a namespace whose PR history is not synced yet), so the
    shares still read as plain fractions."""
    if n_prs <= 0 or len(dfs) != len(tokens):
        return {t: 1.0 for t in tokens}
    out: dict[str, float] = {}
    for t, df in zip(tokens, dfs, strict=True):
        out[t] = max(bm25.idf(df, n_prs), 0.05)
    return out


def _share(tokens: list[str], have: set[str], idf: dict[str, float]) -> tuple[float, list[str]]:
    if not tokens:
        return 0.0, []
    total = sum(idf.get(t, 1.0) for t in tokens)
    hit = [t for t in tokens if t in have]
    got = sum(idf.get(t, 1.0) for t in hit)
    return (got / total if total > 0 else 0.0), hit


def features(
    draft: Draft,
    cand: Candidate,
    idf: dict[str, float],
    *,
    mass_files: int = 200,
) -> dict[str, Any]:
    # the draft's words in the candidate's TITLE count most: a PR whose title is
    # the draft's subject must outrank one whose description only mentions it
    anywhere, text_hits = _share(draft.title_tokens, cand.tokens, idf)
    in_title, title_hits = _share(draft.title_tokens, set(code_tokens(cand.title)), idf)
    text = max(in_title, BODY_SHARE * anywhere)
    body, _ = _share(draft.body_tokens, cand.tokens, idf) if cand.has_body else (0.0, [])
    shared = [p for p in draft.paths if p in set(cand.files)]
    mass = len(cand.files) > mass_files
    if draft.paths and cand.files and not mass:
        path_share = len(shared) / len(draft.paths)
        path_focus = len(shared) / len(cand.files)
        paths = 0.5 * (path_share + path_focus)
    else:
        path_share = path_focus = paths = 0.0
    sym_tokens = [
        t for t in code_tokens(" ".join(draft.symbols)) if t not in set(code_tokens(draft.title))
    ]
    symbols, _ = _share(sym_tokens, cand.tokens, idf) if sym_tokens else (0.0, [])
    issue_hits = sorted(set(draft.issues) & set(cand.linked_issues))
    return {
        "text": round(text, 3),
        "text_hits": title_hits[:12],
        "body_hits": [t for t in text_hits if t not in set(title_hits)][:12],
        "body": round(body, 3),
        "paths": round(paths, 3),
        "path_share": round(path_share, 3),
        "path_focus": round(path_focus, 3),
        "shared_paths": shared,
        "mass_change": mass,
        "symbols": round(symbols, 3),
        "issue": 1.0 if issue_hits else 0.0,
        "issue_hits": issue_hits,
    }


def score(f: dict[str, Any]) -> float:
    s = (
        W_TEXT * float(f["text"])
        + W_BODY * float(f["body"])
        + W_PATHS * float(f["paths"])
        + W_SYMBOLS * float(f["symbols"])
        + W_ISSUE * float(f["issue"])
    )
    return round(min(max(s, 0.0), 1.0), 3)


def _when(v: Any) -> str:
    s = str(v or "")
    return s[:10] if s and not s.startswith("1970") else ""


def evidence(draft: Draft, cand: Candidate, f: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if cand.state == "open":
        lines.append(
            f"open PR by {cand.author or 'unknown'}"
            + (f", opened {_when(cand.opened_at)}" if _when(cand.opened_at) else "")
        )
    elif cand.state == "merged":
        lines.append(f"merged {_when(cand.merged_at)} by {cand.author or 'unknown'}".rstrip())
    else:
        lines.append(f"closed without merging {_when(cand.closed_at)}".rstrip())
    shared = list(f.get("shared_paths") or [])
    listed = ", ".join(shared[:_FILES_LISTED]) + (", …" if len(shared) > _FILES_LISTED else "")
    if f.get("mass_change"):
        lines.append(
            f"touches {len(shared)}/{len(draft.paths)} of your files inside a "
            f"{len(cand.files)}-file change (a mass change; files do not vote)"
        )
    elif shared:
        lines.append(f"touches {len(shared)}/{len(draft.paths)} of your files: {listed}")
    elif draft.paths and cand.state == "open" and not cand.files:
        lines.append("files of this open PR not indexed yet")
    src, branch = cand.backport()
    if src:
        whose = "your PR" if src == draft.pr_number and draft.pr_number else f"#{src}"
        lines.append(f"backport of {whose} to {branch}")
    hits = list(f.get("text_hits") or [])
    if hits:
        lines.append(f"title shares: {', '.join(hits[:8])}")
    body_hits = list(f.get("body_hits") or [])
    if body_hits:
        lines.append(f"body mentions: {', '.join(body_hits[:8])}")
    if not cand.has_body and cand.state != "merged":
        lines.append("body not indexed for this PR; matched on title and files")
    for n in f.get("issue_hits") or []:
        lines.append(f"closes the same issue #{n}")
    if float(f.get("symbols") or 0) > 0:
        named = int(round(float(f["symbols"]) * len(draft.symbols)))
        lines.append(f"names {named}/{len(draft.symbols)} of your symbols")
    return lines


def verdict(
    scored: list[tuple[Candidate, dict[str, Any], float]],
    *,
    duplicate: float,
    related: float,
) -> tuple[str, bool]:
    """(`duplicate_of` | `related` | `none`, undecided). A closed-unmerged
    PR is capped at `related`. `undecided` when the top candidate sits in
    the upper half of the related band."""
    best = 0.0
    dup = False
    for cand, _f, s in scored:
        best = max(best, s)
        if s >= duplicate and cand.state != "closed":
            dup = True
    if dup:
        return "duplicate_of", False
    if best >= related:
        return "related", best >= (related + duplicate) / 2
    return "none", False


def relation(cand: Candidate, s: float, *, duplicate: float) -> str:
    return "duplicate" if s >= duplicate and cand.state != "closed" else "related"


__all__ = [
    "BACKPORT_RE",
    "Candidate",
    "Draft",
    "backport_of",
    "evidence",
    "features",
    "idf_table",
    "relation",
    "score",
    "verdict",
]
