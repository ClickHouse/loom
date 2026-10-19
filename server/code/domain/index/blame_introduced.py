"""Pure helpers for the second stage of `code.blame_range`: from "the PR that LAST
touched these lines" to "the PR that introduced the behaviour".

The first stage's ranking is exact for last-touched, but last-touched is only a proxy for
"introduced": against developer-labelled data it reaches about 0.73 precision, and a sizeable share
of inducing changes are invisible to blame (a later refactor took the line) or blameless. This stage
re-ranks the same candidates with three records the first stage already has or can read within a
bound:

- DEMOTIONS from the ledger and the diff: a backport / cherry-pick / sync
  PR, a revert (title or body), a PR another window PR
  reverted, and a PR whose change to the file is whitespace-only or an
  identifier-consistent rename (one `git diff -w -U0 parent..merge -- path`
  per candidate, at most MAX_DIFF_CALLS per request, cached). A title that
  says refactor / rename / style (`meta`) is a SOFT tier —
  such titles lie often enough that only the diff demotes outright.
- PROMOTIONS from the function's record: the
  enclosing symbol's bare name in the PR's title or body (`named`), a human
  review comment inside the span, the QA diff naming the symbol when the QA
  namespace is bound (else the bounded diff's hunk headers, which name the
  enclosing declaration as of the PR's own commit); and — when the caller
  says what misbehaves (`mentions`) — a linked issue whose title carries
  those words ranks first, a PR title that does ranks after blame.
- RE-LANDS: a PR merged after a revert whose title equals
  the reverted PR's (modulo `Revert "…"` / `Reland` wrappers, quotes and a
  trailing `(#N)`), or whose body names the reverted PR (`#N`, "re-land of",
  "relands", "revert of the revert"), is `reland_of: N`. The reverted PR is
  then NOT demoted (`relanded_by: R` — its code lives on through the re-land)
  and the re-land inherits the original's symbol marks; a re-land is never a
  revert, whatever its body says about the revert it undoes
  (`Revert "Revert …"` is the same case with GitHub's title).
- BLAMELESS FALLBACK: when no blamed line lies inside the window, the
  window's file-touching PRs are ranked by symbol touch, then span comments,
  then recency, under `introduced_unknown` / `lower_bound`. A range whose
  in-window lines are all noise (braces, comments) is `introduced_unknown`
  too, but keeps blame's pick as the first key — on the gold set that pick
  was right every time the keys were dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.index.blame_range import Candidate, unix_of

STAGE_INTRODUCED = "introduced"
STAGE_LAST_TOUCHED = "last_touched"
STAGES = (STAGE_INTRODUCED, STAGE_LAST_TOUCHED)
INTRODUCED_UNKNOWN = "introduced_unknown"

# Bounds: the diff test runs for at most this many candidates per request — blamed ones
# first, then ledger-only by recency — each one `git diff -w -U0 <merge>^1 <merge> --
# <path>`, cached like the blame.
MAX_DIFF_CALLS = 10
# how many candidates the first stage hands to the second before the caller's
# limit cuts the list (a promoted PR may sit past `limit` in last-touched order)
STAGE_POOL = 25

# Derivative PRs repeat an original change
_DERIVATIVE_RE = re.compile(r"(?i)^\s*(?:backport\b|cherry[ -]?pick\b)|\[sync\]")
# "Reverts ClickHouse/ClickHouse#N", "Reverts https://…/pull/N", 'Revert "x" (#N)'
_REVERTS_RE = re.compile(r"(?i)\brevert(?:s|ed)?\b[^\n#]{0,80}?(?:pull/|#)(\d+)")
_REVERT_WRAP_RE = re.compile(r"(?i)^\s*revert\b\s*:?\s*")
# The title says it is a re-land ("Reland …", "Re-land: …", "Reapply …"), or the body
# says which PR it re-lands
_RELAND_TITLE_RE = re.compile(r"(?i)^\s*(?:re-?land(?:ing|s|ed)?|re-?appl(?:y|ies|ied))\b")
# the phrase must sit next to the number: ClickHouse's PR template puts a
# `Related: …/pull/N` line in most bodies, and a bare `#N` naming a reverted
# PR was a follow-up, not a re-land, in 8 of 20 hand-checked cases
_RELAND_BODY_RE = re.compile(
    r"(?i)\b(?:re-?land(?:s|ed|ing)?(?:\s+of)?|re-?appl(?:y|ies|ied|ying)(?:\s+of)?|"
    r"re-?introduc(?:e|es|ed|ing)(?:\s+of)?|re-?add(?:s|ed|ing)?(?:\s+of)?|"
    r"(?:adds?|added|adding|brings?|bringing|brought|puts?|putting)\s+back|"
    r"revert\s+of\s+the\s+revert(?:\s+of)?|reverts?\s+the\s+revert(?:\s+of)?|"
    r"lands?\s+again|land(?:ed|ing)\s+again|second\s+attempt\s+(?:at|of))"
    r"\b[^\n#]{0,60}?(?:pull/|#)(\d+)"
)
# `Revert "X"` / `Reland "X"` / `Re-land: X` wrappers, quotes, a trailing `(#N)`
_TITLE_WRAP_RE = re.compile(
    r"(?i)^\s*(?:revert|re-?land(?:ing|s|ed)?|re-?appl(?:y|ies|ied))\b\s*(?:of\b)?\s*:?\s*"
)
_QUOTE_CHARS = "\"'“”‘’`"
_TRAILING_PR_RE = re.compile(r"\s*\(\s*#\d+\s*\)\s*$")
_PR_NUMBER_RE = re.compile(r"(?:pull/|#)(\d+)\b")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
# C-family words whose change is behaviour, never a rename
_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "do",
        "return",
        "break",
        "continue",
        "switch",
        "case",
        "default",
        "goto",
        "true",
        "false",
        "nullptr",
        "NULL",
        "const",
        "static",
        "auto",
        "void",
        "int",
        "bool",
        "char",
        "long",
        "short",
        "unsigned",
        "signed",
        "float",
        "double",
        "size_t",
        "new",
        "delete",
        "this",
        "throw",
        "try",
        "catch",
        "sizeof",
        "template",
        "typename",
        "class",
        "struct",
        "enum",
        "union",
        "namespace",
        "using",
        "public",
        "private",
        "protected",
        "virtual",
        "override",
        "final",
        "inline",
        "constexpr",
        "noexcept",
        "explicit",
        "mutable",
        "volatile",
        "and",
        "or",
        "not",
        "in",
        "is",
        "def",
        "lambda",
        "yield",
        "None",
        "True",
        "False",
        "import",
        "from",
        "elif",
        "pass",
        "raise",
        "with",
        "as",
        "assert",
    }
)
_STOP_WORDS = frozenset(
    {
        "with",
        "when",
        "that",
        "this",
        "from",
        "into",
        "after",
        "before",
        "fixes",
        "fixed",
        "error",
        "query",
        "queries",
        "clickhouse",
        "table",
        "tables",
        "function",
        "functions",
        "should",
        "does",
        "doesn",
        "return",
        "returns",
        "returned",
        "instead",
        "using",
        "used",
        "have",
        "been",
        "were",
        "will",
        "than",
        "then",
        "also",
        "only",
        "some",
        "more",
        "over",
        "under",
        "while",
        "where",
        "which",
        "there",
        "their",
        "about",
        "issue",
        "wrong",
        "incorrect",
        "correct",
        "result",
        "results",
        "value",
        "values",
        "case",
        "cases",
        "make",
        "made",
        "work",
        "works",
        "properly",
        "support",
        "allow",
        "test",
        "tests",
        "possible",
        "server",
        "client",
        "column",
        "columns",
        "data",
        "type",
        "types",
        "setting",
        "settings",
        "version",
        "release",
        "master",
        "still",
        "even",
        "again",
        "without",
        "because",
        "cannot",
        "could",
        "would",
        "same",
        "other",
        "another",
        "every",
        "each",
        "very",
        "much",
        "many",
        "like",
        "just",
        "them",
        "they",
        "what",
        "your",
        "https",
        "github",
        "pull",
        "issues",
    }
)


# ---- demotions -----------------------------------------------------------------


def is_derivative(title: str) -> bool:
    """A backport, cherry-pick or sync PR: the same change again, not the event that
    introduced it."""
    return bool(_DERIVATIVE_RE.search(title or ""))


def is_revert(title: str, body: str = "") -> bool:
    """A revert by title or body — except a re-land, which carries the change's
    behaviour whatever its body says about the revert it undoes: `Revert "Revert …"`,
    `Reland …`, or a body that says "re-lands #N"."""
    from server.code.domain.index.pr_signals import is_revert_pr

    if is_reland_title(title) or _RELAND_BODY_RE.search(body or ""):
        return False
    return is_revert_pr(title, body) or bool(_REVERTS_RE.search(body or ""))


def revert_depth(title: str) -> int:
    """How many `Revert "…"` wrappers open the title: 1 is a revert, 2 a
    revert of a revert (a re-land), 3 a revert again — parity decides."""
    t = (title or "").strip()
    depth = 0
    while _REVERT_WRAP_RE.match(t):
        depth += 1
        t = _REVERT_WRAP_RE.sub("", t, count=1).strip().lstrip(_QUOTE_CHARS).strip()
    return depth


def is_reland_title(title: str) -> bool:
    """An even number of `Revert "…"` wrappers (GitHub's title for a revert
    of a revert; a third revert is a revert again) or a title that says
    re-land / reapply."""
    t = title or ""
    depth = revert_depth(t)
    return (depth >= 2 and depth % 2 == 0) or bool(_RELAND_TITLE_RE.match(t))


def reverted_prs(body: str) -> set[int]:
    """The PR numbers a revert's body says it reverts."""
    return {int(m.group(1)) for m in _REVERTS_RE.finditer(body or "")}


