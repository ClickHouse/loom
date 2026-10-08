"""A context pack for a task that is not a PR yet.

`code.context_pack` answers "brief me on merged PR N" (the precomputed pack)
and, with no PR, "orient me in this repository" (the map). A coding session's
usual question is a third one — "I am about to add a new tracker op: where
does that go, and what else does such a change touch?" — which the map's
most-included files do not answer.

A task briefing is assembled from what the index already holds, per call:

- where the task lives: one code.search for the task text, grouped by file;
- what a change there also touches: the files that changed together with the
  top files in merged PRs (co-change), which is where the registry list, the
  generated SDKs and the registry test of "add a new op" come from — no
  hand-written map of change kinds;
- how it was done before: the merged PRs touching the top files whose titles
  share the task's words, with their file lists (the template to copy);
- the tests mapped to the top files, and who calls the top symbols.

Every section is a lead, not a fact, and degrades to absence on a failed
read; the budget trims whole sections from the bottom.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)

TOP_FILES = 6
CO_CHANGE_FILES = 2  # the top files whose co-change is read
CO_CHANGE_CAP = 8
CO_CHANGE_MIN_PRS = 2
CO_CHANGE_MIN_SHARE = 0.2
PAST_PRS = 3
PR_FILES_SHOWN = 8
MASS_PR_FILES = 80  # a PR touching more co-changes with everything
TESTS_CAP = 6
THIN_HISTORY = 3  # fewer usable PRs on a file: read its directory's

_DOC_PREFIXES = ("docs/",)
_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
_STOP = frozenset(
    "a an and the to of for in on with into from by at as is it this that be "
    "add adds adding new create make change changes changing update use using "
    "support how where what which i we should".split()
)
_ADDITIVE = re.compile(r"(?i)^\s*(add|adding|new|create|introduce|implement)\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def is_doc(path: str) -> bool:
    return path.startswith(_DOC_PREFIXES) or path.lower().endswith(_DOC_SUFFIXES)


def is_test(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or base.startswith("test_")
        or (".test." in base or ".spec." in base)
    )


def task_terms(task: str) -> set[str]:
    """Content words of a task, lowercased, a plural's trailing s dropped."""
    out: set[str] = set()
    for w in _WORD.findall(task.lower()):
        if w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def title_score(task: str, title: str) -> float:
    """How much a merged PR's title looks like the task: shared content words,
    plus half a point when both add something (a `feat` PR for an "add" task)."""
    t = task_terms(task)
    if not t:
        return 0.0
    words = task_terms(title)
    score = float(len(t & words))
    if score and _ADDITIVE.match(task) and title.lower().startswith("feat"):
        score += 0.5
    return score


