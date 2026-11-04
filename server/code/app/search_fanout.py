"""code.search over several namespaces.

A request naming `namespaces` runs the single-namespace search once per namespace,
concurrently, and interleaves the per-namespace rankings by RANK, round-robin in the
order given. Scores are never merged: the lexical leg's idf is computed per namespace,
so a 0.71 in one index is not a 0.71 in another — the rank is the one
thing the namespaces share. Two caps bound the request: the single-namespace token
budget (the interleaved list is budgeted again, so a fan-out never serves more than one
namespace's call would — the byte cap) and a wall deadline (a namespace that has not
answered is dropped and disclosed, never waited for — the latency cap). Every namespace
is authorized before anything runs, because the transport bound only `namespace`: one
unreadable namespace refuses the whole request; nothing is silently dropped.

The fan-out itself is not memoized and writes no trace row of its own: each
per-namespace call goes through the ordinary handler and keeps its memo, its trace row
and its calibration. There is no continuation under fan-out — a cursor pages ONE ranked
pool.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.units import CodeUnit

SingleSearch = Callable[["CodeSearchInput"], Awaitable["CodeSearchOutput"]]

# The wire model's own cap on `namespaces`; the setting may only lower it.
MAX_NAMESPACES = 8
INTERLEAVE = "rank"
_RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "abstain": 3}

# (output, wall ms) per namespace that answered inside the deadline
Answered = dict[str, tuple["CodeSearchOutput", int]]


def normalize(inp: CodeSearchInput) -> CodeSearchInput:
    """A one-entry list is the plain single-namespace search on that entry."""
    if len(inp.namespaces) != 1:
        return inp
    return inp.model_copy(update={"namespace": inp.namespaces[0], "namespaces": []})


async def fan_out(inp: CodeSearchInput, single: SingleSearch) -> CodeSearchOutput:
    """The whole fan-out: authorize, validate, run, interleave, budget, disclose."""
    from server.code.app.common import resolve_code_identity
    from server.code.app.spans import set_code_span_attributes

    namespaces = list(inp.namespaces)
    org, _ = resolve_code_identity(inp.org, namespaces[0])
    await authorize(org, namespaces)
    validate(inp, namespaces)
    timeout_ms = int(settings.code_search_fanout_timeout_ms)
    answered, dropped = await run_all(inp, namespaces, single, timeout_ms=timeout_ms)
    # The per-namespace searches noted their shape in their own tasks; the fan-out is
    # priced as the hybrid it runs
    from server.observability.budget_shape import note_budget_shape

    note_budget_shape("hybrid")
    served, tokens = budget(interleave(answered, namespaces, inp.top_k), inp)
    out = assemble(
        inp,
        namespaces,
        answered,
        dropped,
        served,
        tokens,
        timeout_ms=timeout_ms,
        output_model=CodeSearchOutput,
    )
    set_code_span_attributes(
        fanout=len(namespaces),
        fanout_dropped=len(dropped),
        route=out.route,
        results=len(out.results),
        tokens_returned=out.tokens_returned,
        retrieval_risk=out.retrieval_risk,
        abstain=out.abstain,
        consumer=inp.consumer,
    )
    return out


async def authorize(org: str, namespaces: list[str], *, op: str = "code.search") -> None:
    """Fail closed: every namespace must be readable by the principal, else the request
    is refused (403) — never trimmed to the readable subset. Shared with the presence
    mode of code.symbol / code.grep."""
    from server.auth.authz import row_in_scope
    from server.core.errors import AuthInsufficientError
    from server.operations.base import Role

    verdicts = await asyncio.gather(*(row_in_scope(org, ns, Role.VIEWER) for ns in namespaces))
    refused = [ns for ns, ok in zip(namespaces, verdicts, strict=True) if not ok]
    if refused:
        raise AuthInsufficientError(
            f"{op}: not authorized to read namespace(s) {refused} in org {org!r}"
        )


async def readable_subset(
    org: str, namespaces: list[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """The namespaces the principal may read, and the rest as `{namespace, reason:
    "not_readable"}`. Only for namespaces LOOM added on its own (a release-branch fan-out,
    a trunk or sibling namespace); a list the caller named goes through `authorize`, which
    fails closed."""
    from server.auth.authz import row_in_scope
    from server.operations.base import Role

    verdicts = await asyncio.gather(
        *(row_in_scope(org, ns, Role.VIEWER, hides_answer=False) for ns in namespaces)
    )
    readable = [ns for ns, ok in zip(namespaces, verdicts, strict=True) if ok]
    skipped = [
        {"namespace": ns, "reason": "not_readable"}
        for ns, ok in zip(namespaces, verdicts, strict=True)
        if not ok
    ]
    return readable, skipped


def validate(inp: CodeSearchInput, namespaces: list[str]) -> None:
    from server.core.errors import ValidationError

    cap = max(1, min(MAX_NAMESPACES, int(settings.code_search_fanout_max_namespaces)))
    if len(namespaces) > cap:
        raise ValidationError(
            f"code.search: {len(namespaces)} namespaces exceed this deployment's fan-out "
            f"cap of {cap}"
        )
    if inp.cursor:
        raise ValidationError(
            "code.search: cursor cannot be combined with namespaces — a continuation "
            "is a point read of ONE ranked pool; page one namespace "
            "at a time"
        )


async def _timed(ns: str, call: Awaitable[CodeSearchOutput]) -> tuple[str, CodeSearchOutput, int]:
    t0 = time.monotonic()
    out = await call
    return ns, out, int((time.monotonic() - t0) * 1000)


async def run_all(
    inp: CodeSearchInput, namespaces: list[str], single: SingleSearch, *, timeout_ms: int
) -> tuple[Answered, list[str]]:
    """Run the single-namespace search per namespace concurrently under one
    deadline. A namespace still running at the deadline is cancelled and
    listed as dropped; a namespace that failed raises its own error."""
    tasks: dict[str, asyncio.Task[tuple[str, CodeSearchOutput, int]]] = {}
    for ns in namespaces:
        sub = inp.model_copy(update={"namespace": ns, "namespaces": []})
        tasks[ns] = asyncio.ensure_future(_timed(ns, single(sub)))
    try:
        done, pending = await asyncio.wait(tasks.values(), timeout=max(0.001, timeout_ms / 1000.0))
    except BaseException:
        # the caller was cancelled: `wait` leaves its tasks running
        for t in tasks.values():
            t.cancel()
        raise
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    answered: Answered = {}
    for t in done:
        ns, out, ms = t.result()
        answered[ns] = (out, ms)
    dropped = [ns for ns, t in tasks.items() if t in pending]
    return answered, dropped


def interleave(answered: Answered, namespaces: list[str], top_k: int) -> list[CodeUnit]:
    """Round-robin by rank in the given order over the namespaces that
    returned results; each unit is tagged with the namespace that served it."""
    queues: list[tuple[str, deque[CodeUnit]]] = [
        (ns, deque(answered[ns][0].results))
        for ns in namespaces
        if ns in answered and not answered[ns][0].abstain and answered[ns][0].results
    ]
    out: list[CodeUnit] = []
    while queues and len(out) < top_k:
        for ns, q in list(queues):
            if len(out) >= top_k:
                break
            out.append(q.popleft().model_copy(update={"namespace": ns}))
            if not q:
                queues.remove((ns, q))
    return out


def budget(units: list[CodeUnit], inp: CodeSearchInput) -> tuple[list[CodeUnit], int]:
    """The request's token budget, resolved the way the single path resolves
    it, applied to the interleaved list: trailing bodies elide first."""
    from server.code.app.search.shared import page_plan
    from server.code.domain.units import budget_units, estimate_unit_tokens

    plan = page_plan(inp)
    served, _ = budget_units(units, plan.token_budget, bodies=plan.bodies)
    return served, sum(estimate_unit_tokens(u) for u in served)


def _merge_work(
    answered: Answered, namespaces: list[str], dropped: list[str]
) -> dict[str, int | str]:
    work: dict[str, int | str] = {}
    for out, _ms in answered.values():
        for k, v in out.retrieval_work.items():
            # the per-namespace searches run concurrently: a stage's wall is the slowest
            # namespace's, the counters add up
            if isinstance(v, str):
                # a label (risk_source): kept when every namespace agrees, "mixed"
                # otherwise
                work[k] = v if work.get(k, v) == v else "mixed"
            elif k.startswith("stage_ms_"):
                work[k] = max(int(work.get(k, 0)), int(v))
            else:
                work[k] = int(work.get(k, 0)) + int(v)
    work["fanout_namespaces"] = len(namespaces)
    work["fanout_dropped"] = len(dropped)
    return work


def _disclosure(
    answered: Answered, namespaces: list[str], dropped: list[str], *, timeout_ms: int
) -> dict[str, Any]:
    per: dict[str, Any] = {}
    for ns in namespaces:
        if ns not in answered:
            continue
        out, ms = answered[ns]
        per[ns] = {
            "ms": ms,
            "results": len(out.results),
            "abstain": out.abstain,
            "route": out.route,
            "commit": str(out.as_of.get("commit") or ""),
            "trace_run_id": out.trace_run_id,
        }
    return {
        "namespaces": list(namespaces),
        "interleave": INTERLEAVE,
        "timeout_ms": timeout_ms,
        "per_namespace": per,
        "dropped": list(dropped),
    }


def assemble(
    inp: CodeSearchInput,
    namespaces: list[str],
    answered: Answered,
    dropped: list[str],
    served: list[CodeUnit],
    tokens: int,
    *,
    timeout_ms: int,
    output_model: type[CodeSearchOutput],
) -> CodeSearchOutput:
    """One response from the per-namespace ones: the first namespace that
    answered leads (route, as_of, calibration text, trace id); the risk is
    the best among those that returned; abstain only when none did."""
    first = next((answered[ns][0] for ns in namespaces if ns in answered), None)
    returned = [ns for ns in namespaces if ns in answered and not answered[ns][0].abstain]
    lead = answered[returned[0]][0] if returned else first
    # the best (lowest) risk among the namespaces that returned
    risk: Literal["low", "medium", "high", "abstain"] = "abstain"
    for ns in returned:
        if _RISK_ORDER.get(answered[ns][0].retrieval_risk, 3) < _RISK_ORDER[risk]:
            risk = answered[ns][0].retrieval_risk
    n = len(namespaces)
    fanout_note = (
        f"fanout={n} namespaces, ranks interleaved round-robin ({INTERLEAVE}; scores are "
        f"not comparable across namespaces), no continuation under fan-out"
    )
    abstain_reason = (
        ""
        if returned
        else (
            lead.abstain_reason
            if lead is not None
            else f"every namespace missed the {timeout_ms} ms fan-out deadline"
        )
    )
    return output_model(
        results=served,
        next_cursor="",
        has_more=False,
        route=(lead.route if lead is not None else "hybrid") + "+fanout",
        abstain=not returned,
        as_of=dict(lead.as_of) if lead is not None else {},
        abstain_code=(lead.abstain_code if lead is not None and not returned else ""),
        abstain_reason=abstain_reason,
        retrieval_risk=risk,
        retrieval_confidence=lead.retrieval_confidence if lead is not None else 0.0,
        route_agreement=lead.route_agreement if lead is not None else 0.0,
        calibration_explanation=(
            (lead.calibration_explanation + "; " if lead is not None else "")
            + f"fanout: risk is the best of {returned or 'none'}, dropped={dropped}"
        ),
        tokens_returned=tokens,
        score_explanation=(
            ((lead.score_explanation + ", ") if lead is not None else "") + fanout_note
        ),
        retrieval_work=_merge_work(answered, namespaces, dropped),
        trace_run_id=lead.trace_run_id if lead is not None else "",
        top_similarity=max((answered[ns][0].top_similarity for ns in returned), default=0.0),
        constrained=lead.constrained if lead is not None else False,
        matched_files=lead.matched_files if lead is not None else 0,
        fanout=_disclosure(answered, namespaces, dropped, timeout_ms=timeout_ms),
        # A namespace still embedding says so, by name
        note="; ".join(
            f"{ns}: {answered[ns][0].note}"
            for ns in namespaces
            if ns in answered and answered[ns][0].note
        ),
    )