def normalize_title(title: str) -> str:
    """A PR title with its `Revert "…"` / `Reland …` / `Reapply …` wrappers,
    quotes and trailing `(#N)` removed, whitespace collapsed, lower-cased —
    the key on which a re-land's title equals the reverted PR's."""
    t = (title or "").strip()
    for _ in range(4):
        t = _TRAILING_PR_RE.sub("", t).strip().strip(_QUOTE_CHARS).strip()
        stripped = _TITLE_WRAP_RE.sub("", t, count=1)
        if stripped == t:
            break
        t = stripped.strip().strip(_QUOTE_CHARS).strip()
    t = _TRAILING_PR_RE.sub("", t).strip().strip(_QUOTE_CHARS)
    return " ".join(t.lower().split())


def _quoted_title(revert_title: str) -> str:
    """What a revert's title says it reverted: `Revert "X"` → normalize(X);
    "" when the title is not a revert."""
    t = (revert_title or "").strip()
    if not _TITLE_WRAP_RE.match(t) or is_reland_title(t):
        return ""
    return normalize_title(t)


@dataclass
class Reland:
    """One PR that re-lands a reverted change."""

    pr_number: int
    original: int = 0  # the reverted PR, 0 when only the revert is known
    via: int = 0  # the revert it undoes, 0 when only the original is named
    evidence: str = ""  # double_revert | title | body | body_phrase


