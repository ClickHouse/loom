"""Context-pack rendering: everything a review bot needs about a merged PR,
precomputed once at ingest and read by every consumer (write-once,
read-many is the whole token economics of the pack).

The pack is compact markdown, token-budgeted, and lists its sources so
code.outcome can attribute usefulness. Token estimate = chars/4 (same
heuristic the context.pack plane uses; exactness doesn't matter, headroom
does).
"""

from __future__ import annotations

import logging
from typing import Any

from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET_TOKENS = 2500


# Section name -> the H2 header build_pr_pack renders. code.context_pack's
# `sections` filter keys on these; renaming a header here without updating the
# other breaks the filter silently, so keep them adjacent.
SECTION_HEADERS: dict[str, str] = {
    "symbols": "## Touched symbols and who calls them",
    "co_change": (
        "## Often changed together, not touched here (co-change signal, ~1 in 3 is a real gap)"
    ),
    "tests": "## Related tests",
    "reviews": "## What reviewers said about these files before",
    "prs": "## Recent PRs touching the same files",
    "qa": "## tracker record (open issues naming these functions; what this PR fixes or caused)",
    "files": "## Changed files",
}


def filter_sections(pack: str, keep: list[str]) -> str:
    """The pack's header lines plus only the named sections, in pack order.
    Unknown names are ignored; an empty `keep` returns the pack unchanged."""
    if not keep:
        return pack
    wanted = {SECTION_HEADERS[k] for k in keep if k in SECTION_HEADERS}
    out: list[str] = []
    keeping = True  # header (before the first H2) is always kept
    for line in pack.split("\n"):
        if line.startswith("## "):
            keeping = line.strip() in wanted
        if keeping:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def estimate_tokens(text: str) -> int:
    """Markdown pack text: measured 4.23 chars/token, so the shared default
    rate (4.0, slightly conservative) applies."""
    from server.code.domain.units import estimate_tokens as _est

    return _est(text)


def fit_pack_budget(pack: str, token_budget: int) -> tuple[str, bool]:
    """Trim cached PR packs at read time while retaining requested sections."""
    if estimate_tokens(pack) <= token_budget:
        return pack, False
    marker = "\n… [context truncated to token budget]"
    blocks: list[list[str]] = []
    for line in pack.splitlines():
        if line.startswith("## ") and blocks:
            blocks.append([])
        if not blocks:
            blocks.append([])
        blocks[-1].append(line)

    def render() -> str:
        return "\n".join(line for block in blocks for line in block).rstrip()

    # estimate_tokens is len(text)//4. Track lengths while removing rows;
    # repeatedly rendering a long cached pack would make this quadratic.
    sizes = [sum(len(line) + 1 for line in block) for block in blocks]
    room = 4 * token_budget + 3 - len(marker)
    total = sum(sizes) - 1
    while total > room:
        candidates = [i for i, block in enumerate(blocks) if i > 0 and len(block) > 1]
        if not candidates:
            break
        largest = max(candidates, key=lambda i: sizes[i])
        removed = blocks[largest].pop()
        amount = len(removed) + 1
        sizes[largest] -= amount
        total -= amount
    text = render()
    if estimate_tokens(text + marker) > token_budget:
        # A pathological header can exceed the budget on its own.
        text = text[: max(0, (token_budget - estimate_tokens(marker)) * 4)]
        while text and estimate_tokens(text + marker) > token_budget:
            text = text[:-4]
    return text + marker, True


def _anchor(row: dict[str, Any]) -> str:
    """`path:start_line` when the row knows its line, else the bare path."""
    path = str(row.get("path") or "")
    try:
        line = int(row.get("start_line") or 0)
    except (TypeError, ValueError):
        line = 0
    return f"{path}:{line}" if path and line > 0 else path


