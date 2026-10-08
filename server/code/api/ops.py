"""The code.* registry: one `Operation` per op, in the order the wire lists them.

The only module here that imports handlers; the model modules stay free of them.
"""

from __future__ import annotations

from server.code.api.models_context import (
    CodeContextPackInput,
    CodeContextPackOutput,
    CodeContextPacksInput,
    CodeContextPacksOutput,
    CodeMapInput,
    CodeMapOutput,
)
from server.code.api.models_conventions import (
    CodeConventionsInput,
    CodeConventionsOutput,
    CodeSettingFactsInput,
    CodeSettingFactsOutput,
)
from server.code.api.models_grep import (
    CodeFilesInput,
    CodeFilesOutput,
    CodeGrepInput,
    CodeGrepOutput,
)
from server.code.api.models_history import (
    CodeBackportGapInput,
    CodeBackportGapOutput,
    CodeBackportGapsInput,
    CodeBackportGapsOutput,
    CodeBisectCandidatesInput,
    CodeBisectCandidatesOutput,
    CodeBlameRangeInput,
    CodeBlameRangeOutput,
    CodeFirstBuildInput,
    CodeFirstBuildOutput,
    CodeHistoryInput,
    CodeHistoryOutput,
    CodeSymbolHistoryInput,
    CodeSymbolHistoryOutput,
)
from server.code.api.models_ingest import (
    CodeCalibrateInput,
    CodeCalibrateOutput,
    CodeRefreshInput,
    CodeRefreshOutput,
    CodeReindexInput,
    CodeReindexOutput,
    CodeScipIngestInput,
    CodeScipIngestOutput,
    CodeScipStatusInput,
    CodeScipStatusOutput,
)
from server.code.api.models_navigation import (
    CodeEnclosingInput,
    CodeEnclosingOutput,
    CodeExamplesInput,
    CodeExamplesOutput,
    CodeOutlineInput,
    CodeOutlineOutput,
    CodeTraceInput,
    CodeTraceOutput,
)
from server.code.api.models_outcome import CodeOutcomeInput, CodeOutcomeOutput
from server.code.api.models_repos import (
    CodeRepoAddBranchInput,
    CodeRepoAddInput,
    CodeRepoAddOutput,
    CodeRepoRemoteBranchesInput,
    CodeRepoRemoteBranchesOutput,
    CodeRepoRemoveInput,
    CodeRepoRemoveOutput,
    CodeRepoSetAccessInput,
    CodeRepoSetAccessOutput,
    CodeRepoSetSourceInput,
    CodeRepoSetSourceOutput,
    CodeReposInput,
    CodeReposOutput,
)
from server.code.api.models_review import (
    CodePrCheckInput,
    CodePrCheckOutput,
    CodeReviewBriefInput,
    CodeReviewBriefOutput,
)
from server.code.api.models_search import (
    CodeAskInput,
    CodeAskOutput,
    CodeSearchInput,
    CodeSearchOutput,
)
from server.code.api.models_status import (
    CodeCoverageInput,
    CodeCoverageOutput,
    CodeIndexErrorsInput,
    CodeIndexErrorsOutput,
    CodeIndexStatusInput,
    CodeIndexStatusOutput,
    CodeRecentCommitsInput,
    CodeRecentCommitsOutput,
)
from server.code.api.models_symbol import (
    CodeCallersInput,
    CodeCallersOutput,
    CodeImpactInput,
    CodeImpactOutput,
    CodeSymbolInput,
    CodeSymbolOutput,
)
from server.code.api.models_testing import (
    CodeCoverageClaimInput,
    CodeCoverageClaimOutput,
    CodeTestGateInput,
    CodeTestGateOutput,
    CodeTestHomeInput,
    CodeTestHomeOutput,
    CodeTestsForInput,
    CodeTestsForOutput,
)
from server.code.api.models_verify import (
    CodePathGuardsInput,
    CodePathGuardsOutput,
    CodeVerifyCitationsInput,
    CodeVerifyCitationsOutput,
    CodeVerifyInput,
    CodeVerifyOutput,
)
from server.code.app.admin import handle_code_calibrate, handle_code_reindex
from server.code.app.ask import handle_code_ask
from server.code.app.backport import handle_code_backport_gap
from server.code.app.backport_gaps import handle_code_backport_gaps
from server.code.app.bisect import handle_code_bisect_candidates
from server.code.app.blame import handle_code_blame_range
from server.code.app.citations import handle_code_verify_citations
from server.code.app.context import (
    handle_code_context_pack,
    handle_code_context_packs,
    handle_code_map,
)
from server.code.app.conventions import handle_code_conventions
from server.code.app.coverage import (
    handle_code_coverage_claim,
    handle_code_test_home,
    handle_code_tests_for,
)
from server.code.app.files import handle_code_files
from server.code.app.first_build import handle_code_first_build
from server.code.app.grep import handle_code_grep
from server.code.app.index_status import (
    handle_code_coverage,
    handle_code_index_errors,
    handle_code_index_status,
    handle_code_recent_commits,
)
from server.code.app.ingest import (
    handle_code_refresh,
    handle_code_scip_ingest,
    handle_code_scip_status,
)
from server.code.app.outcome import handle_code_outcome
from server.code.app.path_guards import handle_code_path_guards
from server.code.app.pr_check import handle_code_pr_check
from server.code.app.repo_admin import (
    handle_code_repo_add,
    handle_code_repo_add_branch,
    handle_code_repo_remote_branches,
    handle_code_repo_remove,
    handle_code_repo_set_access,
    handle_code_repo_set_source,
)
from server.code.app.repos import handle_code_repos
from server.code.app.review_brief import handle_code_review_brief
from server.code.app.search.handler import handle_code_search
from server.code.app.settings import handle_code_setting_facts
from server.code.app.symbol import (
    handle_code_callees,
    handle_code_callers,
    handle_code_enclosing,
    handle_code_examples,
    handle_code_history,
    handle_code_impact,
    handle_code_outline,
    handle_code_symbol,
    handle_code_test_gate,
    handle_code_trace,
    handle_code_verify,
)
from server.code.app.symbol_history import handle_code_symbol_history
from server.operations.base import Operation, Role

