"""Building a review brief: each section from its op call, the brief row for a merge
or an open PR, and the tier budget it is served under."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from server.code.app.index.review_brief import (
    EMPTY,
    IMPACT_SEED_FILES,
    MAX_COVERAGE_SETTINGS,
    MAX_COVERAGE_SYMBOLS,
    MAX_DIFF_CHARS,
    MAX_HISTORY_SYMBOLS,
    MAX_SETTINGS,
    MAX_SYMBOL_ROWS,
    NOT_BUILT,
    PAYLOAD_VERSION,
    SECTION_ORDER,
    SERVED,
    BriefInputs,
    CallOp,
    blame_verdict,
    canonical,
    decisions_of,
    deleted_ranges,
    failed_section,
    history_verdict,
    parse_diff,
    paths_verdict,
    section,
    settings_verdict,
    source_stems,
    tests_verdict,
)

logger = logging.getLogger(__name__)


async def build_brief(
    inp: BriefInputs,
    *,
    call: CallOp,
    repo: Any = None,
    tracker_rows: Callable[..., Awaitable[tuple[str, list[dict[str, Any]]]]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Assemble the brief: one code.impact read seeds `paths` and `symbols`
    and names the functions every other section fans out over (bounded);
    the fan-outs run concurrently, each op failing open to a `failed`
    section. Returns the payload dict (canonicalise with `canonical`)."""
    started = time.monotonic()
    sections: dict[str, dict[str, Any]] = {}
    facts = parse_diff(inp.diff)
    paths = list(dict.fromkeys([*inp.changed_paths, *facts.paths]))

    impact: dict[str, Any] | None = None
    try:
        impact = await call(
            "code.impact",
            {
                "org": inp.org,
                "namespace": inp.ns,
                "files": paths[:IMPACT_SEED_FILES],
                "diff": inp.diff[:MAX_DIFF_CHARS],
                "depth": 1,
                "per_hop_limit": 200,
            },
        )
    except Exception as e:  # noqa: BLE001 — every section fails open
        logger.info("review_brief: code.impact failed for PR %s: %s", inp.pr_number, e)
        sections["symbols"] = failed_section("code.impact", e)

    sections["paths"] = _paths_section(impact, paths, inp.path_prefixes)
    touched = list((impact or {}).get("touched_symbols") or [])[:MAX_SYMBOL_ROWS]
    fn_rows = await _function_rows(inp, repo, [str(t.get("path") or "") for t in touched] or paths)
    if impact is not None:
        sections["symbols"] = await _symbols_section(impact, touched, inp, repo, fn_rows)
    ranked = sorted(
        sections["symbols"].get("rows") or [],
        key=lambda r: (-int(r.get("callers_hop1") or 0), str(r.get("qualified_name") or "")),
    )
    names = [str(r["qualified_name"]) for r in ranked]
    stems = source_stems(paths)
    setting_names = [n for n, _ in facts.settings][:MAX_SETTINGS]
    from server.code.domain.index.verdicts import is_graphed_path

    ranges = deleted_ranges(facts, src_only=is_graphed_path)

    tests_t = _tests_section(call, inp, names[:MAX_COVERAGE_SYMBOLS], setting_names, stems)
    settings_t = _settings_section(call, inp, facts.settings[:MAX_SETTINGS])
    history_t = _history_section(call, inp, names[:MAX_HISTORY_SYMBOLS])
    blame_t = _blame_section(call, inp, ranges)
    qa_t = _tracker_section(inp, touched, tracker_rows)
    risk_t = _risk_section(inp, paths, repo, now=now)
    legs: list[Awaitable[dict[str, Any]]] = [tests_t, settings_t, history_t, blame_t, qa_t, risk_t]
    names_built = ["tests", "settings", "history", "blame", "tracker", "risk"]
    gathered = await asyncio.gather(*legs)
    for name, built in zip(names_built, gathered, strict=True):
        sections[name] = built
    sections["do_not_flag"] = section(
        NOT_BUILT,
        verdict="",
        note="mined do-not-flag rules are not built yet",
    )
    ordered = {n: sections[n] for n in SECTION_ORDER}
    built_at = (now or (lambda: datetime.now(UTC)))().replace(microsecond=0)
    # The count of functions the open PR adds rides from the impact read (the symbols
    # section is capped at 30 rows); the key is absent on a merged brief
    extra: dict[str, Any] = {}
    if impact is not None and impact.get("overlay"):
        extra["overlay_added"] = int(impact.get("overlay_added") or 0)
        if impact.get("overlay_note"):
            extra["overlay_note"] = str(impact["overlay_note"])  # what the overlay left out
    return {
        **extra,
        "v": PAYLOAD_VERSION,
        "pr_number": int(inp.pr_number),
        "merge_sha": inp.merge_sha,
        "title": inp.title[:200],
        "built_at": built_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "indexed_commit": inp.indexed_commit,
        "build_ms": int((time.monotonic() - started) * 1000),
        "decisions": decisions_of(ordered),
        "sections": ordered,
    }