async def build_pr_pack(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    pr_number: int,
    title: str,
    commit_sha: str,
    changed_paths: list[str],
    changed_symbols: list[dict[str, Any]],  # qualified_name/signature/path rows
    token_budget: int = _DEFAULT_BUDGET_TOKENS,
) -> str:
    """Render the pack for one merged PR. All reads bounded; every section
    degrades to absence rather than failing the pack."""
    lines: list[str] = [
        f"# PR #{pr_number}: {title}".strip(),
        f"merge commit: {commit_sha[:12]} · files changed: {len(changed_paths)}"
        f" · symbols touched: {len(changed_symbols)}",
    ]

    # Order = what a reviewer cannot get from the diff itself, first. Budget
    # trimming drops sections from the bottom, so the file list — which the
    # bot already has — goes last.
    callers = await _callers_of(repo, org, ns, changed_symbols)
    by_target: dict[str, list[dict[str, Any]]] = {}
    for c in callers:
        by_target.setdefault(str(c["to"]), []).append(c)
    # Tests mapped to the changed files (rendered further down).
    tests = await _tests_for(repo, org, ns, changed_paths)
    if changed_symbols:
        lines += ["", "## Touched symbols and who calls them"]
        env_line = await _envelope_line(repo, org, ns, changed_symbols)
        if env_line:
            lines.append(env_line)
        for s in changed_symbols[:30]:
            sig = str(s.get("signature") or "")
            if sig.startswith("(scip:"):
                sig = ""  # overload hash, not a human signature
            q = str(s["qualified_name"])
            cs = by_target.get(q, [])
            # path:line is the unit's stable handle: it greps, it resolves
            # through code.enclosing, and a reviewer can cite it. Callers
            # below carry one too.
            head = f"- `{q}{sig}` — {_anchor(s)}"
            if cs:
                head += f" · {len(cs)} caller{'s' if len(cs) != 1 else ''}"
            lines.append(head)
            lines.extend(f"    ← `{c['from']}` ({c['path']}:{c['line']})" for c in cs[:3])
            if len(cs) > 3:
                lines.append(f"    ← … {len(cs) - 3} more")

    # Files that usually change together with these but are NOT in this PR —
    # the single highest-yield review finding (co_change_missing in code.impact).
    gaps = await _co_change_gaps(repo, org, ns, changed_paths, exclude_pr=pr_number)
    if gaps:
        lines += ["", SECTION_HEADERS["co_change"]]
        lines.extend(
            f"- {g['path']} (changed in {g['prs']} of the last {g['of']} PRs touching these files)"
            for g in gaps
        )

    if tests:
        lines += ["", "## Related tests"]
        lines.extend(f"- {t}" for t in tests[:20])

    # What human reviewers said on these exact files in earlier PRs — the
    # repeated feedback that never made it into an instructions file.
    reviews = await _reviews_for(repo, org, ns, changed_paths, exclude_pr=pr_number)
    if reviews:
        lines += ["", SECTION_HEADERS["reviews"]]
        lines.extend(render_review_lines(reviews))

    # Recent PRs overlapping these files
    related = await _related_prs(repo, org, ns, changed_paths, exclude_pr=pr_number)
    if related:
        lines += ["", "## Recent PRs touching the same files"]
        lines.extend(
            f"- #{r['pr_number']}: {r['title']} ({r['overlap']} shared files)" for r in related[:8]
        )

    # The tracker record joined at the pack's symbols — open issues whose text names the
    # touched functions, and the PR's own fixes / caused edges — read live from the
    # tracker namespace bound to this code namespace.
    lines += await _tracker_lines(org, ns, pr_number, changed_symbols)

    lines += ["", "## Changed files"]
    lines.extend(f"- {p}" for p in changed_paths[:15])
    if len(changed_paths) > 15:
        lines.append(f"- … and {len(changed_paths) - 15} more (the diff has them all)")

    text = "\n".join(lines)
    # Budget: trim whole sections from the bottom until it fits.
    while estimate_tokens(text) > token_budget and "\n## " in text:
        text = text[: text.rfind("\n## ")]
    return text