@dataclass
class _PoolEntry:
    pr_number: int
    title: str
    body: str
    ts: int


def _reland_by_title(
    e: _PoolEntry,
    reverts: list[tuple[_PoolEntry, set[int], str]],
    by_title: dict[str, list[_PoolEntry]],
) -> Reland | None:
    """The re-land whose title equals a reverted PR's title (or the revert's
    quoted title), merged after the revert."""
    nt = normalize_title(e.title)
    for v, reverted, quoted in reverts:
        if v.ts >= e.ts or v.pr_number == e.pr_number:
            continue
        names_v = e.pr_number != v.pr_number and v.pr_number in _numbers_in(e.body)
        originals = [o for o in by_title.get(nt, []) if o.pr_number != e.pr_number and o.ts < v.ts]
        if quoted and nt == quoted:
            original = originals[-1].pr_number if originals else (min(reverted) if reverted else 0)
            kind = "double_revert" if is_reland_title(e.title) else "title"
            return Reland(e.pr_number, original=original, via=v.pr_number, evidence=kind)
        if is_reland_title(e.title) and names_v:
            original = originals[-1].pr_number if originals else (min(reverted) if reverted else 0)
            return Reland(e.pr_number, original=original, via=v.pr_number, evidence="double_revert")
        if originals and any(o.pr_number in reverted for o in originals):
            original = next(o.pr_number for o in originals if o.pr_number in reverted)
            return Reland(e.pr_number, original=original, via=v.pr_number, evidence="title")
    return None