def _paths_section(
    impact: dict[str, Any] | None,
    paths: list[str],
    prefixes: tuple[str, ...],
) -> dict[str, Any]:
    rows = list((impact or {}).get("paths_seen") or [])
    if not rows and paths:
        from server.code.domain.index.verdicts import paths_seen

        rows = paths_seen(paths, prefixes=prefixes)
    rows = [{"path": str(r.get("path") or ""), "class": str(r.get("class") or "")} for r in rows]
    return section(SERVED, verdict=paths_verdict(rows), epistemic="exact", rows=rows)


async def _function_rows(inp: BriefInputs, repo: Any, paths: list[str]) -> list[dict[str, Any]]:
    """The functions the index holds in the touched paths — read once, shared
    by the symbols section (ids for the caller counts). Empty when the repo
    has no such read."""
    wanted = list(dict.fromkeys(p for p in paths if p))[:60]
    if repo is None or not wanted:
        return []
    try:
        return list(await repo.functions_for_paths(inp.org, inp.ns, wanted, limit=400))
    except Exception as e:  # noqa: BLE001 — decoration; the sections degrade
        logger.info("review_brief: functions_for_paths failed for PR %s: %s", inp.pr_number, e)
        return []


async def _symbols_section(
    impact: dict[str, Any],
    touched: list[dict[str, Any]],
    inp: BriefInputs,
    repo: Any,
    fn_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = [
        {
            "qualified_name": str(t.get("qualified_name") or ""),
            "path": str(t.get("path") or ""),
            "start_line": int(t.get("start_line") or 0),
            "tier": str(t.get("tier") or ""),
            "callers_hop1": 0,
            "fan_in": 0,
            "fan_in_nontest": 0,
            # A function the open PR adds (overlay seed); the key rides only then, so a
            # merged brief carries no such key
            **({"via": "added"} if str(t.get("via") or "") == "added" else {}),
        }
        for t in touched
    ]
    if rows and repo is not None:
        try:
            await _decorate_callers(rows, inp, repo, fn_rows or [])
        except Exception as e:  # noqa: BLE001 — counts are decoration
            logger.info("review_brief: caller counts failed for PR %s: %s", inp.pr_number, e)
    rows.sort(key=lambda r: (-int(r["callers_hop1"]), r["qualified_name"], r["path"]))
    verdict = str(impact.get("verdict") or "") or ("touched" if rows else "no_symbols")
    by_hop: dict[Any, Any] = dict(impact.get("by_hop_counts") or {})
    hop1 = int(by_hop.get(1) or by_hop.get("1") or 0)
    note = f"{len(rows)} touched functions; {hop1} hop-1 call sites in total" if rows else ""
    return section(
        SERVED,
        verdict=verdict,
        epistemic=str(impact.get("epistemic") or "lower_bound"),
        causes=[str(c) for c in (impact.get("causes") or [])],
        rows=rows,
        note=note,
    )


async def _decorate_callers(
    rows: list[dict[str, Any]],
    inp: BriefInputs,
    repo: Any,
    fn_rows: list[dict[str, Any]],
) -> None:
    """symbol_id per (path, qualified_name) through the functions the index holds in the
    touched paths, then one grouped edge read for hop-1 call sites and the weekly fan-in
    stats when they are on."""
    from server.code.app import fan_in as code_fan_in

    ids: dict[tuple[str, str], str] = {}
    for f in sorted(fn_rows, key=lambda f: 0 if str(f.get("tier")) == "scip" else 1):
        ids.setdefault((str(f["path"]), str(f["qualified_name"])), str(f["symbol_id"]))
    keys = [(r["path"], r["qualified_name"]) for r in rows]
    wanted = [ids[k] for k in keys if k in ids]
    counts = await repo.caller_counts(inp.org, inp.ns, wanted) if wanted else {}
    stats: dict[str, dict[str, Any]] = {}
    if wanted:
        try:
            stats = await code_fan_in.stats_for(repo, inp.org, inp.ns, wanted)
        except Exception as e:  # noqa: BLE001 — fan-in is decoration on decoration
            logger.info("review_brief: fan-in stats failed for PR %s: %s", inp.pr_number, e)
    for r in rows:
        sid = ids.get((r["path"], r["qualified_name"]), "")
        c = counts.get(sid) or {}
        r["callers_hop1"] = int(c.get("call_sites") or 0)
        # How many of those sites are registration functions; the key rides only when
        # there are any, so a brief without them carries no extra key
        if int(c.get("registration") or 0):
            r["callers_registration"] = int(c["registration"])
        # A stats row carries `fan_in_calls` / `fan_in_nontest` (the table's columns);
        # the brief's `fan_in` is every live call site (the row's key is not `fan_in`).
        st = stats.get(sid) or {}
        r["fan_in"] = int(st.get("fan_in_calls") or 0)
        r["fan_in_nontest"] = int(st.get("fan_in_nontest") or 0)


async def _tests_section(
    call: CallOp,
    inp: BriefInputs,
    names: list[str],
    setting_names: list[str],
    stems: list[str],
) -> dict[str, Any]:
    base = {"org": inp.org, "namespace": inp.ns}
    subjects = [("symbol", n) for n in names] + [
        ("setting", s) for s in setting_names[:MAX_COVERAGE_SETTINGS]
    ]
    bare = [n.split("::")[-1] for n in names]
    tasks: list[Awaitable[dict[str, Any]]] = [
        call("code.coverage_claim", {**base, kind: subject, "limit": 5})
        for kind, subject in subjects
    ]
    home_wanted = bool(stems or setting_names or bare)
    if home_wanted:
        tasks.append(
            call(
                "code.test_home",
                {
                    **base,
                    "file_stems": stems[:10],
                    "settings": setting_names[:10],
                    "symbols": bare[:10],
                    "limit": 5,
                },
            )
        )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rows: list[dict[str, Any]] = []
    causes: list[str] = []
    epistemic = "lower_bound"
    for (kind, subject), res in zip(subjects, results[: len(subjects)], strict=True):
        if isinstance(res, BaseException):
            causes.append(f"coverage_claim:{type(res).__name__}")
            continue
        tests = list(res.get("tests") or [])
        rows.append(
            {
                "kind": kind,
                "subject": subject,
                "verdict": str(res.get("verdict") or "unknown"),
                "tests_total": int(res.get("tests_total") or 0),
                "tests": [str(t.get("test_path") or "") for t in tests[:2]],
                "epistemic": str(res.get("epistemic") or "lower_bound"),
            }
        )
    if home_wanted:
        res = results[-1]
        if isinstance(res, BaseException):
            causes.append(f"test_home:{type(res).__name__}")
        else:
            rows.append(
                {
                    "kind": "home",
                    "subject": " ".join(str(p) for p in (res.get("parts") or [])[:6]),
                    "verdict": str(res.get("verdict") or "unknown"),
                    "home": str(res.get("home") or ""),
                    "candidates": [
                        str(c.get("path") or "") for c in (res.get("candidates") or [])[:3]
                    ],
                    "next_free_stateless_number": int(res.get("next_free_stateless_number") or 0),
                }
            )
    if rows and all(r.get("epistemic") == "exact" for r in rows if r.get("kind") != "home"):
        epistemic = "exact"
    return section(
        SERVED,
        verdict=tests_verdict(rows),
        epistemic=epistemic,
        causes=causes,
        rows=rows,
    )


async def _settings_section(
    call: CallOp,
    inp: BriefInputs,
    settings: list[tuple[str, str]],
) -> dict[str, Any]:
    if not settings:
        return section(EMPTY, verdict="none", epistemic="exact")
    results = await asyncio.gather(
        *(
            call(
                "code.setting_facts",
                {"org": inp.org, "namespace": inp.ns, "name": n, "history_limit": 3},
            )
            for n, _ in settings
        ),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    causes: list[str] = []
    for (name, source), res in zip(settings, results, strict=True):
        if isinstance(res, BaseException):
            causes.append(f"setting_facts:{type(res).__name__}")
            continue
        rand = dict(res.get("randomized_in_ci") or {})
        hist = list(res.get("history") or [])
        rows.append(
            {
                "name": name,
                "source": source,
                "verdict": str(res.get("verdict") or "unknown"),
                "tier": str(res.get("tier") or ""),
                "randomized_in_ci": bool(rand.get("randomized") or rand.get("sampler")),
                "history_entries": len(hist),
                "last_change": str((hist[0] or {}).get("version") or "") if hist else "",
                "tests_total": int(res.get("tests_total") or 0),
            }
        )
    return section(
        SERVED,
        verdict=settings_verdict(rows),
        epistemic="exact" if rows else "lower_bound",
        causes=causes,
        rows=rows,
    )


async def _history_section(call: CallOp, inp: BriefInputs, names: list[str]) -> dict[str, Any]:
    if not names:
        return section(EMPTY, verdict="no_symbols", epistemic="lower_bound")
    results = await asyncio.gather(
        *(
            call(
                "code.symbol_history", {"org": inp.org, "namespace": inp.ns, "name": n, "limit": 5}
            )
            for n in names
        ),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    causes: list[str] = []
    for name, res in zip(names, results, strict=True):
        if isinstance(res, BaseException):
            causes.append(f"symbol_history:{type(res).__name__}")
            continue
        s = dict(res.get("summary") or {})
        rows.append(
            {
                "qualified_name": name,
                "verdict": str(res.get("verdict") or "unknown"),
                "prs_total": int(s.get("prs_total") or 0),
                "fix_prs": int(s.get("fix_prs") or 0),
                "named_fix_prs": int(s.get("named_fix_prs") or 0),
                "reverts": int(s.get("reverts") or 0),
                "caused_issues": len(list(s.get("caused_issues") or [])),
                "open_issues_naming": int(s.get("open_issues_naming") or 0),
                "last_touched": str(s.get("last_touched") or ""),
                "tracker": not bool(res.get("tracker_unavailable"))
                and bool(res.get("tracker_namespace")),
            }
        )
    rows.sort(
        key=lambda r: (
            -(r["named_fix_prs"] + r["reverts"] + r["caused_issues"]),
            -r["fix_prs"],
            r["qualified_name"],
        )
    )
    return section(
        SERVED,
        verdict=history_verdict(rows),
        epistemic="lower_bound",
        causes=causes,
        rows=rows,
        note="touch is file-level unless `qa` is true on the row; every count is a floor",
    )


async def _blame_section(
    call: CallOp,
    inp: BriefInputs,
    ranges: list[tuple[str, int, int]],
) -> dict[str, Any]:
    if not ranges:
        return section(EMPTY, verdict="no_deleted_lines", epistemic="exact")
    if not inp.parent_sha:
        return section(EMPTY, verdict="unknown", epistemic="lower_bound", causes=["parent_unknown"])
    results = await asyncio.gather(
        *(
            call(
                "code.blame_range",
                {
                    "org": inp.org,
                    "namespace": inp.ns,
                    "path": p,
                    "start_line": s,
                    "end_line": e,
                    "to_sha": inp.parent_sha,
                    "limit": 3,
                },
            )
            for p, s, e in ranges
        ),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    causes: list[str] = []
    exact = True
    for (p, s, e), res in zip(ranges, results, strict=True):
        if isinstance(res, BaseException):
            causes.append(f"blame_range:{type(res).__name__}")
            exact = False
            continue
        exact = exact and str(res.get("epistemic") or "") == "exact"
        rows.append(
            {
                "path": p,
                "start": s,
                "end": e,
                "verdict": str(res.get("verdict") or "unknown"),
                "latest_pr": int(res.get("latest_pr") or 0),
                "candidates": [
                    {
                        "pr_number": int(c.get("pr_number") or 0),
                        "title": str(c.get("title") or "")[:80],
                        "merged_at": str(c.get("merged_at") or "")[:10],
                        "lines_blamed": int(c.get("lines_blamed") or 0),
                    }
                    for c in (res.get("candidates") or [])[:3]
                ],
            }
        )
        for c in res.get("causes") or []:
            if str(c) not in causes:
                causes.append(str(c))
    return section(
        SERVED,
        verdict=blame_verdict(rows, ranges=len(ranges)),
        epistemic="exact" if exact and rows else "lower_bound",
        causes=causes,
        rows=rows,
        note="blame is exact for last touched, not for introduced",
    )


async def _tracker_section(
    inp: BriefInputs,
    touched: list[dict[str, Any]],
    tracker_rows: Callable[..., Awaitable[tuple[str, list[dict[str, Any]]]]] | None,
) -> dict[str, Any]:
    reader = tracker_rows
    if reader is None:
        from server.code.app.index.packs import tracker_section_rows

        reader = tracker_section_rows
    try:
        status, rows = await reader(inp.org, inp.ns, inp.pr_number, touched)
    except Exception as e:  # noqa: BLE001 — the section reports its failure; the brief still serves
        return failed_section("tracker", e)
    if status != SERVED:
        return section(
            status,
            verdict="tracker_unavailable" if status != EMPTY else "no_record",
            epistemic="lower_bound",
        )
    return section(
        SERVED,
        verdict="record" if rows else "no_record",
        epistemic="lower_bound",
        rows=rows,
    )


RISK_NOTE = "priors for ranking hypotheses, never a gate"


async def _risk_section(
    inp: BriefInputs,
    paths: list[str],
    repo: Any,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """`jit_risk` and `author_revert_prior` from the stored fit — one point read of
    code_index_state, then arithmetic. Lines added are counted on the diff in hand,
    files changed are the PR's paths, the author is the ledger's login (blank when the
    ledger has no row yet: the prior is then the base rate and the row says so)."""
    from server.code.app.index import risk_fit
    from server.code.domain.index import risk_calibration as rc

    no_fit = section(EMPTY, verdict="no_fit", epistemic="prior", causes=["no_fit"], note=RISK_NOTE)
    if repo is None:
        return no_fit
    try:
        fit = await risk_fit.stored_fit(repo, inp.org, inp.ns)
    except Exception as e:  # noqa: BLE001 — the section reports its failure; the brief still serves
        return failed_section("risk_fit", e)
    if fit is None:
        return no_fit
    lines_added = rc.lines_added_in(inp.diff)
    files_changed = len(paths)
    scored = rc.score(fit, lines_added=lines_added, files_changed=files_changed, author=inp.author)
    holdout = dict(fit.get("holdout") or {})
    causes: list[str] = []
    if not scored["author_known"]:
        causes.append("author_unknown" if not scored["author"] else "author_unseen")
    from server.settings import settings

    moment = (now or (lambda: datetime.now(UTC)))()
    max_age = float(settings.code_jit_risk_max_age_days)
    stale = rc.fit_age_days(fit, moment) > max_age
    if stale:
        causes.append("fit_stale")
    rows = [
        {
            "kind": "jit_risk",
            "value": scored["jit_risk"],
            "lines_added": lines_added,
            "files_changed": files_changed,
            "author_prior": scored["author_revert_prior"],
            "base_rate": float(fit.get("base_rate") or 0.0),
            "fit_at": str(fit.get("fit_at") or ""),
            "fit_n": int(fit.get("n") or 0),
            "fit_positives": int(fit.get("positives") or 0),
            "holdout_auc": float(holdout.get("auc") or 0.0),
            "coefficients": dict(fit.get("coef") or {}),
        },
        {
            "kind": "author_revert_prior",
            "value": scored["author_revert_prior"],
            "author": scored["author"],
            "merged": scored["author_merged"],
            "reverted_or_caused": scored["author_reverted"],
            "window_months": int(fit.get("window_months") or 0),
            "smoothing_m": float(fit.get("smoothing_m") or 0.0),
        },
    ]
    return section(
        SERVED,
        verdict="scored_stale" if stale else "scored",
        epistemic="prior",
        causes=causes,
        rows=rows,
        note=RISK_NOTE,
    )


# ---------------------------------------------------------------------------
# Store + serve
# ---------------------------------------------------------------------------


def brief_row(org: str, ns: str, payload: dict[str, Any]) -> dict[str, Any]:
    from server.code.store.index import build_row

    sections = payload.get("sections") or {}
    return build_row(
        "code_review_briefs",
        org=org,
        namespace=ns,
        pr_number=int(payload["pr_number"]),
        merge_sha=str(payload.get("merge_sha") or ""),
        payload=canonical(payload),
        sections=[
            n for n in SECTION_ORDER if (sections.get(n) or {}).get("status") in (SERVED, EMPTY)
        ],
        indexed_commit=str(payload.get("indexed_commit") or ""),
        tokens=sum(int((sections.get(n) or {}).get("tokens") or 0) for n in SECTION_ORDER),
        build_ms=int(payload.get("build_ms") or 0),
    )


async def build_for_merge(
    repo: Any,
    org: str,
    ns: str,
    *,
    git_dir: str,
    pr_number: int,
    title: str,
    merge_sha: str,
    parent_sha: str = "",
    changed_paths: list[str] | None = None,
    path_prefixes: tuple[str, ...] = (),
    call: CallOp,
    store: bool = True,
    provenance: dict[str, Any] | None = None,
    author: str = "",
) -> dict[str, Any]:
    """Build the brief of one merged PR from the mirror's diff and store it. Used by the
    ingest step after the pack and by the op on a miss. `provenance` rides the payload
    when given — a brief built without a ledger row says so; a ledger-backed payload
    carries no such key. The author comes from the
    caller or the ledger's row, when the PR backfill has written it (one point read);
    blank otherwise."""
    from server.code.app.index import gitrepo

    if not author:
        author = await _ledger_author(repo, org, ns, pr_number)

    parent = parent_sha
    if not parent:
        try:
            parent = (await gitrepo.run(git_dir, "rev-parse", f"{merge_sha}^1")).strip()
        except Exception as e:  # noqa: BLE001 — blame section degrades without it
            logger.info("review_brief: parent of %s unknown: %s", merge_sha[:12], e)
            parent = ""
    diff = ""
    if parent:
        try:
            diff = await gitrepo.run(git_dir, "diff", "--no-renames", parent, merge_sha)
        except Exception as e:  # noqa: BLE001 — the brief still has the file list
            logger.info("review_brief: diff of %s failed: %s", merge_sha[:12], e)
    head = ""
    try:
        head = str(await repo.get_state(org, ns, "git_head") or "")
    except Exception:  # noqa: BLE001 — an unread head is blank; the brief still serves
        head = ""
    inp = BriefInputs(
        org=org,
        ns=ns,
        pr_number=int(pr_number),
        merge_sha=merge_sha,
        title=title,
        changed_paths=list(changed_paths or []),
        diff=diff,
        parent_sha=parent,
        indexed_commit=head,
        path_prefixes=path_prefixes,
        author=author,
    )
    payload = await build_brief(inp, call=call, repo=repo)
    if provenance:
        payload["provenance"] = dict(provenance)
    # A PR that changed files but whose diff could not be read (no mirror here
    # yet, or a failed git call) gets a brief without its
    # blame and diff sections: serve it, but leave the store to a process that
    # reads the diff, or every later read would serve the degraded copy.
    if store and changed_paths and not diff:
        logger.info("review_brief: PR %s served unstored, its diff was not read", pr_number)
    elif store:
        try:
            await repo.insert_rows("code_review_briefs", [brief_row(org, ns, payload)])
        except Exception as e:  # noqa: BLE001 — served now, stored on a later call
            logger.warning(
                "review_brief: store of PR %s failed (served unstored): %s", pr_number, e
            )
            payload["stored"] = False
    return payload


async def _ledger_author(repo: Any, org: str, ns: str, pr_number: int) -> str:
    """The ledger row's author, for a caller that has none; the row is one point read
    and may not exist yet."""
    try:
        facts = await repo.pr_merge_facts(org, ns, pr_number=int(pr_number))
    except Exception as e:  # noqa: BLE001 — the prior falls back to the base rate
        logger.info("review_brief: ledger facts read failed for PR %s: %s", pr_number, e)
        facts = None
    return str((facts or {}).get("author") or "")


def overlay_call(pr_number: int, call: CallOp) -> CallOp:
    """The op seam with the PR's number on the impact read: the post-image overlay seeds
    the functions the PR adds; every other section reads the index as it stands."""

    async def _call(op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "code.impact":
            body = {**body, "pr_number": int(pr_number)}
        return await call(op, body)

    return _call


async def build_for_open(
    repo: Any,
    org: str,
    ns: str,
    *,
    git_dir: str,
    pr_number: int,
    title: str,
    head_sha: str,
    base_sha: str,
    changed_paths: list[str] | None = None,
    path_prefixes: tuple[str, ...] = (),
    call: CallOp,
    store: bool = True,
    author: str = "",
    diff: str | None = None,
) -> dict[str, Any]:
    """The brief of an OPEN PR: its head on the mirror (`refs/pull/N/head`) diffed
    against its merge-base with the indexed branch, through the same builder with the
    overlay on the impact read. Stored under the head as the row's `merge_sha` key, so a
    pushed head is a miss and a rebuild; the payload says `overlay: true`, `head_sha`,
    `base_sha`, and its `merge_sha` is ''. A `diff` the caller sent replaces the
    mirror's, and git is not read."""
    from server.code.app.index import gitrepo
    from server.code.app.index import pr_overlay as po

    if diff is None:
        diff = ""
        try:
            diff = await gitrepo.run(git_dir, "diff", "--no-renames", base_sha, head_sha)
        except Exception as e:  # noqa: BLE001 — the brief still has the file list
            logger.info("review_brief: open diff of PR %s failed: %s", pr_number, e)
    head = ""
    try:
        head = str(await repo.get_state(org, ns, "git_head") or "")
    except Exception:  # noqa: BLE001 — an unread head is blank; the brief still serves
        head = ""
    inp = BriefInputs(
        org=org,
        ns=ns,
        pr_number=int(pr_number),
        merge_sha="",
        title=title,
        changed_paths=list(changed_paths or []),
        diff=diff,
        parent_sha=base_sha,
        indexed_commit=head,
        path_prefixes=path_prefixes,
        author=author,
    )
    payload = await build_brief(inp, call=overlay_call(pr_number, call), repo=repo)
    payload["overlay"] = True
    payload["head_sha"] = head_sha
    payload["base_sha"] = base_sha
    if store and po.NOT_REACHED in str(payload.get("overlay_note") or ""):
        # served, never stored: every later read would serve the overlay a later
        # request completes
        logger.info("review_brief: open PR %s served unstored, its overlay is partial", pr_number)
        payload["stored"] = False
    elif store:
        try:
            await repo.insert_rows(
                "code_review_briefs", [brief_row(org, ns, {**payload, "merge_sha": head_sha})]
            )
        except Exception as e:  # noqa: BLE001 — served now, stored on a later call
            logger.warning(
                "review_brief: store of open PR %s failed (served unstored): %s", pr_number, e
            )
            payload["stored"] = False
    return payload
