"""code.pr_check handler: a PR draft asks whether a PR like it already exists (open, merged,
or tried and closed) and gets a verdict with the evidence per candidate. Four candidate legs
over loom's own PR ledgers, one scoring rule (code/domain/pr_check.py), no GitHub call."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

from server.code.api.models_review import CodePrCheckOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.domain import pr_check as pc
from server.code.domain.extract.tokenize import code_tokens
from server.code.store.index import CodeRepo
from server.core.datetimes import naive_utc_now
from server.core.errors import ValidationError
from server.settings import settings

_GH_POOL = 400
_TEXT_POOL = 40
_PATHS_POOL = 60
_ISSUE_POOL = 16


def thresholds() -> dict[str, float]:
    return {
        "duplicate": float(settings.code_pr_check_duplicate_threshold),
        "related": float(settings.code_pr_check_related_threshold),
    }


def _tokens_of(row: dict[str, Any]) -> set[str]:
    lex = str(row.get("lexemes") or "")
    if lex:
        return set(lex.split())
    return set(code_tokens(str(row.get("title") or "")))


def _cand_from_item(row: dict[str, Any], state: str, leg: str) -> pc.Candidate:
    return pc.Candidate(
        number=int(row.get("number") or 0),
        state=state,
        title=str(row.get("title") or ""),
        author=str(row.get("author") or ""),
        files=[str(f) for f in (row.get("files") or [])],
        tokens=_tokens_of(row),
        opened_at=row.get("opened_at"),
        closed_at=row.get("closed_at"),
        labels=[str(x) for x in (row.get("labels") or [])],
        has_body=False,
        legs={leg},
    )


def _cand_from_merged(row: dict[str, Any], legs: set[str]) -> pc.Candidate:
    return pc.Candidate(
        number=int(row.get("pr_number") or 0),
        state="merged",
        title=str(row.get("title") or ""),
        author=str(row.get("author") or ""),
        files=[str(f) for f in (row.get("files") or [])],
        tokens=_tokens_of(row),
        opened_at=row.get("opened_at"),
        merged_at=row.get("merged_at"),
        linked_issues=[int(x) for x in (row.get("linked_issues") or []) if int(x) > 0],
        labels=[str(x) for x in (row.get("labels") or [])],
        has_body=bool(row.get("lexemes")),
        legs=set(legs),
    )


async def _leg(status: dict[str, Any], name: str, coro: Any) -> list[dict[str, Any]]:
    """One candidate leg; a failed read is a stated `unavailable`, never a 500."""
    try:
        rows = await coro
    except Exception as e:  # noqa: BLE001 — a slow replica, a refused read
        status[name] = f"unavailable: {type(e).__name__}"
        return []
    status[name] = len(rows)
    return list(rows)


async def _merged_candidates(
    repo: CodeRepo,
    org: str,
    ns: str,
    draft: pc.Draft,
    status: dict[str, Any],
    *,
    since: datetime,
) -> tuple[dict[int, set[str]], dict[str, float]]:
    """Merged-PR numbers per leg plus the idf table of the draft's title tokens."""
    legs: dict[int, set[str]] = {}
    idf: dict[str, float] = {t: 1.0 for t in draft.title_tokens}
    if draft.paths:
        for r in await _leg(
            status,
            "merged_paths",
            repo.pr_history_for(org, ns, paths=draft.paths, limit=_PATHS_POOL),
        ):
            legs.setdefault(int(r["pr_number"]), set()).add("paths")
    if draft.title_tokens:
        try:
            dfs, n_prs = await repo.pr_corpus_stats(org, ns, draft.title_tokens)
            idf = pc.idf_table(draft.title_tokens, [int(d) for d in dfs], int(n_prs))
            idfs = [idf[t] for t in draft.title_tokens]
            rows = await repo.pr_history_search(org, ns, draft.title_tokens, idfs, pool=_TEXT_POOL)
            status["merged_text"] = len(rows)
            for r in rows:
                legs.setdefault(int(r["pr_number"]), set()).add("text")
        except Exception as e:  # noqa: BLE001 — the leg reports unavailable
            status["merged_text"] = f"unavailable: {type(e).__name__}"
    if draft.issues:
        for r in await _leg(
            status,
            "merged_issues",
            repo.prs_linking_issue(org, ns, draft.issues, limit=_ISSUE_POOL),
        ):
            legs.setdefault(int(r["pr_number"]), set()).add("issue")
    # merged PRs older than the window are history, not a duplicate (the paths
    # leg is newest-first already; the text and issue legs are not)
    return legs, idf


def _item(
    cand: pc.Candidate, f: dict[str, Any], s: float, *, duplicate: float, draft: pc.Draft
) -> dict[str, Any]:
    return {
        "pr_number": cand.number,
        "state": cand.state,
        "title": cand.title,
        "author": cand.author,
        "opened_at": str(cand.opened_at or "") or None,
        "merged_at": str(cand.merged_at or "") or None,
        "closed_at": str(cand.closed_at or "") or None,
        "relation": pc.relation(cand, s, duplicate=duplicate),
        "score": s,
        "evidence": pc.evidence(draft, cand, f),
        "shared_paths": list(f.get("shared_paths") or [])[:20],
        "files_total": len(cand.files),
        "linked_issues": cand.linked_issues[:10],
        "labels": cand.labels[:10],
        "found_by": sorted(cand.legs),
        "backport_of": cand.backport()[0] or None,
        "features": {k: f[k] for k in ("text", "body", "paths", "symbols", "issue")},
    }


