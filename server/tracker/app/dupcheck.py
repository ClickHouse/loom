"""tracker.duplicate_check: a draft issue asks whether it already exists and gets a verdict
(`duplicate_of` with the items and a calibrated confidence, `related` with the items, or
`none` with nothing) or, when nobody could read the record, `tracker_unavailable`.

The legs are `tracker.similar`'s, run once through the same service path with the draft's
title, body, frames, symbols, component,
version and mentions as the query; a confidence per fused candidate comes
from the namespace's stored logistic fit (`server/tracker/app/dupcheck_fit.py`),
and the verdict is the thresholds' (settings, on the probability scale).
Two things outrank the score: the relation the tracker graph already records
between the filed item (`issue_number`) and a candidate — `related_to` is
never promoted to `duplicate` — and the draft's code paths a candidate's
fix PR touched (`fix_path`, one edge read and one file read per call when
the draft carries paths). Without a fit the op still answers — `related`
with the candidates and a null confidence, `epistemic: uncalibrated` — and
says so.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from server.code.domain.index.repos import registry_unread
from server.core.llm_free import reads_llm_free
from server.llm import customer
from server.settings import settings
from server.tracker.api.models import (
    TrackerDuplicateCheckInput,
    TrackerDuplicateCheckOutput,
    TrackerLegStatus,
)
from server.tracker.app import common
from server.tracker.app import dupcheck_legs as dl
from server.tracker.app import source as tsource
from server.tracker.app.dupcheck_answer import (
    answer,
    judge_uncertain,
    parse_as_of,
    query_block,
    row_number,
    thresholds,
    with_canonical,
    with_closest,
)
from server.tracker.app.dupcheck_candidates import LEGS_ON, candidates, pool_cap
from server.tracker.domain.errors import TrackerUnavailable

logger = logging.getLogger(__name__)


async def handle_tracker_duplicate_check(
    input: TrackerDuplicateCheckInput,
) -> TrackerDuplicateCheckOutput:
    from server.core.errors import ValidationError
    from server.tracker.app.dupcheck_fanout import readable_tracker_namespaces
    from server.tracker.domain.namespaces import tracker_namespace

    input = common._with_default_org(input)
    # A tracker namespace addressed by name is a single read; a code namespace (or
    # `namespaces`) reads every tracker record the caller may.
    requested = list(input.namespaces or [])
    explicit = tracker_namespace(input.org, input.namespace) is not None and not requested
    linked = [] if explicit else await common.linked_trackers(input.org, input.namespace)
    # the code namespace the call named (pr_check reads it)
    code_ns = "" if explicit or tracker_namespace(input.org, input.namespace) else input.namespace
    input = await common._bound_input(input)
    title, body = str(input.title or "").strip(), str(input.body or "").strip()
    if not title and not body and int(input.issue_number or 0) <= 0:
        raise ValidationError("title, body or issue_number is required")
    wall = parse_as_of(input.as_of)
    # The PRs that may already address the draft are searched while the checks run, on the
    # code namespace the call was addressed at.
    wants_prs = bool(input.related_prs) and not explicit
    # Read before anything the answer is built from: one computed before the code
    # registry's first read is narrower, and is not kept.
    cache = not registry_unread()
    pr_task = _start_pr_check(input.org, code_ns, title, body) if wants_prs else None
    deadline = dl.start_deadline()
    try:
        if explicit:
            out = await _checked([input], wall, int(input.top_k))
            return await _with_links(
                out, input.org, input.namespace, pr_task, wall, code_ns, title, cache=cache
            )
        bound = input.namespace if tracker_namespace(input.org, input.namespace) is not None else ""
        cap = int(settings.tracker_dupcheck_fanout_namespaces)
        # A namespace with no tracker of its own (unbound, unlinked) widens to nobody's:
        # another repository's record is not this one's, so its `none` would read as
        # clearance. It answers `tracker_unavailable`, and a caller searches elsewhere.
        anchored = bool(bound or linked or requested)
        targets = (
            await readable_tracker_namespaces(input.org, bound, requested, cap, linked=linked)
            if anchored
            else []
        )
        if len(targets) <= 1:
            one = input.model_copy(update={"namespace": targets[0]}) if targets else input
            out = await _checked([one], wall, int(input.top_k))
            return await _with_links(
                out, input.org, one.namespace, pr_task, wall, code_ns, title, cache=cache
            )
        out = await _checked(
            [input.model_copy(update={"namespace": ns}) for ns in targets], wall, int(input.top_k)
        )
        return await _with_links(
            out, input.org, targets[0], pr_task, wall, code_ns, title, cache=cache
        )
    finally:
        dl.end_deadline(deadline)
        if pr_task is not None and not pr_task.done():
            pr_task.cancel()


def _start_pr_check(org: str, code_ns: str, title: str, body: str) -> Any:
    """code.pr_check over the draft, as a task, or None (the call named no code namespace,
    or no title)."""
    if not code_ns or not title:
        return None

    async def run() -> Any:
        from server.code.api.models_review import CodePrCheckInput
        from server.code.app.pr_check import handle_code_pr_check

        try:
            return await handle_code_pr_check(
                CodePrCheckInput(org=org, namespace=code_ns, title=title[:500], body=body[:20000])
            )
        except Exception as e:  # noqa: BLE001 — fail open: the issue answer stands alone
            logger.info("tracker.duplicate_check: related PR search skipped: %s", e)
            return None

    return asyncio.ensure_future(run())


async def _with_links(
    out: TrackerDuplicateCheckOutput,
    org: str,
    default_ns: str,
    pr_task: Any,
    wall: datetime | None = None,
    code_ns: str = "",
    title: str = "",
    *,
    cache: bool = True,
) -> TrackerDuplicateCheckOutput:
    """Every issue row shown carries its linked PRs and the release its fix ships in; on
    none / related the PRs that may already address the draft ride along; and one sentence
    leads the answer. Each read fails open to the answer as it was."""
    from server.tracker.app import dupcheck_links as dl

    if out is None or getattr(out, "tracker_unavailable", False):
        return out
    d = out.model_dump()
    rows = [
        r for k in ("items", "closest", "nearest") for r in (d.get(k) or []) if isinstance(r, dict)
    ]
    if rows:
        # one budget for both reads: loom_search's tracker leg calls this
        # under its own 3 s timeout, and the verdict must not wait on a fix
        try:
            await asyncio.wait_for(
                _links_and_releases(rows, org, default_ns, wall, code_ns),
                timeout=settings.tracker_dupcheck_links_budget_s,
            )
        except Exception as e:  # noqa: BLE001 — rows keep whatever landed in time
            logger.info("tracker.duplicate_check: fix links cut short: %s", e)
    related: list[dict[str, Any]] = []
    lead = dl.lead_issue(d)
    # a reported issue leads the answer: the PR search adds nothing to it
    if pr_task is not None and lead is None and str(d.get("verdict") or "") in ("none", "related"):
        try:
            pc_out = await asyncio.wait_for(
                asyncio.shield(pr_task), timeout=settings.tracker_dupcheck_pr_wait_s
            )
        except Exception:  # noqa: BLE001 — a slow or failed search adds nothing
            pc_out = None
        exclude = {int(lk.get("number") or 0) for r in rows for lk in (r.get("links") or [])}
        related = dl.related_prs(pc_out, exclude) if pc_out is not None else []
    d["related_prs"] = related
    d["answer"] = dl.answer_line(d, float(settings.tracker_dupcheck_lead_min))
    final = out.model_copy(
        update={k: d.get(k) for k in ("items", "closest", "nearest", "related_prs", "answer")},
    )
    if wall is None and code_ns and cache:
        remember_answer(org, code_ns, title, final)
    return final


async def _links_and_releases(
    rows: list[dict[str, Any]], org: str, default_ns: str, wall: datetime | None, code_ns: str
) -> None:
    from server.tracker.app import dupcheck_links as dl

    await _attach_links(rows, org, default_ns, wall)
    try:
        await dl.release_of_merges(org, code_ns, rows)
    except Exception as e:  # noqa: BLE001 — the tracker's estimate stands
        logger.info("tracker.duplicate_check: release lookup skipped: %s", e)


# Recent full answers, so bisect's own check on a report the session already checked costs
# nothing. Per process (a uvicorn worker), keyed by the draft's words; entries live
# `tracker_dupcheck_recent_ttl_s`. An answer spans every tracker record its caller could
# read and links fixes in one code namespace, so it is served back only to the same
# principal asking about the same code namespace.
_RECENT: dict[str, tuple[float, frozenset[str], Any]] = {}
_RECENT_MAX = 256
_RECENT_JACCARD = 0.7


def _words(text: str) -> frozenset[str]:
    import re as _re

    return frozenset(w for w in _re.findall(r"[a-z0-9_]{3,}", text.lower()))


def _recent_scope(org: str, code_ns: str) -> str:
    from server.auth.context import current_caller

    return f"{org}|{current_caller()[0]}|{code_ns}|"


def remember_answer(org: str, code_ns: str, text: str, out: Any) -> None:
    words = _words(text)
    if len(words) < 3:
        return
    now = time.monotonic()
    for k in [k for k, v in _RECENT.items() if now - v[0] > settings.tracker_dupcheck_recent_ttl_s]:
        _RECENT.pop(k, None)
    if len(_RECENT) >= _RECENT_MAX:
        _RECENT.pop(min(_RECENT, key=lambda k: _RECENT[k][0]), None)
    _RECENT[_recent_scope(org, code_ns) + " ".join(sorted(words))] = (now, words, out)


def recent_answer(org: str, code_ns: str, title: str, body: str) -> Any:
    """The freshest answer remembered for this principal and code namespace whose draft
    shares ≥ 70% of its words (Jaccard) with this report's title, or None."""
    words = _words(title)
    if len(words) < 3:
        return None
    now = time.monotonic()
    scope = _recent_scope(org, code_ns)
    best, best_j = None, 0.0
    for key, (at, w, out) in list(_RECENT.items()):
        if not key.startswith(scope) or now - at > settings.tracker_dupcheck_recent_ttl_s:
            continue
        j = len(words & w) / max(1, len(words | w))
        if j >= _RECENT_JACCARD and j > best_j:
            best, best_j = out, j
    return best


