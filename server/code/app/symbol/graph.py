"""code.callers / code.callees: one hop over the edge graph from the resolved
seeds, with dispatch widening, candidate blocks and the coverage notes of the answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from server.code.api.models_symbol import CodeCallersInput, CodeCallersOutput, CodeEdgeWire
from server.code.app.candidates import callee_candidates, caller_candidates
from server.code.app.common import (
    answer_args,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    risk_for,
    schedule_code_trace,
    tier_of,
)
from server.code.app.index.pr_overlay import NO_GIT_MIRROR, reads_post_image
from server.code.app.index.symbol_forms import resolved_by_of
from server.code.app.spans import (
    code_span,
    current_stage_ms,
    set_code_span_attributes,
    span_attrs,
    start_stage_wall,
)
from server.code.app.symbol.coverage import (
    _CTOR_NOTE,
    _ctor_edges_absent,
    _declined_outgoing_safe,
    _declined_safe,
    _env_fields,
    _has_ctor_seed,
    _scip_coverage,
    _scip_stale,
)
from server.code.app.symbol.dispatch import (
    _dispatch_missing,
    _dispatch_seeds,
    _fold_members,
    _widen_dispatch,
)
from server.code.app.symbol.edges import _pr_overlay, _walk_edges
from server.code.app.symbol.resolve import _resolve_symbols, _seed_ids_of
from server.code.domain.index.epistemic import envelope as _envelope
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError
from server.settings import settings


@reads_post_image
async def handle_code_callers(input: CodeCallersInput) -> CodeCallersOutput:
    return await _graph_hop(input, direction="callers")


async def handle_code_callees(input: CodeCallersInput) -> CodeCallersOutput:
    return await _graph_hop(input, direction="callees")


def _hop_started() -> float:
    """The hop's clock and its stage wall in one call."""
    start_stage_wall()
    return time.monotonic()