def top_files(
    hits: list[dict[str, Any]], cap: int = TOP_FILES
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group search hits by file, preferring implementation symbols.

    Search rank still breaks ties. Tests, generated output and configuration remain
    useful supporting evidence, but should not consume the small implementation budget.
    """

    def priority(hit: dict[str, Any]) -> int:
        path = str(hit.get("path") or "").lower()
        symbol = str(hit.get("symbol") or hit.get("qualified_name") or "")
        if is_doc(path):
            return 3
        if path.startswith(("test/", "tests/", "fixtures/")) or "/test" in path:
            return 2
        if path.endswith((".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".lock")):
            return 2
        return 0 if symbol else 1

    seen: dict[str, dict[str, Any]] = {}
    for _, h in sorted(enumerate(hits), key=lambda pair: (priority(pair[1]), pair[0])):
        path = str(h.get("path") or "")
        if path and path not in seen:
            seen[path] = h
    code = [h for p, h in seen.items() if not is_doc(p)]
    docs = [h for p, h in seen.items() if is_doc(p)]
    return code[:cap], docs[:2]


def usable_prs(prs: list[dict[str, Any]], exclude_pr: int = 0) -> list[dict[str, Any]]:
    """PRs that vote on co-change: not the excluded one, not a revert, not a
    mass change."""
    return [
        pr
        for pr in prs
        if int(pr.get("pr_number") or 0) != exclude_pr
        and not pr.get("is_revert")
        and 0 < len(pr.get("files") or []) <= MASS_PR_FILES
    ]


def pick_anchors(paths: list[str], k: int = CO_CHANGE_FILES) -> list[str]:
    """The files whose co-change is read: the ones whose directory holds the
    most of the task's top hits (where the change lives), search rank
    breaking ties — a lone hit elsewhere is a mention, not the place."""

    def folder(p: str) -> str:
        return p.rsplit("/", 1)[0] if "/" in p else ""

    count: dict[str, int] = {}
    for p in paths:
        count[folder(p)] = count.get(folder(p), 0) + 1
    order = sorted(range(len(paths)), key=lambda i: (-count[folder(paths[i])], i))
    return [paths[i] for i in order[:k]]


def co_changed(
    prs: list[dict[str, Any]], anchor: str, *, exclude_pr: int = 0
) -> list[dict[str, Any]]:
    """The files merged PRs touching `anchor` also touched: support >=
    CO_CHANGE_MIN_PRS and >= CO_CHANGE_MIN_SHARE of those PRs, mass PRs and
    decision docs excluded (every PR has its own)."""
    usable = usable_prs(prs, exclude_pr)
    counts: dict[str, int] = {}
    for pr in usable:
        for f in set(map(str, pr.get("files") or [])):
            if f != anchor and not f.startswith("docs/decisions/"):
                counts[f] = counts.get(f, 0) + 1
    of = len(usable)
    return [
        {"path": p, "prs": n, "of": of, "anchor": anchor}
        for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if of and n >= CO_CHANGE_MIN_PRS and n / of >= CO_CHANGE_MIN_SHARE
    ]


def past_prs(
    task: str, prs: list[dict[str, Any]], *, exclude_pr: int = 0, cap: int = PAST_PRS
) -> list[dict[str, Any]]:
    """Merged PRs whose titles share the task's words, best match then newest."""
    best: dict[int, dict[str, Any]] = {}
    for pr in prs:
        n = int(pr.get("pr_number") or 0)
        if not n or n == exclude_pr or pr.get("is_revert"):
            continue
        if len(pr.get("files") or []) > MASS_PR_FILES:
            continue
        s = title_score(task, str(pr.get("title") or ""))
        if s > 0 and (n not in best or s > best[n]["score"]):
            best[n] = {**pr, "score": s}
    ranked = sorted(best.values(), key=lambda r: (-r["score"], -int(r["pr_number"])))
    return ranked[:cap]


def pr_file_list(files: list[str], cap: int = PR_FILES_SHOWN) -> str:
    """A PR's files as a template: source first, then tests, docs last; files under
    `docs/decisions/` are counted, not named."""
    fs = [str(f) for f in files]
    decisions = [f for f in fs if f.startswith("docs/decisions/")]
    rest = [f for f in fs if f not in decisions]
    rest.sort(key=lambda f: (is_doc(f), is_test(f), f))
    shown = rest[:cap]
    tail = []
    if len(rest) > cap:
        tail.append(f"+{len(rest) - cap} more")
    if decisions:
        tail.append("a decision doc")
    return ", ".join(shown) + (f" ({', '.join(tail)})" if tail else "")


def render(
    task: str,
    code: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    co: list[dict[str, Any]],
    past: list[dict[str, Any]],
    tests: list[str],
    callers: list[dict[str, Any]],
    token_budget: int,
) -> str:
    """The briefing as markdown, whole sections trimmed from the bottom."""
    from server.code.app.index.packs import estimate_tokens

    lines = [
        f"# Task briefing: {task.strip()[:200]}",
        "Leads from the index (search, co-change, merged PRs) — verify before relying on one.",
    ]
    if code:
        lines += ["", "## Where this lives (code.search for the task)"]
        for h in code:
            span = f"{h.get('start_line') or 0}-{h.get('end_line') or 0}"
            sym = str(h.get("symbol") or "")
            lines.append(f"- {h['path']}:{span}" + (f" `{sym}`" if sym else ""))
    if co:
        lines += ["", "## A change there usually also touches (co-change in merged PRs)"]
        lines.extend(
            f"- {c['path']} — with {c['anchor']} in {c['prs']} of {c['of']} PRs" for c in co
        )
    if past:
        lines += ["", "## Merged PRs that made a change like this (their files are the template)"]
        lines.extend(
            f"- #{p['pr_number']} {str(p.get('title') or '')[:120]} — "
            f"{pr_file_list(list(p.get('files') or []))}"
            for p in past
        )
    if tests:
        lines += ["", "## Related tests"]
        lines.extend(f"- {t}" for t in tests)
    if callers:
        lines += ["", "## Who calls the top symbols"]
        lines.extend(f"- `{c['to']}` ← `{c['from']}` ({c['path']}:{c['line']})" for c in callers)
    if docs:
        lines += ["", "## Docs that explain it"]
        lines.extend(f"- {d['path']}" for d in docs)
    text = "\n".join(lines)
    while estimate_tokens(text) > token_budget and "\n## " in text:
        text = text[: text.rfind("\n## ")]
    return text


async def _safe(coro: Any, default: Any) -> Any:
    try:
        return await coro
    except Exception:  # a failed section takes its default; the pack still serves
        logger.info("task pack section failed", exc_info=True)
        return default


async def build_task_pack(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    task: str,
    hits: list[dict[str, Any]],
    token_budget: int,
    exclude_pr: int = 0,
) -> tuple[str, list[str]]:
    """(briefing text, the paths it names). `hits` are code.search results
    for the task; `exclude_pr` keeps one merged PR out of the co-change and
    past-PR reads (the measurement's ground truth)."""
    from server.code.app.index import packs

    code, docs = top_files(hits)
    anchors = pick_anchors([str(h["path"]) for h in code])
    paths = [str(h["path"]) for h in code]
    names = [str(h.get("symbol") or "") for h in code if "::" in str(h.get("symbol") or "")][:4]

    async def history(path: str) -> tuple[str, list[dict[str, Any]]]:
        """(the anchor it is read for, merged PRs). A file each change adds a
        sibling of (a migration, a worker) is touched by one PR; its
        directory's PRs say what adding one there touches."""
        rows: list[dict[str, Any]] = await repo.pr_history_for(org, ns, paths=[path], limit=60)
        prefix = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        # a top-level folder (server/) co-changes with everything: two levels
        # at least (schemas/migrations/, server/workers/)
        if len(usable_prs(rows, exclude_pr)) >= THIN_HISTORY or prefix.count("/") < 2:
            return path, rows
        under: list[dict[str, Any]] = await repo.pr_history_under(org, ns, prefix=prefix, limit=60)
        return (f"a file under {prefix}", under) if under else (path, rows)

    async def callers_of() -> list[dict[str, Any]]:
        ids = await repo.symbol_ids_for_names(org, ns, names)
        seeds = [{"symbol_id": i} for v in ids.values() for i in v]
        rows = await packs._callers_of(repo, org, ns, seeds)
        per: dict[str, int] = {}
        seen_pairs: set[tuple[str, str]] = set()
        out = []
        for r in rows:  # one line per caller (a caller calling thrice is one lead)
            pair = (str(r["to"]), str(r["from"]))
            if pair in seen_pairs or per.get(pair[0], 0) >= 3:
                continue
            seen_pairs.add(pair)
            per[pair[0]] = per.get(pair[0], 0) + 1
            out.append(r)
        return out[:9]

    hist, tests, callers = await asyncio.gather(
        asyncio.gather(*(_safe(history(a), (a, [])) for a in anchors)),
        _safe(packs._tests_for(repo, org, ns, paths), []),
        _safe(callers_of(), []) if names else asyncio.sleep(0, []),
    )
    co: list[dict[str, Any]] = []
    seen = set(paths)
    for anchor, prs in hist:
        for c in co_changed(prs, anchor, exclude_pr=exclude_pr):
            if c["path"] not in seen:
                seen.add(c["path"])
                co.append(c)
    co = co[:CO_CHANGE_CAP]
    all_prs = [pr for _, prs in hist for pr in prs]
    past = past_prs(task, all_prs, exclude_pr=exclude_pr)
    test_list = list(dict.fromkeys([*tests, *(c["path"] for c in co if is_test(c["path"]))]))[
        :TESTS_CAP
    ]
    text = render(task, code, docs, co, past, test_list, callers, token_budget)
    named = [
        *paths,
        *(c["path"] for c in co),
        *test_list,
        *(f for p in past for f in (p.get("files") or [])),
    ]
    return text, list(dict.fromkeys(named))