async def tracker_section_rows(
    org: str, ns: str, pr_number: int, changed_symbols: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """The tracker record at one merged PR and its symbols, as (status, rows): the PR's
    own fact (what it fixes / caused / backports) and the open issues whose text names
    the touched functions. Shared by the pack's `qa` section and the review brief's; the
    gate is the same for both — packs and briefs are built by the ingest process and
    cached under the CODE namespace, so only a tracker namespace that is NOT restricted
    may feed one: a restricted tracker namespace's rows must never be readable through a
    code namespace's cache. Status is `not_bound`, `restricted`, `served` or `empty`; a
    failed read raises."""
    from server.identity.app.namespace_classification import is_restricted
    from server.tracker.app.binding import refresh_overrides, tracker_namespace_for
    from server.tracker.app.facts import fact_of, tracker_fact_line
    from server.tracker.app.source import tracker_source
    from server.tracker.domain import bisect as bi

    await refresh_overrides()  # the links as of the refresh interval, branches included
    tracker_ns = tracker_namespace_for(org, ns)
    if not tracker_ns:
        return "not_bound", []
    if await is_restricted(org, tracker_ns):
        return "restricted", []
    source = tracker_source(org, tracker_ns)
    frames = bi.frame_names([str(s.get("qualified_name") or "") for s in changed_symbols])
    issues = await source.open_issues_naming(org, tracker_ns, frames, limit=5) if frames else []
    rows = await source.items_by_number(org, tracker_ns, [pr_number]) if pr_number else {}
    edges = (
        await source.edges_from(
            org,
            tracker_ns,
            [f"{rows[pr_number]['repo']}#{pr_number}"],
            ["fixes", "caused_by", "backports"],
        )
        if rows.get(pr_number)
        else []
    )
    out: list[dict[str, Any]] = []
    if rows.get(pr_number):
        fact = fact_of(rows[pr_number], edges)
        line = tracker_fact_line(pr_number, fact)
        if line:
            out.append({"kind": "pr", "number": int(pr_number), "line": line, **fact})
    for r in issues[:5]:
        out.append(
            {
                "kind": "open_issue",
                "number": int(r.get("number") or 0),
                "severity": str(r.get("severity_level") or "unrated"),
                "bisect": str(r.get("bisect_outcome") or ""),
                "title": str(r.get("title") or "")[:110],
            }
        )
    return ("served" if out else "empty"), out


async def _tracker_lines(
    org: str, ns: str, pr_number: int, changed_symbols: list[dict[str, Any]]
) -> list[str]:
    """The `qa` section, or [] (absence, never a failed pack). The reads and the
    restricted-namespace gate are tracker_section_rows'."""
    try:
        status, rows = await tracker_section_rows(org, ns, pr_number, changed_symbols)
    except Exception as e:  # noqa: BLE001 — a pack section degrades to absence
        logger.debug("pack qa section skipped for PR %s: %s", pr_number, e)
        return []
    if status != "served":
        return []
    out: list[str] = []
    for r in rows:
        if r.get("kind") == "pr":
            out.append(f"- {r['line']}")
        else:
            bis = f", bisect {r['bisect']}" if r.get("bisect") else ""
            out.append(
                f"- open #{int(r.get('number') or 0)} [{r.get('severity')}{bis}]: "
                f"{r.get('title') or ''}"
            )
    if not out:
        return []
    return ["", SECTION_HEADERS["qa"], *out]


async def _envelope_line(
    repo: CodeRepo,
    org: str,
    ns: str,
    changed_symbols: list[dict[str, Any]],
) -> str:
    """One line under the section header saying whether the caller counts below are
    exact or a floor. ~20 tokens. Fails open to no line: the pack's other sections never
    depend on it."""
    from server.code.domain.index.epistemic import envelope, scip_coverage_from_rows

    try:
        bare = list(
            dict.fromkeys(
                str(s.get("name") or str(s.get("qualified_name") or "").split("::")[-1])
                for s in changed_symbols
            )
        )
        misses = await repo.call_misses_for_names(org, ns, [b for b in bare if b])
        unresolved = sum(int(c.get("unresolved", 0)) for c in misses.values())
        ambiguous = sum(int(c.get("ambiguous", 0)) for c in misses.values())
        paths = list(dict.fromkeys(str(s.get("path") or "") for s in changed_symbols))
        covered = scip_coverage_from_rows(changed_symbols)
        remaining = [p for p in paths if p and p not in covered]
        if remaining:
            covered |= await repo.scip_covered_paths(org, ns, remaining)
        env = envelope(
            unresolved=unresolved,
            ambiguous=ambiguous,
            seed_paths=paths,
            scip_paths=covered,
        )
    except Exception:  # noqa: BLE001 — a pack line degrades to absence
        return ""
    line = f"callers below are {env.epistemic}"
    if env.causes:
        line += f" ({', '.join(env.causes)})"
    return line


async def _callers_of(
    repo: CodeRepo, org: str, ns: str, changed_symbols: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids = [s["symbol_id"] for s in changed_symbols if s.get("symbol_id")][:50]
    if not ids:
        return []
    rows = await repo.live_callers_of_symbols(org, ns, ids, limit=50)
    return [{"from": r["f"], "to": r["t"], "path": r["path"], "line": r["line"]} for r in rows]


def render_co_change_gaps(
    co_changed: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    min_prs: int = 4,
    min_conf: float = 0.15,
    cap: int = 5,
) -> list[dict[str, Any]]:
    """Pure: from past PRs' file lists, the files that co-changed with this
    change but are absent from it — support >= min_prs, confidence
    (support / PRs considered) >= min_conf, at most `cap`, most frequent first.

    Measured on 400 historical ClickHouse PRs against what a follow-up PR touched within
    14 days: a looser floor (2 of 50, up to 10 leads) put ~9 leads in 91% of packs at
    25% precision; this floor gives ~4 leads at 36% with the same 6-in-10 chance that a
    pack holds a real gap. Excluding hub files (Settings.cpp,
    SettingsChangesHistory.cpp) LOWERED precision — they really are the files follow-ups
    forget. Co-change is a statistical lead, never a fact; the section header says so."""
    present = set(changed_paths)
    counts: dict[str, int] = {}
    for pr in co_changed:
        for f in pr.get("files") or []:
            f = str(f)
            if f not in present:
                counts[f] = counts.get(f, 0) + 1
    of = len(co_changed)
    out = [
        {"path": p, "prs": n, "of": of}
        for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if n >= min_prs and of and n / of >= min_conf
    ]
    return out[:cap]


async def _co_change_gaps(
    repo: CodeRepo, org: str, ns: str, changed_paths: list[str], *, exclude_pr: int
) -> list[dict[str, Any]]:
    if not changed_paths:
        return []
    try:
        co_changed = await repo.pr_history_for(org, ns, paths=changed_paths[:50], limit=50)
    except Exception:  # noqa: BLE001 — a pack section degrades to absence
        return []
    co_changed = [pr for pr in co_changed if int(pr.get("pr_number") or 0) != exclude_pr]
    return render_co_change_gaps(co_changed, changed_paths)


async def _reviews_for(
    repo: CodeRepo, org: str, ns: str, changed_paths: list[str], *, exclude_pr: int
) -> list[dict[str, Any]]:
    if not changed_paths:
        return []
    try:
        return await repo.review_comments_for(
            org, ns, changed_paths[:50], exclude_pr=exclude_pr, limit=30
        )
    except Exception:  # noqa: BLE001 — a pack section degrades to absence
        return []


def render_review_lines(
    reviews: list[dict[str, Any]], *, cap: int = 5, excerpt_chars: int = 160
) -> list[str]:
    """Pure: one line per comment, newest first, one comment per path so a
    hot file does not crowd the section out, excerpt flattened to a line."""
    out: list[str] = []
    seen_paths: set[str] = set()
    for r in reviews:
        path = str(r.get("path") or "")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        body = " ".join(str(r.get("body") or "").split())
        if len(body) > excerpt_chars:
            body = body[: excerpt_chars - 1].rstrip() + "…"
        where = f"{path}:{int(r.get('line') or 0)}" if int(r.get("line") or 0) else path
        out.append(f'- {where} — "{body}" (#{r.get("pr_number")}, @{r.get("author")})')
        if len(out) >= cap:
            break
    return out


async def _tests_for(repo: CodeRepo, org: str, ns: str, changed_paths: list[str]) -> list[str]:
    if not changed_paths:
        return []
    # Relevance floor: PR co-occurrence alone drags harness config (tests/config/*.xml) and oracle
    # files in beside the one real test. Structural evidence (path convention, include graph) ranks
    # first, then co-occurrence weighted by how many changed files a test is tied to; harness
    # config, integration-test config fixtures (configs/*.xml), oracles, fuzzers and .reference
    # files are never "related tests".
    return await repo.related_tests_for_paths(org, ns, changed_paths[:100])


async def _related_prs(
    repo: CodeRepo, org: str, ns: str, changed_paths: list[str], *, exclude_pr: int
) -> list[dict[str, Any]]:
    if not changed_paths:
        return []
    rows = await repo.prs_overlapping_paths(org, ns, changed_paths[:100], exclude_pr=exclude_pr)
    return [
        {"pr_number": int(r["pr_number"]), "title": r["title"], "overlap": int(r["overlap"])}
        for r in rows
    ]
