"""Index state: code.index_status, code.coverage, code.index_errors and
code.recent_commits.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from server.code.api.models_common import _CodeIdentity

# ============================================================
# code.index_status
# ============================================================


class CodeIndexStatusInput(_CodeIdentity):
    # A merge commit (7–40 hex) to check against every tier. The answer rides
    # `commit_coverage`: `ready` once the fast tier and every compiler tier with a
    # watermark have reached it. A reviewer polls this before analysing a merged PR.
    commit: str = Field(
        "",
        description="Optional merge commit SHA: report whether every index tier covers it "
        "(commit_coverage.ready).",
    )
    # The reviewer's changed files; `commit_coverage` then names which of them a later
    # merge changed (`paths_changed_overlap`).
    paths: list[str] = Field(
        default_factory=list,
        description="With `commit`: the PR's changed paths; the answer lists those a later "
        "merge changed.",
    )
    # A compact answer (identity, watermarks, lags, tiers, commit_coverage) without
    # `state`, the op catalogue and the workers — ~116 KB → ~2 KB on a large repository.
    # Defaults on when `commit` is given (a reviewer polling every few minutes), off
    # otherwise.
    brief: bool | None = Field(
        None,
        description="Compact answer without state/op catalogue/workers. Default: on over "
        "MCP and when `commit` is set; pass false for the full answer.",
    )


class CodeCommitTierCoverage(BaseModel):
    """One compiler tier against the asked commit."""

    producer: str = ""
    label: str = ""
    watermark_key: str = ""
    watermark: str = ""
    covered: bool | None = None  # None: the mirror does not hold the commit or watermark
    # First-parent commits this tier's watermark is past the asked commit (-1 until it covers it).
    commits_since: int = -1


class CodeCommitCoverage(BaseModel):
    """Whether every index tier has reached one commit. `ready` is true only when the
    fast tier and every compiler tier with a watermark have reached it; `waiting_on`
    names the ones that have not."""

    commit: str = ""
    in_mirror: bool = False
    fast: bool | None = None
    tiers: list[CodeCommitTierCoverage] = Field(default_factory=list)
    ready: bool = False
    waiting_on: list[str] = Field(default_factory=list)
    # How far the index (the fast tier, the newest) has moved past the commit. Paths a
    # later merge changed read differently in the index than at the commit; a reviewer
    # trusts its own checkout for them.
    commits_since: int = -1
    paths_changed_since: list[str] = Field(default_factory=list)
    paths_changed_count: int = -1
    paths_truncated: bool = False
    paths_changed_overlap: list[str] = Field(default_factory=list)


class CodeWorkerStatus(BaseModel):
    """Process-local snapshot of one code_* worker (this replica only)."""

    enabled: bool = True
    interval_seconds: int = 0
    last_run_at: str = ""
    last_duration_ms: int = 0
    consecutive_failures: int = 0
    total_runs: int = 0
    # Why the last pass failed: the error's class, or the pass's status (`cancelled`) when
    # it raised none; `""` after a clean pass. The text is in the server log.
    last_error: str = ""


class CodeCompilerTier(BaseModel):
    """One compiler tier of a repository: the producer, the watermark it advances and
    where that watermark stands against the tip. `primary` marks the tier the entry is
    configured for; the others are tiers whose watermark is set (a C++ tree with a
    Python tier)."""

    producer: str  # clang | typescript | go | python
    tool: str  # scip-clang | scip-typescript | scip-go | scip-python
    label: str  # "C++", "TypeScript / JavaScript", …
    watermark_key: str  # the code_index_state key the producer advances
    commit: str = ""
    lag: int = -1
    updated_at: str = ""
    primary: bool = False
    workers: list[str] = Field(default_factory=list)
    note: str = ""


class CodeTierHealth(BaseModel):
    """One tier's last recorded outcome on the entry.

    A tier with no record has not failed since records began, or no longer runs on the
    entry. `state` is `failing` from the first failed pass until the next pass that
    succeeds; `kind` classifies the error and `fix` is the one action that clears it."""

    # fetch | ingest | clang | typescript | go | python | clang_full (the weekly C++ full run)
    tier: str
    state: str = "ok"  # ok | failing | unavailable (left out for a dependency it cannot fetch)
    # credential | pin_missing | submodule_url | egress | network | timeout | file_refused |
    # disk | git_lock | permission | harness | module | dependency_unavailable | cmake |
    # unknown
    kind: str = ""
    error: str = ""  # the end of the error output, credentials redacted
    fix: str = ""
    failing_since: str = ""  # ISO-8601 UTC of the first failed pass in the current run
    last_failed_at: str = ""
    failed_passes: int = 0  # failed passes in a row
    last_ok_at: str = ""  # the pass that cleared the last run of failures
    # Commits the tier's watermark trails the fetched tip by; -1 unknown or not a
    # watermark tier (fetch).
    lag_commits: int = -1


class CodeIndexStatusOutput(BaseModel):
    # The repository identity. Non-GitHub remotes include their host.
    repo: str
    # The branch this entry indexes.
    branch: str
    org: str = ""
    namespace: str
    provider: str = ""
    profile: str = "generic"
    # Compiler tiers inferred from the indexed tree, even before a compiler
    # worker has produced its first watermark.
    compiler_candidates: list[str] = Field(default_factory=list)
    # Actionable readiness gaps. Empty means the indexed fast/vector tiers are healthy;
    # compiler candidates remain optional precision upgrades.
    quality_warnings: list[str] = Field(default_factory=list)
    # The fast tier's watermark: the last commit whose files are ingested; '' before the first pass.
    git_head: str = ""
    # `scip_commit` / `scip_lag` are the ENTRY'S compiler tier — the one `scip_producer`
    # names — not the C++ key by name; a TypeScript repository reports its
    # scip-typescript watermark here. `compiler_tiers` carries every tier, the primary
    # first.
    scip_commit: str = ""
    # The scip-python watermark by name, whatever the entry's primary tier; '' when unset.
    scip_commit_py: str = ""
    # The primary compiler tier's producer (clang | typescript | go | python); '' when no tier has
    # a watermark.
    scip_producer: str = ""
    # Every compiler tier with a watermark or configured for the entry, the primary first.
    compiler_tiers: list[CodeCompilerTier] = Field(default_factory=list)
    # The last recorded outcome of each tier that has one (fetch, ingest, the compiler
    # tiers and the weekly C++ full run): a failing tier names its error kind, since
    # when, and the fix.
    tier_health: list[CodeTierHealth] = Field(default_factory=list)
    # The PR-history poller's cursor: the newest `updated_at` (ISO-8601) of the merged PRs it
    # fetched; '' before its first pass.
    pr_cursor: str = ""
    # Set when the request named a `commit`.
    commit_coverage: CodeCommitCoverage | None = None
    # Freshness. clone_head is the bare clone's branch tip as of the last
    # poller fetch (<= interval old); *_lag = first-parent commits between a
    # watermark and that tip, -1 when a watermark is unset or the clone is
    # unreadable. 0 everywhere == converged.
    clone_head: str = ""
    # When the ingest pass last fetched this entry's mirror (the `clone_head`
    # row's updated_at, ISO-8601 UTC; '' unknown) and whether that is older
    # than `code_fetch_stale_after_seconds`. The lags below are counted
    # against the local mirror and read 0 for as long as it stalls.
    clone_fetched_at: str = ""
    # True when `clone_fetched_at` is older than `code_fetch_stale_after_seconds`.
    fetch_overdue: bool = False
    # First-parent commits between `git_head` and `clone_head`; -1 when either is unknown.
    fast_tier_lag: int = -1
    # First-parent commits between `scip_commit` and `clone_head`; -1 when either is unknown.
    scip_lag: int = -1
    # First-parent commits between `scip_commit_py` and `clone_head`; -1 when either is unknown.
    scip_py_lag: int = -1
    # Last code.refresh request (the CI hook). Empty = never reached us.
    last_refresh_at: str = ""
    # The caller of that request, as `agent=<agent> expected_head=<first 12 hex>`; a field
    # the caller left empty is left out.
    last_refresh_detail: str = ""
    # Deployment-level read rule (LOOM_CODE_ORG_READABLE / per-entry): when
    # true every org principal reads this entry regardless of visibility.
    org_readable: bool = False
    # The path prefixes the entry indexes; `[""]` = the whole tree.
    path_prefixes: list[str] = Field(default_factory=list)
    # Live row counts per code table (`code_files`, `code_chunks`, `code_symbols`, …); empty
    # in the brief answer.
    counts: dict[str, int] = Field(default_factory=dict)
    # Every code_index_state row: {key: {value, detail, updated_at}}.
    state: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Per `code_*` worker, its enabled flag, interval and last pass; empty in the brief answer.
    workers: dict[str, CodeWorkerStatus] = Field(default_factory=dict)
    # Capability catalogue: the build this server runs and every served code.*
    # operation with the request fields its input model accepts, so a client
    # checks for a route or a field instead of probing for a 404 or a 422.
    build_commit: str = ""
    # Every served `code.*` operation, sorted; empty in the brief answer.
    ops: list[str] = Field(default_factory=list)
    # Per served op, the request fields its input model accepts; empty in the brief answer.
    request_fields: dict[str, list[str]] = Field(default_factory=dict)
    # Per served op, its maturity (stable | experimental) and the reach
    # kinds a caller has to it.
    op_maturity: dict[str, str] = Field(default_factory=dict)
    # Per served op, its reach kinds (mcp_core | mcp_atomic | mcp_all | sdk_method | sdk_example |
    # plugin_hook | dashboard | http_only); empty in the brief answer.
    op_reach: dict[str, list[str]] = Field(default_factory=dict)
    # Per served op, every MCP toolset whose listing carries it (core | coding | atomic
    # | all; empty when hidden).
    op_toolsets: dict[str, list[str]] = Field(default_factory=dict)
    # The weekly symbol fan-in table for this namespace — live rows with
    # callers and the newest pass stamp (ISO 8601, UTC). 0 / "" = the pass has not written it: every
    # `fan_in` field, the fan-in sort and the rank prior answer empty.
    symbol_stats_rows: int = 0
    # The newest fan-in pass stamp (ISO-8601 UTC); '' when the pass has not written the table.
    symbol_stats_at: str = ""
    # The precomputed risk table for this namespace — live rows and the newest stamp
    # (ISO 8601, UTC), so an operator sees the weekly leg's progress and a resumed
    # pass's remainder. 0 / "" = no row written (every served unit is read live).
    unit_risk_rows: int = 0
    # The newest risk-table stamp (ISO-8601 UTC); '' when no row is written.
    unit_risk_at: str = ""
    # Live chunks, those carrying a vector, and their ratio (0..1; reference fixtures
    # unembedded by design are outside the denominator). Below
    # LOOM_CODE_READY_EMBED_COVERAGE the search is lexical-only and code.search says so.
    chunks_total: int = 0
    # Live chunks that carry an embedding vector.
    chunks_embedded: int = 0
    # Embedded share of the embeddable chunks (0..1); 1.0 when the counts do not carry it.
    embedding_coverage: float = 1.0


# ============================================================
# code.coverage — what is indexed, per language / tier, and WHEN
# ============================================================


class CodeCoverageInput(_CodeIdentity):
    pass


class CodeLanguageCoverage(BaseModel):
    lang: str  # cpp | python | shell | sql | config | doc | reference | '' (not in code_files)
    files: int = 0
    files_errored: int = 0
    files_first_at: str = ""
    files_last_at: str = ""  # newest file row = last time this language was (re)indexed
    chunks: int = 0
    chunks_embedded: int = 0
    chunks_described: int = 0  # LLM context descriptions (code_summary worker)
    chunks_last_at: str = ""
    symbols_treesitter: int = 0
    symbols_treesitter_last_at: str = ""
    symbols_scip: int = 0
    symbols_scip_last_at: str = ""


class CodeTierCoverage(BaseModel):
    tier: str  # treesitter | scip
    edges: int = 0
    edges_last_at: str = ""


class CodeCoverageGaps(BaseModel):
    """What the index does NOT know — the honesty column beside coverage: source
    functions with no recorded caller (virtual dispatch, templates, entry points and
    genuinely dead code all land here), source files that map to no test, and code
    files that yield zero symbols."""

    functions_scanned: int = 0  # compiler-tier functions/methods outside tests/
    functions_uncalled: int = 0  # no `calls` edge points at them
    uncalled_share: float = 0.0
    source_files: int = 0  # code-language files outside tests/
    source_files_untested: int = 0  # no code_test_map row
    untested_share: float = 0.0
    code_files: int = 0
    code_files_no_symbols: int = 0  # indexed, but the parser produced nothing
    no_symbols_share: float = 0.0


class CodeCoverageOutput(BaseModel):
    """Answers "what is in the index and when was each part done" — for a
    human reading the Health tab and for an agent deciding whether the tier
    it is about to trust (compiler-precise vs syntax-only) covers the
    language it is asking about."""

    org: str
    namespace: str
    as_of_commit: str = ""  # git_head
    # One row per language, the most files first.
    languages: list[CodeLanguageCoverage]
    # Call-graph edge counts per tier.
    tiers: list[CodeTierCoverage]
    # None when the gap queries failed — absence, never a fabricated zero.
    gaps: CodeCoverageGaps | None = None
    # The oldest merge time among the stored merged PRs; '' when none are stored.
    pr_history_from: str = ""
    # The newest merge time among the stored merged PRs; '' when none are stored.
    pr_history_to: str = ""
    # When the PR-history poller last wrote a row; '' never.
    pr_history_last_fetch_at: str = ""
    # Whether the PR-history poller is enabled on this deployment.
    pr_history_enabled: bool = False
    pr_history_auth: str = ""  # token | anonymous (60 req/h GitHub limit)
    # When the newest live PR context pack was built; '' when none exists.
    context_packs_last_at: str = ""
    # When the newest chunk context description was written; '' when none exists.
    summaries_last_at: str = ""


# ============================================================
# code.index_errors — files the indexer could not process
# ============================================================


class CodeIndexErrorsInput(_CodeIdentity):
    # At most this many files, newest first (1..1000).
    limit: int = Field(default=100, ge=1, le=1000)


class CodeIndexErrorRow(BaseModel):
    path: str
    lang: str = ""
    error: str
    indexed_commit: str = ""
    at: str = ""


class CodeIndexErrorsOutput(BaseModel):
    # Files whose last ingest recorded an error, newest first.
    rows: list[CodeIndexErrorRow]
    # Every errored live file in the namespace, not only the rows returned.
    total: int = 0


# ============================================================
# code.recent_commits — what the fast tier ingested lately
# ============================================================


class CodeRecentCommitsInput(_CodeIdentity):
    # At most this many commits, newest ingest first (1..100).
    limit: int = Field(default=20, ge=1, le=100)


class CodeRecentCommitRow(BaseModel):
    commit: str
    subject: str = ""  # from the bare clone; empty if unreadable
    committed_at: str = ""
    ingested_at: str = ""  # newest row written for this commit
    files: int = 0  # files (re)indexed at this commit
    pr_number: int = 0  # when a context pack ties the merge to a PR


class CodeRecentCommitsOutput(BaseModel):
    # Commits the fast tier wrote files at, newest ingest first.
    rows: list[CodeRecentCommitRow]