async def _attach_links(
    rows: list[dict[str, Any]], org: str, default_ns: str, wall: datetime | None = None
) -> None:
    """One `fixes`-edge read per namespace for every row shown, in
    parallel, bounded by the text budget; a failed read leaves its rows
    without links. A replay (`as_of`) sees only fixes merged before it."""
    from server.tracker.app import dupcheck_links as dl

    by_ns: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_ns.setdefault(str(r.get("namespace") or default_ns), []).append(r)

    async def one(ns: str, group: list[dict[str, Any]]) -> None:
        src = tsource.tracker_source(org, ns)
        if not src.known:
            return
        repos = src.repos()
        only = repos[0] if len(repos) == 1 else ""
        keys = {(dl.repo_of(r, only), int(r.get("number") or 0)) for r in group}
        found = await src.fix_prs_of_issues(org, ns, sorted(keys))
        for r in group:
            prs = found.get((dl.repo_of(r, only), int(r.get("number") or 0)), [])
            dl.attach(r, dl.as_of(prs, wall))

    results = await asyncio.gather(*(one(ns, g) for ns, g in by_ns.items()), return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            logger.info("tracker.duplicate_check: fix links skipped: %s", res)


async def _checked(
    inputs: list[TrackerDuplicateCheckInput], wall: datetime | None, top_k: int
) -> TrackerDuplicateCheckOutput:
    """Every namespace's check, and one rerank call over all
    their pools. Each check hands its pool over as soon as its fit has
    answered, and the call runs while the checks finish (judge, closest,
    canonical), so its latency hides behind theirs. One namespace: its own
    answer; several: merged."""
    from server.tracker.app.dupcheck_fanout import merge_outputs

    loop = asyncio.get_running_loop()
    ready = [loop.create_future() for _ in inputs]
    tasks = [
        asyncio.ensure_future(_check_one(inp, wall, pool_ready=f))
        for inp, f in zip(inputs, ready, strict=True)
    ]
    for t, f in zip(tasks, ready, strict=True):
        t.add_done_callback(_empty_pool_on_exit(f))
    owned: list[asyncio.Future[Any]] = list(tasks)
    try:
        # Cancelling a gather over the bare pool futures does not reach the checks
        # (the loom_search tracker leg runs this under wait_for): every task this
        # call started is cancelled on the way out unless it finished.
        pools = await asyncio.gather(*ready)
        rerank = asyncio.ensure_future(
            _rerank_over(inputs[0], list(pools)) if inputs[0].rerank else _none()
        )
        owned.append(rerank)
        outs = await asyncio.gather(*tasks, return_exceptions=True)
        kept: list[Any] = []
        for inp, o in zip(inputs, outs, strict=True):
            if isinstance(o, BaseException):
                if inp is inputs[0] or isinstance(o, customer.HOLDS):
                    raise o  # a held model call is the answer, from any tracker
                logger.info(
                    "tracker.duplicate_check on %s failed inside a fan-out: %s", inp.namespace, o
                )
                kept.append(None)
            else:
                kept.append(o)
        out: TrackerDuplicateCheckOutput
        if len(inputs) == 1:
            out = kept[0]
        else:
            out = merge_outputs([i.namespace for i in inputs], kept, top_k)
        picked = await rerank
    finally:
        for started in owned:
            if not started.done():
                started.cancel()
    if picked is None:
        return out
    rr, picks = picked
    return _apply_picks(out, picks, rr)


async def _none() -> None:
    return None


def _empty_pool_on_exit(fut: Any) -> Any:
    """A check that ends before handing its pool over (it failed early, or
    found the record unavailable) counts as an empty pool."""

    def done(_task: Any) -> None:
        if not fut.done():
            fut.set_result([])

    return done


async def _rerank_over(input: Any, pools: list[list[dict[str, Any]]]) -> Any:
    """One rerank call over every namespace's pool (the namespaces taking turns) ->
    (Rerank, pick rows), or None. A pick names its namespace: two repositories' issue
    numbers can collide, so the model sees positional keys."""
    from server.tracker.app import dup_rerank as dr

    lists = [p for p in pools if p]
    if not lists:
        return None
    cands: list[dict[str, Any]] = []
    for i in range(max(len(p) for p in lists)):
        for p in lists:
            if i < len(p):
                cands.append({**p[i], "key": len(cands) + 1})
    cands = cands[: pool_cap()]
    title = str(cands[0].get("draft_title") or input.title or "")
    body = str(cands[0].get("draft_body") if "draft_body" in cands[0] else input.body or "")
    rr = await _rerank_call(input.model_copy(update={"title": title, "body": body}), cands)
    if rr is None:
        return None
    rows = dr.pick_rows(rr, {int(c["key"]): c for c in cands}, exclude=set())
    # the block reports issue numbers, not the positional keys the model saw
    rr.picks = [
        (int(r["number"]), str(r["rerank"]["label"]), float(r["rerank"]["confidence"]))
        for r in rows
    ]
    return rr, rows


def _apply_picks(
    out: TrackerDuplicateCheckOutput, picks: list[dict[str, Any]], rr: Any
) -> TrackerDuplicateCheckOutput:
    """The picks first in `nearest` (never a served item or a `closest`
    row), then the rows already there that are not picked, capped like any
    nearest list; the rerank block on the answer."""
    from server.tracker.app.dupcheck_fanout import picks_cap

    single = not out.namespaces

    def ident(r: dict[str, Any]) -> tuple[str, int]:
        return ("" if single else str(r.get("namespace") or ""), int(row_number(r)))

    shown = {ident(r) for r in [*(out.items or []), *(out.closest or [])]}
    picks = [p for p in picks if ident(p) not in shown]
    taken = {ident(p) for p in picks}
    rows = [*picks, *[r for r in out.nearest or [] if ident(r) not in taken]]
    if single:
        rows = [{k: v for k, v in r.items() if k != "namespace"} if r in picks else r for r in rows]
    from server.tracker.domain import dupcheck_closest as dcl

    capped = dcl.keep_leaders(rows, max(picks_cap(out), 0))
    return out.model_copy(update={"nearest": capped, "rerank": rr.block()})


async def _check_one(input: Any, wall: datetime | None, pool_ready: Any = None) -> Any:
    """The duplicate check of one tracker namespace. `pool_ready` receives the namespace's
    rerank pool as soon as the fit has answered; the caller reranks while this finishes."""

    src = tsource.tracker_source(input.org, input.namespace)
    status: dict[str, str] = {}
    try:
        run = await candidates(src, input, wall=wall, status=status)
    except TrackerUnavailable as e:
        common._unavailable("tracker.duplicate_check", input.namespace, e)
        return TrackerDuplicateCheckOutput(
            verdict="none",
            epistemic="unavailable",
            thresholds=thresholds(),
            legs={leg: "tracker_unavailable" for leg in sorted(LEGS_ON)},
            leg_status=[
                TrackerLegStatus(**r)
                for r in dl.all_down(dl.classify(e), namespace=str(input.namespace or ""))
            ],
            tracker_unavailable=True,
        )
    ran = [v for k, v in status.items() if k != "fields" and v != "skipped"]
    if ran and all(v == "tracker_unavailable" for v in ran):
        return TrackerDuplicateCheckOutput(
            verdict="none",
            epistemic="unavailable",
            thresholds=thresholds(),
            legs=status,
            leg_status=run.get("leg_status") or [],
            query=query_block(run, wall),
            tracker_unavailable=True,
        )
    handing = None
    if pool_ready is not None:
        # The pool reads start as soon as the legs have answered and run beside the fit and
        # the judge; the caller's rerank call starts the moment every namespace has handed its
        # pool over (a pick that turns out served is dropped when the picks are applied).
        async def hand_over() -> None:
            try:
                pool = await _rerank_pool(src, input, run, wall)
            except Exception as e:  # noqa: BLE001 — the rerank is optional
                logger.info("duplicate_check: rerank pool failed (%s)", e)
                pool = []
            title, body = _draft_text(input, run)
            rows = [
                {**c, "namespace": input.namespace, "draft_title": title, "draft_body": body}
                for c in pool
            ]
            if not pool_ready.done():
                pool_ready.set_result(rows)

        handing = asyncio.ensure_future(hand_over())
    try:
        fit = await cached_fit(input.org, input.namespace)
        out = answer(run, fit, status=status, wall=wall, top_k=int(input.top_k))
        out = await judge_uncertain(src, input, run, out, fit)
        if handing is not None:
            await handing
    finally:
        # A failed or cancelled check does not leave its pool read running.
        if handing is not None and not handing.done():
            handing.cancel()
    out = with_closest(out, run, input, fit)
    out = await with_canonical(src, input, out)
    return out


async def _rerank_pool(
    src: Any, input: Any, run: dict[str, Any], wall: datetime | None
) -> list[dict[str, Any]]:
    """The rerank's candidates for one namespace (each leg's own head, the legs taking
    turns, then the fingerprint rows) with their title, body head, state and url, none filed
    after the wall. [] when off, when the record could not be read, on a draft with no text,
    or on any read failure."""
    from server.tracker.app import dup_rerank as dr

    on = bool(settings.tracker_dupcheck_rerank_enabled)
    if not on or not getattr(input, "rerank", True) or _draft_text(input, run) == ("", ""):
        return []
    own = int(getattr(run.get("q"), "number", 0) or 0)
    served = {own}
    heads = dict(run.get("leg_heads") or {})
    per = max(1, int(settings.tracker_dupcheck_rerank_per_leg))
    chars = int(settings.tracker_dupcheck_rerank_body_chars)
    fingerprint = list(run.get("fingerprint") or [])
    cap = int(settings.tracker_dupcheck_rerank_ns_cap)
    pool = dr.build_pool(heads, per_leg=per, extra=[fingerprint], exclude=served, cap=cap)
    if not pool:
        return []
    try:
        rows = list(
            await src.embed_items_by_number(
                input.org, input.namespace, kind="issue", numbers=pool, body_chars=chars
            )
        )
    except Exception as e:  # noqa: BLE001 — no text, no rerank
        logger.info("duplicate_check: rerank text unavailable (%s)", e)
        return []
    by = {int(r["number"]): r for r in rows if int(r.get("number") or 0) > 0}
    lexical_head = set(heads.get("lexical", [])[:ANCHOR_LEXICAL_RANK])
    fp = set(fingerprint)
    code = int(getattr(run.get("q"), "error_code", 0) or 0)

    def anchors(n: int) -> list[str]:
        out = []
        if n in fp:
            out.append(f"same error code {code}" if code else "same STID")
        if n in lexical_head:
            out.append("lexical")
        return out

    return [
        {**by[n], **_row_view(by[n], wall), "anchors": anchors(n)}
        for n in pool
        if n in by and not _after(by[n].get("created_at"), wall)
    ]


# A lexical anchor is a lexical row this high
ANCHOR_LEXICAL_RANK = 10


def _draft_text(input: Any, run: dict[str, Any]) -> tuple[str, str]:
    title, body = str(input.title or "").strip(), str(input.body or "").strip()
    if not title and not body:  # an issue_number replay: the item's own text
        item = run.get("item") or {}
        title, body = str(item.get("title") or ""), str(item.get("body") or "")
    return title, body


async def _rerank_call(input: Any, cands: list[dict[str, Any]]) -> Any:
    """One model call over `cands`, under its timeout; None on no candidates or
    while reads are LLM-free."""
    import asyncio as _aio

    from server.llm.client import get_llm_client_for_model
    from server.tracker.app import dup_rerank as dr

    if not cands or reads_llm_free():
        return None
    title, body = str(input.title or "").strip(), str(input.body or "").strip()
    model = str(settings.tracker_dupcheck_rerank_model) or None
    timeout = float(settings.tracker_dupcheck_rerank_timeout_s)
    chars = int(settings.tracker_dupcheck_rerank_body_chars)
    try:
        return await _aio.wait_for(
            dr.rerank(get_llm_client_for_model(model), title, body, cands, body_chars=chars),
            timeout=timeout,
        )
    except TimeoutError:
        return dr.Rerank(pool=len(cands), error="timeout")


def _after(created: Any, wall: datetime | None) -> bool:
    return (
        wall is not None
        and isinstance(created, datetime)
        and created.replace(tzinfo=None) >= wall.replace(tzinfo=None)
    )


def _row_view(r: dict[str, Any], wall: datetime | None) -> dict[str, Any]:
    """title, state (as of the wall) and url of a text row."""
    closed = r.get("closed_at")
    is_closed = isinstance(closed, datetime) and (
        wall is None or closed.replace(tzinfo=None) < wall.replace(tzinfo=None)
    )
    sid = str(r.get("source_id") or "")
    repo, _, num = sid.rpartition("#")
    return {
        "title": r.get("title") or "",
        "state": "closed" if is_closed else "open",
        "url": f"https://github.com/{repo}/issues/{num}" if repo and num else "",
    }


# ---------------------------------------------------------------------------
# the stored fit, read once per namespace per TTL
# ---------------------------------------------------------------------------

_FIT_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}