def _reland_by_body(
    e: _PoolEntry, reverts: list[tuple[_PoolEntry, set[int], str]]
) -> Reland | None:
    """The re-land whose body says so next to a PR number — "re-lands #N",
    "reintroduces #N", "adds back #N", "revert of the revert #V": `body`
    when N is a PR a revert in the pool reverted (merged before this one),
    `body_phrase` when the pool knows no such revert; a number that is
    itself a revert in the pool resolves to what that revert reverted."""
    for m in _RELAND_BODY_RE.finditer(e.body or ""):
        n = int(m.group(1))
        if n == e.pr_number:
            continue
        for v, reverted, _q in reverts:
            if v.ts >= e.ts or v.pr_number == e.pr_number:
                continue
            if n in reverted:
                return Reland(e.pr_number, original=n, via=v.pr_number, evidence="body")
            if n == v.pr_number:
                original = min(reverted) if reverted else 0
                return Reland(e.pr_number, original=original, via=v.pr_number, evidence="body")
        return Reland(e.pr_number, original=n, via=0, evidence="body_phrase")
    return None


def _numbers_in(text: str) -> set[int]:
    return {int(m.group(1)) for m in _PR_NUMBER_RE.finditer(text or "")}


def detect_relands(pool: list[tuple[int, str, str, int]]) -> dict[int, Reland]:
    """Re-lands among `pool` = [(pr_number, title, body, merged_unix)]: for
    each revert in the pool (title or body; a re-land is not one) the PRs it
    reverted (its body's `#N`, or the PR whose normalized title equals the
    revert's quoted title); then every non-derivative PR merged after the
    revert whose title equals the reverted title or whose body names the
    reverted PR; and any PR whose body says "re-lands #N" outright. Keyed by
    the re-land's number."""
    entries = [_PoolEntry(n, t or "", b or "", int(ts or 0)) for n, t, b, ts in pool]
    reverts: list[tuple[_PoolEntry, set[int], str]] = []
    by_title: dict[str, list[_PoolEntry]] = {}
    for e in entries:
        if is_derivative(e.title):
            continue
        if is_revert(e.title, e.body):
            reverts.append((e, reverted_prs(e.body), _quoted_title(e.title)))
            continue
        by_title.setdefault(normalize_title(e.title), []).append(e)
    for lst in by_title.values():
        lst.sort(key=lambda x: x.ts)
    out: dict[int, Reland] = {}
    for e in entries:
        if is_derivative(e.title) or (is_revert(e.title, e.body) and not is_reland_title(e.title)):
            continue
        found = _reland_by_title(e, reverts, by_title) or _reland_by_body(e, reverts)
        if found is not None and found.original != e.pr_number:
            out[e.pr_number] = found
    return out


def classify_diff(diff_text: str) -> str:
    """What one PR did to one file, from `git diff -w -U0 parent merge --
    path`: `whitespace_only` when the whitespace-blind diff prints nothing;
    `rename_only` when every removed line pairs with an added line that is
    identical once identifiers are masked, the identifier substitutions form
    one consistent bijection, and no keyword changed — a rename, an include
    move, a namespace change; "" when the change is substantive (added or
    removed lines, a changed literal, operator or keyword)."""
    minus: list[str] = []
    plus: list[str] = []
    for line in (diff_text or "").splitlines():
        if line.startswith(("---", "+++", "@@", "diff ", "index ")):
            continue
        if line.startswith("-"):
            minus.append(line[1:])
        elif line.startswith("+"):
            plus.append(line[1:])
    if not minus and not plus:
        return "whitespace_only"
    if len(minus) != len(plus):
        return ""
    forward: dict[str, str] = {}
    backward: dict[str, str] = {}
    renamed = False
    for a, b in zip(minus, plus, strict=True):
        ta, tb = _IDENT_RE.findall(a), _IDENT_RE.findall(b)
        if len(ta) != len(tb) or _shape(a) != _shape(b):
            return ""
        for x, y in zip(ta, tb, strict=True):
            if x != y and (x in _KEYWORDS or y in _KEYWORDS):
                return ""
            # identities are recorded too: `f(a, b)` → `f(a, a)` collapses two
            # names into one, which is behaviour, not a rename
            if forward.setdefault(x, y) != y or backward.setdefault(y, x) != x:
                return ""
            renamed = renamed or x != y
    return "rename_only" if renamed else "whitespace_only"


def _shape(line: str) -> list[str]:
    """A line with every identifier masked and whitespace collapsed."""
    return _IDENT_RE.sub("\x00", line).split()


_HUNK_CONTEXT_RE = re.compile(r"^@@[^@]*@@ ?(.*)$")


