"""The code plane: ingest, indexes, search, ask, review."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded, JsonArray, JsonObject


class CodeSettings(BaseModel):
    # ------------------------------------------------------------------
    # Code intelligence plane. One code
    # namespace indexes ONE repo. No repository is configured by default: set
    # LOOM_CODE_REPO / ORG / NAMESPACE (or LOOM_CODE_REPOS_JSON, or add one from
    # the Repositories page) before anything is indexed. The index-building workers (ingest, SCIP,
    # PR history, graph stats, cards, embed recovery) default off and no-op
    # on a namespace without a git_head watermark or pending work.
    # ------------------------------------------------------------------
    # Master kill switch, checked at the top of every code.* handler.
    code_intel_enabled: bool = True

    # code.test_gate run recipes (LOOM_CODE_TEST_GATE_RUN_TEMPLATES): JSON list of
    # {"pattern": <regex over the repo-relative test path>, "run": <template>},
    # tried before the built-ins. Templates may use {path} {dir} {stem} {name}
    # {parent}. A path no recipe matches is reported run_unknown, never guessed.
    code_test_gate_run_templates: Annotated[str, JsonArray] = ""

    # Retrieval context (tokens) code.ask hands to synthesis when the caller
    # passes no token_budget.
    code_ask_token_budget: int = 4000
    # The code.ask answer cache's hit cosine. 0 = the per-embedding-model table
    # in code/app/answers.py; a value in (0, 1] pins one threshold for
    # every model.
    code_ask_cache_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    # A cached code.ask answer older than this many days is a miss (checked
    # and tombstoned at read time; no table TTL). 0 = no bound.
    code_ask_cache_max_age_days: int = Field(default=14, ge=0)
    # Seconds an abstain stays cached for its exact normalised question under
    # the index head it abstained at. 0 = off.
    code_ask_negative_cache_ttl_s: int = Field(default=3600, ge=0)
    # code.ask's pre-synthesis LLM calls (identifier proposal, HyDE, concept
    # legs) read the per-namespace exact-prompt response cache; no_cache or a
    # traffic class with llm_cache off bypasses it.
    code_ask_llm_cache: bool = True
    # The scalar repository entry: its loom org, its code namespace and its
    # GitHub `owner/name`. Empty repo = no scalar entry (nothing indexed until a
    # repository is configured); an empty org or namespace is never a default
    # for a code.* request, which then must name its own.
    code_org: str = ""
    # The scalar entry's code namespace, used by a code.* request or `loom code` command
    # that names none; empty = the request must name one.
    code_namespace: str = ""
    # The scalar entry's GitHub `owner/name`; empty = no scalar entry.
    code_repo: str = ""

    # The memory namespace code.ask fuses decision/rationale memories (the
    # "why") from into its answer; empty = no enrichment, never guessed.
    code_ask_memory_namespace: str = ""
    # The org of that namespace; empty = `code_org`.
    code_ask_memory_org: str = ""
    # Who may READ the code index. False: the namespace's own visibility and
    # grants. True: every authenticated principal of the org reads every
    # configured code namespace (right for public or org-internal repos).
    # Writes stay grant-driven. Per-entry "org_readable" in LOOM_CODE_REPOS_JSON.
    code_org_readable: bool = False
    # The scalar repo entry is public: its namespace registers join_policy=open
    # and discoverable. Per-entry `"public": true` does the same.
    code_repo_public: bool = False
    # Positive-control symbol code.test_home / code.coverage_claim grep for (one
    # every test suite of the repository names); empty makes every claim
    # `unknown`.
    code_coverage_control_symbol: str = ""
    # The setting code.setting_facts must find before it claims
    # `not_a_setting` for any other name; empty = it never claims it.
    code_setting_control: str = ""
    # Max bytes of a PR patch code.conventions reads from the mirror; a longer
    # patch is cut at a file boundary and flagged `diff_truncated`.
    code_conventions_diff_max_bytes: int = 1_500_000
    # A touched function is "widely called" when its hop-1 callers span more
    # than this many `src/<dir>` directories (rule wide_callers_untested).
    code_conventions_wide_caller_dirs: int = 5
    # Multi-repo: JSON array of repo configs (code/domain/index/repos.py);
    # empty = the scalar single-repo settings. Missing per-entry fields fall
    # back to the scalars.
    code_repos_json: Annotated[str, JsonArray] = ""
    # Repositories added from the Repositories page (loom.code_repos) merge
    # with the entries above; the environment wins on a namespace collision.
    # Off = the operator's JSON is the only source.
    code_self_service_enabled: bool = True
    # Where the worker clones a self-service entry's bare mirror (one per
    # GitHub repository, shared by its branches).
    code_repos_root: str = "var/code_repos/self"
    # Active self-service entries per org (branches count); past it the add
    # refuses with a message.
    code_self_service_max_repos: int = 20
    # Files indexed per pass while a new entry's initial index builds; the
    # cursor persists in code_index_state (400 ≈ 1 min at the live rate).
    code_bootstrap_files_per_pass: int = 400
    # Seconds between re-reads of loom.code_repos.
    code_repos_refresh_seconds: int = 30
    # Seconds a bare clone may take before the worker gives up for the pass
    # and retries next pass.
    code_bootstrap_clone_timeout_s: float = 1800.0
    # rlimits for each process of a sandboxed repository job (clone, fetch,
    # dependency install, compiler-tier harness; code/app/index/sandbox.py).
    # They bound each process, not a job's total: there is no cgroup.
    # CPU seconds a process may use.
    code_sandbox_cpu_seconds: int = 7200
    # Data memory (heap and private mappings, RLIMIT_DATA) a process may use, in
    # MiB. Not address space: node reserves far more of that than it uses.
    code_sandbox_memory_mb: int = 16384
    # Processes and threads the server's uid may run at once (RLIMIT_NPROC):
    # shared by every job running at the time, not one job's own.
    code_sandbox_max_pids: int = 2048
    # Largest file a process may write, in MiB (a pack or index.scip).
    code_sandbox_max_file_mb: int = 32768
    # Bare clone the ingest worker fetches into (created by `loom code init`)
    # and its remote. Empty = derived from code_repo: `var/code_repos/<name>.git`
    # and `https://github.com/<owner>/<name>.git`.
    code_git_dir: str = ""
    # The scalar entry's fetch remote; empty = `https://github.com/<owner>/<name>.git`.
    code_git_remote: str = ""
    # The scalar entry's branch: the one the ingest worker fetches and indexes.
    code_git_branch: str = "main"
    # Comma-separated fnmatch globs of extra remote branches the mirror
    # fetches besides the configured one (`release/*` = release branches,
    # `backport/*` = backport staging). Refs are never pruned. Empty = the
    # configured branch only.
    code_git_extra_branches: str = ""
    # Fetch ledger merge commits no branch reaches by sha and pin them under
    # refs/loom/merges/<sha>: one ledger page and at most the batch below per
    # entry per pass; the cursor wraps. Off = branches only.
    code_git_fetch_ledger_merges: bool = True
    # Ledger merge rows checked for a missing sha per entry per pass.
    code_git_ledger_merge_page: int = 500
    code_git_ledger_merge_batch: int = 40  # 40 shas ≈ 8.5 s
    # The mirror carries `refs/pull/N/head` for every PR the ledger holds OPEN
    # (refetched when the tip moves, never pruned), so pre-merge reads diff the
    # PR's own head. Only open heads: fetching every PR head is too costly.
    # Per-entry `git_fetch_pr_heads` overrides. Off = the overlay reports
    # `head_not_in_mirror`.
    code_git_fetch_pr_heads: bool = True
    # Max PR heads fetched per shared mirror per ingest pass.
    code_git_pr_head_batch: int = 200
    # Open-PR rows per pass whose head_sha / base_sha / files the PR-history
    # worker fills from the mirror; rows the mirror lacks fall back to GitHub
    # (batch below).
    code_gh_items_head_fill_batch: int = 200
    # GitHub `GET /pulls/N` calls per pass for open PRs the mirror lacks;
    # 0 = the mirror path only.
    code_gh_items_head_github_batch: int = 20
    # CSV path prefixes of the scalar entry that are indexed; everything else
    # is skipped. Empty = the whole tree (the ingest denylist still applies).
    code_path_prefixes: str = ""
    # GitHub token for the default repository: git fetches of a private
    # remote, the PR-history and issue mirrors, and fork lookups. Empty = the
    # REST calls go anonymously (60 requests/h per IP, public repos only; one
    # warning) and the issue mirror is skipped.
    code_github_token: str = ""
    # Per-merge fast tier: git-poll → diff → re-chunk/re-embed changed files →
    # tree-sitter symbols/edges → context pack → advance watermark.
    code_ingest_enabled: bool = False
    # Seconds between fast-tier ingest passes; extra-branch fetches of a shared mirror
    # are spaced by 0.9 of it (at least 30 s).
    code_ingest_interval_seconds: int = 120
    # Seconds an ingesting process caches the namespace's live path set (the
    # import-resolution oracle); files it writes join at once.
    code_ingest_live_paths_ttl_s: float = 60.0
    # Seconds after which the weekly pass refits the jit-risk model.
    code_jit_risk_refit_interval_s: int = 7 * 24 * 3600
    # Months of merged PRs the fit reads.
    code_jit_risk_window_months: int = 12
    # Days after which a fit is served as `scored_stale` / `fit_stale`.
    code_jit_risk_max_age_days: int = 183
    # as_of.stale is true when the fast tier is more than this many
    # first-parent commits behind the fetched branch tip (60 s cache).
    code_stale_after_commits: int = 5
    # as_of.stale is also true when the mirror has not fetched for this many
    # seconds (lag is counted against the local mirror). 0 = off.
    code_fetch_stale_after_seconds: int = 3600
    # code.callers/callees: a bare name with more distinct definitions than
    # this returns `ambiguous` + candidates instead of walking them all.
    code_callers_max_distinct_seeds: int = 5
    # Commits past the watermark one pass reads; later ones wait for the next pass.
    code_ingest_max_commits_per_pass: int = 20
    # Prior ingest cost per changed file (seconds), refined by observation. A
    # commit predicted to overrun the pass deadline is not started unless it
    # is the pass's first.
    code_ingest_seconds_per_file: float = 2.5
    # Changed files indexed per commit; past it the rest are skipped with a warning
    # and heal on the next backfill.
    code_ingest_max_files_per_commit: int = 500
    # The ingest pass's own deadline (seconds), kept at the supervisor's cap;
    # the budget below keeps a pass under it.
    code_ingest_max_pass_seconds: int = 900
    # Share of the commit phase one oversized commit may take per pass while
    # other entries wait (it is sliced across passes).
    code_ingest_slice_share: float = Field(default=0.5, ge=0.05, le=1.0)
    # Fraction of the deadline after which the pass stops STARTING units,
    # finishes the in-flight round, saves cursors and returns `partial: true`.
    code_ingest_pass_budget_fraction: float = 0.75
    # Share of that budget the commit phase leaves to the walk (reconcile heal,
    # extractor re-chunk). 0 = commits may take it all.
    code_ingest_walk_budget_share: float = 0.3
    # Files per round in the walk, the bootstrap slice and a commit's file
    # list; a round prefetches its per-file resolution reads in two
    # statements. 0 = per-file reads.
    code_ingest_round_files: int = 25
    # Entries whose commits ingest side by side in the commit phase. Each
    # entry's own commits stay one at a time, oldest first (the watermark
    # contract); a lane waits on ClickHouse round trips, not on its capacity,
    # so lanes add throughput. 1 = one commit at a time across all entries.
    code_ingest_lanes: int = Field(default=1, ge=1, le=8)
    # SCIP delta apply (compiler-grade tier uploaded via code.scip_ingest).
    code_scip_apply_enabled: bool = False
    # Seconds between SCIP apply passes.
    code_scip_apply_interval_seconds: int = 60
    # Max bytes of one code.scip_ingest payload part; a larger part is refused.
    code_scip_max_part_bytes: int = 8 * 1024 * 1024
    # Max `total_parts` of one code.scip_ingest upload; more is refused.
    code_scip_max_parts: int = 512
    # C++ compiler-tier per-merge deltas: scip-clang over changed TUs when
    # git_head outruns scip_commit. Needs the configured checkout and a
    # scip-clang binary; CPU-heavy in bursts.
    code_scip_delta_enabled: bool = True
    # Seconds between C++ delta passes.
    code_scip_delta_interval_seconds: int = 300
    # The scalar entry's working tree the compiler tiers index; other entries default to a
    # sibling named after their namespace, and an entry's `scip_checkout_dir` wins.
    code_scip_checkout_dir: str = "var/code_repos/checkout"
    # Path of the scip-clang binary the delta pass and `loom code` run.
    code_scip_clang_path: str = "var/bin/scip-clang"
    # Max translation units one delta indexes; past it only the changed `.cpp` files are
    # indexed and the header fan-out heals on the next full run.
    code_scip_delta_max_tus: int = 200
    # Seconds the C++ delta harness may run before it is killed.
    code_scip_delta_timeout_seconds: int = 3600
    # Directory of the per-entry lock files every compiler-tier pass on this
    # host takes (`scip.<org>.<namespace>.lock`): a full re-index and a delta
    # never work on one entry at once; different entries run side by side. The
    # host cron scripts lock the same files with flock(1).
    code_scip_lock_dir: str = "var/cron/locks"
    # TypeScript compiler-tier deltas: scip-typescript over an entry with
    # `scip_producer` "typescript" when git_head outruns scip_commit_ts. The
    # worker prepares dependencies with lifecycle scripts disabled; needs Node
    # 22+ and the toolchain from scripts/install-code-toolchains.sh (in the image).
    code_scip_ts_delta_enabled: bool = True
    # Seconds between TypeScript delta passes.
    code_scip_ts_delta_interval_seconds: int = 600
    # The scip-typescript binary: a name looked up on PATH, or a path.
    code_scip_typescript_path: str = "scip-typescript"
    # Seconds the TypeScript delta harness may run before it is killed.
    code_scip_ts_delta_timeout_seconds: int = 3600
    # Rewrite consumers' tsconfig `paths` from workspace `exports` before
    # indexing an unbuilt checkout; off for a built checkout.
    code_scip_ts_paths: bool = True
    # Node heap size (MiB) given to scip-typescript.
    code_scip_ts_node_heap_mb: int = 8192
    # Python compiler-tier deltas: scip-python over an entry with
    # `scip_producer` "python", over the files the window touched (a first run
    # indexes the tree). Needs node and scip-python.
    code_scip_py_delta_enabled: bool = True
    # Seconds between Python delta passes.
    code_scip_py_delta_interval_seconds: int = 600
    # The scip-python binary: a name looked up on PATH, or a path.
    code_scip_python_path: str = "scip-python"
    # Seconds the Python delta harness may run before it is killed.
    code_scip_py_delta_timeout_seconds: int = 3600
    code_scip_py_heap_mb: int = 16384  # Pyright dies silently at 8 GB
    # Go compiler-tier deltas: scip-go per touched module of an entry with
    # `scip_producer` "go". Needs the Go toolchain and scip-go
    # (`go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7`, the image's version).
    code_scip_go_delta_enabled: bool = True
    # Seconds between Go delta passes.
    code_scip_go_delta_interval_seconds: int = 900
    # The scip-go binary, looked up on PATH or in `code_scip_go_bin_dir`.
    code_scip_go_path: str = "scip-go"
    code_scip_go_bin_dir: str = ""  # Where `go` and scip-go live when not on PATH
    # Seconds the Go delta harness may run before it is killed; also the
    # pass deadline.
    code_scip_go_delta_timeout_seconds: int = 7200
    code_scip_go_jobs: int = 2  # Parallel module builds (~4 GB each)
    # Root of the Go module caches kept across passes: one per entry beneath it
    # (<root>/<org>/<namespace>), never shared between repositories.
    code_scip_go_modcache: str = "var/gocache"
    code_scip_go_private: str = ""  # GOPRIVATE / GONOSUMDB (a vanity import host)

    # Merged-PR history sync (GitHub REST, cursor-resumable).
    code_pr_history_enabled: bool = False
    # Seconds between PR-history sync passes.
    code_pr_history_interval_seconds: int = 900

    # --- The set-wide backport report ----------------------------------------
    # Nightly pass: runs `code.backport_gap(exact, history)` for every trunk PR
    # of the last `days` labelled for backport and writes one row per release
    # branch into loom.code_backport_gaps. Paced one fix per `pace_s`, at most
    # `prs_per_pass` a night, stalest first, stopping at `budget_s`; settled
    # fixes are skipped. Off = nothing written; the read op serves the table.
    code_backport_gaps_enabled: bool = True
    # Seconds between passes; the read op reports the table stale past twice this.
    code_backport_gaps_interval_seconds: float = 86400.0
    # Days back of backport-labelled trunk PRs a pass considers.
    code_backport_gaps_days: int = 90
    # Max fixes checked per pass, stalest first.
    code_backport_gaps_prs_per_pass: int = 300
    # Seconds of pause between two fixes.
    code_backport_gaps_pace_s: float = 1.0
    # Seconds after which a pass stops starting fixes.
    code_backport_gaps_budget_s: float = 1800.0
    # The pass's hard deadline (seconds) under the worker supervisor.
    code_backport_gaps_max_pass_seconds: float = 3600.0
    # --- coverage watches -----------------------------------------------------
    # Coverage watches: post one code.coverage.ready event on the watcher's task
    # inbox once every index tier covers a watched commit. A pass with no
    # pending watch is one read of an empty table.
    code_coverage_notify_enabled: bool = True
    # Seconds between passes while watches are pending.
    code_coverage_notify_interval_seconds: int = 60
    # Seconds between passes that found no pending watch.
    code_coverage_notify_idle_interval_seconds: int = 120
    # --- bisect ---------------------------------------------------------------
    # Days of the repo's own PR history bisect reads back from the window's
    # end when there is no tracker (or no changed files) and no `after`.
    code_bisect_history_days: int = Field(90, ge=1, le=3650)
    # Minor releases searched below the failing one when the report names no
    # clean one.
    code_bisect_lookback_minors: int = Field(6, ge=1, le=24)
    # Merge-time window (days) for a report naming no release.
    code_bisect_window_days: int = Field(90, ge=7, le=730)
    # The rerank's wall budget (seconds); a timeout keeps the fused order.
    code_bisect_rerank_timeout_s: float = Field(6.0, ge=1.0, le=30.0)
    # The wide read's merge-time window (days) with no release named.
    code_bisect_wide_days: int = Field(180, ge=30, le=730)
    # The dupcheck's budget inside a bisect, from the call's
    # start; it runs beside the resolve, the scan and the fusion reads and is
    # awaited only before the ranking stages. Past it the bisect answers
    # without it (`dupcheck_fix.status: timeout`).
    code_bisect_dupcheck_timeout_s: float = Field(4.0, ge=0.5, le=30.0)
    # --- PR review comments ---------------------------------------------------
    # Also fetch each merged PR's inline review comments into
    # loom.code_pr_reviews (one request per PR; skipped without a GitHub token).
    code_pr_reviews_enabled: bool = True
    # --- GitHub issues and PRs mirror -----------------------------------------
    # Keep loom.code_gh_items current: every issue and PR updated since the
    # last pass via /issues; the first pass reaches back backfill_days.
    # Skipped anonymously.
    code_gh_items_enabled: bool = True
    # Days back the first mirror pass (and `loom code` with no start date) reads.
    code_gh_items_backfill_days: int = 730
    # --- code.pr_check -------------------------------------------------------
    # Verdict: `duplicate_of` when a candidate's heuristic score reaches the
    # duplicate line (a closed-unmerged PR caps at related), `related` at the
    # related line, else `none`. A title alone never reaches duplicate.
    code_pr_check_duplicate_threshold: float = 0.55
    # Score at or above which a candidate is `related`.
    code_pr_check_related_threshold: float = 0.2
    # merged PRs older than this do not vote as duplicates
    code_pr_check_merged_days: int = 365
    # closed-without-merge PRs read back this far
    code_pr_check_closed_days: int = 180
    # a candidate touching more files than this is a mass change: files do not vote
    code_pr_check_mass_files: int = 200
    # --- merged-PR backfill ---------------------------------------------------
    # Months of merged PRs `loom code backfill-prs` (and a cold worker)
    # fetch; matches the churn window in code/app/risk.py.
    code_pr_backfill_months: int = 12
    # --- embedding recovery ---------------------------------------------------
    # Embedding recovery for code_chunks/code_summaries stubs (dim=0).
    code_embed_recovery_enabled: bool = False
    # Seconds between recovery passes while stubs are pending.
    code_embed_recovery_interval_seconds: int = 30
    # Sleep after a pass that found no pending stub; the normal interval
    # returns once a pass writes a row.
    code_embed_recovery_idle_interval_seconds: int = 120
    # The pending scan reads above a version watermark; once per this many
    # seconds it scans from version 0 to catch late rows. 0 = always from 0.
    code_embed_recovery_full_scan_seconds: int = 1800
    # Embed attempts per stub before it is poisoned (`embed_failed_at` set) and skipped.
    code_embed_max_attempts: int = 8
    # Stub keys the recovery scan takes per repo per pass (also texts per
    # embed call); raise it to onboard large repositories faster.
    code_embed_recovery_batch: int = 100
    # Share of embeddable chunks that must carry a vector before a repo is
    # `ready`; below it code.search says "lexical only". Poisoned rows count
    # against it; fixtures unembedded by design do not.
    code_ready_embed_coverage: float = 0.95
    # --- ingest limits and denylist -------------------------------------------
    # Files above this size are recorded (index_error) and not chunked.
    # 0 = no cap.
    code_ingest_max_file_bytes: int = 1_000_000
    # Ingest denylist (code/app/index/denylist.py): the built-in
    # generated/minified/vendored rules are off (they match 0.19% of rows);
    # `exclude` globs (CSV here, a list per repo entry) and the repo-local
    # ignore file are always honoured.
    code_ingest_denylist_builtin: bool = False
    # CSV of path globs never indexed (the scalar entry's `exclude`).
    code_ingest_exclude: str = ""
    # Name of the repo-local ignore file whose patterns are never indexed.
    code_ingest_ignore_file: str = ".loomignore"
    # --- reconciliation and re-chunking ---------------------------------------
    # Content-based reconciliation (code/app/index/reconcile.py): each
    # interval, compare every path's chunk commit with the branch head and
    # re-ingest up to heal_per_pass stale paths.
    code_reconcile_enabled: bool = True
    # Seconds between reconciliation runs per namespace.
    code_reconcile_interval_seconds: int = 3600
    # Stale paths re-ingested per run.
    code_reconcile_heal_per_pass: int = 200
    # Max distinct chunk commits a run compares; past it the stale set is a floor
    # (`truncated`).
    code_reconcile_max_commits: int = 1000
    # When the recorded parser/chunker/grammar stamp differs from the running
    # process, re-chunk the namespace in paced batches per ingest pass. A first
    # observation is recorded, never acted on.
    code_extractor_rechunk_enabled: bool = True
    # Live paths re-chunked per ingest pass while the stamp differs.
    code_extractor_rechunk_per_pass: int = 200
    # Files a re-chunk re-derives at once. A re-derive waits mostly on its
    # ClickHouse reads and writes, so raising this shortens a heal roughly in
    # proportion, at that much more concurrent load on the cluster.
    code_extractor_rechunk_concurrency: int = 4
    # --- dependents, closure repair and call guards ---------------------------
    # After a commit, re-extract edges only on untouched files whose call
    # sites or edges reference symbols it added, removed or tombstoned (or,
    # opt-in, that include a touched header). Capped per commit; overflow
    # drains through reconcile. A replay closed every gap at 20% of the ingest
    # wall.
    code_ingest_dependents_enabled: bool = True
    # Dependent files re-extracted per commit; the overflow queues for reconcile.
    code_ingest_dependents_max_files: int = 200
    # Dependents re-extracted at once; the batch is bound by ClickHouse read
    # throughput, so more is not faster.
    code_ingest_dependents_concurrency: int = Field(default=4, ge=1, le=64)
    # Size cap of that overflow queue; paths past it are dropped and counted.
    code_ingest_dependents_pending_max: int = 5000
    # Closure repair: a bare function name the commit added with exactly ONE
    # live definition repo-wide binds its `unresolved` sites at 0.6 without
    # re-parsing; several definitions leave them declined. Capped per commit.
    code_closure_repair_enabled: bool = True
    # Added and removed names per commit closure repair considers; the rest are skipped.
    code_closure_repair_max_names: int = 500
    # Unresolved call sites read per commit for those names.
    code_closure_repair_max_sites: int = 2000
    # --- graph and symbol statistics ------------------------------------------
    # Weekly graph statistics: PageRank, degrees, test_map rebuild.
    code_graph_stats_enabled: bool = False
    # Seconds between graph-statistics passes.
    code_graph_stats_interval_seconds: int = 7 * 24 * 3600
    # This worker's pass deadline (seconds); a pass past it is cancelled and
    # counted a failure.
    code_graph_stats_max_pass_seconds: int = 3600
    # Seconds from pass start the unit-risk leg may schedule
    # rounds; past it the pass succeeds with `unit_risk_partial: true` and the
    # next resumes. Must stay under the deadline above.
    code_graph_stats_unit_risk_budget_s: int = 2400
    # Cap of code.search's fan-in rank prior:
    # score *= 1 + cap * log1p(fan_in) / log1p(max fan_in in the pool);
    # skipped under test intent and on noise paths; inert without the stats.
    # 0 = off.
    code_fan_in_prior_cap: float = 0.25
    # The floor below which a declined site code.callers / code.callees return
    # is counted, not returned; a request may pass `candidate_min_confidence`.
    code_callers_candidate_min_confidence: float = 0.2
    # Fan-in at or above which the `uses` block says `skipped: hub` instead of
    # listing sites. 0 = never.
    code_symbol_uses_hub_fan_in: int = 200
    # --- summary cards --------------------------------------------------------
    # Weekly LLM file/module summary cards (inputs_hash-gated).
    code_summary_enabled: bool = False
    # Seconds between card passes.
    code_summary_interval_seconds: int = 7 * 24 * 3600
    # Cards written per pass across every repository, shared out evenly (a
    # share one repository does not use rolls to the next).
    code_summary_max_scopes_per_pass: int = 200
    # Cards in flight per pass; 1 = sequential.
    code_summary_concurrency: int = 1
    # A bounded card pass considers files by merged-PR count per path over
    # these days (source tree), then by include-graph rank. Days of merged PRs
    # the churn order counts.
    code_summary_churn_days: int = 365
    # CSV path prefixes the churn order may propose (empty = any) and CSV
    # prefixes it never proposes, at any depth (vendored trees).
    code_summary_churn_prefixes: str = ""
    # CSV of path prefixes the churn order never proposes, at any depth.
    code_summary_churn_exclude: str = ""
    # CSV of summary models whose cards are rewritten when code_summary_model
    # differs, even for an unchanged file. Empty = an unchanged file keeps its
    # card.
    code_summary_upgrade_from_models: str = ""
    # --- chunking and language lanes ------------------------------------------
    # Chunking: whole AST units; functions larger than this split at top-level
    # statement boundaries, each piece re-prefixed with the signature header.
    code_chunk_max_chars: int = 6000
    # SQL tests as statement records: one `statement` symbol per record,
    # chunks packed by whole statements, facet rows in loom.code_test_facets.
    # Off = line-window chunks for `.sql`. Existing namespaces need `loom code
    # backfill --paths-file` (outside the extractor stamp).
    code_sql_statement_records: bool = False
    # Statement packing target (chars); a larger single statement splits at
    # lines at `code_chunk_max_chars`.
    code_sql_chunk_target_chars: int = Field(default=2000, ge=1)
    # SQL a Python file runs (database-client call arguments and SQL-looking
    # literals) becomes `statement` symbols, facets (confidence 0.7 with a
    # format hole) and table edges. Off = functions only. Needs a
    # `--paths-file` backfill of the Python tree.
    code_py_sql_islands: bool = False
    # Column-level SQL lineage: `<table>.<column>` symbols, `column_read` /
    # `column_write` facets and statement → column edges, for `.sql` and
    # Python SQL islands. Off = table-level lineage. Needs a
    # `--paths-file` backfill.
    code_sql_column_lineage: bool = False
    # A shell test's SQL strings (`$CLICKHOUSE_CLIENT -q`, heredocs, `echo |`)
    # become `statement` symbols with facets; leftover chunks coalesce up to
    # the target below, never across a function. Needs a `--paths-file`
    # backfill.
    code_shell_islands: bool = False
    # Leftover coalescing target in chars for shell files.
    code_shell_chunk_target_chars: int = Field(default=2000, ge=1)
    # Shell inside CI/build files (Actions `run:`, GitLab `script:`,
    # Dockerfile `RUN`, Makefile recipes, Jenkinsfile `sh`) is parsed for
    # facets, script `includes` edges and SQL; jobs, targets and stages become
    # symbols, and extensionless Dockerfile / Makefile / Jenkinsfile index as
    # config. Needs a `--paths-file` backfill.
    code_host_islands: bool = False
    # --- chunk descriptions and ingest rounds ---------------------------------
    # Contextual-embedding one-liner per chunk, inline on the per-merge path;
    # falls back to structural-only on any LLM failure.
    code_context_description_enabled: bool = True
    # Shape of the embedded chunk description: `oneliner` (what the code does)
    # or `questions` (file card + chunk → what it does and the questions it
    # answers). Changing it re-texts new rows only.
    code_context_description_prompt: Literal["oneliner", "questions"] = "oneliner"
    # A file's chunk descriptions in flight at once; 1 = serial.
    code_describe_concurrency: int = Field(default=8, ge=1, le=64)
    # Model calls one round runs at once.
    code_ingest_round_model_concurrency: int = Field(default=16, ge=1, le=128)
    # A round writes one INSERT per table (flushed when a read could see the
    # rows, at the row count below, or at round end). Buffered rows at which a
    # round writes a table early.
    code_ingest_round_write_rows: int = Field(default=2000, ge=1, le=100_000)
    # --- bot authors -----------------------------------------------------------
    # Comma-separated GitHub logins of autonomous agents that author pull requests
    # or issues in the indexed repositories, beyond the generic bots (dependabot,
    # github-actions, …). They are never suggested as owners, their names are not
    # tracker entities, and bisect ignores the lines they sign.
    code_bot_authors: str = ""
    # --- search ranking, legs and caches (code.search) ------------------------
    # Fire-and-forget code_search_trace/code_search_outcome telemetry.
    code_trace_log_enabled: bool = True
    # Post-fusion rank priors in code.search (path-noise penalty, query↔path
    # stem affinity, file coherence), applied after RRF and before the outcome
    # boost. Kill switch.
    code_fusion_signals: bool = True
    # Long-question trim: past this many lexemes the question is treated as an
    # issue body (template headings stripped, the rarest 32 tokens gate, the
    # first 512 cleaned words embed). Under the cap nothing changes. 0 = off.
    code_search_query_max_tokens: int = 48
    # Per-file saturation: the 2nd, 3rd... unit from one path is multiplied by
    # decay^excess before the cut; a file's best unit never decays. 0 = off.
    # Skipped under path_prefix, symbol routes and test intent.
    code_search_file_saturation: float = 0.5
    # CSV of vector-model specs a request may name as `vector_model` (e.g.
    # "prefix:512:text-embedding-3-large"); only the vector leg switches. Empty = the
    # override is refused.
    code_search_vector_models: str = ""
    # JSON {label: {"table": "loom.x", "dim": 256}}: where a label's chunk
    # rows live (the coded vector column is fixed-width).
    # Absent label = loom.code_chunks. Inserts and the vector leg route by it.
    code_vector_tables: Annotated[str, JsonObject] = ""
    # With a vector_model set and this > 0, the second model's top-N ids are
    # rescored by exact cosine on the served vectors. 0 = its own distances
    # rank. Per-request CodeSearchInput.vector_shortlist.
    code_search_vector_shortlist: int = 0
    # Over-fetch multiplier of the file-card leg's codes shortlist once
    # loom.code_summaries carries the codec.
    code_search_cards_fetch_multiplier: float = 10.0
    # Default vector-leg shortlist label (`prefix:<dim>:<served model>`, listed
    # in code_search_vector_models and code_vector_tables), rescored by the
    # served vectors. Needs code_search_vector_shortlist > 0; an empty
    # shortlist falls back to the full scan. Empty = off.
    code_search_vector_model_default: str = ""
    # A bulk `loom code` pass that wrote at least this many chunk rows ends by
    # compacting the prefix/twin vector tables past code_compact_min_ratio.
    # 0 = off; loom.code_chunks is compacted only by the CLI.
    code_compact_after_bulk_rows: int = 10_000
    # Copies per key (active rows / distinct keys) at which a code vector table
    # is OPTIMIZE ... FINAL'd.
    code_compact_min_ratio: float = Field(default=1.5, gt=0.0)
    # Per-result risk annotations (churn/amp/tested) and one-hop callee
    # context on code.search results; reserves part of the token budget.
    # Kill switch.
    code_risk_annotations: bool = True
    # Age (seconds) past which a risk row is stale and annotated live; must
    # exceed the weekly pass interval.
    code_unit_risk_max_age_s: int = 10 * 24 * 3600
    # code.search `bodies` when the request omits it: "auto" fills bodies into
    # the budget; "none" serves handles only.
    code_search_bodies_default: Literal["auto", "none"] = "auto"
    # Memoise served code.search responses per (org, namespace, request shape)
    # in loom.code_search_memo and replay within the TTL (route "+memo");
    # `fresh=true` bypasses it.
    code_search_memo_enabled: bool = True
    # Seconds a memoised response, and a cursor's stored pool, stay replayable.
    code_search_memo_ttl_s: int = Field(default=1800, ge=1)
    # Max bytes of a response the memo stores; a larger one is not memoised.
    # 0 = no cap.
    code_search_memo_max_payload_bytes: int = 200_000
    # Seconds the BM25 corpus statistics (term df, chunk count, mean length)
    # are cached per process; 0 = no cache. Bounded by the term cap, oldest
    # out.
    code_corpus_stats_ttl_s: int = 300
    # Max terms whose df the per-process cache holds; the oldest goes first.
    code_corpus_stats_max_terms: int = 50_000
    # A computed code.search / code.grep stores its ranked pool (no bodies)
    # in loom.code_search_memo and issues `next_cursor`; the next page is a
    # point read. The pool size is capped below.
    code_search_cursor_enabled: bool = True
    # Units kept in a cursor's ranked pool; capped at 200.
    code_search_cursor_pool: int = Field(default=100, ge=1)
    # Wall deadline (ms) per namespace in a multi-namespace search; a late
    # namespace is dropped and named in `fanout.dropped`.
    code_search_fanout_timeout_ms: int = 3000
    # Max namespaces one request fans out to; can only lower the wire cap of 8.
    code_search_fanout_max_namespaces: int = 8
    # A `pattern` admitting more files than this is refused with
    # `pattern_too_broad` and the count (1,500 = 5% of a large namespace's files).
    code_search_pattern_max_files: int = 1500
    # The card leg: file cards are searched by cosine and the top files' chunks
    # join fusion as a capped RRF list (the history leg's shape). No per-request
    # override.
    code_search_card_leg: bool = False
    # Files the card leg votes for.
    code_search_card_leg_files: int = 15
    # Skeleton chunks per voted file (the history leg's shape).
    code_search_card_leg_units_per_file: int = 2
    # What a voted file contributes: `skeleton` (its class skeletons) or
    # `best_chunk` (its source chunks nearest the query by exact cosine).
    code_search_card_leg_mode: Annotated[Literal["skeleton", "best_chunk"], CaseFolded] = "skeleton"
    # Max share of the best two-leg RRF score a card row contributes.
    code_search_card_leg_cap: float = 1.0
    # CSV of prose question kinds the card leg votes on; `any` = all.
    code_search_card_leg_kinds: str = "responsibility"
    # Reranker code.search and code.ask use when the request names none (same
    # values as the per-request `rerank`: a builtin or a name in
    # LOOM_RERANK_ENDPOINTS_JSON; rerank="none" turns it off). Empty = no default rerank.
    code_search_rerank_default: str = ""
    # Prose question kinds the default rerank applies to (`any` = all);
    # symbol-shaped or constrained searches are never default-reranked.
    code_search_rerank_kinds: str = "responsibility"
    # How many fused units the default rerank reads.
    code_search_rerank_top: int = 20
    # Characters of each candidate shown to the default rerank (path and
    # signature first, then the body head).
    code_search_rerank_excerpt_chars: int = 300
    # Fusion multiplier for settings-catalogue units (Settings.cpp and kin) on
    # the kinds below; 1.0 = off.
    code_search_catalogue_penalty: float = 1.0
    # Fusion multiplier for prose files (documentation, release notes, changelogs)
    # unless the query asks for documentation or the search is constrained to paths;
    # 1.0 = off.
    code_search_prose_penalty: float = 0.25
    # The question kinds the catalogue penalty applies to.
    code_search_catalogue_kinds: str = "responsibility"
    # Lexical list's RRF weight for a responsibility question (vector = 1.0).
    # 1.0 = equal weights.
    code_search_rrf_lexical_weight_responsibility: float = 1.0
    # A prose question whose nearest file card's cosine is below this
    # abstains `off_domain`; independent of the card leg. 0 = off.
    code_search_abstain_card_below: float = 0.0
    # Calibrated confidence below which a result is flagged
    # `low_calibrated_confidence` (still returned). The deployment sets 0.30.
    code_search_abstain_confidence_below: float = 0.35
    # The calibrated confidences from which a code.search result reads
    # `retrieval_risk` "medium" and "low" (below the first it reads "high").
    # Code embeddings score a right hit lower than memory text does: on the
    # conceptual question set every right top 3 scored 0.40–0.63, so the
    # memory plane's 0.55 / 0.75 labelled three in four right answers "high".
    code_search_medium_risk_from: float = 0.40
    code_search_low_risk_from: float = 0.55
    # The question kinds the hypothetical answer votes on.
    code_search_hyde_kinds: str = "responsibility"
    # The list's cap × the two-leg top (the history rule).
    code_search_hyde_cap: float = 1.0
    # The passage's length budget.
    code_search_hyde_max_tokens: int = 220
    # Default code.search request budget (ms) when the request sets no
    # `deadline_ms`; what it cuts (late legs, rerank) makes the answer
    # `partial`. 0 = none.
    code_search_deadline_ms: int = 0
    # Ms a model-backed leg (HyDE) may run past the last plain leg before it
    # is dropped. 0 = no bound.
    code_search_llm_leg_grace_ms: int = 0
    # Default `llm_legs`: `auto` runs the configured model-backed legs; `none`
    # skips them.
    code_search_llm_legs_default: Annotated[Literal["auto", "none"], CaseFolded] = "auto"
    # Max share of the best two-leg RRF score a concept unit contributes.
    code_search_concept_expand_cap: float = 0.5
    # Identifiers + phrases kept from the model's answer.
    code_search_concept_expand_max_terms: int = 8
    # CSV of prose question kinds the concept leg expands; `any` = all.
    code_search_concept_expand_kinds: str = "responsibility"
    # Expansion tokens below this BM25 idf are dropped (2.0 ≈ in more than an
    # eighth of chunks).
    code_search_concept_expand_min_idf: float = 2.0
    # History leg: merged-PR text scored lexically votes for the files each PR
    # touched, as a third RRF list. "off" | "issue" (issue-shaped queries) |
    # "always".
    code_search_pr_leg: Literal["off", "issue", "always"] = "issue"
    # PRs read per query, files that may vote, chunk units per voted file.
    code_search_pr_leg_pool: int = 30
    # Files that may vote per query.
    code_search_pr_leg_max_files: int = 10
    # Chunk units a voted file contributes.
    code_search_pr_leg_units_per_file: int = 2
    # A namespace with fewer live voting PRs than this skips the leg.
    code_search_pr_leg_min_prs: int = 500
    # Per-unit cap on the leg's contribution as a fraction of the top two-leg
    # fusion score.
    code_search_pr_leg_cap: float = 0.5
    # Only files under these prefixes (CSV) receive votes; "" = every file.
    code_search_pr_leg_path_prefixes: str = ""
    # History intent: a question about a change is answered from the PR plane
    # as a `history` section. "off" | "number" (explicit issue/PR numbers,
    # point reads) | "on" (also why/when/who-changed phrasings).
    code_search_history_route: Literal["off", "number", "on"] = "number"
    # PR rows served in the section (no bodies; inside the token budget).
    code_search_history_max_prs: int = 3
    # Max PR files of a number intent that anchor the ranking (present files
    # stay, absent ones insert at rank 1); a phrase intent never anchors.
    code_search_history_anchor_max_files: int = 5

    # --- measured coverage ---------------------------------------------------
    # Import per-test line coverage from an instrumented CI run into the test
    # map (`method='coverage'`) and the per-file executed lines
    # (`code_coverage_files`). Off by default; `loom code coverage-import`
    # runs one import by hand whatever this says.
    code_coverage_import_enabled: bool = False
    # ClickHouse HTTP endpoint holding the coverage tables
    # (`checks_coverage_lines`, `checks`); "" = no source, the import is a no-op.
    code_coverage_source_url: str = ""
    # User and password for the source endpoint.
    code_coverage_source_user: str = ""
    code_coverage_source_password: str = ""
    # Database of the source tables.
    code_coverage_source_database: str = "default"
    # Namespaces (CSV) whose repository the source's runs cover; every other
    # namespace is skipped.
    code_coverage_namespaces: str = ""
    # The run's coverage is read in this many slices by file hash, two
    # statements per slice. One slice must fit the source server's per-query
    # memory, and a source with a per-statement-shape hourly quota (a public
    # playground allows 100) must take a whole import plus a retry: a nightly
    # run read in 16 slices is 16 statements of each shape.
    code_coverage_shards: int = Field(default=16, ge=1, le=4096)
    # A line executed by more tests than this names none of them (shared
    # plumbing every query runs); a test-map row needs a line under the cap.
    code_coverage_specific_max_tests: int = Field(default=200, ge=1)
    # Seconds one source query may take before the import stops (no rows
    # are tombstoned by an import that did not finish).
    code_coverage_source_timeout_s: float = 300.0
    # Check names (a LIKE pattern) whose runs carry per-test coverage.
    code_coverage_check_pattern: str = "%per_test_coverage%"
    # Jobs of one run start within this many hours of its newest job.
    code_coverage_run_window_hours: int = Field(default=6, ge=1)
    # Seconds between import passes, and the pass's hard deadline.
    code_coverage_import_interval_seconds: float = 86400.0
    code_coverage_import_max_pass_seconds: float = 7200.0

    def code_coverage_namespace_set(self) -> frozenset[str]:
        """`code_coverage_namespaces` as a set."""
        return frozenset(n.strip() for n in self.code_coverage_namespaces.split(",") if n.strip())

    def code_bot_author_names(self) -> frozenset[str]:
        """`code_bot_authors` as a set of lower-cased logins."""
        return frozenset(a.strip().lower() for a in self.code_bot_authors.split(",") if a.strip())
