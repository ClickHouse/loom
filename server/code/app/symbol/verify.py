"""code.verify: a reviewer's claim about a call (subject calls object, or subject
is unused) checked against the edge graph and the definition body.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from server.code.api.models_verify import CodeVerifyInput, CodeVerifyOutput
from server.code.app.common import (
    answer_args,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
    tier_of,
)
from server.code.app.index.ingest_stats import shown_index_error
from server.code.app.index.symbol_forms import natural_name
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.coverage import _ctor_edges_absent, _has_ctor_seed, _scip_coverage
from server.code.app.symbol.edges import _GRAPH_EDGE_KINDS
from server.code.app.symbol.resolve import (
    _FILE_LINE,
    _file_row,
    _resolve_symbols,
    _seed_ids_of,
    near_misses,
    symbol_body,
)
from server.code.domain.index.verdicts import is_abstain, verify_verdict
from server.code.store.index import CodeRepo
from server.code.store.index import prefer_scip_edges as _prefer_scip_edges
from server.core.errors import NotFoundError

_TEMPLATE_ARGS = re.compile(r"<[^<>]*>")


def _normalize_claim_name(name: str) -> str:
    """`Foo<T>::bar<int>(x)` → `Foo::bar`: reviewers write template arguments
    and call parentheses; the symbol table stores neither."""
    n = name.strip()
    for _ in range(3):
        n = _TEMPLATE_ARGS.sub("", n)
    if "(" in n:
        n = n.split("(", 1)[0]
    return n.strip().rstrip(":")


async def _resolve_claim_symbol(
    repo: CodeRepo, org: str, ns: str, name: str
) -> list[dict[str, Any]]:
    """Resolve a claim's subject/object as a reviewer writes it: a symbol name
    (template args and parens tolerated), or `path:line` — the enclosing
    function at that line, which is how lambdas and file-local helpers get
    named in review comments."""
    return await _resolve_symbols(repo, org, ns, symbol_id="", name=_normalize_claim_name(name))


async def _body_complete(repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]) -> bool:
    """Do the indexed chunks cover the subject's whole definition span? The
    span comes from the row that has one (tree-sitter; scip rows are
    single-line). No span, or a gap in chunk coverage, means an absent name
    proves nothing. Fail-closed (False) on any error."""
    spans = [
        (str(s["path"]), int(s.get("start_line") or 0), int(s.get("end_line") or 0))
        for s in seeds
        if int(s.get("end_line") or 0) > int(s.get("start_line") or 0)
    ]
    if not spans:
        return False
    path, start, end = max(spans, key=lambda t: t[2] - t[1])
    try:
        span = await repo.chunk_line_span(org, ns, path, start, end)
    except Exception:  # noqa: BLE001 — an unreadable span proves nothing
        return False
    return span is not None and span[0] <= start and span[1] >= end


async def _subject_bodies(
    repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]
) -> tuple[list[str], bool]:
    """The seeds' definition bodies and whether the chunks cover the definition span, read
    together."""
    return await asyncio.gather(
        asyncio.gather(*(symbol_body(repo, org, ns, s) for s in seeds)),
        _body_complete(repo, org, ns, seeds),
    )


def _name_in_body(body: str, bare: str) -> bool:
    return bool(bare) and re.search(r"\b" + re.escape(bare) + r"\b", body) is not None


async def handle_code_verify(input: CodeVerifyInput) -> CodeVerifyOutput:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    out = await _verify(input, repo, org, ns)

    # One trace row per verdict (a refusal raised NotFoundError above and
    # leaves none, like every other op). Evidence rows carry the paths.
    paths = list(dict.fromkeys(str(e.get("path") or "") for e in out.evidence if e.get("path")))
    not_established = out.verdict == "not_established"
    # The index's own verdict behind a floor. A graph kind ran its walk from resolved
    # seeds (an answer: no static edge); a file kind is an abstain, split by whether the
    # index holds the file at all (one point read, only on this path).
    index_verdict = ""
    if not_established:
        file_row = None
        if input.kind in ("defines", "contains"):
            file_row = await _file_row(repo, org, ns, input.subject)
        index_verdict = verify_verdict(
            kind=input.kind, claim_verdict=out.verdict, file_row=file_row
        )
    abstain = is_abstain(index_verdict)
    risk = "abstain" if abstain else "low"
    route = f"verify_{input.kind}"
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.verify",
        query=f"{input.kind} {input.subject} {input.object}".strip(),
        route=route,
        returned_ids=[out.verdict],
        returned_paths=paths,
        returned_scores=[],
        top_k=1,
        tokens_returned=0,
        abstain=abstain,
        retrieval_risk=risk,
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=index_verdict,
        reason=out.reason,
        args=answer_args(input, "kind", "depth"),
    )
    set_code_span_attributes(
        route=route,
        kind=input.kind,
        verdict=out.verdict,
        reason=out.reason,
        results=len(out.evidence),
        depth=input.depth,
        abstain=abstain,
        retrieval_risk=risk,
        consumer=input.consumer,
    )
    out.trace_run_id = trace_run_id
    out.index_verdict = index_verdict
    return out


BFS_MAX_VISITED = 2000


# near misses a not-found verify subject or object carries
VERIFY_NEAR_MISSES = 5


_BFS_READ_LIMIT = 1000


async def bfs_callees(
    repo: CodeRepo,
    org: str,
    ns: str,
    seed_ids: list[str],
    *,
    want: str,
    want_is_path: bool,
    depth: int,
) -> tuple[list[dict[str, Any]] | None, int, bool]:
    """Bounded forward BFS over `calls` edges, compiler tier preferred per pair. Returns
    (chain to the first match or None, nodes visited, exhausted): `exhausted` is true when
    the walk stopped at the depth or node cap with a frontier still open, or a hop's read
    hit its row limit: a "not found" that is a cap, not an answer. Chain rows carry the
    public keys (from, to, path, line, tier) plus from_symbol_id / to_symbol_id /
    confidence for a keyed follow-up read and `sites`, every call site of that (from, to)
    pair the batch held (a pair with several sites is one hop with several places to
    check); the chain is rebuilt through parent pointers, seed first."""
    parents: dict[str, tuple[str, dict[str, Any]]] = {}
    # every call site of a (from, to) pair the walk saw — the batch already
    # holds them, so a chain row can list the pair's other sites for free
    sites: dict[tuple[str, str], list[dict[str, Any]]] = {}
    visited: set[str] = set(seed_ids)
    frontier = list(seed_ids)
    read_capped = False
    for _hop in range(depth):
        if not frontier or len(visited) > BFS_MAX_VISITED:
            break
        raw = await repo.edges_for_symbols(
            org,
            ns,
            frontier,
            direction="callees",
            edge_kinds=["calls"],
            limit=_BFS_READ_LIMIT,
        )
        # a hop read cut at its limit may have left callees unseen
        read_capped = read_capped or len(raw) >= _BFS_READ_LIMIT
        rows = _prefer_scip_edges(raw)
        frontier = []
        # first pass: every site of every pair in the batch, BEFORE the walk
        # can return on a hit halfway through it
        for r in rows:
            sites.setdefault((str(r["from_symbol_id"]), str(r["to_symbol_id"])), []).append(
                {
                    "path": str(r["path"]),
                    "line": int(r["line"] or 0),
                    "tier": str(r["tier"]),
                    "confidence": float(r.get("confidence") or 0.0),
                }
            )
        for r in rows:
            dst = str(r["to_symbol_id"])
            if dst in visited:
                continue
            visited.add(dst)
            parents[dst] = (str(r["from_symbol_id"]), r)
            name = str(r["to_qualified_name"])
            hit = (
                str(r["path"]).startswith(want.rstrip("/"))
                if want_is_path
                else (name == want or name.endswith("::" + want))
            )
            if hit:
                chain: list[dict[str, Any]] = []
                cur = dst
                while cur in parents:
                    cur, edge = parents[cur]
                    chain.append(
                        {
                            "from": str(edge["from_qualified_name"]),
                            "to": str(edge["to_qualified_name"]),
                            "path": str(edge["path"]),
                            "line": int(edge["line"] or 0),
                            "tier": str(edge["tier"]),
                            "from_symbol_id": str(edge["from_symbol_id"]),
                            "to_symbol_id": str(edge["to_symbol_id"]),
                            "confidence": float(edge.get("confidence") or 0.0),
                            "sites": sorted(
                                sites.get(
                                    (str(edge["from_symbol_id"]), str(edge["to_symbol_id"])), []
                                ),
                                key=lambda x: (x["path"], x["line"]),
                            ),
                        }
                    )
                chain.reverse()
                return chain, len(visited), False
            frontier.append(dst)
    return None, len(visited), bool(frontier) or read_capped


def _no_chain_answer(
    input: CodeVerifyInput, visited: int, exhausted: bool, graph_floors: dict[str, Any]
) -> CodeVerifyOutput:
    """calls/reaches with no chain found. The walk ran out of graph before its bounds: the
    index holds no such chain (still a floor — dynamic dispatch is invisible to both
    tiers). Otherwise the bound cut it."""
    return CodeVerifyOutput(
        verdict="not_established",
        reason="depth_exhausted" if exhausted else "no_path_in_graph",
        explanation=f"no call chain from {input.subject} to "
        f"{input.object} within depth {input.depth}"
        + (
            " — the walk stopped at its bounds; a floor, not a proof of absence."
            if exhausted
            else " — every callee the index holds was walked; a floor, not a proof "
            "of absence (dynamic dispatch is invisible to it)."
        ),
        floors={
            "visited_symbols": visited,
            "depth": input.depth,
            "exhausted": exhausted,
            **graph_floors,
        },
    )


async def _ctor_gap_verdict(
    input: CodeVerifyInput,
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    bare: str,
    *,
    floors: dict[str, Any],
) -> CodeVerifyOutput | None:
    """A `calls` claim whose OBJECT is a constructor, in an index whose tier for the
    subject predates constructor calls through factories: the missing edge has a known
    cause (`std::make_shared<T>(...)` in the body binds no edge yet), so the verdict names
    it instead of the generic "name mentioned, no edge". None when the gap does not
    apply."""
    if not _has_ctor_seed(objects):
        return None
    seed_paths = [str(s.get("path") or "") for s in seeds]
    scip_paths = await _scip_coverage(repo, org, ns, seed_paths, seeds)
    if not await _ctor_edges_absent(repo, org, ns, seed_paths, scip_paths):
        return None
    return CodeVerifyOutput(
        verdict="not_established",
        reason="ctor_edges_absent",
        explanation=f"no call edge from {input.subject} to {input.object}; the object is a "
        "constructor and this index predates constructor calls through factories "
        f"(std::make_shared<T>, std::make_unique<T>) — a construction of `{bare}` in the "
        "subject may bind no edge yet.",
        floors={**floors, "epistemic": "lower_bound", "causes": ["ctor_edges_absent"]},
    )


async def _verify_unused(
    input: CodeVerifyInput,
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    graph_floors: dict[str, str],
) -> CodeVerifyOutput:
    """`unused`: any recorded caller edge refutes; none is never a proof. A constructor
    subject in an index that predates constructor calls through factories says so: the
    class may be constructed where no edge says so."""
    with code_span("verify.edge_lookup", kind=input.kind) as sp:
        rows = _prefer_scip_edges(
            await repo.edges_for_symbols(
                org,
                ns,
                _seed_ids_of(seeds),
                direction="callers",
                edge_kinds=_GRAPH_EDGE_KINDS,
                limit=50,
            )
        )
        span_attrs(sp, edges=len(rows))
    if rows:
        return CodeVerifyOutput(
            verdict="refuted",
            explanation=f"{input.subject} has {len(rows)} recorded caller edge(s) — it is used.",
            evidence=[
                {
                    "from": str(r["from_qualified_name"]),
                    "path": str(r["path"]),
                    "line": int(r["line"] or 0),
                    "tier": str(r["tier"]),
                }
                for r in rows[:10]
            ],
        )
    if _has_ctor_seed(seeds):
        seed_paths = [str(s.get("path") or "") for s in seeds]
        scip_paths = await _scip_coverage(repo, org, ns, seed_paths, seeds)
        if await _ctor_edges_absent(repo, org, ns, seed_paths, scip_paths):
            return CodeVerifyOutput(
                verdict="not_established",
                reason="ctor_edges_absent",
                explanation=f"no caller edges recorded for {input.subject}, a constructor, "
                "in an index that predates constructor calls through factories "
                "(std::make_shared<T>, std::make_unique<T>) — the class may "
                "be constructed where no edge says so; grep the class name.",
                floors={
                    "epistemic": "lower_bound",
                    "causes": ["ctor_edges_absent"],
                    **graph_floors,
                },
            )
    return CodeVerifyOutput(
        verdict="not_established",
        reason="no_caller_edges",
        explanation=f"no caller edges recorded for {input.subject} — "
        "'unused' can never be confirmed from a floor graph.",
        floors=graph_floors,
    )


async def _symbol_not_found(
    repo: CodeRepo, org: str, ns: str, name: str, role: str
) -> NotFoundError:
    """The refusal for a claim name that resolves nothing: code.symbol's near misses
    ride it as `did_you_mean`, so the claim can be re-checked under the right name."""
    near = await near_misses(repo, org, ns, name, limit=VERIFY_NEAR_MISSES)
    # Suggest the spelling a caller writes; it binds back to the same row.
    for r in near:
        spelled = natural_name(str(r["qualified_name"]))
        if spelled != r["qualified_name"]:
            r["write_as"] = spelled
    hint = ", ".join(dict.fromkeys(str(r.get("write_as") or r["qualified_name"]) for r in near))
    return NotFoundError(
        f"{role} symbol not found: {name}" + (f" — did you mean: {hint}" if hint else ""),
        role=role,
        did_you_mean=near,
    )


async def _verify(input: CodeVerifyInput, repo: CodeRepo, org: str, ns: str) -> CodeVerifyOutput:
    # The standing blind spots every graph verdict inherits. Stated once, attached to every
    # not_established, so a zero can never be read as "none exists".
    graph_floors = {
        "edges_are_name_matched": "dynamic dispatch through function "
        "pointers/virtual tables is invisible to both tiers",
        "treesitter_drops_ambiguous_callers": "cross-file callers of "
        "multi-overload names exist only in the scip tier",
    }

    async def _need_symbol(name: str, role: str) -> list[dict[str, Any]]:
        with code_span(f"verify.resolve_{role}") as sp:
            rows = await _resolve_claim_symbol(repo, org, ns, name)
            span_attrs(sp, results=len(rows), tier=tier_of(rows))
        if not rows:
            # A refusal is not a verdict of zero: an unresolvable name means the claim
            # cannot even be evaluated. Suggest, don't guess.
            raise await _symbol_not_found(repo, org, ns, name, role)
        return rows

    async def _bfs_callees(
        seed_ids: list[str], *, want: str, want_is_path: bool
    ) -> tuple[list[dict[str, Any]] | None, int, bool]:
        chain, visited, exhausted = await bfs_callees(
            repo,
            org,
            ns,
            seed_ids,
            want=want,
            want_is_path=want_is_path,
            depth=input.depth,
        )
        if chain is None:
            return None, visited, exhausted
        # the verify evidence row is the five public keys; the ids ride the
        # richer row for code.path_guards
        return (
            [{k: h[k] for k in ("from", "to", "path", "line", "tier")} for h in chain],
            visited,
            False,
        )

    if input.kind in ("calls", "reaches"):
        if not input.object:
            raise NotFoundError(f"{input.kind} needs an object")
        seeds = await _need_symbol(input.subject, "subject")
        want_is_path = input.kind == "reaches" and "/" in input.object
        objects: list[dict[str, Any]] = []
        if not want_is_path:
            objects = await _need_symbol(input.object, "object")  # refuse on typos
        want = input.object if want_is_path else _normalize_claim_name(input.object)
        # `path:line` objects: match by the resolved name, not the literal
        if not want_is_path and objects and _FILE_LINE.match(input.object.strip()):
            want = str(objects[0]["qualified_name"])
        with code_span("verify.edge_lookup", kind=input.kind, depth=input.depth) as sp:
            chain, visited, exhausted = await _bfs_callees(
                _seed_ids_of(seeds),
                want=want,
                want_is_path=want_is_path,
            )
            span_attrs(sp, results=visited, edges=len(chain or []))
        if chain is not None:
            return CodeVerifyOutput(
                verdict="confirmed",
                explanation=f"{input.subject} reaches {input.object} in "
                f"{len(chain)} hop(s); the chain is the evidence.",
                evidence=chain,
            )
        # Distinguish "no such edge" from "cannot tell". A direct-call claim
        # is refuted when the subject is compiler-indexed (scip), the object
        # resolved, and the object's bare name does not occur anywhere in the
        # subject's definition text — no edge AND no mention leaves nothing
        # for a macro, template, lambda or dispatch to hide behind. A mention
        # without an edge stays not_established, with the reason.
        if input.kind == "calls" and objects:
            bare = want.split("::")[-1]
            with code_span("verify.body_check", seeds=min(len(seeds), 3)) as sp:
                bodies, complete = await _subject_bodies(repo, org, ns, seeds[:3])
                mentioned = any(_name_in_body(b, bare) for b in bodies)
                span_attrs(sp, results=sum(1 for b in bodies if b), stale=not complete)
            scip_subject = any(str(s.get("tier")) == "scip" for s in seeds)
            # A refutation is about the DIRECT call only: the lexical check reads
            # the subject's own body, which says nothing about intermediates, so
            # the verdict text says "directly" and the transitive absence stays
            # a floor. It also needs a subject the caller named precisely: a
            # bare name can resolve to a homonym (`optimizeTree` → the
            # AccessRights one) while the intended function is unindexed.
            subj = input.subject.strip()
            qualified = "::" in subj or bool(_FILE_LINE.match(subj))
            if not qualified:
                return CodeVerifyOutput(
                    verdict="not_established",
                    reason="no_edge_subject_unqualified",
                    explanation=f"no call edge from {input.subject} to {input.object}; the "
                    f"subject was given as a bare name and resolved to "
                    f"{', '.join(str(s['qualified_name']) for s in seeds[:3])} — qualify it "
                    "(Class::method or path:line) before treating absence as evidence.",
                    floors={"visited_symbols": visited, "depth": input.depth, **graph_floors},
                )
            if scip_subject and any(bodies) and complete and not mentioned:
                return CodeVerifyOutput(
                    verdict="refuted",
                    reason="no_edge_no_mention",
                    explanation=f"{input.subject} does not directly call {input.object}: no "
                    f"edge in either tier, no chain within depth {input.depth}, and `{bare}` "
                    f"does not appear anywhere in the subject's definition text (transitive "
                    "reachability beyond that depth remains unverified).",
                    evidence=[
                        {
                            "subject": str(s["qualified_name"]),
                            "path": str(s["path"]),
                            "tier": str(s.get("tier") or ""),
                            "body_chars": len(b),
                        }
                        for s, b in zip(seeds[:3], bodies, strict=False)
                    ],
                )
            if mentioned or not complete:
                gap = await _ctor_gap_verdict(
                    input,
                    repo,
                    org,
                    ns,
                    seeds[:3],
                    objects,
                    bare,
                    floors={"visited_symbols": visited, "depth": input.depth, **graph_floors},
                )
                if gap is not None:
                    return gap
            if mentioned:
                reason, why = (
                    "no_edge_name_mentioned",
                    (
                        f"`{bare}` appears in the subject's text but no resolved edge exists — "
                        "macro, template instantiation, lambda body, or dynamic dispatch"
                    ),
                )
            elif not scip_subject:
                reason, why = (
                    "no_edge_subject_not_compiler_indexed",
                    (
                        "subject is not compiler-indexed (tree-sitter only), "
                        "so absence is not evidence"
                    ),
                )
            else:
                reason, why = (
                    "no_edge_body_incomplete",
                    (
                        "the subject's definition text is not fully indexed, so the name's absence "
                        "proves nothing"
                    ),
                )
            return CodeVerifyOutput(
                verdict="not_established",
                reason=reason,
                explanation=f"no call chain from {input.subject} to {input.object} within "
                f"depth {input.depth}; {why}.",
                floors={"visited_symbols": visited, "depth": input.depth, **graph_floors},
            )
        return _no_chain_answer(input, visited, exhausted, graph_floors)

    if input.kind == "unused":
        seeds = await _need_symbol(input.subject, "subject")
        return await _verify_unused(input, repo, org, ns, seeds, graph_floors)

    if input.kind == "defines":
        return await _verify_defines(input, repo, org, ns)
    return await _verify_contains(input, repo, org, ns)


async def _verify_defines(
    input: CodeVerifyInput, repo: CodeRepo, org: str, ns: str
) -> CodeVerifyOutput:
    """`defines`: the subject file's symbol table names the object (confirmed), is clean
    and does not (refuted), or cannot say (not indexed, parse error)."""
    if not input.object:
        raise NotFoundError("defines needs an object symbol")
    want = input.object.split("::")[-1]
    with code_span("verify.resolve_subject", kind=input.kind):
        for tier in ("scip", "treesitter"):
            for row in await repo.live_symbols_for_path(org, ns, input.subject, tier):
                qn = str(row["qualified_name"])
                if str(row["name"]) == want and (
                    qn == input.object
                    or qn.endswith("::" + input.object)
                    or "::" not in input.object
                ):
                    return CodeVerifyOutput(
                        verdict="confirmed",
                        explanation=f"{input.subject} defines {qn} at line "
                        f"{int(row['start_line'] or 0)}.",
                        evidence=[
                            {
                                "qualified_name": qn,
                                "path": input.subject,
                                "line": int(row["start_line"] or 0),
                                "kind": str(row["kind"]),
                                "tier": str(row["tier"]),
                            }
                        ],
                    )
        files = await repo.live_files_for_path(org, ns, input.subject)
    f = files[0] if files else None
    if f is not None and not str(f.get("index_error") or ""):
        return CodeVerifyOutput(
            verdict="refuted",
            explanation=f"{input.subject} is indexed clean "
            f"({int(f.get('symbol_count') or 0)} symbols) and none of "
            f"them is {input.object}.",
            evidence=[{"path": input.subject, "symbol_count": int(f.get("symbol_count") or 0)}],
        )
    return CodeVerifyOutput(
        verdict="not_established",
        reason="subject_not_indexed" if f is None else "subject_parse_error",
        explanation=f"{input.subject} is "
        + ("not indexed" if f is None else "indexed with a parse error")
        + " — its symbol table cannot refute the claim.",
        floors={
            "indexed": f is not None,
            "index_error": shown_index_error(str((f or {}).get("index_error") or "")),
        },
    )


async def _verify_contains(
    input: CodeVerifyInput, repo: CodeRepo, org: str, ns: str
) -> CodeVerifyOutput:
    """`contains`: the literal in the subject's indexed chunks confirms; its absence
    cannot refute, chunk coverage not being byte-complete."""
    if not input.object:
        raise NotFoundError("contains needs an object literal")
    with code_span("verify.body_check", kind=input.kind) as sp:
        hits = await repo.chunk_contains(org, ns, input.subject, input.object)
        span_attrs(sp, results=len(hits))
    if hits:
        return CodeVerifyOutput(
            verdict="confirmed",
            explanation=f"literal found in {input.subject} ({len(hits)} chunk(s)).",
            evidence=[
                {
                    "start_line": int(h["start_line"] or 0),
                    "scope": str(h["scope"] or ""),
                    "excerpt": str(h["excerpt"] or ""),
                }
                for h in hits
            ],
        )
    return CodeVerifyOutput(
        verdict="not_established",
        reason="no_chunk_match",
        explanation=f"literal not found in {input.subject}'s indexed "
        "chunks — chunk coverage is not byte-complete, so absence here "
        "cannot refute.",
        floors={
            "chunk_coverage": "functions/class skeletons/comments/"
            "file headers; not every byte of the file"
        },
    )