async def handle_code_pr_check(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    draft = pc.Draft.build(
        title=input.title,
        body=input.body,
        paths=list(input.paths),
        symbols=list(input.symbols),
        issues=list(input.issues),
        pr_number=int(input.pr_number or 0),
    )
    if draft.empty():
        raise ValidationError("code.pr_check needs a title, changed paths or an issue number")
    repo = CodeRepo()
    status: dict[str, Any] = {}
    th = thresholds()
    since = naive_utc_now() - timedelta(days=int(settings.code_pr_check_merged_days))
    cands: dict[tuple[str, int], pc.Candidate] = {}
    closed_days = int(settings.code_pr_check_closed_days)
    with code_span("pr_check.candidates") as sp:
        # the three ledgers are independent reads: one round trip, not three
        open_rows, closed_rows, (merged_legs, idf) = await asyncio.gather(
            _leg(
                status,
                "open",
                repo.gh_prs_matching(
                    org,
                    ns,
                    state="open",
                    tokens=draft.title_tokens,
                    paths=draft.paths,
                    limit=_GH_POOL,
                ),
            ),
            _leg(
                status,
                "closed",
                repo.gh_prs_matching(
                    org,
                    ns,
                    state="closed",
                    tokens=draft.title_tokens,
                    paths=draft.paths,
                    days=closed_days,
                    limit=_GH_POOL,
                ),
            ),
            _merged_candidates(repo, org, ns, draft, status, since=since),
        )
        for r in open_rows:
            c = _cand_from_item(r, "open", "open")
            cands[("open", c.number)] = c
        for r in closed_rows:
            c = _cand_from_item(r, "closed", "closed")
            cands.setdefault(("closed", c.number), c)
        hydrated = await _leg(
            status, "merged_hydrated", repo.merged_prs_full(org, ns, list(merged_legs))
        )
        for r in hydrated:
            c = _cand_from_merged(r, merged_legs.get(int(r["pr_number"]), set()))
            if (
                c.merged_at is not None
                and isinstance(c.merged_at, datetime)
                and c.merged_at < since
            ):
                c.legs.add("old")
            cands[("merged", c.number)] = c
        span_attrs(sp, results=len(cands))
    if input.pr_number:
        cands = {k: v for k, v in cands.items() if v.number != int(input.pr_number)}
    unavailable = [k for k, v in status.items() if isinstance(v, str)]
    if not cands and len(unavailable) >= 2:
        return CodePrCheckOutput(
            verdict="none",
            epistemic="unavailable",
            thresholds=th,
            legs=status,
            draft=_draft_view(draft),
            trace_run_id=new_trace_run_id(),
        )
    mass = int(settings.code_pr_check_mass_files)
    scored: list[tuple[pc.Candidate, dict[str, Any], float]] = []
    for c in cands.values():
        f = pc.features(draft, c, idf, mass_files=mass)
        s = pc.score(f)
        if "old" in c.legs:
            s = round(min(s, th["duplicate"] - 0.01), 3)  # history, never a duplicate verdict
        if s >= th["related"]:
            scored.append((c, f, s))
    scored.sort(key=lambda x: (-x[2], x[0].state != "open", -x[0].number))
    verdict, undecided = pc.verdict(scored, duplicate=th["duplicate"], related=th["related"])
    top = scored[: int(input.top_k)] if verdict != "none" else []
    items = [_item(c, f, s, duplicate=th["duplicate"], draft=draft) for c, f, s in top]
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.pr_check",
        query=draft.title[:200] or " ".join(draft.paths[:5]),
        route="pr_check",
        returned_ids=[f"pr:{it['pr_number']}" for it in items],
        returned_paths=list(draft.paths[:20]),
        returned_scores=[float(it["score"]) for it in items],
        top_k=int(input.top_k),
        tokens_returned=0,
        abstain=verdict == "none",
        retrieval_risk="low" if verdict == "duplicate_of" else ("medium" if items else "abstain"),
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(
        route="pr_check",
        results=len(items),
        seeds=len(cands),
        abstain=verdict == "none",
        retrieval_risk="low" if items else "abstain",
        consumer=input.consumer,
    )
    return CodePrCheckOutput(
        verdict=verdict,
        items=items,
        undecided=undecided,
        epistemic="heuristic" if not unavailable else "lower_bound",
        thresholds=th,
        legs=status,
        draft=_draft_view(draft),
        trace_run_id=trace_run_id,
    )


def _draft_view(draft: pc.Draft) -> dict[str, Any]:
    return {
        "title_tokens": draft.title_tokens[:24],
        "paths": len(draft.paths),
        "symbols": len(draft.symbols),
        "issues": draft.issues[:10],
    }


__all__ = ["handle_code_pr_check", "thresholds"]
