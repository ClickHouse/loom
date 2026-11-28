"""code.test_gate: the tests the index links to a diff's touched definitions, each row
under its evidence, and the touched definitions no test is linked to.
"""

from __future__ import annotations

import asyncio
import time

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
    _graph_paths,
    _scip_coverage,
    _unindexed_paths,
)
from server.code.app.symbol.diff import (
    DEFINITION_KINDS,
    _parse_unified_diff_full,
    _touched_rows_for_diff,
)
from server.code.app.symbol.edges import _hop_counts
from server.code.app.symbol.test_links import link_tests
from server.code.domain.index.epistemic import CAUSE_NO_COVERAGE, CAUSE_UNMAPPED
from server.code.domain.index.epistemic import envelope as _envelope
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.settings import settings

_GATE_READ_LIMIT = 300


async def handle_code_test_gate(input: CodeTestGateInput) -> CodeTestGateOutput:
    """Pre-PR obligations for a diff: tests to run (evidence-ordered, with a run command
    when derivable), the touched definitions a coverage run measured unexecuted and the
    ones the index links no test to. See code/domain/index/test_gate.py for the rules."""
    from server.code.domain.index.test_gate import GateInputs, build_gate, is_test_path

    require_code_intel()
    if not (input.files or input.diff):
        raise ValidationError("code.test_gate needs files and/or a unified diff")
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    with code_span("test_gate.diff_parse") as sp:
        hunks, _, changes = _parse_unified_diff_full(input.diff) if input.diff else ({}, {}, {})
        span_attrs(sp, results=len(hunks))
    changed = list(dict.fromkeys([*input.files, *hunks.keys()]))[:200]
    src_paths = [p for p in changed if not is_test_path(p)]

    with code_span("test_gate.functions_for_paths", seeds=len(src_paths)) as sp:
        symbol_rows = (
            await repo.functions_for_paths(org, ns, src_paths, limit=2000, kinds=DEFINITION_KINDS)
            if src_paths
            else []
        )
        span_attrs(sp, results=len(symbol_rows))
    rows, seed_ids, unmapped = _touched_rows_for_diff(symbol_rows, changes)
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
            links,
            (unresolved_n, ambiguous_n),
            scip_paths,
            unindexed,
        ) = await asyncio.gather(
            link_tests(
                repo,
                org,
                ns,
                rows,
                symbol_rows,
                depth=input.depth,
                read_limit=_GATE_READ_LIMIT,
                neighbors=input.limit,
            ),
            _declined_safe(repo, org, ns, touched),
            _scip_coverage(repo, org, ns, known_paths, symbol_rows),
            _unindexed_paths(repo, org, ns, bare_paths),
        )
        impacted = links.impacted
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
            neighbors=links.neighbors,
            measured=links.measured,
            coverage=links.coverage,
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
        truncated=links.capped,
    )
    # The graph is never proof that no test runs a definition: the answer is `measured`
    # only when a coverage run settled every touched definition and every changed line sat
    # in one.
    causes = [
        *env.causes,
        *([CAUSE_UNMAPPED] if unmapped else []),
        *([CAUSE_NO_COVERAGE] if gate.unmeasured else []),
    ]
    measured = bool(touched) and not unmapped and not gate.unmeasured
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
        unlinked=gate.unlinked,
        changed_files=changed,
        touched_symbols=[{k: v for k, v in t.items() if k != "symbol_id"} for t in touched][:200],
        obligations=gate.obligations,
        tests_total=gate.tests_total,
        untested_total=gate.untested_total,
        unlinked_total=gate.unlinked_total,
        tests_capped=gate.tests_total > len(gate.tests),
        untested_capped=gate.untested_total > len(gate.untested),
        unlinked_capped=gate.unlinked_total > len(gate.unlinked),
        by_hop_counts=by_hop_counts,
        epistemic="measured" if measured else "lower_bound",
        causes=[] if measured else causes,
        trace_run_id=trace_run_id,
    )