OPERATIONS: list[Operation] = [
    Operation(
        name="code.search",
        input_model=CodeSearchInput,
        output_model=CodeSearchOutput,
        handler=handle_code_search,
        idempotent=True,
        # Priced per shape with bench/code_search_stage_latency.py (`live --handler`, the
        # whole op in process with the real embedder): a shape's budget is its sampled
        # max x ~1.6. The base covers the exact symbol route and every answer before the
        # legs; `hybrid` runs the legs without a model call, `hybrid_hyde` with one.
        budget_p99_ms=400,
        shape_budgets_p99_ms={"hybrid": 1500, "hybrid_hyde": 6000},
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.ask",
        input_model=CodeAskInput,
        output_model=CodeAskOutput,
        handler=handle_code_ask,
        idempotent=True,
        # Cache hits are ~ms; a synthesis miss pays retrieval + one LLM call.
        budget_p99_ms=8000,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.symbol",
        input_model=CodeSymbolInput,
        output_model=CodeSymbolOutput,
        handler=handle_code_symbol,
        idempotent=True,
        budget_p99_ms=250,
        # A lookup ranked by the change at hand (near_paths / pr_number) reads the
        # PR's files and the candidates' neighbourhoods; the compact uses block adds a
        # mention leg. Each is priced from its served p99.
        shape_budgets_p99_ms={"ranked": 3000, "uses_brief": 1500},
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.callers",
        input_model=CodeCallersInput,
        output_model=CodeCallersOutput,
        handler=handle_code_callers,
        idempotent=True,
        budget_p99_ms=400,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.callees",
        maturity="experimental",
        input_model=CodeCallersInput,
        output_model=CodeCallersOutput,
        handler=handle_code_callees,
        idempotent=True,
        budget_p99_ms=400,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.impact",
        input_model=CodeImpactInput,
        output_model=CodeImpactOutput,
        handler=handle_code_impact,
        idempotent=True,
        # Two edge hops + test map + PR co-change.
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.test_gate",
        maturity="experimental",
        input_model=CodeTestGateInput,
        output_model=CodeTestGateOutput,
        handler=handle_code_test_gate,
        idempotent=True,
        # Up to three edge hops + test map; same ceiling as code.impact.
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.test_home",
        maturity="experimental",
        input_model=CodeTestHomeInput,
        output_model=CodeTestHomeOutput,
        handler=handle_code_test_home,
        idempotent=True,
        # up to ~30 prefix-scoped grep_files reads run together + the test map
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.bisect_candidates",
        maturity="experimental",
        input_model=CodeBisectCandidatesInput,
        output_model=CodeBisectCandidatesOutput,
        handler=handle_code_bisect_candidates,
        idempotent=True,
        # ≤12 symbol lookups gathered + two bounded tracker reads (300 ms each)
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.backport_gap",
        maturity="experimental",
        input_model=CodeBackportGapInput,
        output_model=CodeBackportGapOutput,
        handler=handle_code_backport_gap,
        idempotent=True,
        # two bounded tracker reads + ≤ 9 ledger reads gathered, then ≤ 8 presence
        # fan-outs over ≤ 8 branch namespaces under a 1.2 s wall; `history` (on request)
        # adds git reads on the mirror under an 8 s wall — the nightly worker's shape,
        # not the default's
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.backport_gaps",
        maturity="experimental",
        input_model=CodeBackportGapsInput,
        output_model=CodeBackportGapsOutput,
        handler=handle_code_backport_gaps,
        idempotent=True,
        # one range read on loom's own table plus one stats read
        budget_p99_ms=500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.blame_range",
        maturity="experimental",
        input_model=CodeBlameRangeInput,
        output_model=CodeBlameRangeOutput,
        handler=handle_code_blame_range,
        idempotent=True,
        # window refs (≤4 cheap git reads + ≤2 state reads), then one blame
        # (≤400 lines, 5 s cap, cached 10 min) beside one rev-list and two
        # ledger reads, gathered; optional bounded tracker reads
        budget_p99_ms=6000,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.setting_facts",
        maturity="experimental",
        input_model=CodeSettingFactsInput,
        output_model=CodeSettingFactsOutput,
        handler=handle_code_setting_facts,
        idempotent=True,
        # two regex greps over src/, one whole-file read (cached 10 min),
        # one excerpt read, one grep count — gathered
        budget_p99_ms=1200,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.conventions",
        maturity="experimental",
        input_model=CodeConventionsInput,
        output_model=CodeConventionsOutput,
        handler=handle_code_conventions,
        idempotent=True,
        # one ledger point read + one bounded `git diff` on the mirror, then
        # one gathered round: history chunks (cached), ≤4 docs greps, ≤30
        # file reads, two code_files reads, ≤3 census reads (cached), and
        # the wide-caller leg (functions, one edge read, ≤5 coverage greps)
        budget_p99_ms=4000,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.symbol_history",
        maturity="experimental",
        input_model=CodeSymbolHistoryInput,
        output_model=CodeSymbolHistoryOutput,
        handler=handle_code_symbol_history,
        idempotent=True,
        # symbol resolve + two loom reads gathered + items + up to three
        # bounded tracker reads (300 ms each, two gathered)
        budget_p99_ms=1800,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.first_build",
        maturity="experimental",
        input_model=CodeFirstBuildInput,
        output_model=CodeFirstBuildOutput,
        handler=handle_code_first_build,
        idempotent=True,
        # one ledger point read beside two cheap git reads, then two bounded
        # tracker reads gathered (900 ms each, cached), then ≤ 10 is-ancestor calls
        budget_p99_ms=2500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.coverage_claim",
        maturity="experimental",
        input_model=CodeCoverageClaimInput,
        output_model=CodeCoverageClaimOutput,
        handler=handle_code_coverage_claim,
        idempotent=True,
        budget_p99_ms=1200,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.review_brief",
        maturity="experimental",
        input_model=CodeReviewBriefInput,
        output_model=CodeReviewBriefOutput,
        handler=handle_code_review_brief,
        idempotent=True,
        # one FINAL point read on the brief's sort-key prefix; a miss builds
        # the brief (one impact read seeding ≤ 20 bounded op reads, gathered)
        # and stores it — the build path is the ingest's, not the read's
        budget_p99_ms=8000,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.grep",
        input_model=CodeGrepInput,
        output_model=CodeGrepOutput,
        handler=handle_code_grep,
        idempotent=True,
        # Priced per mode (the handler notes the shape) with bench/code_grep_handler.py,
        # real calls through handle_code_grep in process, caches warm and cold (the
        # replica-restart worst case). `message` also ranks templates in Python.
        budget_p99_ms=600,
        shape_budgets_p99_ms={"files": 600, "message": 2000},
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.tests_for",
        maturity="experimental",
        input_model=CodeTestsForInput,
        output_model=CodeTestsForOutput,
        handler=handle_code_tests_for,
        idempotent=True,
        # one keyed range read over code_test_facets per (kind, name) prefix
        budget_p99_ms=400,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.files",
        maturity="experimental",
        input_model=CodeFilesInput,
        output_model=CodeFilesOutput,
        handler=handle_code_files,
        idempotent=True,
        # one keyed read over code_files (~10k rows per namespace)
        budget_p99_ms=400,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.enclosing",
        input_model=CodeEnclosingInput,
        output_model=CodeEnclosingOutput,
        handler=handle_code_enclosing,
        idempotent=True,
        # <=200 locations -> <=10 batched symbol-table reads
        budget_p99_ms=800,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.outline",
        maturity="experimental",
        input_model=CodeOutlineInput,
        output_model=CodeOutlineOutput,
        handler=handle_code_outline,
        idempotent=True,
        # one symbol-table read served by the path bloom index + as_of
        budget_p99_ms=300,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.verify",
        input_model=CodeVerifyInput,
        output_model=CodeVerifyOutput,
        handler=handle_code_verify,
        idempotent=True,
        # calls/reaches run a bounded BFS (depth<=6, <=2000 nodes).
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.verify_citations",
        maturity="experimental",
        input_model=CodeVerifyCitationsInput,
        output_model=CodeVerifyCitationsOutput,
        handler=handle_code_verify_citations,
        idempotent=True,
        # keyed reads: file rows + symbol spans + chunk spans per cited path
        # (batched), items by number, one name lookup per qualified name
        # (<=8 gathered); the optional mirror leg is capped at 20 calls / 5 s
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.path_guards",
        maturity="experimental",
        input_model=CodePathGuardsInput,
        output_model=CodePathGuardsOutput,
        handler=handle_code_path_guards,
        idempotent=True,
        # the verify(reaches) BFS (depth<=4) plus one keyed read of the
        # guards of every hop and, on a miss, one namespace probe
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.trace",
        input_model=CodeTraceInput,
        output_model=CodeTraceOutput,
        handler=handle_code_trace,
        idempotent=True,
        # Frame parse is local; one batched symbol resolve per frame set.
        budget_p99_ms=800,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.context_pack",
        input_model=CodeContextPackInput,
        output_model=CodeContextPackOutput,
        handler=handle_code_context_pack,
        idempotent=True,
        # A task briefing is one code.search plus four bounded reads —
        # 0.7-1.5 s cold; the PR and map paths stay ~0.2 s
        budget_p99_ms=2000,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.examples",
        maturity="experimental",
        input_model=CodeExamplesInput,
        output_model=CodeExamplesOutput,
        handler=handle_code_examples,
        idempotent=True,
        budget_p99_ms=500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.history",
        input_model=CodeHistoryInput,
        output_model=CodeHistoryOutput,
        handler=handle_code_history,
        idempotent=True,
        budget_p99_ms=300,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.pr_check",
        maturity="experimental",
        input_model=CodePrCheckInput,
        output_model=CodePrCheckOutput,
        handler=handle_code_pr_check,
        idempotent=True,
        budget_p99_ms=600,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.map",
        maturity="experimental",
        input_model=CodeMapInput,
        output_model=CodeMapOutput,
        handler=handle_code_map,
        idempotent=True,
        budget_p99_ms=300,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.outcome",
        input_model=CodeOutcomeInput,
        output_model=CodeOutcomeOutput,
        handler=handle_code_outcome,
        idempotent=True,
        budget_p99_ms=150,
        required_role=Role.VIEWER,
        writes=True,
    ),
    Operation(
        name="code.index_status",
        input_model=CodeIndexStatusInput,
        output_model=CodeIndexStatusOutput,
        handler=handle_code_index_status,
        idempotent=True,
        budget_p99_ms=200,
        required_role=Role.VIEWER,
    ),
    # Org-level: the listing that says what a person may join cannot itself be
    # gated on the first code namespace — without org-readable widening that
    # would 403 every principal who has not joined the trunk. The handler
    # authorizes each entry and lists the rest request-only.
    Operation(
        name="code.repos",
        input_model=CodeReposInput,
        output_model=CodeReposOutput,
        handler=handle_code_repos,
        idempotent=True,
        budget_p99_ms=800,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        # A scoped token (ns_roles) has no single namespace to bind this
        # org-level listing to, so the transport gate admits it by its best
        # per-namespace role instead of answering IDENTITY_INCOMPLETE; the handler still
        # authorizes every entry per namespace and hides the rest.
        deployment_scoped=True,
    ),
    # Org-level (no namespace to bind yet); the handler checks the caller belongs to the
    # org, the quota, and that GitHub answers.
    Operation(
        name="code.repo.add",
        input_model=CodeRepoAddInput,
        output_model=CodeRepoAddOutput,
        handler=handle_code_repo_add,
        idempotent=False,
        budget_p99_ms=30000,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
        audit=True,
    ),
    # A read of the remote before the add dialog offers branches.
    Operation(
        name="code.repo.remote_branches",
        input_model=CodeRepoRemoteBranchesInput,
        output_model=CodeRepoRemoteBranchesOutput,
        handler=handle_code_repo_remote_branches,
        idempotent=True,
        budget_p99_ms=30000,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
    ),
    Operation(
        name="code.repo.add_branch",
        input_model=CodeRepoAddBranchInput,
        output_model=CodeRepoAddOutput,
        handler=handle_code_repo_add_branch,
        idempotent=False,
        budget_p99_ms=30000,
        required_role=Role.ADMIN,
        audit=True,
    ),
    Operation(
        name="code.repo.remove",
        input_model=CodeRepoRemoveInput,
        output_model=CodeRepoRemoveOutput,
        handler=handle_code_repo_remove,
        idempotent=True,
        budget_p99_ms=30000,
        required_role=Role.ADMIN,
        audit=True,
    ),
    Operation(
        name="code.repo.set_access",
        input_model=CodeRepoSetAccessInput,
        output_model=CodeRepoSetAccessOutput,
        handler=handle_code_repo_set_access,
        # Two directory writes and two cache refreshes
        idempotent=True,
        budget_p99_ms=5000,
        required_role=Role.ADMIN,
        audit=True,
        maturity="experimental",
    ),
    Operation(
        name="code.repo.set_source",
        input_model=CodeRepoSetSourceInput,
        output_model=CodeRepoSetSourceOutput,
        handler=handle_code_repo_set_source,
        # One state write and one cache refresh
        idempotent=True,
        budget_p99_ms=3000,
        required_role=Role.ADMIN,
        audit=True,
        maturity="experimental",
    ),
    Operation(
        name="code.index_errors",
        input_model=CodeIndexErrorsInput,
        output_model=CodeIndexErrorsOutput,
        handler=handle_code_index_errors,
        idempotent=True,
        budget_p99_ms=400,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.recent_commits",
        input_model=CodeRecentCommitsInput,
        output_model=CodeRecentCommitsOutput,
        handler=handle_code_recent_commits,
        idempotent=True,
        budget_p99_ms=800,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.coverage",
        input_model=CodeCoverageInput,
        output_model=CodeCoverageOutput,
        handler=handle_code_coverage,
        idempotent=True,
        budget_p99_ms=1500,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.context_packs",
        input_model=CodeContextPacksInput,
        output_model=CodeContextPacksOutput,
        handler=handle_code_context_packs,
        idempotent=True,
        budget_p99_ms=300,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.refresh",
        input_model=CodeRefreshInput,
        output_model=CodeRefreshOutput,
        handler=handle_code_refresh,
        idempotent=False,
        # Fire-and-forget default returns immediately; wait=True is bounded
        # by its own timeout, not this budget.
        budget_p99_ms=1000,
        required_role=Role.WRITER,
        audit=True,
    ),
    Operation(
        name="code.scip_ingest",
        input_model=CodeScipIngestInput,
        output_model=CodeScipIngestOutput,
        handler=handle_code_scip_ingest,
        idempotent=True,  # re-upload of the same (batch_id, seq) converges
        budget_p99_ms=2000,
        required_role=Role.WRITER,
        audit=True,
    ),
    Operation(
        name="code.scip_status",
        input_model=CodeScipStatusInput,
        output_model=CodeScipStatusOutput,
        handler=handle_code_scip_status,
        idempotent=True,
        budget_p99_ms=150,
        required_role=Role.VIEWER,
    ),
    Operation(
        name="code.reindex",
        maturity="experimental",
        input_model=CodeReindexInput,
        output_model=CodeReindexOutput,
        handler=handle_code_reindex,
        idempotent=False,
        budget_p99_ms=500,
        required_role=Role.ADMIN,
        audit=True,
    ),
    Operation(
        name="code.calibrate",
        input_model=CodeCalibrateInput,
        output_model=CodeCalibrateOutput,
        handler=handle_code_calibrate,
        idempotent=False,
        # Probes the live embedder over the whole corpus: slow by design.
        budget_p99_ms=30000,
        required_role=Role.ADMIN,
        audit=True,
    ),
]