async def _graph_hop(input: CodeCallersInput, *, direction: str) -> CodeCallersOutput:
    require_code_intel()
    started = _hop_started()  # code.callers records its stages too

    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    route = f"graph_{direction}"
    query = input.name or input.symbol_id

    def _trace(
        *,
        ids: list[str],
        paths: list[str],
        rows: list[Any],
        abstain: bool,
        reason: str = "",
        args: dict[str, Any] | None = None,
    ) -> str:
        trace_run_id = new_trace_run_id()
        schedule_code_trace(
            org=org,
            ns=ns,
            op=f"code.{direction}",
            query=query,
            route=route,
            returned_ids=ids,
            returned_paths=paths,
            returned_scores=[],
            top_k=input.max_nodes,
            tokens_returned=0,
            abstain=abstain,
            retrieval_risk="abstain" if abstain else risk_for(rows),
            embedding_model="",
            started_monotonic=started,
            trace_run_id=trace_run_id,
            consumer=input.consumer,
            agent=input.agent,
            user_id=input.user_id,
            reason=reason,
            args=args,
            stage_ms=current_stage_ms(),
        )
        return trace_run_id

    seeds, overlay = await _graph_seeds(repo, org, ns, input, direction=direction)
    if not seeds:
        raise NotFoundError(_not_found(input, overlay))
    # Ambiguity guard: a bare short name resolving to many unrelated
    # definitions (`setColumns` → 60 classes) would walk every one of them —
    # and run dispatch resolution for each — for an answer nobody asked for.
    # Refuse fast with the candidates instead; a qualified name proceeds.
    distinct = list(dict.fromkeys(str(r["qualified_name"]) for r in seeds))
    # Rows bound through a registration are the methods of the one class a SQL name
    # implements, not an ambiguity to refuse.
    one_class = resolved_by_of(seeds) == "registration"
    if len(distinct) > settings.code_callers_max_distinct_seeds and not one_class:
        return _ambiguous_refusal(input, route, distinct, _trace)
    seed_ids = _seed_ids_of(seeds)

    # An override has (almost) no direct callers: call sites bind to the base
    # virtual. Walk to the base declarations and seed from them too, saying so.
    # Bounded to a few seeds: dispatch resolution is several queries per seed.
    dispatch_attempted = direction == "callers" and len(distinct) <= 3
    dispatch, dispatch_rows, seed_ids, note = (
        await _base_dispatch(repo, org, ns, seeds, seed_ids, direction=direction)
        if dispatch_attempted
        else ([], [], seed_ids, "")
    )

    # A class or interface seed has few direct callers: its uses are calls to its members.
    # Their callers join the walk, each edge tagged `via: member`.
    via_member: dict[str, str] = {}
    if direction == "callers" and len(distinct) <= 3:
        via_member, seed_ids, note = await _fold_members(repo, org, ns, seeds, seed_ids, note)

    # On request the walk widens across dispatch: a base seed's overrides join the seeds,
    # and every edge reached through a dispatch seed carries `via_override`.
    via_for: dict[str, str] = {}
    widen, min_conf = _widening(input)
    if widen and direction == "callers":
        via_for, seed_ids, dispatch, note = await _widen_dispatch(
            repo,
            org,
            ns,
            seed_ids=seed_ids,
            dispatch_rows=dispatch_rows,
            dispatch=dispatch,
            note=note,
        )

    edges_out, truncated, hops, by_hop_counts = await _walk_edges(
        repo,
        org,
        ns,
        input,
        direction=direction,
        seed_ids=seed_ids,
        via_for=via_for,
        overlay=overlay,
        via_member=via_member,
    )
    # the far end of each edge is the answer: callers -> from, callees -> to
    node_ids = list(
        dict.fromkeys(
            e.from_symbol_id if direction == "callers" else e.to_symbol_id for e in edges_out
        )
    )
    # How low is the floor: declined call sites named like the seed, compiler coverage of
    # the seed files, an override seed without its base. Independent reads, gathered; all
    # fail open.
    seed_paths = list(dict.fromkeys(str(r.get("path") or "") for r in seeds))
    (unresolved_n, ambiguous_n), scip_paths, dispatch_missing, stale_paths = await _floor_reads(
        repo, org, ns, seeds, seed_paths, edges_out, direction, dispatch_rows, dispatch_attempted
    )
    # A constructor with no edge at all: until the tier that answers for it carries
    # constructor calls through factories, the empty answer is a lower bound with a named
    # cause, never `exact`.
    ctor_absent = False
    if not edges_out and _has_ctor_seed(seeds):
        ctor_absent = await _ctor_edges_absent(repo, org, ns, seed_paths, scip_paths)
        if ctor_absent:
            note = f"{note}; {_CTOR_NOTE}" if note else _CTOR_NOTE
    env = _envelope(
        unresolved=unresolved_n,
        ambiguous=ambiguous_n,
        seed_paths=seed_paths,
        scip_paths=scip_paths,
        dispatch_missing=dispatch_missing,
        ctor_edges_absent=ctor_absent,
        scip_stale=bool(stale_paths),
        truncated=truncated,
    )
    note = _joined_note(note, _stale_note(stale_paths))
    trace_run_id = _trace(
        ids=node_ids,
        paths=list(dict.fromkeys(e.path for e in edges_out)),
        rows=edges_out,
        abstain=not edges_out,
        # why the walk is empty, and the knobs it ran under
        reason=("" if edges_out else (",".join(env.causes) or "no_edges_from_seeds")),
        args=answer_args(
            input,
            "depth",
            "max_nodes",
            "min_confidence",
            "pr_number",
            "edge_kinds",
            include_ambiguous=widen,
            candidate_min_confidence=min_conf,
        ),
    )
    set_code_span_attributes(
        route=route,
        tier=tier_of(edges_out),
        results=len(edges_out),
        edges=len(edges_out),
        seeds=len(seed_ids),
        dispatch_seeds=len(dispatch),
        hops=hops,
        depth=input.depth,
        truncated=truncated,
        abstain=not edges_out,
        ambiguous=False,
        retrieval_risk="abstain" if not edges_out else risk_for(edges_out),
        consumer=input.consumer,
    )
    note = _joined_note(note, _declined_note(unresolved_n, ambiguous_n))
    candidate_block = (
        await _spanned_candidates(
            repo,
            org,
            ns,
            seeds,
            edges_out,
            direction=direction,
            max_nodes=input.max_nodes,
            virtual=bool(via_for),
            min_confidence=min_conf,
        )
        if widen
        else None
    )
    return CodeCallersOutput(
        edges=edges_out,
        seed_symbols=[str(r["qualified_name"]) for r in seeds],
        dispatch_seeds=dispatch,
        note=note,
        truncated=truncated,
        candidate_sites=candidate_block,
        unresolved_calls=unresolved_n,
        ambiguous_calls=ambiguous_n,
        by_hop_counts=by_hop_counts,
        resolved_by=resolved_by_of(seeds),
        **_env_fields(env),
        **(overlay.disclosure() if overlay is not None else {}),
        trace_run_id=trace_run_id,
    )


