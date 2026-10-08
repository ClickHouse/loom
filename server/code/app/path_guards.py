"""code.path_guards — the conditions that stand between two symbols.

A common analyst question is "is there a check on X between the call site and the throw":
answering it by hand means reading the whole path (3,000 lines in one measured case), and
most such hypotheses end unconfirmed. code.verify(reaches) says whether A reaches B; this op
walks the same chain and, for each hop, reads the guards the ingest pass stored for the hop's
caller (loom.code_caller_guards, one row per caller function, read on the table's own key)
and says whether any guard of any call site of the hop names the identifier asked about.

The verdict vocabulary keeps a cap apart from an answer: a walk that hit its
depth or node cap with paths still open is `unknown` (depth_exhausted), not
`path_not_found`; a hop with no guard row is "unguarded" only when the
namespace holds guard rows at all (has_call_guards — the positive control;
the guard stage has not reached it), otherwise `unknown` (guards_not_indexed).
Every answer is `lower_bound`: the extractor is syntactic and
intraprocedural (call_guards.py says what it cannot see).

Mechanisms this is a keyed version of: an abstract interpreter's
manifest / latent / unknown labelling of a reported issue (the verdicts
here map the same three ways: a guard names it, none does, cannot tell),
the intraprocedural control-dependence traversal of a code property graph
(the enclosing-condition walk), the path constraints a checker collects
along a call chain (the per-hop guards), and the observation from missing-check detection that
about 70% of conditionals are not security checks (why the op reports the guard that NAMES the
identifier and leaves sufficiency to the reader).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from server.code.api.models_verify import CodePathGuardsInput, CodePathGuardsOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index.symbol_forms import natural_name
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol import (
    _FILE_LINE,
    _normalize_claim_name,
    _resolve_claim_symbol,
    bfs_callees,
)
from server.code.app.ttl_cache import TtlCache
from server.code.domain.extract.call_guards import callee_tag, guard_names, site_guards_of
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError

logger = logging.getLogger(__name__)

CAUSE_DEPTH = "depth_exhausted"
CAUSE_NOT_INDEXED = "guards_not_indexed"
# The guard stage's paced walk has not converged on the namespace: some files carry rows,
# others not yet, so an absent row is "not walked", not "unconditional".
CAUSE_INDEXING = "guards_indexing"
CAUSE_NO_SITE = "hop_without_site"
_GUARD_STAGE_CACHE: TtlCache[tuple[str, str], bool] = TtlCache(300.0)


async def _guard_stage_converged(repo: Any, org: str, ns: str) -> bool:
    """Is the namespace's recorded `guard` stage stamp the running one? The worker records
    the key when its walk completes (or the backfill when it indexed with this extractor);
    while the walk is in flight the key is missing or older. One keyed state read per
    namespace per five minutes; an unreadable state falls back to the row probe alone rather
    than turning a measured verdict into `unknown`."""
    hit = _GUARD_STAGE_CACHE.get((org, ns))
    if hit is not None:
        return hit
    try:
        from server.code.domain.extract.extractor_stamp import stage_stamps

        state = await repo.all_state(org, ns)
        recorded = str((state.get("extractor_stamp_guard") or {}).get("value") or "")
        ok = bool(recorded) and recorded == stage_stamps()["guard"]
    except Exception:  # noqa: BLE001 — state unreadable: do not invent an abstain
        ok = True
    _GUARD_STAGE_CACHE.put((org, ns), ok)
    return ok


async def _unindexed_verdict(
    repo: Any,
    org: str,
    ns: str,
    hops: list[dict[str, Any]],
    causes: list[str],
    visited: int,
) -> CodePathGuardsOutput | None:
    """The positive control behind an unguarded hop: `unknown` when the
    namespace holds no guard row at all (the writer never ran here), or
    when it holds some but the guard stage's walk has not converged; otherwise None, and the
    caller judges the path."""
    with code_span("path_guards.probe") as sp:
        indexed = await repo.has_call_guards(org, ns)
        converged = indexed and await _guard_stage_converged(repo, org, ns)
        span_attrs(sp, results=int(indexed), converged=int(bool(converged)))
    if indexed and converged:
        return None
    if not indexed:
        cause, why = (
            CAUSE_NOT_INDEXED,
            (
                f"the path ({len(hops)} hop(s)) was found but namespace {ns} holds no guard "
                "rows: the guard stage has not run here yet, so an absent "
                "guard is unindexed, not unconditional."
            ),
        )
    else:
        cause, why = (
            CAUSE_INDEXING,
            (
                f"the path ({len(hops)} hop(s)) was found but the guard stage's walk has not "
                f"converged on namespace {ns}: a hop's file may not have been "
                "walked yet, so an absent guard is unindexed, not unconditional."
            ),
        )
    return CodePathGuardsOutput(
        verdict="unknown",
        causes=[cause, *causes, *FLOORS],
        hops=hops,
        visited_symbols=visited,
        explanation=why,
    )


# The standing floors of a syntactic, intraprocedural read — attached to
# every answer so a `no_guard_named` is never read as "unchecked".
FLOORS: tuple[str, ...] = ("syntactic", "intraprocedural", "name_matched_edges")


async def handle_code_path_guards(input: CodePathGuardsInput) -> CodePathGuardsOutput:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    out = await _path_guards(input, repo, org, ns)

    paths = list(dict.fromkeys(str(h.get("path") or "") for h in out.hops if h.get("path")))
    unknown = out.verdict == "unknown"
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.path_guards",
        query=f"{input.from_symbol} -> {input.to_symbol} mentions {input.mentions}".strip(),
        route="path_guards",
        returned_ids=[out.verdict],
        returned_paths=paths,
        returned_scores=[],
        top_k=1,
        tokens_returned=0,
        abstain=unknown,
        retrieval_risk="abstain" if unknown else "low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict="no_match" if unknown else "",
    )
    set_code_span_attributes(
        route="path_guards",
        verdict=out.verdict,
        results=len(out.hops),
        depth=input.depth,
        abstain=unknown,
        retrieval_risk="abstain" if unknown else "low",
        consumer=input.consumer,
    )
    out.trace_run_id = trace_run_id
    return out


def _judge_hops(
    chain: list[dict[str, Any]],
    by_site: dict[tuple[str, int, str, int], list[dict[str, str]]],
    mentions: str,
) -> tuple[list[dict[str, Any]], list[str], list[str], bool, bool]:
    """Judge every call site of every hop against the per-site guard lists
    expanded from the chain's caller rows (`site_guards_of`, keyed
    (caller, callee tag, path, line)): (hops, causes, named_at, any_hit,
    missing) where `missing` says some site with a line had no guard list —
    the positive-control question the caller then asks the namespace."""
    hops: list[dict[str, Any]] = []
    causes: list[str] = []
    named_at: list[str] = []
    any_hit = False
    missing = False
    for h in chain:
        # the hop is the (caller, callee) PAIR: every call site of it stands
        # on the path, so each is judged and listed (the BFS batch held them)
        own = {
            "path": h["path"],
            "line": int(h["line"]),
            "tier": h["tier"],
            "confidence": h["confidence"],
        }
        pair_sites = list(h.get("sites") or [])
        if not any(x["path"] == own["path"] and x["line"] == own["line"] for x in pair_sites):
            pair_sites.append(own)
        judged: list[dict[str, Any]] = []
        for x in pair_sites:
            key = (h["from_symbol_id"], callee_tag(h["to_symbol_id"]), x["path"], int(x["line"]))
            found = by_site.get(key)
            if int(x["line"]) == 0 and CAUSE_NO_SITE not in causes:
                causes.append(CAUSE_NO_SITE)
            guards = list(found) if found is not None else []
            if found is None and int(x["line"]) != 0:
                missing = True
            hit = bool(mentions) and any(guard_names(g["text"], mentions) for g in guards)
            if hit:
                named_at.append(f"{x['path']}:{x['line']}")
            judged.append(
                {"path": x["path"], "line": int(x["line"]), "guards": guards, "mentions_hit": hit}
            )
        hop_hit = any(x["mentions_hit"] for x in judged)
        any_hit = any_hit or hop_hit
        hops.append(
            {
                "caller": h["from"],
                "callee": h["to"],
                "path": h["path"],
                "line": int(h["line"]),
                "tier": h["tier"],
                "confidence": h["confidence"],
                "guards": judged[0]["guards"] if len(judged) == 1 else _own_guards(judged, own),
                "mentions_hit": hop_hit,
                "sites": judged,
                "sites_named": sum(1 for x in judged if x["mentions_hit"]),
            }
        )

    return hops, causes, named_at, any_hit, missing


def _own_guards(judged: list[dict[str, Any]], own: dict[str, Any]) -> list[dict[str, Any]]:
    """The guards of the chain's own site among a pair's judged sites."""
    for x in judged:
        if x["path"] == own["path"] and x["line"] == own["line"]:
            return list(x["guards"])
    return []


async def _resolve(repo: CodeRepo, org: str, ns: str, name: str, role: str) -> list[dict[str, Any]]:
    rows = await _resolve_claim_symbol(repo, org, ns, name)
    if not rows:
        fuzzy = await repo.symbols_fuzzy(org, ns, name.split("::")[-1])
        hint = ", ".join(dict.fromkeys(natural_name(str(r["qualified_name"])) for r in fuzzy[:5]))
        raise NotFoundError(
            f"{role} symbol not found: {name}" + (f" — did you mean: {hint}" if hint else "")
        )
    return rows


async def _path_guards(
    input: CodePathGuardsInput, repo: CodeRepo, org: str, ns: str
) -> CodePathGuardsOutput:
    with code_span("path_guards.resolve") as sp:
        seeds = await _resolve(repo, org, ns, input.from_symbol, "from")
        want_is_path = "/" in input.to_symbol
        want = input.to_symbol if want_is_path else _normalize_claim_name(input.to_symbol)
        if not want_is_path:
            objects = await _resolve(repo, org, ns, input.to_symbol, "to")
            if _FILE_LINE.match(input.to_symbol.strip()):
                want = str(objects[0]["qualified_name"])
        span_attrs(sp, results=len(seeds))

    with code_span("path_guards.walk", depth=input.depth) as sp:
        chain, visited, exhausted = await bfs_callees(
            repo,
            org,
            ns,
            [str(r["symbol_id"]) for r in seeds],
            want=want,
            want_is_path=want_is_path,
            depth=input.depth,
        )
        span_attrs(sp, results=visited, edges=len(chain or []))

    if chain is None:
        if exhausted:
            return CodePathGuardsOutput(
                verdict="unknown",
                causes=[CAUSE_DEPTH, *FLOORS],
                visited_symbols=visited,
                explanation=f"no chain from {input.from_symbol} to {input.to_symbol} within "
                f"depth {input.depth} ({visited} symbols visited) and paths were still open "
                "at the cap — a bound, not an absence.",
            )
        return CodePathGuardsOutput(
            verdict="path_not_found",
            causes=list(FLOORS),
            visited_symbols=visited,
            explanation=f"no call chain from {input.from_symbol} to {input.to_symbol}: the "
            f"walk emptied its frontier after {visited} symbols. Edges are name-matched "
            "(dynamic dispatch and unresolved sites are invisible), so this is a floor.",
        )

    with code_span("path_guards.guards", hops=len(chain)) as sp:
        rows = await repo.call_guards_for_callers(org, ns, [h["from_symbol_id"] for h in chain])
        by_site = site_guards_of(rows)
        span_attrs(sp, results=len(rows))

    hops, causes, named_at, any_hit, missing = _judge_hops(chain, by_site, input.mentions)

    # the positive control: an unguarded hop means "no condition" only in a namespace the
    # guard stage ran on and converged
    if missing and not any_hit:
        unindexed = await _unindexed_verdict(repo, org, ns, hops, causes, visited)
        if unindexed is not None:
            return unindexed

    verdict: Literal["guarded_by", "no_guard_named", "path_found"]
    if any_hit:
        verdict, why = (
            "guarded_by",
            (
                f"a guard on the path names `{input.mentions}` at {', '.join(named_at)} "
                f"({len(named_at)} of {sum(len(h['sites']) for h in hops)} call site(s)) — read "
                "there; the guard's sufficiency is the reader's call, and the sites without "
                "one are listed per hop."
            ),
        )
    elif input.mentions:
        verdict, why = (
            "no_guard_named",
            (
                f"the path ({len(hops)} hop(s), {sum(len(h['sites']) for h in hops)} call site(s)) "
                f"was found and no if/else/switch/ternary/loop condition or early exit dominating "
                f"any of its call sites names `{input.mentions}`. "
                "Syntactic and intraprocedural: a check inside a callee or an alias is not seen."
            ),
        )
    else:
        verdict, why = "path_found", f"{len(hops)} hop(s); guards listed per hop, nothing asked."
    return CodePathGuardsOutput(
        verdict=verdict,
        causes=[*causes, *FLOORS],
        explanation=why,
        hops=hops,
        named_at=named_at,
        visited_symbols=visited,
    )