async def cached_fit(org: str, ns: str) -> dict[str, Any] | None:
    from server.tracker.app import dupcheck_fit as df

    now = time.monotonic()
    hit = _FIT_CACHE.get((org, ns))
    if hit is not None and hit[0] > now:
        return hit[1]
    fit = await df.stored_fit(df.state_repo(), org, ns)
    if fit is None:
        fit = await _borrowed_fit(df, org, ns)
    _FIT_CACHE[(org, ns)] = (now + settings.tracker_dupcheck_fit_ttl_s, fit)
    return fit


async def _borrowed_fit(df: Any, org: str, ns: str) -> dict[str, Any] | None:
    """A restricted tracker namespace is never refit (its gold stays behind its own gate), so
    it has no fit of its own. Rather than answering `uncalibrated` with the judge off, it
    serves the fit of the org's standard tracker namespace (the same features over the same
    record shape; coefficients carry no row of either record), marked
    `borrowed_from`. A standard namespace with no fit borrows nothing."""
    from server.tracker.domain.namespaces import configured_namespaces, tracker_namespace

    own = tracker_namespace(org, ns)
    if own is None or not own.restricted:
        return None
    for name, cfg in sorted(configured_namespaces().items()):
        if cfg.org != org or cfg.restricted or name == ns:
            continue
        fit = await df.stored_fit(df.state_repo(), org, name)
        if fit is not None:
            logger.info("dupcheck_fit: %s/%s borrows the fit of %s", org, ns, name)
            return {**fit, "borrowed_from": name}
    return None


def reset_fit_cache_for_tests() -> None:
    _FIT_CACHE.clear()
