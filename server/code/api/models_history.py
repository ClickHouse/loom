"""Change history: code.bisect_candidates, code.backport_gap(s),
code.blame_range, code.symbol_history, code.first_build and code.history.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from server.code.api.models_common import _CodeIdentity, _git_sha, _trim_namespaces

# What may bound a frameless bisect scan
_WIDENERS = frozenset({"patterns", "component", "fix_candidate"})


class CodeBisectCandidatesInput(_CodeIdentity):
    """Rank PRs that may have introduced a bug; Loom does not test or reproduce it.

    This is candidate prioritization, not automated reproduction or `git bisect`:
    Loom does not discover whether the bug exists or prove that a candidate caused it.
    The crashing frames
    (qualified symbol names from the stack trace) and/or source paths, and
    the release range — stated (`bad_version`, `good_version`) or taken from
    the triage columns of `issue_number` in the tracker
    (`reproduces_on` / `clean_on`, observations recorded upstream in the
    issue's triage comment). Loom resolves the frames to files through the code index
    and asks QA for the merged, non-derivative PRs whose first release lies
    in the range and that touched those files, ranked by the frames their
    diff names, each carrying marks — backport, revert, reverted, re-land,
    whitespace / rename-only, `touches_symbol` from the diff's hunk headers
    (`marks=false` for the bare ranked list). With no frame (a wrong-result
    report) and an `issue_number`, the scan is bounded instead by the
    issue's rarest identifiers in the window's diffs, the component's
    directories and the fix's files (`wideners` names which, `widened_by`
    says which applied). Addressed at the CODE namespace; the tracker
    namespace is the one bound to it."""

    # Crashing frames from the stack trace (qualified symbol names, at most 12); the code
    # index resolves them to files, and their specific bare names rank the diffs.
    symbols: list[str] = Field(default_factory=list, max_length=12)
    # Repo-relative source paths to scan (at most 20), beside the frames' files.
    paths: list[str] = Field(default_factory=list, max_length=20)
    # Greppable literals from the report (an error code, an identifier in
    # the message, a setting name); with `issue_number` they are also derived
    # from the issue's text. A PR whose diff ADDED one outranks a PR that
    # only touched the file; a rare one outranks a common one (IDF over the
    # issue corpus).
    patterns: list[str] = Field(default_factory=list, max_length=12)
    # A tracker issue number: Loom reads but does not verify its previously recorded
    # triage observations for the release range, its text for patterns, and its
    # `caused_by` / `fixes` edges for the named culprit and fix. 0 = none.
    issue_number: int = Field(default=0, ge=0)
    # The report's own words (title, body, error) when there is no tracker
    # issue to read them from — they feed the fusion's vector and lexical
    # legs and the suspects the report names. With `issue_number` the
    # issue's text is read.
    text: str = Field(default="", max_length=8000)
    # One LLM call reranks the fused top 20 (+~2 s); a weak fused top reranks
    # without asking
    rerank: bool = False
    # Answer within this many milliseconds with the best candidates found so far. The
    # optional stages (fix blame, marks, the duplicate check's wait, the rerank) wait no
    # longer than what is left and are skipped when too little is; the scan always runs.
    # The answer names what was cut in `partial` / `partial_reason`. None or 0 = none.
    deadline_ms: int | None = Field(default=None, ge=0, le=60000)
    # The PR that fixed the regression, when one exists — its rewritten lines, blamed at
    # its parent, name the change that broke them. With `issue_number` the
    # record's `fixes` edge supplies it.
    fix_pr: int = Field(default=0, ge=0)
    # The first release on which the caller or an upstream test observed the bug
    # (`26.3`, or a full version cut to its minor); Loom does not test it, and the value
    # wins over the issue's `reproduces_on`. "" = from the issue.
    bad_version: str = Field(default="", max_length=16)
    # The inclusive upper bound of an approximate merge-time investigation window when
    # release observations are unavailable; it narrows candidate history and does not
    # mean Loom reproduced the bug. `after` is the optional lower bound (a known-good
    # time); `bad_sha` names a commit whose time becomes `before` (resolved on the code
    # mirror). Release versions still win when the caller has them.
    before: str = Field(default="", max_length=40)
    # Optional lower bound of the merge-time window (ISO-8601); must be earlier than `before`.
    after: str = Field(default="", max_length=40)
    # A commit (hex) whose commit time on the code mirror becomes `before` when `before`
    # is unset; a sha the mirror lacks is refused with `BAD_SHA`.
    bad_sha: str = Field(default="", max_length=64)
    # The newest release that the caller or an upstream test observed as clean; Loom
    # does not test it, and the value wins over the issue's `clean_on`. "" = from the
    # issue, or a lookback from `bad_version`.
    good_version: str = Field(default="", max_length=16)
    # widen the candidate set from the frames' files to their directories
    # when frames or patterns can rank within it
    widen_to_directories: bool = True
    # What may bound the scan when no frame resolves to a file — `patterns`
    # (the window's diffs carrying the issue's rarest identifiers),
    # `component` (the directories the issue's component touches),
    # `fix_candidate` (the fix's own files: the record's `fixes` edge, else
    # the link pass's candidate). Empty = the fix candidate's files only.
    wideners: list[str] = Field(
        default_factory=lambda: ["patterns", "component", "fix_candidate"], max_length=3
    )
    # Run the scan even when the record already names the culprit (an introducing commit
    # or a `caused_by` edge) — to check a pin; `introduced_commit` / `named_by_graph`
    # still say what the record holds
    rescan: bool = False
    # At most this many candidates (1–25).
    limit: int = Field(default=10, ge=1, le=25)
    # Marks on the candidates (backport, revert, reverted, re-land,
    # whitespace / rename-only, touches_symbol, named); false returns the
    # ranked candidate dicts unmarked
    marks: bool = True
    # "name at least one of symbols, paths, issue_number" is the handler's
    # check, raised after the namespace gate

    @field_validator("wideners")
    @classmethod
    def _known_wideners(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for w in v:
            name = str(w).strip()
            if name not in _WIDENERS:
                raise ValueError(f"wideners must be among {sorted(_WIDENERS)}, got {name!r}")
            if name not in out:
                out.append(name)
        return out

    # Resolved with `git log <bad_sha>` on the mirror.
    @field_validator("bad_sha")
    @classmethod
    def _bad_sha_is_a_sha(cls, v: str) -> str:
        return _git_sha(v)


class CodeBisectCandidatesOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_absent_candidate(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # fix_candidate is off the wire without a link candidate, and the
        # frameless fields are absent without a widened scan
        data: dict[str, Any] = handler(self)
        if data.get("fix_candidate") is None:
            data.pop("fix_candidate", None)
        for key in (
            "widened_by",
            "component",
            "component_dirs",
            "pattern_weights",
            "dropped_after_report",
            "fix_blame",
            "fusion",
            "dupcheck_fix",
            "partial",
            "partial_reason",
        ):
            if not data.get(key):
                data.pop(key, None)
        return data

    # candidates | no_candidates | already_bisected | graph_named | unknown
    verdict: str = "unknown"
    # Where the candidates came from — `tracker` (its PR diff scan), `code_history`
    # (loom's own PR ledger of the repository, when no tracker is linked or it holds
    # no changed files), or `none`
    candidates_from: str = "tracker"
    # Why the answer is thin or empty: tracker_unbound | tracker_unavailable |
    # no_failing_version | no_resolved_paths | introduced_commit_known | caused_by_edge.
    causes: list[str] = Field(default_factory=list)
    # always lower_bound: the upstream file list and the diff text are what
    # was searched; an empty answer excludes nothing
    epistemic: str = "lower_bound"
    # {pr_number, title, author, merged_at, minor_version, matched_paths,
    #  matched_symbols, matched_patterns, added_patterns, kind change|meta,
    #  has_test, url, score, merge_commit_sha, merge_sha_source stored|mirror|"",
    #  named_by?}; with `marks`:
    #  demoted? (backport|revert|reverted|whitespace_only|rename_only|meta_title),
    #  promoted?[] (touches_symbol|names_symbol|reland_inherits_symbol),
    #  touches_symbol? (true|named|false), reland_of?, reland_via?, relanded_by?
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    # what the marks stage did: {status ok|skipped|ledger_failed,
    # git_calls, demoted, promoted, relands}
    marks: dict[str, Any] = Field(default_factory=dict)
    # PR numbers the tracker graph's `caused_by` edges name for the issue;
    # when set, the candidates are these and no scan ran
    named_by_graph: list[int] = Field(default_factory=list)
    # What blaming the fix's rewritten lines found — the fix, its parent, the ranges
    # blamed and the PRs they name; absent without a fix
    fix_blame: dict[str, Any] = Field(default_factory=dict)
    # What the fusion read — {window_prs, signals: {name: count}, suspects: {named,
    # mentioned}, rerank?}; each fused candidate carries `why` (the signals that ranked
    # it in their top 10); absent when fusion did not run. `margin` and `weak` (whether
    # the rerank and the wider window ran), rerank.{trigger, top_p, wider_window,
    # confidence}, and `note` when a weak top stays unconfirmed — the answer is then a
    # lead, not a cause
    fusion: dict[str, Any] = Field(default_factory=dict)
    # the greppable literals the ranking used (given ∪ derived from the issue)
    patterns: list[str] = Field(default_factory=list)
    # IDF over the issue corpus per pattern — what each is worth in the ranking; absent
    # when the vocabulary was not read
    pattern_weights: dict[str, float] = Field(default_factory=dict)
    # file | directory | patterns — how wide the candidate set was drawn
    scope: str = "file"
    # Which frameless wideners bounded the scan — patterns | component | fix_candidate —
    # in the order they applied; absent when a frame or a path resolved
    widened_by: list[str] = Field(default_factory=list)
    # With no fix_pr and no issue, the duplicate check matched the report to a reported
    # issue whose linked merged fix became fix_pr — {issue, fix_pr, match: duplicate,
    # namespace}; absent otherwise
    dupcheck_fix: dict[str, Any] = Field(default_factory=dict)
    # the issue's QA component and the directories it widened the scan to
    component: str = ""
    # The directories the issue's component widened the scan to; absent when none did.
    component_dirs: list[str] = Field(default_factory=list)
    # Scanned PRs merged AFTER the issue was opened — the fix, or nothing — left out of
    # the list; absent when none was
    dropped_after_report: int = 0
    # The release minors whose PRs were scanned; empty when a time window bounded it.
    versions_searched: list[str] = Field(default_factory=list)
    # The failing release the range used (given, else the issue's); "" when unknown.
    bad_version: str = ""
    # The clean release the range used (given, else the issue's); "" when unknown.
    good_version: str = ""
    # clean_to_bad_plus_slack | lookback_from_bad | master_after_clean — how
    # the release range was drawn
    window: str = ""
    # hub files (Settings.cpp, CMake, docs, …) left out of the file filter
    hub_paths_dropped: list[str] = Field(default_factory=list)
    # [{symbol, paths}] — what the code index resolved each frame to
    resolved: list[dict[str, Any]] = Field(default_factory=list)
    # The files the scan filtered on: fix files, given paths, the frames' files and the
    # widening's, with hub files removed.
    paths: list[str] = Field(default_factory=list)
    # The bare frame names the diffs were ranked by: distinct, at least 6 characters,
    # generic names dropped.
    frames: list[str] = Field(default_factory=list)
    # the issue's QA fact, as the tracker serves it, when issue_number was given
    issue: dict[str, Any] = Field(default_factory=dict)
    # The introducing commit the issue record holds; "" when none.
    introduced_commit: str = ""
    # The issue's probable fix from the link pass when no `caused_by` edge and no
    # introducing commit exist (score above the floor) — or the record's own `fixes`
    # edge (`source: record`); `paths_used` on it when its source files became the
    # scan's paths because no frame resolved to a file; absent otherwise
    fix_candidate: dict[str, Any] | None = None
    # The tracker namespace bound to this code namespace that was read; "" when none
    # is bound.
    tracker_namespace: str = ""
    # True when a tracker is bound but its read failed.
    tracker_unavailable: bool = False
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
    # `deadline_ms` cut an optional stage: the candidates are the best found in the
    # budget, and `partial_reason` names the stages that did not run or finish. Off the
    # wire otherwise.
    partial: bool = False
    partial_reason: str = ""
    # The time window the scan ran under, when no release versions bounded it ("" otherwise).
    before: str = ""
    # The window's lower bound (ISO-8601 UTC); "" when releases bounded the scan or no
    # lower bound was given.
    after: str = ""


class CodeBackportGapInput(_CodeIdentity):
    """Which release branches still carry the bug this fix removed, and the exact PR to
    cherry-pick there? Name the merged fix (`pr_number`) or the issue it closes
    (`issue_number`: the tracker graph's `fixes` edges name the PR). Per release-branch
    namespace of the same repository (every one configured, newest first, or `branches`
    — namespaces or versions such as `26.4`), three legs meet: the tracker's
    backport record (`is_backport_pr`, `upstream_pr_number` / "Backport #N to X:",
    `base_branch`, merged or open) beside the branch ledger's merged backports; the
    fix's first release (`minor_version`: a branch cut at or after it carries the fix);
    and code.symbol presence of the functions the fix's hunks touch on each branch (≤
    `max_symbols` fan-outs; the fix's source files when the hunks name nothing). Verdict
    per branch: `gap` (code present, no backport — with `cherry_pick`), `pending` (a
    backport PR is open), `covered`, `not_affected`, `unknown`. `exact: true` adds
    hunk-level patch presence per branch: the fix's removed lines still on the branch
    with its added lines absent make `gap` exact (basis `hunk`); its added lines present
    with the removed lines gone and no backport in the record make `not_affected` exact
    (the branch already holds the fixed state); `presence.patch` discloses the mode
    (exact | symbol_only), the line counts and the token-bag overlaps (disclosed, never
    a verdict). Addressed at the trunk's CODE namespace; the tracker namespace is the
    one bound to it."""

    # The merged trunk fix to check; 0 = take it from `issue_number`.
    pr_number: int = Field(default=0, ge=0)
    # An issue the fix closes: the tracker graph's `fixes` edges name the PR.
    issue_number: int = Field(default=0, ge=0)
    # Release branches to check, as namespaces or versions (`26.4`), at most 8; empty =
    # every configured release branch of the repository the caller can read.
    branches: list[str] = Field(default_factory=list, max_length=8)
    # At most this many touched functions (1–8) are looked up on each branch.
    max_symbols: int = Field(default=8, ge=1, le=8)
    # Hunk-level presence per branch, on request (one chunk read per branch and the
    # fix's diff sections from the tracker record)
    exact: bool = False
    # For the branches the lines leave undecided, the fix's removed lines on the
    # branch's own history in the mirror (git grep at the tip, git log -S over the
    # first-parent history; at most 3 lines, 2 s per branch) — the pre-image at the tip
    # is `gap` exact (basis `history`), never carried or removed since is
    # `not_affected`. Implies `exact`.
    history: bool = False

    @field_validator("branches")
    @classmethod
    def _branches_trimmed_and_unique(cls, v: list[str]) -> list[str]:
        return _trim_namespaces(v)

    # "pr_number or issue_number" is enforced in the handler after the namespace
    # gate (as code.review_brief does), so a refused principal reads the same
    # refusal as everyone.


class CodeBackportGapOutput(BaseModel):
    # Configured release branches this caller cannot read,
    # skipped (never silently): [{namespace, reason}]. Empty when branches were named.
    skipped_branches: list[dict[str, str]] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _drop_absent_candidate(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # fix_candidate is off the wire without a link candidate
        data: dict[str, Any] = handler(self)
        if data.get("fix_candidate") is None:
            data.pop("fix_candidate", None)
        return data

    # gap | pending | covered | not_affected | unknown — the worst branch's word
    verdict: str = "unknown"
    # tracker_unbound | tracker_unavailable | fix_not_found | no_fix | no_branches |
    # per-branch unknown reasons (deadline, no_index, no_branch_version, …)
    causes: list[str] = Field(default_factory=list)
    # {pr_number, title, url, base_branch, minor_version, merged_at, merge_sha,
    #  changelog_category, src_components, files, hunk_contexts, ledger} plus,
    #  under `exact`, `patch: {files, hunks, added, removed, symbols}`
    fix: dict[str, Any] = Field(default_factory=dict)
    # from issue_number: {number, title, affected_versions, fix_versions, fixed_by}
    issue: dict[str, Any] = Field(default_factory=dict)
    # every PR the issue's `fixes` edges name, with merged / backport flags
    fixes: list[dict[str, Any]] = Field(default_factory=list)
    # the touched functions presence was tested with (from the hunk headers)
    symbols: list[str] = Field(default_factory=list)
    # the fix's source files (the file leg when no hunk named a function)
    files: list[str] = Field(default_factory=list)
    # one row per branch, gaps first, newest branch first: {namespace, branch,
    #  verdict, epistemic exact|lower_bound, basis, reason, backport, presence,
    #  cherry_pick {pr_number, merge_sha}, evidence [...]}; under `exact`,
    #  presence.patch = {mode exact|symbol_only, added_total, added_present,
    #  removed_total, removed_gone, hunks, functions_matched, chunks_read,
    #  new_tokens, new_token_overlap, dropped_tokens, dropped_token_overlap,
    #  similar, reason} and basis may read hunk
    branches: list[dict[str, Any]] = Field(default_factory=list)
    # verdict → [branch versions]
    summary: dict[str, list[str]] = Field(default_factory=dict)
    # When no `fixes` edge named a trunk PR, the link pass's top candidate above
    # LOOM_TRACKER_LINKS_MIN_SCORE stood in as the fix ({pr_number, score, rank, why,
    # features, …, epistemic candidate}) and `fix.source` reads `candidate`; absent
    # otherwise
    fix_candidate: dict[str, Any] | None = None
    # The tracker namespace bound to the trunk namespace that was read; "" when none
    # is bound.
    tracker_namespace: str = ""
    # True when a tracker is bound but its read failed.
    tracker_unavailable: bool = False
    # Wall time of the whole read, in milliseconds.
    ms: int = 0
    # {fix: ms of the record + ledger stage, presence: ms of the presence stage,
    #  patch: ms of the hunk-level stage (under `exact` only)}
    stage_ms: dict[str, int] = Field(default_factory=dict)
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


class CodeBackportGapsInput(_CodeIdentity):
    """The set-wide backport report: which merged fixes labelled for
    backport does a release branch still owe? Rows come from
    loom.code_backport_gaps, written nightly by the `code_backport_gaps`
    worker — one `code.backport_gap(exact, history)` per labelled fix of the
    last 90 days — gaps first, newest fix first, with counts per verdict.
    `branch` names one release-branch namespace or version (`26.6`); unset
    reads every configured release branch of the repository. `wanted_only`
    (default) keeps the fixes the maintainers labelled (`pr-must-backport*`
    on the ledger, QA's `backport_required`); every row carries whether a
    `v<branch>-must-backport` label names that branch (`wanted_branch`).
    Addressed at the trunk's CODE namespace; every branch namespace is
    authorized first, fail closed."""

    # One release branch, as a namespace or a version (`26.6`); "" = every configured
    # release branch the caller can read.
    branch: str = Field(default="", max_length=128)
    # Keep only the fixes labelled for backport; false also returns unlabelled fixes.
    wanted_only: bool = True
    # Only fixes merged in the last this many days (1–365).
    days: int = Field(default=90, ge=1, le=365)
    # At most this many rows (1–5000).
    limit: int = Field(default=500, ge=1, le=5000)


class CodeBackportGapsOutput(BaseModel):
    # Configured release branches this caller cannot read,
    # skipped (never silently): [{namespace, reason}]. Empty when branches were named.
    skipped_branches: list[dict[str, str]] = Field(default_factory=list)
    # the branch asked for ('' = every configured release branch)
    branch: str = ""
    # The namespace of the branch asked for; "" when every branch was read.
    branch_namespace: str = ""
    # the release-branch namespaces the rows cover, newest first
    branches: list[str] = Field(default_factory=list)
    # one row per (fix, branch), gaps first, wanted-on-this-branch first, newest
    # fix first: {pr_number, title, merged_at, minor_version, branch,
    #  branch_namespace, verdict, epistemic, basis, reason, cherry_pick
    #  {pr_number, merge_sha}, backport_pr, label_wanted, wanted_branch,
    #  wanted_source, settled, computed_at}
    rows: list[dict[str, Any]] = Field(default_factory=list)
    # verdict → rows returned
    counts: dict[str, int] = Field(default_factory=dict)
    # branch version → verdict → rows
    counts_by_branch: dict[str, dict[str, int]] = Field(default_factory=dict)
    # How many distinct fixes the returned rows cover.
    fixes: int = 0
    # Echo of the request's `wanted_only`.
    wanted_only: bool = True
    # The merge-time lower bound applied (ISO-8601 UTC): now minus `days`.
    since: str = ""
    # the table's state for this trunk: {fixes, rows, first_computed, last_computed}
    computed: dict[str, Any] = Field(default_factory=dict)
    # no pass has written this trunk yet, or the last pass is older than two intervals
    stale: bool = False
    # Wall time of the read, in milliseconds.
    ms: int = 0


class CodeBlameRangeInput(_CodeIdentity):
    """Which merged PRs last touched these lines, between release A and B?
    `path` and a line range (at the upper end of the window, at most 400
    lines), the window as release versions (`26.3` → the release-branch
    namespace's recorded head, `26.3.4.12` → its tag) or commit shas; `to`
    defaults to this namespace's head, `from` to the oldest release cut.
    Two legs: loom's merged-PR ledger (file lists in the window — the
    floor) and `git blame -w -M -L` on the bare mirror, each blamed commit
    mapped to its pull request through the window's merge graph, ranked by
    lines touched then recency (blank / comment / format-only lines
    dropped, latest change wins). Exact for "last touched", never for
    "introduced the bug" — `note` says so. The tracker namespace
    bound to this code namespace adds `has_test` / `caused_by` / `fixes`
    when it is available; the answer is complete without it.

    `stage`: `introduced` (default) re-ranks those candidates toward the change that
    introduced the behaviour — backports, reverts, reverted PRs and whitespace-only /
    rename-only touches of the file are demoted; a PR that names the enclosing function,
    drew a review comment inside it, or whose linked issue names the words of `mentions`
    (what misbehaves, in the caller's words) is promoted; a re-land of a reverted change
    is `reland_of` (the reverted PR is then not demoted, the re-land inherits its symbol
    marks); when blame sees no in-window line the verdict is `introduced_unknown` and
    the list is the window's file-touching PRs by symbol evidence and recency.
    `last_touched` keeps the first stage's order and shape; either way `last_touched` in
    the response carries the first stage's ranking.

    A blamed candidate lists every blamed `commits` credited to it (a multi-commit PR's
    commits map to its oldest first-parent merge) and `lines_in_range` (its lines incl.
    blank / comment / brace ones); each candidate carries what reviewers said — up to
    `reviews_per_pr` human review comments on a line inside the range, newest first —
    and `review_count`, the PR's total on the file."""

    # The repo-relative file to blame (a leading `/` is stripped).
    path: str = Field(min_length=1, max_length=512)
    # First line of the range, 1-based, at the window's upper end.
    start_line: int = Field(ge=1)
    # Last line, inclusive; a range over 400 lines is cut to 400 from `start_line`.
    end_line: int = Field(ge=1)
    # Window start as a release (`26.3` or a full tag version); "" with no `from_sha`
    # = the oldest release cut.
    from_version: str = Field(default="", max_length=32)
    # Window end as a release; "" with no `to_sha` = this namespace's head.
    to_version: str = Field(default="", max_length=32)
    # Window start as a commit sha; give a version or a sha, not both.
    from_sha: str = Field(default="", max_length=40)
    # Window end as a commit sha; give a version or a sha, not both.
    to_sha: str = Field(default="", max_length=40)
    # At most this many candidates (1–25).
    limit: int = Field(default=10, ge=1, le=25)
    # Each candidate carries up to this many human inline review comments on a line
    # inside the range, newest first, plus `review_count` (the PR's total on the file);
    # 0 omits both.
    reviews_per_pr: int = Field(
        default=3,
        ge=0,
        le=10,
        description="review comments per candidate inside the range (0 = none)",
    )
    stage: Literal["introduced", "last_touched"] = Field(
        default="introduced",
        description="introduced: rank toward the change that introduced the behaviour; "
        "last_touched: the blame order only",
    )
    mentions: str = Field(
        default="",
        max_length=300,
        description="what misbehaves, in words: a candidate whose linked issue or title "
        "names them ranks first",
    )

    @model_validator(mode="after")
    def _ordered(self) -> CodeBlameRangeInput:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not be below start_line")
        if self.from_version and self.from_sha or self.to_version and self.to_sha:
            raise ValueError("name each window end once: a version or a sha")
        return self


class CodeBlameRangeOutput(BaseModel):
    # candidates | no_candidates | unknown | introduced_unknown (stage introduced:
    # blame saw no in-window line; the list is the file-touching fallback)
    verdict: str = "unknown"
    # window_unresolved | path_not_indexed | blame_failed | ledger_failed | tracker_unavailable
    causes: list[str] = Field(default_factory=list)
    # exact when the blame leg ran (line attribution), lower_bound when only
    # the ledger answered (file-level) or the introduced stage is blameless
    epistemic: str = "lower_bound"
    # the order `candidates` is in
    stage: Literal["introduced", "last_touched"] = "last_touched"
    # What the answer can and cannot claim (last touched, not introduced), plus lines
    # blame could not map to a PR.
    note: str = ""
    # The path blamed, normalized.
    path: str = ""
    # True when the code index holds the path as a live file.
    path_indexed: bool = False
    # {start, end, requested_end, truncated}
    range: dict[str, Any] = Field(default_factory=dict)
    # {from_ref, to_ref, from_sha, to_sha, base_sha, from_time, to_time,
    #  sources{from,to: namespace_head|mirror_ref|sha|default}, error}
    window: dict[str, Any] = Field(default_factory=dict)
    # ranked: {pr_number, title, author, merged_at, first_release, lines_blamed,
    #  share_of_range, touches_file, evidence blame|ledger|both, kind, merge_commit_sha,
    #  merge_sha_source stored|mirror|"", url, has_test?, caused_by?[], fixes?[]};
    #  stage introduced adds
    #  last_touched_rank, demoted? (backport|revert|reverted|whitespace_only|
    #  rename_only|meta_title), promoted?[], touches_symbol?,
    #  behaviour_words?[]; reland_of? / reland_via? on a PR that
    #  re-lands a reverted change, relanded_by? on the reverted PR (not demoted)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    # the first stage's order, always: {pr_number, rank, lines_blamed, evidence}
    last_touched: list[dict[str, Any]] = Field(default_factory=list)
    # the enclosing function of the range when the index has one:
    # {qualified_name, start_line, end_line}
    symbol: dict[str, Any] = Field(default_factory=dict)
    # the newest PR among those blame named (0 when none)
    latest_pr: int = 0
    # {total, attributed, noise, outside_window, unmapped}
    lines: dict[str, Any] = Field(default_factory=dict)
    # {blame{status, warm, ms, window_commits, graph_truncated},
    #  ledger{status, skipped: [{namespace, reason: not_readable}] when the trunk is unreadable},
    #  introduced{status ok|skipped, ms, git_calls, demoted, promoted, blameless}}
    legs: dict[str, Any] = Field(default_factory=dict)
    # How `first_release` was derived; "" when the mirror has no branch-cut tags.
    first_release_basis: str = ""
    # The tracker namespace bound to this code namespace; "" when none is bound.
    tracker_namespace: str = ""
    # True when a tracker is bound but its read failed; the candidates stand without
    # `has_test` / `caused_by` / `fixes`.
    tracker_unavailable: bool = False
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


class CodeSymbolHistoryInput(_CodeIdentity):
    """The regression history of one function: the merged PRs that touched it (with
    linked issues, reverts, inline review comments inside its span), and — through the
    tracker namespace bound to this code namespace — which of those diffs name the
    function, their test presence, CI result and first release, the issues traced back
    to them, and the open issues that name it today. `name` is a qualified symbol or a
    `path:line`; `symbol_id` wins when given."""

    # A qualified symbol name or `path:line`.
    name: str = Field(default="", max_length=300)
    # An index symbol id; wins over `name`.
    symbol_id: str = Field(default="", max_length=200)
    # At most this many PR rows (1–60).
    limit: int = Field(default=15, ge=1, le=60)
    # "name or symbol_id is required" is the handler's check, raised after
    # the namespace gate


class CodeSymbolHistoryOutput(BaseModel):
    # history | no_history | unknown
    verdict: str = "unknown"
    # symbol_unresolved | symbol_ambiguous | tracker_unavailable
    causes: list[str] = Field(default_factory=list)
    # Always `lower_bound`: the PRs are those the ledger holds over the definition's file.
    epistemic: str = "lower_bound"
    # {qualified_name, path, start_line, end_line, tier}
    symbol: dict[str, Any] = Field(default_factory=dict)
    # As on code.callers: the name resolved to several distinct definitions; no history
    # was read, `candidates` are their qualified names (qualify the name or pass symbol_id).
    ambiguous: bool = False
    # With `ambiguous`: up to 20 qualified names to pick from.
    candidates: list[str] = Field(default_factory=list)
    # newest first: {pr_number, title, author, merged_at, is_revert, is_fix,
    #  touches_symbol true|named|false|unknown, linked_issues[{number,title,state}],
    #  comments_on_symbol, qa{has_test, ci_result, first_release, caused[], names[]}}
    prs: list[dict[str, Any]] = Field(default_factory=list)
    # inline review comments inside the symbol's span, newest first (≤5)
    review_comments: list[dict[str, Any]] = Field(default_factory=list)
    # open issues whose text names the function (QA), most severe first (≤5)
    open_issues_naming: list[dict[str, Any]] = Field(default_factory=list)
    # {prs_total, named_prs, fix_prs, named_fix_prs, reverts, caused_issues[],
    #  open_issues_naming, last_touched, oldest_in_window, authors[], note}
    summary: dict[str, Any] = Field(default_factory=dict)
    # The tracker namespace that answered the QA side; "" when none is bound or
    # its read failed.
    tracker_namespace: str = ""
    # True when a tracker is bound but its read failed.
    tracker_unavailable: bool = False
    # Id of this call's trace row; pass it to `code.outcome`. Empty on an unresolved
    # or ambiguous name.
    trace_run_id: str = ""


class CodeFirstBuildInput(_CodeIdentity):
    """Which CI build first carried this commit? One subject — a commit sha (7–40 hex)
    or a merged PR's number (the ledger supplies its merge commit and time). The tracker
    namespace bound to this code namespace supplies `version_history`: runs naming the
    sha are exact; otherwise the bases of the runs after the merge are checked on the
    mirror (`git merge-base --is-ancestor`, ≤ 10) for the earliest descendant."""

    # A commit sha, 7–40 hex characters; an abbreviated one is expanded on the mirror.
    sha: str = Field(default="", max_length=40, pattern=r"^[0-9a-fA-F]{0,40}$")
    # A merged PR; the ledger supplies its merge commit and time. 0 = use `sha`.
    pr_number: int = Field(default=0, ge=0)
    # "sha or pr_number" (and the 7-hex floor) is checked by the handler,
    # not a model validator: the namespace gate answers a refused principal
    # (403/404) before any shape complaint, as the other code reads do.


class CodeFirstBuildOutput(BaseModel):
    # built | not_yet_built | unknown
    verdict: str = "unknown"
    # tracker_unbound | tracker_unavailable | sha_unknown | candidates_exhausted | pr_not_merged
    causes: list[str] = Field(default_factory=list)
    # exact: a run names the sha (or the PR's own head builds);
    # lower_bound: the earliest of ≤ 10 checked bases descends from it
    epistemic: str = "lower_bound"
    # The full commit sha the answer is about (from the input, the mirror or the ledger).
    sha: str = ""
    # The PR the commit merged, from the input or the ledger; 0 when unknown.
    pr_number: int = 0
    # When the PR merged, from the ledger (ISO-8601); "" when the ledger has no row.
    merged_at: str = ""
    # The commit's time on the mirror (ISO-8601); "" when the mirror lacks the sha.
    commit_time: str = ""
    # True when the PR ledger knows the subject.
    ledger_known: bool = False
    # True when the code mirror holds the commit.
    mirror_known: bool = False
    # {kind named|carrier|own, check_start_time, version, docker_tag, minor_version,
    #  git_ref, pull_request_number, commit_sha, base_sha}
    first_build: dict[str, Any] = Field(default_factory=dict)
    # hours from the merge (or commit) to the first carrying run
    lag_hours: float | None = None
    # runs started after it inside the window (later bases not individually checked)
    later_builds: int = 0
    # How many hours after the merge (or commit) runs are read for carriers.
    window_hours: int = 72
    # How many run bases were checked on the mirror for descent from the commit.
    candidates_checked: int = 0
    # How many candidate run bases the window held.
    candidates_total: int = 0
    # the PR's own head builds, oldest first (≤ 8)
    own_builds: list[dict[str, Any]] = Field(default_factory=list)
    # mirror branches whose head contains the sha, and the namespaces on them
    containing_branches: list[str] = Field(default_factory=list)
    # The repository's namespaces whose branch contains the commit.
    containing_namespaces: list[str] = Field(default_factory=list)
    # the lowest release-branch namespace containing it, else the own branch's
    nearest_namespace: str = ""
    # What the tracker's build view can show; "" when the tracker was not read.
    note: str = ""
    # The tracker namespace bound to this code namespace; "" when none is bound.
    tracker_namespace: str = ""
    # True when a tracker is bound but its read failed.
    tracker_unavailable: bool = False
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


class CodeHistoryInput(_CodeIdentity):
    # A symbol name or `path:line`; the PRs over its definitions' files are returned.
    name: str = ""
    # An index symbol id; wins over `name`.
    symbol_id: str = ""
    # A repo-relative file; wins over `name` / `symbol_id`.
    path: str = ""
    # Issue / PR numbers: the item facts and the merged PRs that are or close them, from
    # the same reads code.search's history section uses. Takes precedence over name /
    # symbol_id / path.
    numbers: list[int] = Field(default_factory=list, max_length=3)
    # At most this many PRs (1–20).
    limit: int = Field(default=5, ge=1, le=20)
    # Each PR row carries up to this many human inline review comments on the queried
    # file(s), newest first, plus `review_count` (the PR's total there); 0 omits both.
    # Path / symbol lookups only.
    reviews_per_pr: int = Field(
        default=3,
        ge=0,
        le=10,
        description="review comments per PR row on the queried file (0 = none)",
    )


class CodeHistoryOutput(BaseModel):
    # Merged PRs, newest first: {pr_number, title, author, merged_at, labels, body_excerpt}
    # plus `reviews` / `review_count` when asked; number lookups return the history rows.
    prs: list[dict[str, Any]]
    # The files the PRs were read for; for number lookups, the files the returned PRs touched.
    paths: list[str]
    # number lookups only: what each number is (issue / pr, state, title)
    items: list[dict[str, Any]] = Field(default_factory=list)
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