def diff_touches(diff_text: str, frames: list[str]) -> bool | None:
    """Whether the PR's change to the file lands inside the function named by `frames`,
    read from the hunk headers of `git diff -U0` — git prints the enclosing declaration
    after the `@@ … @@` of every hunk, as of the PR's own commit, so line drift between
    then and the window's upper commit cannot mislead it (`touches_symbol`, from the
    mirror instead of the tracker record). None when there is no hunk or no frame to
    test."""
    if not frames:
        return None
    seen = False
    for line in (diff_text or "").splitlines():
        m = _HUNK_CONTEXT_RE.match(line)
        if not m:
            continue
        seen = True
        context = m.group(1)
        if any(f and f in context for f in frames):
            return True
    return False if seen else None


# ---- promotions ----------------------------------------------------------------


def behaviour_words(mentions: str, *, limit: int = 12) -> list[str]:
    """The words of the caller's `mentions` worth matching against an issue
    or PR title: four or more letters, not a stop word, lower-cased, first
    appearance order."""
    out: list[str] = []
    for m in _WORD_RE.finditer(mentions or ""):
        w = m.group(0).lower()
        if w in _STOP_WORDS or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def words_in(title: str, words: list[str]) -> list[str]:
    """The behaviour words that appear in `title` as whole words."""
    if not title or not words:
        return []
    have = {m.group(0).lower() for m in _WORD_RE.finditer(title)}
    return [w for w in words if w in have]


def enclosing_symbol(rows: list[dict[str, Any]], start: int, end: int) -> dict[str, Any] | None:
    """The innermost function span that contains the whole range; else the
    innermost that contains its midpoint; else the span with the most lines
    in common with the range; None when no span in the file overlaps it."""
    mid = (start + end) // 2
    containing: list[dict[str, Any]] = []
    at_mid: list[dict[str, Any]] = []
    overlapping: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        lo, hi = int(r.get("start_line") or 0), int(r.get("end_line") or 0)
        if lo <= 0 or hi < lo:
            continue
        overlap = min(hi, end) - max(lo, start) + 1
        if overlap <= 0:
            continue
        if lo <= start and end <= hi:
            containing.append(r)
        elif lo <= mid <= hi:
            at_mid.append(r)
        overlapping.append((overlap, r))
    for pool in (containing, at_mid):
        if pool:
            return min(pool, key=lambda r: int(r["end_line"]) - int(r["start_line"]))
    if overlapping:
        return max(overlapping, key=lambda t: t[0])[1]
    return None


# ---- marks and ranking ---------------------------------------------------------


@dataclass
class Mark:
    """What the second stage learned about one candidate."""

    last_touched_rank: int = 0
    # backport | revert | reverted | whitespace_only | rename_only
    demoted: str = ""
    # meta title: below undemoted change-titled PRs, above the demoted
    meta: bool = False
    # issue_names_behaviour | title_names_behaviour | names_symbol |
    # review_comment_in_span | touches_symbol
    promoted: list[str] = field(default_factory=list)
    touches_symbol: str = "unknown"  # true | named | false | unknown
    behaviour_words: list[str] = field(default_factory=list)
    # This PR re-lands `reland_of` (undoing the revert `reland_via`); `relanded_by` on
    # the reverted PR whose code lives on through the re-land
    reland_of: int = 0
    reland_via: int = 0
    relanded_by: int = 0

    @property
    def symbol_evidence(self) -> bool:
        """Any sign the PR reached the function: the QA diff or the git hunk
        headers name it, its title / body names it, or a reviewer commented
        inside it — the marks are kept apart in `promoted`, the ranking key
        does not weigh one against another."""
        return self.touches_symbol in ("true", "named") or bool(
            {"names_symbol", "review_comment_in_span", "touches_symbol", "reland_inherits_symbol"}
            & set(self.promoted)
        )

    def fold_into(self, out: dict[str, Any]) -> None:
        out["last_touched_rank"] = self.last_touched_rank
        if self.demoted:
            out["demoted"] = self.demoted
        elif self.meta:
            out["demoted"] = "meta_title"
        if self.promoted:
            out["promoted"] = list(self.promoted)
        if self.touches_symbol != "unknown":
            out["touches_symbol"] = self.touches_symbol
        if self.behaviour_words:
            out["behaviour_words"] = list(self.behaviour_words)
        if self.reland_of or self.reland_via:
            out["reland_of"] = self.reland_of
            if self.reland_via:
                out["reland_via"] = self.reland_via
        if self.relanded_by:
            out["relanded_by"] = self.relanded_by


