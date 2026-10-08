"""code.history: the PRs and reviews that touched a symbol's definition."""

from __future__ import annotations

import time
from typing import Any

from server.code.api.models_history import CodeHistoryInput, CodeHistoryOutput
from server.code.app.common import (
    new_trace_run_id,
    repo_relative,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.resolve import _resolve_symbols
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError


async def _history_reviews(
    repo: CodeRepo,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    paths: list[str],
    per_pr: int,
) -> dict[int, tuple[list[dict[str, Any]], int]] | None:
    """The review comments of every returned PR on the queried file(s), one read; {} when
    not asked, None when it failed."""
    from server.code.app.index.pr_reviews import reviews_for

    if not rows or per_pr <= 0:
        return {}
    with code_span("history.reviews", seeds=len(rows)):
        return await reviews_for(
            repo, org, ns, [int(r["pr_number"]) for r in rows], paths, per_pr=per_pr
        )


def _review_fields(
    reviews: dict[int, tuple[list[dict[str, Any]], int]] | None, pr_number: int
) -> dict[str, Any]:
    """`reviews` + `review_count` for one PR row, or nothing when reviews
    were not asked for or their read failed."""
    if not reviews or pr_number not in reviews:
        return {}
    got, count = reviews[pr_number]
    return {"reviews": got, "review_count": count}


async def handle_code_history(input: CodeHistoryInput) -> CodeHistoryOutput:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    # A number is answered from the same reads as code.search's history section: item
    # facts plus the PR that is / the PRs that close it.

    if input.numbers:
        from server.code.app.history import history_for_numbers

        with code_span("history.numbers", seeds=len(input.numbers)) as sp:
            section = await history_for_numbers(
                repo,
                org,
                ns,
                list(input.numbers),
                max_prs=input.limit,
            )
            span_attrs(sp, results=len(section.get("prs") or []))
        prs = list(section.get("prs") or [])
        pr_paths = sorted({str(f) for r in prs for f in (r.get("files") or [])})
        trace_run_id = new_trace_run_id()
        schedule_code_trace(
            org=org,
            ns=ns,
            op="code.history",
            query=" ".join(f"#{n}" for n in input.numbers),
            route="history_number",
            returned_ids=[f"pr:{int(r['pr_number'])}" for r in prs],
            returned_paths=pr_paths,
            returned_scores=[],
            top_k=input.limit,
            tokens_returned=0,
            abstain=not prs,
            retrieval_risk="low" if prs else "abstain",
            embedding_model="",
            started_monotonic=started,
            trace_run_id=trace_run_id,
            consumer=input.consumer,
            agent=input.agent,
            user_id=input.user_id,
        )
        set_code_span_attributes(
            route="history_number",
            results=len(prs),
            seeds=len(input.numbers),
            abstain=not prs,
            retrieval_risk="low" if prs else "abstain",
            consumer=input.consumer,
        )
        return CodeHistoryOutput(
            prs=prs,
            paths=pr_paths,
            items=list(section.get("items") or []),
            trace_run_id=trace_run_id,
        )

    paths: list[str] = []
    if input.path:
        paths = [repo_relative(input.path)]
    else:
        with code_span("history.resolve") as sp:
            seeds = await _resolve_symbols(
                repo, org, ns, symbol_id=input.symbol_id, name=input.name
            )
            span_attrs(sp, results=len(seeds))
        paths = sorted({str(r["path"]) for r in seeds})  # the wire order is stable
    if not paths:
        raise NotFoundError("no symbol or path resolved for history lookup")

    with code_span("history.prs", seeds=len(paths)) as sp:
        rows = await repo.pr_history_for(org, ns, paths=paths, limit=input.limit)
        span_attrs(sp, results=len(rows))
    reviews = await _history_reviews(repo, org, ns, rows, paths, input.reviews_per_pr)
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.history",
        query=input.path or input.name or input.symbol_id,
        route="history_prs",
        returned_ids=[f"pr:{int(r['pr_number'])}" for r in rows],
        returned_paths=paths,
        returned_scores=[],
        top_k=input.limit,
        tokens_returned=0,
        abstain=not rows,
        retrieval_risk="low" if rows else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route="history_prs",
        results=len(rows),
        seeds=len(paths),
        abstain=not rows,
        retrieval_risk="low" if rows else "abstain",
        consumer=input.consumer,
    )
    return CodeHistoryOutput(
        prs=[
            {
                "pr_number": int(r["pr_number"]),
                "title": str(r["title"]),
                "author": str(r["author"]),
                "merged_at": str(r["merged_at"]),
                "labels": [str(x) for x in (r.get("labels") or [])],
                "body_excerpt": str(r.get("body_excerpt") or "")[:500],
                **_review_fields(reviews, int(r["pr_number"])),
            }
            for r in rows
        ],
        paths=paths,
        trace_run_id=trace_run_id,
    )
