"""code.symbol_history: the regression history of one function, assembled at read time from
the PR record and the tracker record.

Resolve the symbol (name, `path:line`, or symbol_id) to its definition; read the merged PRs
that touched its file (with linked issues, reverts and inline review comments inside the
span); when the tracker namespace bound to this code namespace admits the current principal,
ask it which of those PRs' diffs actually name the function, their test presence, CI result
and first release, the issues traced back to them, and the open issues that name the function
now. Without a tracker, the answer is the file-level floor and says so.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

from server.code.api.models_history import CodeSymbolHistoryOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.domain.index import symbol_history as sh
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.tracker.domain import bisect as bi

logger = logging.getLogger(__name__)

_PR_WINDOW = 120
_COMMENT_WINDOW = 120


async def _tracker_side(
    org: str, ns: str, pr_numbers: list[int], frames: list[str], path: str = ""
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, bool]:
    """(pr facts, edges, open issues naming the frames, qa namespace, ok).
    Unbound → ({}, [], [], "", False); unavailable → the same with the
    namespace named, so the caller can say `tracker_unavailable`."""
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.app.source import tracker_source
    from server.tracker.domain.errors import TrackerUnavailable

    tracker_ns = await authorized_tracker_namespace(org, ns)
    if not tracker_ns:
        return {}, [], [], "", False
    source = tracker_source(org, tracker_ns)
    try:
        facts, issues = await asyncio.gather(
            source.prs_facts(org, tracker_ns, pr_numbers, names=frames),
            source.open_issues_naming(org, tracker_ns, frames, limit=5) if frames else _empty(),
        )
        refs = [f"{f['repo']}#{n}" for n, f in facts.items() if f.get("repo")]
        edges = (
            await source.edges_from(org, tracker_ns, refs, ["fixes", "caused_by"]) if refs else []
        )
        # Production incidents whose crashing frame sits in the symbol's file: the restricted
        # namespace's contribution.
        cfg = getattr(source, "cfg", None)
        if path and cfg is not None and getattr(cfg, "restricted", False):
            incidents = await source.incidents_in_area(
                org, tracker_ns, path_prefix=path, days=365, limit=5
            )
            issues = [*incidents, *issues]
    except TrackerUnavailable as e:
        logger.info("symbol_history: qa unavailable on %s (%s)", tracker_ns, e)
        return {}, [], [], tracker_ns, False
    return facts, edges, issues, tracker_ns, True


async def _empty() -> list[Any]:
    return []


def _ambiguous_answer(seeds: list[dict[str, Any]]) -> Any:
    """The code.callers answer for a name that names several definitions: no history is
    read (picking one would attach another definition's PRs), `candidates` say which.
    Several rows of ONE definition (tiers, declaration + definition, a registration's
    methods) are not an ambiguity. None = proceed."""
    from server.code.app.index.symbol_forms import resolved_by_of

    distinct = list(dict.fromkeys(str(r.get("qualified_name") or "") for r in seeds))
    if len(distinct) <= 1 or resolved_by_of(seeds) == "registration":
        return None
    return CodeSymbolHistoryOutput(
        verdict=sh.UNKNOWN,
        causes=["symbol_ambiguous"],
        ambiguous=True,
        candidates=distinct[:20],
        summary={
            "note": f"the name resolves to {len(distinct)} distinct definitions; qualify it "
            "(Class::method) or pass symbol_id"
        },
    )


async def handle_code_symbol_history(input: Any) -> Any:
    from server.code.app.symbol import _resolve_symbols

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if not (input.name.strip() or input.symbol_id.strip()):
        # Checked after the gate, so a refused principal never learns the input shape.
        raise ValidationError("name or symbol_id is required")
    repo = CodeRepo()
    with code_span("symbol_history.resolve"):
        seeds = await _resolve_symbols(repo, org, ns, symbol_id=input.symbol_id, name=input.name)
    ambiguous = _ambiguous_answer(seeds)
    if ambiguous is not None:
        return ambiguous
    seed = seeds[0] if seeds else None
    if seed is None:
        return CodeSymbolHistoryOutput(
            verdict=sh.UNKNOWN,
            causes=["symbol_unresolved"],
            symbol={},
            prs=[],
            summary={"note": "the symbol did not resolve in this index."},
        )
    path = str(seed.get("path") or "")
    qname = str(seed.get("qualified_name") or "")
    span = (int(seed.get("start_line") or 0), int(seed.get("end_line") or 0))
    frames = bi.frame_names([qname]) if qname else []
    with code_span("symbol_history.loom", seeds=1):
        prs, comments = await asyncio.gather(
            repo.pr_history_for(org, ns, paths=[path], limit=_PR_WINDOW),
            repo.review_comments_for(org, ns, [path], limit=_COMMENT_WINDOW, humans_only=True),
        )
    pr_numbers = [int(r.get("pr_number") or 0) for r in prs]
    linked = sorted({int(x) for r in prs for x in (r.get("linked_issues") or []) if int(x) > 0})
    with code_span("symbol_history.facts", seeds=len(pr_numbers)):
        gathered = await asyncio.gather(
            repo.items_by_number(org, ns, linked[:64]) if linked else _empty_dict(),
            _tracker_side(org, ns, pr_numbers, frames, path),
            return_exceptions=True,
        )
    issue_facts = (
        cast(dict[int, dict[str, Any]], gathered[0])
        if not isinstance(gathered[0], BaseException)
        else {}
    )
    if isinstance(gathered[1], BaseException):
        logger.info("symbol_history: qa side failed (open): %s", gathered[1])
        tracker_facts, tracker_edges, open_issues, tracker_ns, qa_ok = {}, [], [], "", False
    else:
        tracker_facts, tracker_edges, open_issues, tracker_ns, qa_ok = gathered[1]
    rows = sh.build_rows(
        prs,
        issue_facts={k: _issue_view(v) for k, v in issue_facts.items()},
        comments=comments,
        span=span,
        tracker_facts=tracker_facts,
        tracker_edges=tracker_edges,
        frames=frames,
        qa_ok=qa_ok,
    )
    summary = sh.summarize(rows, window_prs=_PR_WINDOW, open_issues=len(open_issues), qa_ok=qa_ok)
    verdict, causes = sh.verdict_of(rows, resolved=True)
    if tracker_ns and not qa_ok:
        causes = [*causes, "tracker_unavailable"]
    span_comments = [
        {
            "pr_number": int(c.get("pr_number") or 0),
            "line": int(c.get("line") or 0),
            "author": str(c.get("author") or ""),
            "excerpt": str(c.get("body") or "")[:160],
            "at": sh._iso(c.get("comment_created_at")),
        }
        for c in comments
        if sh.in_span(int(c.get("line") or 0), span[0], span[1])
    ][:5]
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.symbol_history",
        query=qname or input.name or input.symbol_id,
        route="symbol_history",
        returned_ids=[f"pr:{r.pr_number}" for r in rows[: input.limit]],
        returned_paths=[path],
        returned_scores=[],
        top_k=input.limit,
        tokens_returned=0,
        abstain=verdict == sh.UNKNOWN,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(route="symbol_history", results=len(rows), consumer=input.consumer)
    return CodeSymbolHistoryOutput(
        verdict=verdict,
        causes=causes,
        epistemic="lower_bound",
        symbol={
            "qualified_name": qname,
            "path": path,
            "start_line": span[0],
            "end_line": span[1],
            "tier": str(seed.get("tier") or ""),
        },
        prs=[r.as_dict() for r in rows[: input.limit]],
        review_comments=span_comments,
        open_issues_naming=[_open_issue_view(r) for r in open_issues[:5]],
        summary=summary,
        tracker_namespace=tracker_ns,
        tracker_unavailable=bool(tracker_ns) and not qa_ok,
        trace_run_id=trace_run_id,
    )


async def _empty_dict() -> dict[int, dict[str, Any]]:
    return {}


def _issue_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"title": str(row.get("title") or "")[:120], "state": str(row.get("state") or "")}


def _open_issue_view(r: dict[str, Any]) -> dict[str, Any]:
    if r.get("source") == "core_incident":
        return {
            "source": "core_incident",
            "source_id": str(r.get("source_id") or ""),
            "title": str(r.get("title") or "")[:120],
            "severity": str(r.get("severity") or ""),
            "error_type": str(r.get("error_type") or ""),
            "top_function": str(r.get("top_function") or ""),
            "url": str(r.get("url") or ""),
        }
    return {
        "number": int(r.get("number") or 0),
        "title": str(r.get("title") or "")[:120],
        "severity": str(r.get("severity_level") or ""),
        "component": str(r.get("primary_component") or ""),
        "bisect_outcome": str(r.get("bisect_outcome") or ""),
        "url": str(r.get("url") or ""),
    }
