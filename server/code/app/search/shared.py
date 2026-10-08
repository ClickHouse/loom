"""What the code.search, code.grep, code.files and code.ask handlers share: the scope a
call reads, its partial-answer fields, the not-covered answer and the settling read pool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from server.code.domain import deadline as code_deadline


def _scope_of(inp: Any) -> Any:
    """SearchScope from a request's constrained-search fields. code.grep passes its own
    `pattern` to the repo, so only the path filters are taken from it here."""
    from server.code.domain.search_scope import build_scope

    is_grep = hasattr(inp, "mode")
    return build_scope(
        pattern="" if is_grep else str(getattr(inp, "pattern", "") or ""),
        regex=bool(getattr(inp, "regex", False)) and not is_grep,
        case_sensitive=bool(getattr(inp, "case_sensitive", False)) and not is_grep,
        path_globs=list(getattr(inp, "path_globs", []) or []),
        exclude_globs=list(getattr(inp, "exclude_globs", []) or []),
        langs=list(getattr(inp, "langs", []) or []),
    )


@dataclass(frozen=True, slots=True)
class PagePlan:
    """How one code.search page is filled: the token budget, the body mode, and
    whether risk annotations run and what they reserve from the budget."""

    token_budget: int
    bodies: str
    annotate: bool
    reserve: int


def page_plan(inp: Any) -> PagePlan:
    """The page plan of a CodeSearchInput. Page 1, every cursor page and the fan-out
    resolve it here, so a continuation page is budgeted exactly as page 1 was. The
    budget default is route-aware: 1200 for a symbol-shaped query (an exact symbol
    lookup rarely needs the hybrid default's headroom, even when it falls through to
    hybrid), 3000 otherwise; an explicit budget is honored unchanged."""
    from server.code.app import risk as code_risk
    from server.code.app.search.shape import classify_query_shape
    from server.settings import settings as _settings

    token_budget = inp.token_budget or (
        1200 if classify_query_shape(inp.query) == "symbol" else 3000
    )
    annotate = bool(inp.annotate and _settings.code_risk_annotations)
    return PagePlan(
        token_budget=token_budget,
        bodies=inp.bodies or _settings.code_search_bodies_default,
        annotate=annotate,
        reserve=code_risk.reserve_for(inp.top_k) if annotate else 0,
    )


# A pool row is an async insert that becomes readable 0.2-0.4 s after it is issued (measured
# on three replicas), and it is issued after the response that carries the cursor. A caller
# that pages straight back almost always arrives before the row, so it gets up to three
# short waits and re-reads before the re-search fallback: at most ~1 s, still cheaper than
# the fallback, and it serves the exact page.
_SETTLE_WINDOW_S = 3.0
_SETTLE_WAIT_S = 0.35
_SETTLE_POLLS = 3


async def _read_pool_settling(
    org: str, ns: str, key: str, fields: dict[str, Any]
) -> dict[str, Any] | None:

    from server.code.app import search_cursor as paging

    row = await paging.get_pool(org, ns, key)
    polls = 0
    while (
        row is None
        and polls < _SETTLE_POLLS
        and time.time() - int(fields.get("t", 0)) < _SETTLE_WINDOW_S
    ):
        polls += 1
        await asyncio.sleep(_SETTLE_WAIT_S)
        row = await paging.get_pool(org, ns, key)
    return row


def _partial_fields(budget: code_deadline.Budget | None) -> dict[str, Any]:
    """`partial` / `partial_reason` for an output model: set only when the budget cut."""
    if budget is None or not budget.partial:
        return {}
    return {"partial": True, "partial_reason": budget.reason()}


def _not_covered_output[OutT: BaseModel](
    org: str, ns: str, path_prefix: str, output_model: type[OutT]
) -> OutT | None:
    """None when the prefix is inside the index; else an abstaining output that
    names the indexed prefixes so the caller can act on it (only fields the
    given output model has are set — search and grep differ)."""
    from server.code.app.common import path_coverage
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    if cfg is None or not path_prefix:
        return None
    covered, prefixes = path_coverage(cfg, path_prefix)
    if covered:
        return None
    reason = (
        f"path {path_prefix!r} is outside the indexed prefixes {list(prefixes)} — "
        "read the file directly; this index has no rows for it"
    )
    fields = set(output_model.model_fields)
    kw: dict[str, Any] = {
        k: v
        for k, v in {
            "results": [],
            "hits": [],
            "route": "not_covered",
            "abstain": True,
            "abstain_code": "not_covered",
            "abstain_reason": reason,
            "retrieval_risk": "abstain",
            "not_covered": True,
            "note": reason,
            "chunks_matched": 0,
        }.items()
        if k in fields
    }
    for name, f in output_model.model_fields.items():
        if name not in kw and f.is_required():
            ann = f.annotation
            kw[name] = [] if getattr(ann, "__origin__", None) is list else (0 if ann is int else "")
    return output_model(**kw)
