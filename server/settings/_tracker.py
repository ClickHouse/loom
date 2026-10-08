"""The tracker plane: records, duplicate checks, bisect."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from server.settings._env import JsonObject


class TrackerSettings(BaseModel):
    # --- tracker record, read live -------------------------
    # Per-statement `max_execution_time` on the remote, seconds; the client's receive
    # timeout is this plus 5 s.
    tracker_live_timeout_s: float = 1.5
    # Caller-side budget per read, ms: past it the read is `tracker_unavailable` and
    # counts against the breaker.
    tracker_live_budget_ms: int = 300
    # Process cache TTL for live rows, seconds (upstream refreshes every 5–10 min); an
    # empty result is kept at most 60 s.
    tracker_live_cache_ttl_s: float = 300.0
    # Kill switch: off = every tracker read answers `tracker_unavailable`.
    tracker_live_serve: bool = True
    # Budget of the bisect candidate read, which scans a release range's diffs, ms: a
    # triage step, not an interactive one.
    tracker_live_bisect_budget_ms: int = 6000
    # Budget of the text-index reads (similar bugs, threads, changelog, PRs by
    # path, area escalations / incidents by term), ms; above the 300 ms interactive
    # budget.
    tracker_live_text_budget_ms: int = 900
    # Breaker per credential: consecutive failures that open it, and how long
    # it stays open before one probe read.
    tracker_circuit_failure_threshold: int = 3
    # Seconds an open breaker stays open before one probe read (at least 1 s).
    tracker_circuit_cooldown_s: float = Field(default=120.0, ge=1.0)
    # --- tracker org and code-namespace bindings ------------------------------
    # The loom org the tracker namespaces belong to; empty (default) = none, so no
    # tracker namespace serves and self-service sources are refused until it is set.
    tracker_org: str = ""
    # Which tracker namespace a code namespace reads its facts from:
    # glob keys → namespace, e.g. {"code-myrepo*": "tracker-myrepo"}. Numbers are
    # repository-global, so every branch namespace binds to its repository's one
    # tracker namespace. '' = no QA facts anywhere.
    tracker_code_namespace_bindings_json: Annotated[str, JsonObject] = ""
    # --- tracker sources and the record contract ------------------------------
    # Every tracker source and the namespaces that read them:
    # {"sources": {"<name>": {"host": "x.clickhouse.cloud", "port": 8443,
    #   "secure": true, "database": "loom_tracker"}},
    #  "namespaces": {"qa-<team>": {"source": "<name>", "repos": ["Org/repo"],
    #   "restricted": false}}}. A namespace naming an unknown source does not serve (one
    # error line, never a guess).
    tracker_sources_json: Annotated[str, JsonObject] = ""
    # The repositories whose numbers name a restricted namespace's escalations and core
    # incidents, as `Org/repo`: {"escalation": "Org/escalations", "incident":
    # "Org/incidents"}. `escalation_context` / `incident_context` read the record
    # `Org/repo#<number>`; a kind with no repository here reads no record by number.
    tracker_restricted_repos_json: Annotated[str, JsonObject] = ""
    # Credentials per source, kept apart from the map above:
    # {"<name>": {"user": "...", "password": "..."}}.
    tracker_credentials_json: Annotated[str, JsonObject] = ""
    # A source serves only while its schema meets the record contract
    # (docs/features/tracker/record-contract.md), checked through its own credential once per
    # this many seconds (at least 1 s); a failed check retries after 60 s.
    tracker_contract_ttl_s: float = Field(default=3600.0, ge=1.0)
    # --- self-service tracker sources -----------------------------------------
    # Self-service tracker sources: any signed-in org member registers
    # one (`tracker.source.add`); credential and schema are checked before any write.
    # Off = only the operator's map serves; loom.qa_sources rows are
    # ignored. `_max_sources` caps the registrations.
    tracker_self_service_enabled: bool = True
    # At most this many self-service sources an org may register; a further
    # `tracker.source.add` is refused.
    tracker_self_service_max_sources: int = 10
    # A registered host must resolve to a public address (no loopback, RFC 1918,
    # link-local, CGNAT 100.64/10 or multicast); True lets a dev stack point
    # at a local ClickHouse.
    tracker_self_service_allow_private_hosts: bool = False
    # How often a process re-reads loom.qa_sources (seconds); a write in this
    # process refreshes at once.
    tracker_sources_refresh_seconds: float = 60.0
    # --- similar items (tracker.similar) --------------------------------------
    # Whether tracker.similar's vector leg (over
    # loom.qa_item_embeddings, filled by the `tracker_embed` pass) is in the default
    # leg set; a caller naming `vector` in `legs` runs it regardless. The leg only adds
    # candidates, after the ranked legs (the agreement boost below aside).
    tracker_similar_vector_leg: bool = True
    # The agreement boost: a tracker.similar candidate found by a ranked leg AND the
    # vector leg gains the vector leg's RRF contribution times this weight (1.0 = counts
    # like a ranked leg, 0.5 = a tie-breaker) and the ranked set is re-sorted;
    # vector-only candidates stay appended.
    tracker_similar_agreement_weight: float = 1.0
    # The deepest vector-leg rank that may boost a ranked candidate; 0 = any rank.
    tracker_similar_agreement_max_rank: int = 5
    # Ms the vector leg waits for its query-time catch-up (embedding the
    # items changed since the pass's cursor), run beside the cosine scan. Past it the
    # call answers from the table (`query.fresh_pending = true`) and the embedding
    # finishes in the background for the next call.
    tracker_similar_catch_up_budget_ms: int = 300
    # --- the tracker_embed pass --------------------------------------------------
    # Every interval (seconds), for each configured tracker namespace, the issues and
    # PRs open or closed within `closed_days` and changed since the last pass are
    # embedded (`title + "\n" + left(body, body_chars)`, `tracker_embed_batch` a call)
    # into loom.qa_item_embeddings; an unchanged text (sha256) is skipped, an item that
    # left the window is tombstoned. Off = nothing is written; the leg serves the table
    # plus its query-time catch-up. The interval bounds only that catch-up, never what
    # a query can find.
    tracker_embed_enabled: bool = True
    # Seconds between `tracker_embed` passes.
    tracker_embed_interval_seconds: float = 1800.0
    # The embedding window for PRs, days: a PR closed longer ago is not embedded
    # and one that leaves the window is tombstoned.
    tracker_embed_closed_days: int = 365
    # The window for ISSUES, days (3650 = every closed issue the record holds):
    # labelled duplicate targets are often closed long before the draft. PRs keep
    # `tracker_embed_closed_days`.
    tracker_embed_issue_closed_days: int = 3650
    # Body characters embedded per item. Must stay under the
    # embedder's 8,191-token input limit (a stack trace runs ~2 chars a token; one
    # oversized text fails the whole batch). A change needs a re-embed
    # (`loom tracker-embed-reset`, then the pass).
    tracker_embed_body_chars: int = 8000
    # Items sent to the embedder in one call (at least 1).
    tracker_embed_batch: int = Field(default=200, ge=1)
    # The nearest read's codes-shortlist over-fetch on
    # loom.qa_item_embeddings (x10's misses are BFloat16 rounding only).
    tracker_embed_fetch_multiplier: float = 10.0
    # Seconds the issue corpus's per-token document frequency (for
    # BM25F) is cached per process; the upstream index cannot count it inside a budget.
    tracker_similar_idf_ttl_s: float = 3600.0
    # --- an issue names the PRs that probably fixed it -----------------------
    # A daily pass ranks, for every issue opened in the last `issue_days`, the merged
    # trunk PRs of the `window_days` after it (from a week before) by BM25F over title
    # and body, the issue's stack frames in the PR's diff, the closing mention and the
    # component, and writes each issue's top `per_issue` into
    # loom.qa_link_candidates. Off = nothing is written; `tracker.links` serves the table.
    tracker_links_enabled: bool = True
    # Seconds between `tracker_links` passes.
    tracker_links_interval_seconds: float = 86400.0
    # Issues opened within this many days are ranked each pass.
    tracker_links_issue_days: int = 90
    # Days after an issue opened in which a merged trunk PR is a fix candidate.
    tracker_links_window_days: int = 90
    # The top candidates written per issue into loom.qa_link_candidates.
    tracker_links_per_issue: int = 20
    # Score = bm25_norm (the pair's BM25F as a share of the issue's best)
    # + weight_mention × (1 closing keyword, 0.5 bare mention) + 0.05 × component_match.
    # The stack-frames-in-diff features ride the row unscored.
    tracker_links_weight_mention: float = 1.5
    # A consumer (code.backport_gap's issue path, the bisect's fix candidate) takes the
    # top candidate as the fix only above `min_score` AND when a closing keyword names
    # the issue. tracker.links serves every row with its score.
    tracker_links_min_score: float = 1.0
    # --- tracker.duplicate_check -------------------------------------------------
    # A draft's verdict: `duplicate_of` when the top candidate's calibrated confidence
    # reaches this, `related` when any candidate reaches the related threshold, else
    # `none` with no items. Both are probabilities from a logistic fit on labelled
    # duplicate pairs, so they survive a refit; the scale is compressed. Measured `none`
    # on 99% of negatives at 0.25.
    tracker_dupcheck_duplicate_threshold: float = 0.25
    # the lower band's floor
    tracker_dupcheck_related_threshold: float = 0.05
    # The fix-in-flight leg reads PRs from the last this many days.
    tracker_dupcheck_fix_path_days: int = 365
    # At most this many PRs the fix-in-flight leg reads per draft.
    tracker_dupcheck_fix_path_prs: int = 20
    # A leg whose read failed for a transient reason (a timeout, a connection
    # error, an upstream 5xx) is run once more inside the op budget; one that still
    # fails leaves the answer `epistemic: degraded` and `leg_status` names it with its
    # reason. The pause before that retry, ms (at most 200).
    tracker_dupcheck_leg_retry_delay_ms: int = Field(default=50, ge=0, le=200)
    # What the retry leaves of the op budget for the steps after the legs
    # (hydrate, fit, judge), ms.
    tracker_dupcheck_leg_retry_reserve_ms: int = 300
    # The shortest retry worth starting, ms: with less left the retry is skipped
    # and `leg_status` says `retry_skipped_budget`.
    tracker_dupcheck_leg_retry_min_ms: int = 200
    # How many tracker namespaces a duplicate check addressed to a code
    # namespace (or its bound tracker namespace) reads when no `namespaces` list is
    # given: every readable tracker record, the bound one first. 1 = the bound one only.
    tracker_dupcheck_fanout_namespaces: int = Field(default=4, ge=1)
    # Bounds the fitted duplicate threshold is clamped into, so one odd fit
    # cannot make every candidate (or none) a duplicate.
    tracker_dupcheck_duplicate_threshold_min: float = 0.15
    # Upper bound of the fitted duplicate threshold.
    tracker_dupcheck_duplicate_threshold_max: float = 0.5
    # The fit (coefficients, curve, holdout) is JSON in loom.code_index_state under the
    # tracker namespace, refitted by the tracker_links pass once older than the refit
    # interval by replaying every gold query at its filing time; `fit_negatives`
    # non-duplicates are sampled by hash per fit. Off = the stored fit keeps serving;
    # with none stored the op answers `uncalibrated`.
    tracker_dupcheck_refit_enabled: bool = True
    # 7 days
    tracker_dupcheck_refit_interval_s: float = 604800.0
    # the negatives sampled per fit
    tracker_dupcheck_fit_negatives: int = 400
    # The judge stage. On a `related` answer one model call reads
    # the draft against the top `judge_top_k` issue candidates' title + body head
    # (`judge_body_chars`) and may promote a same-defect one to `duplicate` (never
    # demotes; a recorded related_to never promotes). `judge_model` "" = default client.
    # Benched on a labelled duplicate set.
    tracker_dupcheck_judge_enabled: bool = True
    # Issue candidates the judge reads (at least 1).
    tracker_dupcheck_judge_top_k: int = Field(default=5, ge=1)
    # Body characters per candidate the judge reads.
    tracker_dupcheck_judge_body_chars: int = 1500
    # The rerank stage. One model call reads the draft against a pool of
    # each leg's own head (`_rerank_per_leg` rows a leg) plus the issues the tracker
    # graph links to the leading candidates; its same/related picks lead `nearest`, the
    # verdict stays the fit's. Fails open.
    tracker_dupcheck_rerank_enabled: bool = True
    # Rows each leg contributes to the rerank pool; also deepens the rows kept
    # per leg when it exceeds the default head.
    tracker_dupcheck_rerank_per_leg: int = Field(default=50, ge=1)
    # The candidates one rerank call reads (0 = dup_rerank.POOL_CAP,
    # 40); 200 lets a 50-per-leg pool through a fan-out's namespaces.
    tracker_dupcheck_rerank_pool_cap: int = 200
    # At most this many candidates the rerank pool holds for one namespace's
    # legs (after dedup, the legs taking turns).
    tracker_dupcheck_rerank_ns_cap: int = Field(default=60, ge=1)
    # A rerank `same` row at or above this is offered as a lead
    # ("nearest reported: … — may be related, check it"), never as the verdict.
    tracker_dupcheck_lead_min: float = Field(0.9, ge=0.0, le=1.0)
    # Body characters per candidate the rerank call reads.
    tracker_dupcheck_rerank_body_chars: int = 200
    # Seconds the rerank call may take; past it the rerank reports
    # error="timeout" and the verdict stands without it.
    tracker_dupcheck_rerank_timeout_s: float = Field(default=4.0, gt=0.0)
    # --- bisect without a stack frame ----------------------------------------
    # When no frame resolves to a file, code.bisect_candidates(issue_number) scans the
    # release window only for PRs whose diff carries one of this many of the issue's
    # rarest patterns (IDF over the issue corpus). 0 turns the leg off.
    tracker_bisect_scope_patterns: int = 6
    # The issue's component widens the scan to at most `component_dirs` `src/`
    # directories its PRs touch, each with `min_lift` × the share every trunk PR has (a
    # hub directory says nothing); the aggregate is cached per component for `ttl_s`.
    tracker_bisect_component_dirs: int = 8
    # A component directory counts only when its share of the component's PRs
    # is at least this multiple of its share of every trunk PR.
    tracker_bisect_component_min_lift: float = 1.5
    # Seconds the per-component directory aggregate is cached.
    tracker_bisect_component_ttl_s: float = 3600.0
    # --- a failing test is read against its own CI history -------------------
    # The FROM clause of the checks reads; '' = none. A tracker names
    # its own (`checks` table, or its source entry's `checks.from`); this is the
    # deployment-wide fallback. Name the table function, not a view over it: a view's
    # filter runs on the QA side, so a hot test's read hits play's 1 GB result cap.
    # ClickHouse's public CI (the `play` user is public) is
    # "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')".
    tracker_checks_from: str = ""
    # the repository whose CI `tracker_checks_from` is ('' = the namespace's
    # only repository)
    tracker_checks_repo: str = ""
    # Budget of the per-test history read, ms: play scans the whole window by its time
    # key, far past the 300 ms interactive budget.
    tracker_live_checks_budget_ms: int = 8000
    # Window of the history read, days; churn window of the ledger leg, days.
    tracker_test_signal_window_days: int = 30
    # Churn window of the ledger leg, days (at least 1).
    tracker_test_signal_churn_days: int = Field(default=54, ge=1)
    # Verdict thresholds: `min_runs` below which nothing is said;
    # `flip_rate` (status changes between consecutive runs of one PR/check sequence)
    # that reads as flaky; a storm hour holds ≥ K other failing tests across
    # ≥ `cofail_prs` PRs, K = max(`cofail_k`, `cofail_multiple` × the window's median
    # failing tests per hour); `cofail_share` of the test's failing hours must be storm
    # hours for `infra_candidate`; a test failing on ≥ `cofail_prs` PRs in one hour is
    # a shared cause by itself.
    tracker_test_signal_min_runs: int = 5
    # Status-change rate between consecutive runs of one PR/check sequence at
    # or above which a test reads as flaky.
    tracker_test_signal_flip_rate: float = 0.02
    # Floor of K, the other failing tests an hour needs to be a storm hour.
    tracker_test_signal_cofail_k: int = 100
    # K is at least this multiple of the window's median failing tests per hour.
    tracker_test_signal_cofail_multiple: float = 5.0
    # A storm hour spans at least this many PRs; a test failing on this many
    # PRs in one hour is a shared cause by itself.
    tracker_test_signal_cofail_prs: int = 3
    # Share of the test's failing hours that must be storm hours for
    # `infra_candidate`.
    tracker_test_signal_cofail_share: float = 0.5
    # --- the per-test daily roll-up and the CI prior read from it -------------
    # The tracker_test_daily worker rolls the CI table into loom.qa_test_daily once per
    # day per test, so tracker.test_signal reads a key range here and the remote only
    # for hours no roll-up covers. Off = the tables stop filling; the op falls back to
    # the live read of the whole window.
    tracker_test_daily_enabled: bool = True
    # Pass interval, seconds; today (and yesterday inside the settle lag) is re-rolled
    # every pass as a partial day, so the live tail the op reads is at most one
    # interval plus the current hour.
    tracker_test_daily_interval_seconds: int = 1800
    # How far back a cold worker rolls, in days (the op's largest window is 90).
    tracker_test_daily_backfill_days: int = 30
    # Days rolled per pass and the pause between remote statements: the shared
    # `play` user is metered at 100 statements of one shape per hour, and the
    # op's live reads share that quota.
    tracker_test_daily_max_days_per_pass: int = 6
    # Seconds paused between remote statements of a pass; 0 = no pause.
    tracker_test_daily_pace_s: float = 5.0
    # Hours after a day ends before its roll is final: a check's row lands
    # when the check finishes, not when it starts.
    tracker_test_daily_lag_hours: float = 3.0
    # Budget of one roll-up statement, ms.
    tracker_test_daily_budget_ms: int = 60000
    # --- the static smell leg -------------------------------------------------
    # The static smell leg reads at most this many chunks of the test's home
    # file(s) from the code plane (flaky-test smell tokens per category, counted).
    tracker_test_signal_smell_chunks: int = 6