def mark_candidates(
    candidates: list[Candidate],
    *,
    bodies: dict[int, str],
    linked: dict[int, list[int]],
    issue_titles: dict[int, str],
    words: list[str],
    frames: list[str],
    span_comment_prs: set[int],
    tracker_names: dict[int, list[str]],
    qa_ok: bool,
    diff_kinds: dict[int, str],
    diff_touch: dict[int, bool] | None = None,
) -> dict[int, Mark]:
    """One Mark per candidate from the records: derivative / revert /
    reverted titles and bodies, the diff kinds the bounded git calls
    returned, the symbol evidence (QA diff names; else the git diff's hunk
    headers naming the function; else loom `named` from title / body and
    span comments), and the behaviour words of `mentions` in linked issue
    or PR titles."""
    touch = diff_touch or {}
    relands = detect_relands(
        [
            (c.pr_number, c.title, bodies.get(c.pr_number, ""), unix_of(c.merged_at))
            for c in candidates
        ]
    )
    relanded = {r.original: r.pr_number for r in relands.values() if r.original}
    reverted: set[int] = set()
    for c in candidates:
        body = bodies.get(c.pr_number, "")
        if c.pr_number not in relands and is_revert(c.title, body):
            reverted |= reverted_prs(body)
    marks: dict[int, Mark] = {}
    for rank, c in enumerate(candidates, start=1):
        m = Mark(last_touched_rank=rank, meta=c.kind == "meta")
        body = bodies.get(c.pr_number, "")
        m.relanded_by = relanded.get(c.pr_number, 0)
        _demote(m, c, body, relands.get(c.pr_number), reverted, diff_kinds)
        text = f"{c.title}\n{body}"
        if c.pr_number in span_comment_prs:
            m.promoted.append("review_comment_in_span")
        names_it = any(f and f in text for f in frames)
        _symbol_touch(
            m,
            c,
            frames=frames,
            names_it=names_it,
            tracker_named=tracker_names.get(c.pr_number)
            if qa_ok and c.pr_number in tracker_names
            else None,
            touch=touch.get(c.pr_number),
            commented=c.pr_number in span_comment_prs,
        )
        if words:
            hit_issue = [
                w
                for n in linked.get(c.pr_number, [])
                for w in words_in(issue_titles.get(n, ""), words)
            ]
            hit_title = words_in(c.title, words)
            if hit_issue:
                m.promoted.append("issue_names_behaviour")
            if hit_title:
                m.promoted.append("title_names_behaviour")
            m.behaviour_words = sorted(set(hit_issue) | set(hit_title))
        marks[c.pr_number] = m
    inherit_reland_marks(marks)
    return marks


def _demote(
    m: Mark,
    c: Candidate,
    body: str,
    reland: Reland | None,
    reverted: set[int],
    diff_kinds: dict[int, str],
) -> None:
    """The one hard demotion a candidate earns, in this order — derivative, then re-land
    (never a revert, whatever its body says about the revert it undoes), revert,
    reverted (unless re-landed), then the diff's verdict."""
    if is_derivative(c.title):
        m.demoted = "backport"
    elif reland is not None:
        m.reland_of, m.reland_via = reland.original, reland.via
    elif is_revert(c.title, body):
        m.demoted = "revert"
    elif c.pr_number in reverted and not m.relanded_by:
        m.demoted = "reverted"
    elif diff_kinds.get(c.pr_number):
        m.demoted = diff_kinds[c.pr_number]


def _symbol_touch(
    m: Mark,
    c: Candidate,
    *,
    frames: list[str],
    names_it: bool,
    tracker_named: list[str] | None,
    touch: bool | None,
    commented: bool,
) -> None:
    """`touches_symbol` from the strongest record at hand: the QA diff's names when
    bound; else the git diff's hunk headers (a blamed PR touched the range, so a header
    miss never says `false`); else loom's `named` from the title / body or a span
    comment. A QA diff that does NOT name the function is a floor, never a demotion, and
    a hunk header settles it (live, the QA `false` was wrong on 12 of 32 blamed golds
    and on 5 of 5 ledger-only inducers it had demoted — a change inside a body need not
    carry the function's name)."""
    if tracker_named is not None and (tracker_named or touch is None):
        m.touches_symbol = "true" if tracker_named else ("false" if frames else "unknown")
        if tracker_named:
            m.promoted.append("touches_symbol")
        return
    if touch is not None:
        m.touches_symbol = "true" if touch or c.lines_blamed else "false"
        if touch:
            m.promoted.append("touches_symbol")
        if names_it:
            m.promoted.append("names_symbol")
        return
    if commented or names_it:
        m.touches_symbol = "named"
        if names_it:
            m.promoted.append("names_symbol")


