"""PR-text lexemes and linked issues for `loom.code_pr_history`.

A merged PR's title and description are the one place the repository says,
in the vocabulary of a bug report, which files a problem lived in. The
history leg of code.search scores that text with the SAME tokenizer the
chunk lexical leg uses (`code_tokens`), so a query and a PR description
tokenize byte-for-byte alike — the lemmatize.py / tokenize.py convention.

What is stored per PR:

* `lexemes`  — space-joined `code_tokens` over title + description +
  the titles of the issues the description links, with the PR-template
  boilerplate stripped first. The template lines ("Changelog category
  (leave one):", the category menu, the CI links block, HTML comments,
  checkbox lists, bare URLs) appear on nearly every PR and carry no
  localization signal; left in, they would give every PR the same
  hundred tokens and flatten the idf of the words that matter.
* `linked_issues` — issue numbers the description closes/fixes/resolves,
  as `#123` or as a full `/issues/123` URL, taken from the FULL body at
  fetch time (the stored excerpt is capped at 2,000 chars).

The row builder over these, `pr_history_row` in the store's row builders, is
the single place the worker, the backfill CLI and the lexeme backfill assemble a
`code_pr_history` row, so the three write the same shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from server.code.domain.extract.tokenize import code_tokens

# (?i)(fix|close|resolve)\w*\s*:?\s*#(\d+) — plus the URL spelling GitHub
# also auto-links ("Closes: https://github.com/Org/Repo/issues/123"). The space runs are
# possessive: two greedy runs around an optional colon split a long run every way.
LINKED_ISSUE_RE = re.compile(
    r"(?i)\b(?:fix|close|resolve)\w*\s*+(?::\s*+)?(?:#|https?://github\.com/[\w.-]+/[\w.-]+/issues/)(\d+)"
)

_CI_BLOCK_RE = re.compile(
    r"<!-- CI automatic block start.*?(?:<!-- CI automatic block end[^>]*-->|$)", re.S
)
_URL_RE = re.compile(r"https?://\S+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]")
_CI_LINK_LINE_RE = re.compile(r"^\s*(Workflow|Sync PR|Job|Report)\s*\[\[", re.I)

# The changelog category menu of the ClickHouse PR template: a body keeps
# exactly one of these as a short bullet, and the bullet names a category,
# not a place. Matched only as a bullet line, so prose that happens to
# begin "Improvement of ..." survives.
_CATEGORY_MENU = (
    "not for changelog",
    "bug fix",
    "critical bug fix",
    "improvement",
    "new feature",
    "performance improvement",
    "backward incompatible change",
    "build/testing/packaging improvement",
    "documentation",
    "experimental feature",
    "ci fix or improvement",
)
_CATEGORY_MAX_LEN = 100

# Prose boilerplate the template or a bot leaves in the body.
_PROSE_BOILERPLATE = (
    "changelog category (leave one)",
    "changelog entry (a",
    "documentation entry for user-facing changes",
    "modify your ci run",
    "this pull request is opened automatically",
    "generated with [claude code]",
)

_MAX_ISSUES = 20


def linked_issues(body: str) -> list[int]:
    """Issue numbers the body closes / fixes / resolves, first-seen order,
    de-duplicated, at most 20."""
    out: list[int] = []
    for m in LINKED_ISSUE_RE.finditer(body or ""):
        n = int(m.group(1))
        if n and n not in out:
            out.append(n)
        if len(out) >= _MAX_ISSUES:
            break
    return out


def _drop_html_comments(text: str) -> str:
    """Each `<!-- … -->` replaced by a space. A `<!--` with no `-->` after it ends the
    search, since none after it has one either: a pattern scans to the end from each."""
    out: list[str] = []
    i = 0
    while (start := text.find("<!--", i)) >= 0 and (end := text.find("-->", start + 4)) >= 0:
        out += [text[i:start], " "]
        i = end + 3
    return "".join(out) + text[i:]


def strip_pr_template(body: str) -> str:
    """The description minus the PR-template boilerplate: HTML comments and
    the CI links block, markdown headings, checkbox items, the changelog
    category menu, CI report lines and bare URLs. Prose, code spans and
    identifiers survive — those are the signal."""
    text = _CI_BLOCK_RE.sub(" ", body or "")
    text = _drop_html_comments(text)
    kept: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _HEADING_RE.match(line) or _CHECKBOX_RE.match(line) or _CI_LINK_LINE_RE.match(line):
            continue
        low = s.lower()
        if any(low.startswith(t) for t in _PROSE_BOILERPLATE):
            continue
        if s[0] in "-*" and len(s) <= _CATEGORY_MAX_LEN:
            item = s.lstrip("-* ").lower()
            if any(item == t or item.startswith(t + " (") for t in _CATEGORY_MENU):
                continue
        kept.append(_URL_RE.sub(" ", line))
    return "\n".join(kept)


def pr_lexemes(title: str, body: str, issue_titles: Iterable[str] = ()) -> str:
    """The exact string stored in code_pr_history.lexemes."""
    parts = [title or "", strip_pr_template(body), *issue_titles]
    return " ".join(code_tokens("\n".join(p for p in parts if p)))