def _ambiguous_refusal(
    input: CodeCallersInput, route: str, distinct: list[str], trace: Callable[..., str]
) -> CodeCallersOutput:
    """The fast refusal of a bare name that resolves to too many distinct definitions:
    the candidates to qualify it with, and no walk."""
    set_code_span_attributes(
        route=route,
        results=0,
        ambiguous=True,
        abstain=True,
        seeds=len(distinct),
        retrieval_risk="abstain",
        consumer=input.consumer,
    )
    return CodeCallersOutput(
        edges=[],
        seed_symbols=[],
        ambiguous=True,
        candidates=distinct[:20],
        note=(
            f"{input.name!r} resolves to {len(distinct)} distinct definitions; "
            "qualify it (Class::method) or pass symbol_id — no graph walk was done"
        ),
        trace_run_id=trace(
            ids=[],
            paths=[],
            rows=[],
            abstain=True,
            reason="ambiguous_seed",
            args=answer_args(input, "depth", "max_nodes", "min_confidence"),
        ),
    )


def _widening(input: CodeCallersInput) -> tuple[bool, float]:
    """Whether the walk widens across dispatch (unless the request says no), and the
    candidate sites' confidence floor: the request's, else the deployment default."""
    widen = input.include_ambiguous is not False
    min_conf = (
        float(input.candidate_min_confidence)
        if input.candidate_min_confidence is not None
        else float(settings.code_callers_candidate_min_confidence)
    )
    return widen, min_conf


async def _base_dispatch(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    seed_ids: list[str],
    *,
    direction: str,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[str], str]:
    """An override seed's base declarations: (the dispatch disclosure, the dispatch rows,
    the seed ids with the bases added, the note saying so)."""
    with code_span(f"{direction}.dispatch", seeds=len(seeds)) as sp:
        dispatch_rows = await _dispatch_seeds(repo, org, ns, seeds)
        span_attrs(
            sp,
            results=len(dispatch_rows),
            route=",".join(sorted({str(r["via"]) for r in dispatch_rows})) or "none",
        )
    if not dispatch_rows:
        return [], dispatch_rows, seed_ids, ""
    dispatch = [{"symbol": str(r["qualified_name"]), "via": str(r["via"])} for r in dispatch_rows]
    extra = [str(r["symbol_id"]) for r in dispatch_rows]
    note = (
        "seed is an override; callers of its base virtual declaration(s) are included "
        "(virtual dispatch binds call sites to the base)"
    )
    return dispatch, dispatch_rows, list(dict.fromkeys([*seed_ids, *extra])), note


async def _spanned_candidates(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    edges_out: list[Any],
    *,
    direction: str,
    max_nodes: int,
    virtual: bool,
    min_confidence: float,
) -> Any:
    """The candidate-sites block of a widened walk, under its own span."""
    with code_span(f"{direction}.candidates", seeds=len(seeds)) as sp:
        block = await _candidate_block(
            repo,
            org,
            ns,
            seeds,
            edges_out,
            direction=direction,
            max_nodes=max_nodes,
            virtual=virtual,
            min_confidence=min_confidence,
        )
        span_attrs(sp, results=block.returned)
    return block