def inherit_reland_marks(marks: dict[int, Mark]) -> None:
    """A re-land inherits the reverted PR's symbol evidence: the original's
    `touches_symbol` / `named` when the re-land has none of its own — the change is the
    same change, landed twice."""
    for m in marks.values():
        o = marks.get(m.reland_of) if m.reland_of else None
        if o is None or m.symbol_evidence or not o.symbol_evidence:
            continue
        if o.touches_symbol in ("true", "named"):
            m.touches_symbol = o.touches_symbol
        m.promoted.append("reland_inherits_symbol")


def rank_introduced(
    candidates: list[Candidate], marks: dict[int, Mark], *, blameless: bool
) -> list[Candidate]:
    """The introduced order. Hard-demoted candidates last; among the rest a
    PR whose linked issue names the behaviour first; then change-titled
    before meta-titled; then — when blame answered — blamed before
    ledger-only; then a PR whose own title names the behaviour (weaker than
    an issue's title: on the inducer set it lifted as many as it displaced
    when ranked above blame); then symbol evidence before none; lines, then
    recency (the first stage's order); when blame is blind, the same without
    the blame keys."""

    def key(c: Candidate) -> tuple[int, ...]:
        m = marks.get(c.pr_number) or Mark()
        head = (
            1 if m.demoted else 0,
            0 if "issue_names_behaviour" in m.promoted else 1,
            1 if m.meta else 0,
        )
        title = 0 if "title_names_behaviour" in m.promoted else 1
        symbol = 0 if m.symbol_evidence else 1
        if blameless:
            return (*head, title, symbol, -unix_of(c.merged_at), -c.pr_number)
        return (
            *head,
            0 if c.lines_blamed else 1,
            title,
            symbol,
            -c.lines_blamed,
            -unix_of(c.merged_at),
            -c.pr_number,
        )

    return sorted(candidates, key=key)


def diff_order(candidates: list[Candidate], marks: dict[int, Mark]) -> list[Candidate]:
    """Which candidates get one of the bounded diff calls: those not already
    demoted by title or body, blamed ones first, then ledger-only by
    recency, at most MAX_DIFF_CALLS."""
    pool = [
        c
        for c in candidates
        if c.merge_commit_sha and not (marks.get(c.pr_number) or Mark()).demoted
    ]
    pool.sort(key=lambda c: (0 if c.lines_blamed else 1, -unix_of(c.merged_at), -c.pr_number))
    return pool[:MAX_DIFF_CALLS]


def is_blameless(*, blame_ok: bool, attributed_lines: int, noise_only: bool) -> bool:
    """The range's blame says nothing about the window: the blame leg failed,
    every blamed line predates the window, or every in-window line is noise."""
    return not blame_ok or attributed_lines <= 0 or noise_only


NOTE_INTRODUCED = (
    " Stage `introduced` re-ranks the same candidates: backports, "
    "reverts, reverted PRs and whitespace-only / rename-only touches of the file are "
    "demoted; a PR that names the enclosing function, drew a review comment inside it, "
    "or whose linked issue names the behaviour in `mentions` is promoted; a re-land "
    "(`reland_of`) keeps the reverted PR undemoted (`relanded_by`) and inherits its symbol "
    "marks. `last_touched` "
    "keeps the first stage's order. `introduced_unknown` means blame saw no in-window "
    "line: the list is the window's file-touching PRs by symbol evidence and recency."
)


__all__ = [
    "INTRODUCED_UNKNOWN",
    "MAX_DIFF_CALLS",
    "NOTE_INTRODUCED",
    "STAGES",
    "STAGE_INTRODUCED",
    "STAGE_LAST_TOUCHED",
    "STAGE_POOL",
    "Mark",
    "Reland",
    "behaviour_words",
    "classify_diff",
    "detect_relands",
    "diff_order",
    "diff_touches",
    "enclosing_symbol",
    "inherit_reland_marks",
    "is_blameless",
    "is_derivative",
    "is_reland_title",
    "is_revert",
    "mark_candidates",
    "normalize_title",
    "rank_introduced",
    "revert_depth",
    "reverted_prs",
    "words_in",
]
