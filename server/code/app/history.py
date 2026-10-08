"""History intent inside code.search: a question ABOUT a change is answered from the
change ledger.

Two narrow shapes, classified deterministically and unit-tested like
`classify_query_shape`:

  number  — the query names an issue or PR: "issue 117585", "PR #114783",
            "what fixed #104969". `code_gh_items` says what the number is
            (issue / PR, state, title); `code_pr_history` supplies the PR's
            own files (a PR number) or the merged PRs that close the issue
            (`has(linked_issues, N)`, pruned by its bloom
            index). A bare `#N` fires only next to a history phrase
            or in a very short query: mined issue titles say "regression
            after #104969" and backport titles say "Backport #95164 to
            25.10" — neither is a question about that number.
  phrase  — "why was", "when was", "who changed", "what fixed", "which PR",
            "fix status", "fixed in": the merged-PR text is scored with the
            history leg's own statement (`pr_history_search`) and the top
            rows are served. Bare "introduced" / "regressed" are NOT
            phrases: issue titles carry them as symptom words.

Either way the answer is a `history` section on the response — up to `code_search_history_max_prs`
PR rows (pr_number, title, author, merged_at, files; no body) plus the item facts — paid for inside
the token budget, and the PRs' `src/` files may ANCHOR the ranking through the rank-lift shape: a
file already inside the served window stays where fusion put it, one that is absent is inserted at
rank 1, at most `anchor_max_files` of them; nothing is filtered out. `code.history` shares these
functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.units import CodeUnit, unit_from_chunk
from server.embedding.normalize import normalize_whitespace

logger = logging.getLogger(__name__)

# "issue 117585", "PR #114783", "pull request 42". The kind word makes the
# number a subject; a trailing "." or digit (a version such as 26.6) is not
# a number mention.
_KIND_NUMBER_RE = re.compile(
    r"(?i)\b(?:issue|issues|pr|prs|pull[ -]request)\s*#?\s*(\d{2,7})(?![\d.]\d)\b"
)
# "#104969" on its own: not preceded by a word character or a dot.
_HASH_NUMBER_RE = re.compile(r"(?<![\w.#])#(\d{2,7})(?![\d.]\d)\b")
_PHRASE_RE = re.compile(
    r"(?i)\b("
    r"why was|when was|when did|"
    r"who (?:changed|added|introduced|removed|wrote|touched|fixed|broke)|"
    r"what fixed|what (?:pr|change) fixed|which (?:pr|prs|pull request|change|commit)|"
    r"fix status|been fixed|fixed in|fixed by|"
    r"(?:is|was|has) (?:it|this|that) (?:been )?fixed|"
    r"(?:review|fix|change|pr) history"
    r")\b"
)
# A bare `#N` in a query this short ("#12345", "status of #12345") is the
# subject; in a longer one it is a citation inside a report.
_HASH_MAX_WORDS = 4
_MAX_NUMBERS = 3


@dataclass
class HistoryIntent:
    kind: str  # "number" | "phrase"
    numbers: list[int] = field(default_factory=list)
    phrase: str = ""


def classify_history_intent(query: str) -> HistoryIntent | None:
    """None for every query that is not a question about a change. A number
    named with its kind word always fires; a bare `#N` only next to a phrase
    or in a query of at most four words; a phrase alone is the phrase intent."""
    q = normalize_whitespace(query or "")
    if not q:
        return None
    m = _PHRASE_RE.search(q)
    phrase = m.group(1).lower() if m else ""
    numbers: list[int] = []
    for hit in _KIND_NUMBER_RE.finditer(q):
        n = int(hit.group(1))
        if n not in numbers:
            numbers.append(n)
    if phrase or len(q.split()) <= _HASH_MAX_WORDS:
        for hit in _HASH_NUMBER_RE.finditer(q):
            n = int(hit.group(1))
            if n not in numbers:
                numbers.append(n)
    if numbers:
        return HistoryIntent("number", numbers[:_MAX_NUMBERS], phrase)
    if phrase:
        return HistoryIntent("phrase", [], phrase)
    return None


# ---- section building ------------------------------------------------------------

# Files listed per PR row in the section (the count is always given); the
# token estimate below is chars / 4, the plane's convention.
_FILES_LISTED = 20


def _pr_row(r: dict[str, Any], *, role: str) -> dict[str, Any]:
    files = [str(f) for f in (r.get("files") or [])]
    row: dict[str, Any] = {
        "pr_number": int(r.get("pr_number") or 0),
        "title": str(r.get("title") or ""),
        "author": str(r.get("author") or ""),
        "merged_at": str(r.get("merged_at") or ""),
        "files": files[:_FILES_LISTED],
        "files_total": len(files),
        "role": role,
    }
    linked = [int(x) for x in (r.get("linked_issues") or []) if int(x) > 0]
    if linked:
        row["linked_issues"] = linked
    if r.get("is_revert"):
        row["is_revert"] = True
    if r.get("pr_score") is not None:
        row["score"] = round(float(r.get("pr_score") or 0.0), 3)
    return row


def _item_row(n: int, it: dict[str, Any] | None) -> dict[str, Any]:
    if not it:
        return {"number": n, "kind": "unknown"}
    merged = it.get("merged_at")
    return {
        "number": n,
        "kind": "pr" if it.get("is_pr") else "issue",
        "state": str(it.get("state") or ""),
        "state_reason": str(it.get("state_reason") or ""),
        "title": str(it.get("title") or ""),
        "author": str(it.get("author") or ""),
        "opened_at": str(it.get("opened_at") or ""),
        "closed_at": str(it.get("closed_at") or "") if it.get("closed_at") else "",
        "merged_at": str(merged) if merged else "",
    }


async def history_for_numbers(
    repo: Any,
    org: str,
    ns: str,
    numbers: list[int],
    *,
    max_prs: int = 3,
) -> dict[str, Any]:
    """The section for explicit number mentions: item facts for every
    number, the PR row for a PR number, the merged PRs closing an issue
    number (newest first). Three small reads, run together."""
    nums = [int(n) for n in numbers if int(n) > 0][:_MAX_NUMBERS]
    if not nums:
        return {}
    items, own, linking = await asyncio.gather(
        repo.items_by_number(org, ns, nums),
        repo.prs_by_number(org, ns, nums),
        repo.prs_linking_issue(org, ns, nums, limit=max(max_prs, 1) * len(nums)),
    )
    prs: list[dict[str, Any]] = []
    seen: set[int] = set()
    for n in nums:
        r = own.get(n)
        if r is not None and n not in seen:
            seen.add(n)
            prs.append(_pr_row(r, role="pr"))
    # PRs closing each issue, in the order the issues were named, newest first
    by_issue: dict[int, list[dict[str, Any]]] = {n: [] for n in nums}
    for r in linking:
        linked = {int(x) for x in (r.get("linked_issues") or [])}
        for n in nums:
            if n in linked:
                by_issue[n].append(r)
    for n in nums:
        for r in by_issue[n]:
            pr = int(r.get("pr_number") or 0)
            if pr in seen:
                continue
            seen.add(pr)
            prs.append(_pr_row(r, role=f"fixes #{n}"))
    prs = prs[: max(max_prs, 1)]
    item_rows = [_item_row(n, items.get(n)) for n in nums]
    # The tracker record beside the PR record — component, severity, bisect verdict,
    # test presence, the fixes / backports graph — from the tracker namespace bound to
    # this code namespace, as the current principal. Additive: unbound, unauthorized or
    # unknown adds nothing.
    facts = await _tracker_facts(org, ns, nums)
    for row in item_rows:
        fact = facts.get(int(row.get("number") or 0))
        if fact:
            row["tracker"] = fact
            if row.get("kind") == "unknown":
                row["kind"] = fact.get("kind", "unknown")
    return {
        "intent": "number",
        "numbers": nums,
        "items": item_rows,
        "prs": prs,
    }


async def _tracker_facts(org: str, ns: str, nums: list[int]) -> dict[int, dict[str, Any]]:
    try:
        from server.tracker.app.binding import authorized_tracker_namespace
        from server.tracker.app.facts import tracker_facts_for_numbers

        tracker_ns = await authorized_tracker_namespace(org, ns)
        if not tracker_ns:
            return {}
        return await tracker_facts_for_numbers(org, tracker_ns, nums)
    except Exception as e:  # noqa: BLE001 — additive
        logger.debug("qa history enrichment skipped: %s", e)
        return {}


async def history_for_phrase(
    repo: Any,
    org: str,
    ns: str,
    tokens: list[str],
    *,
    max_prs: int = 3,
    before: Any = None,
    min_prs: int = 0,
) -> dict[str, Any]:
    """The section for a history phrasing: the top merged PRs by the history leg's
    lexical score over the change text (the history leg's statement, pool = max_prs).
    Two reads."""
    if not tokens:
        return {}
    dfs, n_prs = await repo.pr_corpus_stats(
        org,
        ns,
        tokens,
        before=before,
    )
    if n_prs <= 0 or n_prs < min_prs:
        return {"intent": "phrase", "items": [], "prs": [], "skipped": "sparse", "n_prs": n_prs}
    idfs = [math.log(1.0 + (n_prs - df + 0.5) / (df + 0.5)) for df in dfs]
    rows = await repo.pr_history_search(
        org,
        ns,
        tokens,
        idfs,
        pool=max(max_prs, 1),
        before=before,
    )
    return {
        "intent": "phrase",
        "items": [],
        "prs": [_pr_row(r, role="matched") for r in rows[: max(max_prs, 1)]],
    }


def section_tokens(section: dict[str, Any]) -> int:
    """Tokens the section costs on the wire (chars / 4), paid for inside the
    request's budget before any unit body is filled."""
    if not section:
        return 0
    return max(1, len(json.dumps(section, default=str, separators=(",", ":"))) // 4)


def anchor_paths(section: dict[str, Any], prefixes: list[str], *, max_files: int) -> list[str]:
    """Files the section's PRs touched, PR order then file order, under the
    prefixes (empty = every file), de-duplicated, at most `max_files`."""
    out: list[str] = []
    for pr in section.get("prs") or []:
        for f in pr.get("files") or []:
            path = str(f)
            if prefixes and not any(path.startswith(p) for p in prefixes):
                continue
            if path not in out:
                out.append(path)
            if len(out) >= max_files:
                return out
    return out


async def anchor_units(
    repo: Any, org: str, ns: str, model: str, paths: list[str]
) -> list[CodeUnit]:
    """One live unit per anchored file (class skeleton first), in path order."""
    if not paths:
        return []
    rows = await repo.chunks_for_paths(org, ns, model, paths, per_path=1)
    order = {p: i for i, p in enumerate(paths)}
    rows.sort(key=lambda r: order.get(str(r.get("path")), 1 << 20))
    units: list[CodeUnit] = []
    seen: set[str] = set()
    for r in rows:
        u = unit_from_chunk(r)
        if u.path in seen:
            continue
        seen.add(u.path)
        units.append(u)
    return units


def apply_file_anchors(
    ranked: list[CodeUnit], units: list[CodeUnit], top_k: int
) -> tuple[list[CodeUnit], list[dict[str, Any]]]:
    """The rank-lift shape for files: a path already inside the served window stays
    where fusion put it (`placed="present"`); one that is absent has its unit inserted
    at rank 1, in order (`placed="lifted"`). Nothing is dropped. Returns (ranked,
    disclosure per anchored path)."""
    if not units:
        return ranked, []
    present = {u.path for u in ranked[:top_k]}
    lifted: list[CodeUnit] = []
    disclosure: list[dict[str, Any]] = []
    top = max((u.score for u in ranked), default=0.0)
    for u in units:
        if u.path in present:
            disclosure.append({"path": u.path, "placed": "present"})
            continue
        disclosure.append({"path": u.path, "placed": "lifted"})
        lifted.append(u.model_copy(update={"score": round(top + 1e-6, 6), "via": "history"}))
    if not lifted:
        return ranked, disclosure
    keys = {u.unit_id for u in lifted}
    rest = [u for u in ranked if u.unit_id not in keys]
    return lifted + rest, disclosure