async def _floor_reads(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    seed_paths: list[str],
    edges_out: list[Any],
    direction: str,
    dispatch_rows: Any,
    dispatch_attempted: Any,
) -> tuple[tuple[int, int], set[str], bool, Any]:
    """((unresolved, ambiguous), scip-covered seed paths, dispatch seeds missing, stale
    paths), gathered so a cancelled call takes every read down. The declined count is the
    walk's blind spot: sites named like the seed for callers, the seed's own declined call
    sites for callees."""
    coverage = _scip_coverage(repo, org, ns, seed_paths, seeds)
    stale = _scip_stale(repo, org, ns, edges_out)
    if direction == "callers":
        declined, scip_paths, dispatch_missing, stale_paths = await asyncio.gather(
            _declined_safe(repo, org, ns, seeds),
            coverage,
            _dispatch_missing(repo, org, ns, seeds, dispatch_rows, attempted=dispatch_attempted),
            stale,
        )
        return declined, scip_paths, dispatch_missing, stale_paths
    declined, scip_paths, stale_paths = await asyncio.gather(
        _declined_outgoing_safe(repo, org, ns, seeds), coverage, stale
    )
    return declined, scip_paths, False, stale_paths


def _not_found(input: CodeCallersInput, overlay: Any) -> str:
    """The 404 of a seed neither the index nor the PR's overlay holds; on a host
    without the mirror the caller sent no post-image to, says that is why."""
    msg = f"symbol not found: {input.name or input.symbol_id}"
    reason = str(getattr(overlay, "reason", "") or "")
    if reason.startswith((NO_GIT_MIRROR, "no_git_history")):
        msg += (
            f" (not in the index, and PR {input.pr_number} was not read: {reason};"
            " send the PR's pr_files and pr_diff)"
        )
    return msg


async def _graph_seeds(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeCallersInput,
    *,
    direction: str,
) -> tuple[list[dict[str, Any]], Any]:
    """(seeds, overlay). With `pr_number` an open PR's post-image is merged over the
    index: a seed the PR adds resolves here (tier `overlay`), and its call sites inside
    the PR are edges."""
    overlay: Any = None
    if input.pr_number:
        with code_span(f"{direction}.overlay", seeds=input.pr_number) as sp:
            overlay = await _pr_overlay(repo, org, ns, input.pr_number)
            span_attrs(sp, results=len(overlay.symbols) if overlay.ok else 0)
    with code_span(f"{direction}.resolve") as sp:
        seeds = await _resolve_symbols(repo, org, ns, symbol_id=input.symbol_id, name=input.name)
        if not seeds and overlay is not None and overlay.ok and input.name:
            seeds = [s.row() for s in overlay.symbols_named(input.name)]
        span_attrs(sp, results=len(seeds), tier=tier_of(seeds))
    return seeds, overlay


async def _candidate_block(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    edges: list[CodeEdgeWire],
    *,
    direction: str,
    max_nodes: int,
    virtual: bool,
    min_confidence: float,
) -> Any:
    if direction == "callers":
        return await caller_candidates(
            repo,
            org,
            ns,
            seeds,
            edges,
            max_nodes=max_nodes,
            virtual=virtual,
            min_confidence=min_confidence,
        )
    return await callee_candidates(
        repo, org, ns, seeds, max_nodes=max_nodes, min_confidence=min_confidence
    )


def _joined_note(note: str, extra: str) -> str:
    return f"{note}; {extra}" if note and extra else (note or extra)


def _declined_note(unresolved_n: int, ambiguous_n: int) -> str:
    if not (unresolved_n or ambiguous_n):
        return ""
    return (
        f"{unresolved_n + ambiguous_n} call site(s) named like this symbol were declined "
        f"by the fast-tier resolver ({ambiguous_n} ambiguous, {unresolved_n} unresolved) "
        "and are not in edges"
    )


def _stale_note(stale_paths: set[str]) -> str:
    if not stale_paths:
        return ""
    return (
        f"compiler-tier edges from {len(stale_paths)} file(s) changed since that tier "
        f"last ran ({', '.join(sorted(stale_paths)[:3])}): their lines may be stale"
    )
