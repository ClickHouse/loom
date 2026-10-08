"""code.test_gate: the tests a diff reaches through the callers graph, credited to
the touched symbols they were reached from.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from server.code.api.models_testing import CodeTestGateInput, CodeTestGateOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.coverage import (
    _declined_safe,
    _env_fields,
    _graph_paths,
    _scip_coverage,
    _unindexed_paths,
)
from server.code.app.symbol.diff import _parse_unified_diff_full, _touched_rows_for_diff
from server.code.app.symbol.edges import _GRAPH_EDGE_KINDS, _hop_counts
from server.code.domain.index.epistemic import envelope as _envelope
from server.code.store.index import CodeRepo
from server.code.store.index import prefer_scip_edges as _prefer_scip_edges
from server.core.errors import ValidationError
from server.settings import settings

_GATE_READ_LIMIT = 300


async def _expand_impact_with_roots(
    repo: CodeRepo, org: str, ns: str, root_of: dict[str, str], depth: int
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Like _expand_impact, but every reached node remembers WHICH touched symbols it is
    reachable from (`roots`), so a test reached in the walk can be credited to the specific
    touched symbols it covers: the per-symbol fact the untested complement needs. `root_of`
    maps each seed id (every tier's, see `_seed_ids_of`) to its touched symbol. Depth <= 3.
    Returns (impacted, capped): capped when a hop's read hit its row limit."""

    roots_of: dict[str, set[str]] = {sid: {root} for sid, root in root_of.items()}
    impacted: dict[str, dict[str, Any]] = {}
    frontier = list(root_of)
    capped = False
    for hop in range(max(1, min(depth, 3))):
        if not frontier:
            break
        rows = await repo.edges_for_symbols(
            org,
            ns,
            frontier,
            direction="callers",
            edge_kinds=_GRAPH_EDGE_KINDS,
            limit=_GATE_READ_LIMIT,
        )
        capped = capped or len(rows) >= _GATE_READ_LIMIT
        rows = _prefer_scip_edges(rows)
        nxt: list[str] = []
        for r in rows:
            sid, to = str(r["from_symbol_id"]), str(r["to_symbol_id"])
            inherited = set(roots_of.get(to) or ())
            if sid in roots_of and sid not in impacted:
                continue  # a seed calling a seed
            if sid not in impacted:
                impacted[sid] = {
                    "qualified_name": str(r["from_qualified_name"]),
                    "path": str(r["path"]),
                    "hop": hop + 1,
                    "tier": str(r["tier"]),
                }
                nxt.append(sid)
            merged = roots_of.setdefault(sid, set())
            merged |= inherited
            impacted[sid]["roots"] = sorted(merged)
        frontier = nxt
    return impacted, capped


async def handle_code_test_gate(input: CodeTestGateInput) -> CodeTestGateOutput:
    """Pre-PR obligations for a diff: tests to run (evidence-ordered, with a
    run command when derivable) and the touched symbols no test reaches.
    See code/domain/index/test_gate.py for the ordering rules."""
    from server.code.domain.index.test_gate import GateInputs, build_gate, is_test_path

    require_code_intel()
    if not (input.files or input.diff):
        raise ValidationError("code.test_gate needs files and/or a unified diff")
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    with code_span("test_gate.diff_parse") as sp:
        hunks, header_names = _parse_unified_diff_full(input.diff) if input.diff else ({}, {})
        span_attrs(sp, results=len(hunks))
    changed = list(dict.fromkeys([*input.files, *hunks.keys()]))[:200]
    src_paths = [p for p in changed if not is_test_path(p)]

    with code_span("test_gate.functions_for_paths", seeds=len(src_paths)) as sp:
        symbol_rows = (
            await repo.functions_for_paths(org, ns, src_paths, limit=400) if src_paths else []
        )
        span_attrs(sp, results=len(symbol_rows))
    rows, seed_ids = _touched_rows_for_diff(symbol_rows, hunks, header_names)
    # every seed id credits the touched symbol it stands for
    root_of = {sid: str(r["symbol_id"]) for r in rows for sid in r["tier_ids"]}
    touched = sorted(
        (
            {
                "symbol_id": str(r["symbol_id"]),
                "qualified_name": str(r["qualified_name"]),
                "path": str(r["path"]),
                "start_line": int(r["start_line"] or 0),
                "tier": str(r["tier"]),
            }
            for r in rows
        ),
        key=lambda r: (r["path"], r["start_line"]),
    )

    with code_span("test_gate.hop_walk", seeds=len(seed_ids), depth=input.depth) as sp:
        known_paths, bare_paths = _graph_paths(src_paths, symbol_rows)
        (
            (impacted, capped),
            (unresolved_n, ambiguous_n),
            scip_paths,
            unindexed,
        ) = await asyncio.gather(
            _expand_impact_with_roots(repo, org, ns, root_of, input.depth)
            if seed_ids
            else _empty_impacted(),
            _declined_safe(repo, org, ns, touched),
            _scip_coverage(repo, org, ns, known_paths, symbol_rows),
            _unindexed_paths(repo, org, ns, bare_paths),
        )
        span_attrs(sp, results=len(impacted))
    with code_span("test_gate.tests", seeds=len(src_paths)) as sp:
        test_map = await repo.test_map_for(org, ns, src_paths, limit=200) if src_paths else []
        span_attrs(sp, results=len(test_map))

    gate = build_gate(
        GateInputs(
            changed_paths=changed,
            touched=touched,
            impacted=impacted,
            test_map=test_map,
            run_overrides_json=str(settings.code_test_gate_run_templates),
            limit=input.limit,
        )
    )

    by_hop_counts = _hop_counts(impacted)
    env = _envelope(
        unresolved=unresolved_n,
        ambiguous=ambiguous_n,
        seed_paths=[*known_paths, *sorted(unindexed)],
        scip_paths=scip_paths,
        unindexed_paths=unindexed,
        truncated=capped,
    )
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.test_gate",
        query=",".join(changed) or "diff",
        route="test_gate",
        returned_ids=[],
        returned_paths=[t["test_path"] for t in gate.tests],
        returned_scores=[],
        top_k=input.limit,
        tokens_returned=0,
        abstain=not gate.obligations,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route="test_gate",
        results=len(gate.tests),
        seeds=len(seed_ids),
        depth=input.depth,
        abstain=not gate.obligations,
        consumer=input.consumer,
    )
    return CodeTestGateOutput(
        tests=gate.tests,
        untested=gate.untested,
        changed_files=changed,
        touched_symbols=[{k: v for k, v in t.items() if k != "symbol_id"} for t in touched][:200],
        obligations=gate.obligations,
        tests_total=gate.tests_total,
        untested_total=gate.untested_total,
        tests_capped=gate.tests_total > len(gate.tests),
        untested_capped=gate.untested_total > len(gate.untested),
        by_hop_counts=by_hop_counts,
        **_env_fields(env),
        trace_run_id=trace_run_id,
    )


async def _empty_impacted() -> tuple[dict[str, dict[str, Any]], bool]:
    return {}, False
