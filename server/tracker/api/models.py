"""tracker.* — the tracker reads the number route does not have.

Addressed at a tracker namespace — a directory row served live from QA
Intelligence with that namespace's credential — so the transport's gate
decides who reads, restricted namespaces included. Every op is a bounded keyed read; an unknown
number, component, test or version is an empty answer, and a source that
cannot answer says `tracker_unavailable` rather than answering empty."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from server.operations.base import MCP_HIDDEN


class _TrackerIdentity(BaseModel):
    """`org` defaults to the deployment's QA org the way a
    code.* identity defaults to the code plane, so a caller never has to
    spell it. `namespace` stays required: a tracker op has no single namespace
    to fall back on — it is the tracker namespace itself (tracker-acme,
    tracker-acme-private, …) or a code namespace bound to one."""

    org: str = ""
    namespace: str


class TrackerItemInput(_TrackerIdentity):
    """The tracker record of up to ten issue / PR numbers: component, severity,
    bisect verdict and branches still affected, test presence, CI result,
    reviewers, backport lineage, and the fixes / backports / caused_by graph."""

    # Issue or PR numbers to read (1–10); duplicates fold and numbers <= 0 are dropped.
    numbers: list[int] = Field(min_length=1, max_length=10)


class TrackerItemOutput(BaseModel):
    # One entry per known number: kind, state, title, author, dates, url, the tracker record
    # (`tracker`), a one-line summary (`line`) and the files each `fixed_by` PR touched.
    items: list[dict[str, Any]]
    # numbers the tracker source has no row for — unknown, not absent (the
    # upstream sync lags minutes)
    unknown: list[int] = Field(default_factory=list)
    # the source could not be read (breaker open, budget exceeded, credential
    # unset, kill switch): nothing above is a claim
    tracker_unavailable: bool = False


class TrackerReleaseInput(_TrackerIdentity):
    """Release readiness for one version (`26.3`): the blocker rows with
    their status and the latest health snapshot."""

    # The release to check, as the tracker names it (e.g. `26.3`).
    version: str = Field(min_length=3, max_length=16)


class TrackerReleaseOutput(BaseModel):
    # Echo of the requested version.
    version: str
    # The release's rows: `ref`, `status`, `issue_state`, `fix_version` and `active`
    # (an open `Blockers` row); active ones first.
    blockers: list[dict[str, Any]]
    # How many of `blockers` are active (status `Blockers` and the issue still open).
    active_blockers: int = 0
    # Latest health snapshot: `open_blockers`, `critical_untested`, `total_items`,
    # `snapshot_date`; `{}` when none.
    health: dict[str, Any] = Field(default_factory=dict)
    # True when the source could not be read; the lists above are then empty,
    # not a claim.
    tracker_unavailable: bool = False


class TrackerComponentInput(_TrackerIdentity):
    """One component's risk snapshot and its open issues, most severe first."""

    # Component name, matched exactly as the tracker spells it.
    component: str = Field(min_length=1, max_length=120)
    # At most this many open issues; the server caps it at 50.
    limit: int = Field(default=10, ge=1, le=50)


class TrackerComponentOutput(BaseModel):
    # Echo of the requested component.
    component: str
    # The component's latest risk snapshot row (columns as the source holds them); `{}` when none.
    risk: dict[str, Any] = Field(default_factory=dict)
    # Open issues in the component, most severe first: number, title, severity, labels,
    # versions, bisect outcome, flaky/crash flags, url.
    open_issues: list[dict[str, Any]] = Field(default_factory=list)
    # True when the tracker has a risk row or an open issue for the component.
    known: bool = False
    # True when the source could not be read; the fields above are then empty,
    # not a claim.
    tracker_unavailable: bool = False


class TrackerFlakyInput(_TrackerIdentity):
    """Is this test a known flaky? The flaky-test issues that name it. With
    `include_signal`, the answer also carries the test's own CI history read
    (`tracker.test_signal`) beside the issue verdict — a read of
    seconds, so it is asked for, never implied."""

    # The test to look up: flaky-test issues whose title holds every token of it match.
    test_name: str = Field(min_length=3, max_length=300)
    # At most this many issues; the server caps it at 50.
    limit: int = Field(default=10, ge=1, le=50)
    # Also run `tracker.test_signal` on the test and return it in `signal`.
    include_signal: bool = False
    # History window for `signal`, in days; 0 = the deployment default.
    window_days: int = Field(default=0, ge=0, le=90)


class TrackerFlakyOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_absent_signal(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # without `include_signal` the payload is byte for byte the shape without `signal`
        data: dict[str, Any] = handler(self)
        if data.get("signal") is None:
            data.pop("signal", None)
        return data

    # The test name asked about, trimmed.
    test_name: str
    # True when at least one flaky-test issue names the test.
    known_flaky: bool = False
    # How many of `issues` are still open.
    open_issues: int = 0
    # Matching flaky-test issues, newest first: number, title, state, labels, dates, url.
    issues: list[dict[str, Any]] = Field(default_factory=list)
    # flaky_by_issue | no_flaky_signal | tracker_unavailable — the second is
    # silence, not "not flaky"; the third is "nobody looked"
    verdict: str = "no_flaky_signal"
    # True when the source could not be read; `verdict` is then
    # `tracker_unavailable`.
    tracker_unavailable: bool = False
    # The tracker.test_signal answer, only when asked for
    signal: dict[str, Any] | None = None


class TrackerTestSignalInput(_TrackerIdentity):
    """Flaky, regression or infrastructure? One test's own CI history from
    the `checks` table: runs, fail rate, flip rate, outcome
    entropy, same-commit rerun flips, the last failure on a branch head, the
    hours it failed alongside many unrelated tests, and the churn of its home
    file(s) and mapped sources in loom's PR ledger. `window_days` 0 = the
    deployment default (30). `code_namespace` names the ledger to read churn
    from (default: the deployment's code namespace; read as the current
    principal). The history comes from loom's daily roll-up
    first and the remote only for the hours it does not cover yet;
    `pr_number` adds the previous-outcome streak of that PR head, and the
    test's own source is read for static flakiness smells."""

    # The test to read, as CI names it; an unknown name answers `unknown_test` with
    # the tests it matches.
    test_name: str = Field(min_length=3, max_length=300)
    # History window in days; 0 = the deployment default.
    window_days: int = Field(default=0, ge=0, le=90)
    # Code namespace whose PR ledger churn and smells are read from; "" = the
    # deployment's; the caller needs read access to it.
    code_namespace: str = Field(default="", max_length=120)
    # Read the churn of the test's home file(s) and mapped sources; False skips it.
    include_churn: bool = True
    # The PR head whose previous-outcome streak is read (0 = none)
    pr_number: int = Field(default=0, ge=0)
    # The static smell flags from the test's own source chunks
    include_smells: bool = True


class TrackerTestSignalOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_absent_candidates(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # A known test's answer keeps its original shape, byte for byte
        data: dict[str, Any] = handler(self)
        for k in ("candidates", "candidate_matches", "resolved_from"):
            if not data.get(k):
                data.pop(k, None)
        return data

    # The test the answer is for, trimmed (the matched test when `resolved_from` is set).
    test_name: str
    # flaky | regression_candidate | infra_candidate | no_signal | unknown_test |
    # tracker_unavailable — no_signal is silence, not health; unknown_test
    # is a name no test carries, with the tests it matches
    verdict: str = "no_signal"
    # Plain sentences giving the reasons for `verdict`.
    why: list[str] = Field(default_factory=list)
    # Wilson 95 % lower bound of the failure rate over the window's runs: the
    # least the test can plausibly fail; small n → small bound
    lower_bound: float = 0.0
    # The window actually read, in days (the default when 0 was asked).
    window_days: int = 0
    # Start of the window, ISO UTC (hour-truncated).
    since: str = ""
    # runs, fails, fail_rate, flips, steps, flip_rate, entropy, rerun_flips,
    # prs, failing_prs, branch_fails, last_true_fail(+_head), last_fail_at,
    # first_run_at, fail_hours
    features: dict[str, Any] = Field(default_factory=dict)
    # {k, min_prs, storm_hours, hours, share, available}
    infra: dict[str, Any] = Field(default_factory=dict)
    # {available, days, home_paths, home_prs, home_recent, source_paths,
    #  source_prs, source_recent, code_namespace | reason}
    churn: dict[str, Any] = Field(default_factory=dict)
    # The deployment thresholds the verdict used (min runs, flip rate, co-failure knobs).
    thresholds: dict[str, Any] = Field(default_factory=dict)
    # where the history came from: the checks source clause without its credentials
    source: str = ""
    # Milliseconds spent per leg (`history`, `co_failures`, `churn`, `smells`, …).
    timings_ms: dict[str, int] = Field(default_factory=dict)
    # True when the history could not be read, or an unread leg decides the
    # verdict; `verdict` is then `tracker_unavailable`.
    tracker_unavailable: bool = False
    # What the daily roll-up covered of the window
    # {available, days, from, to, live_since, missing_days, partial_day | reason}
    rollup: dict[str, Any] = Field(default_factory=dict)
    # The previous-outcome streak of (pr_number, test) — {pr_number,
    # known, fail_days, last_fail_day, fails_on_day, tail_streak, last_outcome,
    # subsequent_failure, source}
    streak: dict[str, Any] = Field(default_factory=dict)
    # Flaky-test smell tokens counted over the test's source
    # {available, loc, counts, flags | reason}
    smells: dict[str, Any] = Field(default_factory=dict)
    # {next_run_on_pr, flake_indicators, flake_prior}
    prior: dict[str, Any] = Field(default_factory=dict)
    # Why the source could not be read; "" when it was.
    unavailable_reason: str = ""
    # On `unknown_test`, the rolled tests whose names start with
    # (else contain) the asked one, most runs first — [{test_name, runs, fails}]
    # — and how many match in all; never summed into one test's features
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    # On `unknown_test`: how many rolled tests match the name in all.
    candidate_matches: int = 0
    # The asked name when the answer is for the only test it matched
    resolved_from: str = ""


class TrackerAnswerInput(_TrackerIdentity):
    """Answer a tracker question in prose — or from an artefact's
    anchors — by running the catalogue's templates: escalation / incident
    context, a customer across channels, incidents / escalations / customers in a
    component or path, similar bugs, release blockers with their fixes, changelog
    entries, PRs touching a path, a person's week, test-gap components, a flaky test, a
    number. Every section names the template and parameters it ran, so the reader knows
    exactly what was searched; incidents, escalations, customers and instances come only
    through a restricted tracker namespace and are reported `restricted_required`
    elsewhere. `explain_only` returns the plan."""

    # The question in prose; may be empty when `anchors` are given.
    question: str = Field(default="", max_length=2000)
    # Structured anchors a client already holds: a PR's
    # components and paths, an issue number and its frames, a customer, a
    # version, a window, intent words — {numbers, number_kind, versions,
    # components, paths, handles, customers, tests, terms, quoted, days,
    # words}. Merged with the prose; either may be empty, not both.
    anchors: dict[str, Any] = Field(default_factory=dict)
    # Rows per section, at most; the server caps it at 50.
    limit: int = Field(default=10, ge=1, le=50)
    # Return the plan (sections marked `planned`) without reading the tracker.
    explain_only: bool = False
    # "question or anchors is required" is the handler's check, raised after
    # the namespace gate


class TrackerAnswerOutput(BaseModel):
    # What the planner read the question as (e.g. `release_readiness`); "" when unclear.
    intent: str = ""
    # The anchors the plan used: the prose's merged with the ones passed in.
    anchors: dict[str, Any] = Field(default_factory=dict)
    # [{template, title, access, params, why, status served|empty|restricted_required|
    #   tracker_unavailable|planned, cites, rows}]
    sections: list[dict[str, Any]] = Field(default_factory=list)
    # one string per section: template(params) — what was searched
    searched: list[str] = Field(default_factory=list)
    # Prose lines of the served sections with inline citations; empty with `explain_only`.
    lines: list[str] = Field(default_factory=list)
    # True when the answering tracker namespace is restricted.
    restricted: bool = False
    # planned | answered | empty | unknown (no anchor to plan on)
    verdict: str = "unknown"


class TrackerBindingInput(_TrackerIdentity):
    """What this namespace may ask the tracker plane: a QA
    namespace answers itself; a code namespace answers the tracker namespace
    bound to it (`LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`),
    passed through the gate for the caller; anything else — unbound,
    refused, or no tracker namespace at all — is `kind = none` with no name.
    `templates` are the catalogue templates the caller may run there. The
    dashboard asks this once per namespace to decide whether its Patterns
    mode is shown; no upstream read happens."""


class TrackerBindingOutput(BaseModel):
    # the namespace QA questions on the addressed namespace are answered by; ""
    # when there is none the caller may read
    tracker_namespace: str = ""
    # Every tracker linked to the addressed code namespace the
    # caller may read, primary (`tracker_namespace`) first; the searches read
    # them all, a lookup by number reads the primary — or address another by name
    tracker_namespaces: list[str] = Field(default_factory=list)
    # True when the answering tracker namespace is restricted.
    restricted: bool = False
    # qa: the addressed namespace is a tracker namespace | code: a code namespace
    # bound to one the caller may read | none: nothing to ask (unbound, refused,
    # or neither — the three are one answer, so a refusal names nothing)
    kind: str = "none"
    # True when the caller may ask a tracker here; False with `kind = none`.
    authorized: bool = False
    # catalogue template names the caller may run: public, plus the restricted
    # class when `restricted`
    templates: list[str] = Field(default_factory=list)


class TrackerSimilarInput(_TrackerIdentity):
    """The reports that already say it: duplicate and
    related issues / PRs for a pasted report (`text`), an existing item
    (`issue_number` — its own title, body, component, versions and error
    code become the query, and only items created BEFORE it are
    candidates) or a stack trace (`frames`, or trace lines). Legs: BM25F
    over title and body with component / version / error-code equality
    boosts, position-decayed frame IDF, the items the text names by number,
    fused by reciprocal rank; a vector leg over the embeddings loom
    computes itself for the record's open and recently closed items
    (in the default leg set when
    `LOOM_TRACKER_SIMILAR_VECTOR_LEG` is on, always when `legs` names it) that
    is recall-only: it adds candidates no other leg found, after them, and
    marks the ones it shares; it never re-ranks. `epistemic` is always
    `lower_bound`."""

    # A pasted report to search for; may be empty when `issue_number` or `frames` is given.
    text: str = Field(default="", max_length=20000)
    # An existing item whose own text is the query; only older items are candidates.
    # 0 = none.
    issue_number: int = Field(default=0, ge=0)
    # qualified frames or raw trace lines; normalised the way code.trace parses
    frames: list[str] = Field(default_factory=list, max_length=60)
    # issue | pr | incident (incidents only through a restricted tracker namespace)
    kinds: list[str] = Field(default_factory=lambda: ["issue", "pr"], max_length=3)
    # At most this many items; the server caps it at 50.
    top_k: int = Field(default=10, ge=1, le=50)
    # the legs to run — lexical, fields, frames, mentions, vector; the default
    # is all (the bench arms one at a time; `fields` are the equality boosts on
    # the lexical leg and run only with it; `mentions` are the items the text
    # names by number)
    legs: list[str] = Field(default_factory=list, max_length=5)
    # "text, issue_number or frames is required" is the handler's check,
    # raised after the namespace gate

    @field_validator("kinds")
    @classmethod
    def _known_kinds(cls, v: list[str]) -> list[str]:
        bad = sorted(set(v) - {"issue", "pr", "incident"})
        if bad:
            raise ValueError(f"unknown kinds {bad}")
        return v

    @field_validator("legs")
    @classmethod
    def _known_legs(cls, v: list[str]) -> list[str]:
        bad = sorted(set(v) - {"lexical", "fields", "frames", "mentions", "vector"})
        if bad:
            raise ValueError(f"unknown legs {bad}")
        return v


class TrackerSimilarOutput(BaseModel):
    # [{kind, number, title, state, state_reason, score, why: [lexical|frames|
    #   mentions|vector, component, version, error_code, duplicate_of|
    #   related_to|fixed_by], relation: duplicate_of|related_to|fixed_by|none
    #   (what the tracker graph records between the query item and this one;
    #   the verdict is the caller's), distance (the vector
    #   leg's cosine distance when it saw the item, else null), url, created_at}]
    items: list[dict[str, Any]] = Field(default_factory=list)
    # what was searched: the query item (number, kind, known), the selected
    # terms, the frames and where they came from (caller | body), the wall;
    # with the vector leg: `fresh_items` (items changed
    # upstream since the pass's cursor, embedded on this call), `fresh_left_to_pass`
    # (beyond the cap), `fresh_pending` (the catch-up overran its budget share
    # and finishes in the background), `timings_ms` per leg
    query: dict[str, Any] = Field(default_factory=dict)
    # per leg: served | empty | skipped | restricted_required | zero_embedding
    # | not_computed | tracker_unavailable — a leg that did not run is named,
    # never silent (not_computed: the vector table holds no row for the
    # namespace — the tracker_embed pass has not run, or it is restricted)
    legs: dict[str, str] = Field(default_factory=dict)
    # Always `lower_bound`: a miss does not prove no similar report exists.
    epistemic: str = "lower_bound"
    # every leg failed to read: nothing above is a claim
    tracker_unavailable: bool = False
    # The trackers searched, primary first, when the addressed
    # repository links more than one (each item then names its `namespace`)
    namespaces: list[str] = Field(default_factory=list)


class TrackerDuplicateCheckInput(_TrackerIdentity):
    """Does this draft already exist? Give the draft as a
    reporter would file it — `title`, `body`, the crash's `frames`, the
    `component`, the `version` (`X.Y`), the issue numbers it `mentions` —
    the finding's `paths` and `symbols` — and get a verdict: `duplicate_of`
    (the top candidate's calibrated confidence reaches the duplicate
    threshold), `related` (a candidate reaches the related threshold) or
    `none` with NO items. Runs `tracker.similar`'s legs once and scores
    every fused candidate with the namespace's stored logistic fit;
    `epistemic` is `calibrated`, or `uncalibrated` when no fit is stored
    (then `related` with a null confidence). `issue_number` names the item
    already filed for this draft (triage): it is left out, the tracker graph's
    recorded relation to each candidate is read (`relation_recorded`), and
    a candidate the record links as `related_to` is never `duplicate`.
    `as_of` walls the candidates at a past moment (a replay). `none` means
    "no calibrated evidence of a duplicate", never clearance: the duplicate
    band is carried by citations (a draft that cites no issue numbers is
    rarely called duplicate, and its true duplicate may appear only under
    `related`), so such a draft should still read the `related` list before
    filing. Title, body or issue_number is required — checked
    after the namespace gate."""

    # The draft's title, as a reporter would file it.
    title: str = Field(default="", max_length=500)
    # The draft's body text.
    body: str = Field(default="", max_length=20000)
    # qualified frames or raw trace lines; normalised the way code.trace parses
    frames: list[str] = Field(default_factory=list, max_length=60)
    # The draft's component; boosts candidates with the same component.
    component: str = Field(default="", max_length=120)
    # The version the draft is about (`X.Y`, a longer one is cut to its minor); boosts
    # candidates affecting it.
    version: str = Field(default="", max_length=40)
    # issue / PR numbers the draft refers to, beside the ones its text names
    mentions: list[int] = Field(default_factory=list, max_length=10)
    # the finding's code-path evidence: repository paths a fix PR of a candidate
    # may have touched (`fix_path`), and symbols matched like frames
    paths: list[str] = Field(default_factory=list, max_length=40)
    # Code symbols from the finding, matched like stack frames.
    symbols: list[str] = Field(default_factory=list, max_length=40)
    # the issue already filed for this draft (its own text is the query when
    # title and body are empty; it is excluded; its recorded relations are read)
    issue_number: int = Field(default=0, ge=0)
    # At most this many items; the server caps it at 50.
    top_k: int = Field(default=10, ge=1, le=50)
    # ISO 8601: only items created before it are candidates (replays); '' = now
    as_of: str = Field(default="", max_length=40)
    # The tracker namespaces to read (each a tracker namespace or a code
    # namespace bound to one), the addressed one first; empty = every QA
    # record the caller may read when addressed to a code namespace or the
    # bound one, a single read when a tracker namespace is addressed by name.
    namespaces: list[str] = Field(default_factory=list, max_length=4)
    # Also search the PRs that may already address the draft
    # (on none / related). loom_search's tracker leg turns it off: its 3 s
    # budget has no room for the extra search
    related_prs: bool = Field(default=True, json_schema_extra=MCP_HIDDEN)
    # Bisect's own check runs the legs alone — the rerank's
    # LLM call does not fit the bisect's budget
    rerank: bool = Field(default=True, json_schema_extra=MCP_HIDDEN)


class TrackerLegStatus(BaseModel):
    """One leg of a duplicate check: whether it answered, and if not, why."""

    # lexical | frames | mentions | vector | fix_path
    name: str
    # ok | degraded (the read failed: the verdict was built without it) | skipped
    status: str = "ok"
    # a short code: timeout | connection | upstream_5xx | ch_transient | breaker_open |
    # quota | shape | unconfigured | unavailable | retry_skipped_budget on a degraded leg;
    # recovered_on_retry on an ok one; not_applicable | not_computed | not_run on a skip
    reason: str = ""
    # one fixed sentence for the reason (never the upstream's own text)
    message: str = ""
    # the leg was run a second time after a transient failure
    retried: bool = False
    # the leg's time, both tries when retried
    ms: int = 0
    # on a degraded leg, the read's failure code (the reason too, unless the retry was
    # skipped)
    cause: str = ""
    # the tracker namespace the leg read
    namespace: str = ""


class TrackerDuplicateCheckOutput(BaseModel):
    # duplicate_of | related | none — `none` carries no items (file it)
    verdict: str = "none"
    # [{kind, number, repo, url, title, state, state_reason, component, created_at,
    #   score, confidence (null when uncalibrated), relation: duplicate | related,
    #   relation_recorded: duplicate_of | related_to | fixed_by | none (the QA
    #   graph's edge to the filed `issue_number`),
    #   why: [lexical|frames|mentions|vector, component, version, error_code, fix_path],
    #   evidence: [human-readable lines], features {legs, inv_rank, gap, bm25_share,
    #   lexical, frames, mentions, component, version, error_code, fix_path,
    #   log_age}}], duplicates first
    items: list[dict[str, Any]] = Field(default_factory=list)
    # the top candidate sits in [related, duplicate) AND shares the component:
    # route the list to a human
    undecided: bool = False
    # calibrated (a stored fit scored the items) | uncalibrated (no fit: `related`
    # at most, confidence null) | degraded (a leg, or one of several trackers, did
    # not answer: a `none` is not clearance) | unavailable (nobody read the record)
    epistemic: str = "calibrated"
    # {duplicate, related} — the served thresholds on the probability scale
    thresholds: dict[str, float] = Field(default_factory=dict)
    # The judge stage — ran, model, tokens, labels per
    # candidate number, the numbers it promoted; absent when the stage is off
    judge: dict[str, Any] = Field(default_factory=dict)
    # When the stored calibration fit was computed; "" when no fit is stored.
    fit_at: str = ""
    # {status served | no_fit, fit_at, queries {n, positives, negatives}, auc,
    #  holdout_auc, tracker_namespace}
    fit: dict[str, Any] = Field(default_factory=dict)
    # what was searched: terms, frames and their source, mentions, component,
    # versions, error_code, the wall, the legs' timings
    query: dict[str, Any] = Field(default_factory=dict)
    # per leg: served | empty | skipped | not_computed | tracker_unavailable
    legs: dict[str, str] = Field(default_factory=dict)
    # Per leg of every namespace read: status ok | degraded | skipped, reason and message,
    # retried, ms. A degraded leg failed after its one retry (or the retry was skipped).
    leg_status: list[TrackerLegStatus] = Field(default_factory=list)
    # every leg failed to read: the verdict above is not a claim
    tracker_unavailable: bool = False
    # The tracker namespaces this answer read (bound one first) and
    # each one's own verdict / item count / epistemic / legs; every item in
    # `items` names its `namespace`. Empty on a single-namespace read.
    namespaces: list[str] = Field(default_factory=list)
    # Per namespace read: verdict, item count, epistemic, undecided, legs and
    # `tracker_unavailable`.
    by_namespace: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # On `none` / `related`, up to three rows the fit scored
    # under the related line whose title is near-identical to the draft's —
    # [{namespace, kind, number, title, state, url, confidence,
    #   title_similarity, evidence}]; the verdict above stays the fit's
    closest: list[dict[str, Any]] = Field(default_factory=list)
    # the draft inputs the stored fit rewards and the draft left empty:
    # [{input: body | paths | frames | version | component, what, feature}]
    needs: list[dict[str, str]] = Field(default_factory=list)
    # one sentence: the same-titled record, why the fit did not call it, what to pass
    closest_note: str = ""
    # On `none` / `related`, the fit's best scored-out issues
    # that share a distinctive term with the draft, up to three rows shown in
    # all (served items count) — [{namespace, number, title, state, url,
    # confidence, why}]. Below the related line: NOT duplicates, for the
    # reader to check by eye; the verdict above stays the fit's
    nearest: list[dict[str, Any]] = Field(default_factory=list)
    # The rerank stage — ran, model, prompt, pool size, picks
    # [{number, label: same|related, confidence}], tokens, ms, and the HyDE
    # scan's own block; a pick leads `nearest` and never changes the verdict
    rerank: dict[str, Any] = Field(default_factory=dict)
    # One sentence — already reported? fixed by which PR, in
    # which release? or a PR that may already address it. Issue rows carry
    # `links` [{number, title, state: merged|open|closed, merged_at, fixed_in}],
    # `fixed_in` and `backported_to` when the tracker links a PR
    answer: str = ""
    # On none / related, PRs that may already address the
    # draft (code.pr_check) — [{number, title, state, relation, score, merged_at}]
    related_prs: list[dict[str, Any]] = Field(default_factory=list)


class TrackerSourceListInput(BaseModel):
    """The tracker sources of an org: every configured source with
    its contract verdict, breaker and freshness, and the tracker namespaces reading
    it — each with the directory row's effective join rule and the caller's
    own standing there (`member_role`, `pending_role`), so a card can offer
    Join / Request / Open without another call. Org-level: any signed-in
    principal of the org may read it; no user or password is ever returned."""

    org: str = Field(..., description="The org whose tracker sources to list.")
    # Right after the caller's own add — re-read the registry
    # with read-your-writes instead of the process's snapshot
    fresh: bool = Field(False, description="Re-read the registry now (after your own add).")


class TrackerSourceListOutput(BaseModel):
    # [{name, origin: config|registry, host, port, database, configured,
    #   contract: {ok: bool|None, summary, checked_at, optional_present[], features{}},
    #   breaker: {state, consecutive_failures},
    #   freshness: {issues, prs, last_refresh_at} (a standard namespace's view; {} when
    #              the source could not be read in time or has none),
    #   namespaces: [{namespace, kind: "qa", visibility, repos[], classification,
    #                 join_policy (effective: open|approval|closed), registered,
    #                 member_role, pending_role}]}]
    sources: list[dict[str, Any]] = Field(default_factory=list)
    # Whether tracker.source.add/rotate/remove answer on this
    # deployment (LOOM_TRACKER_SELF_SERVICE_ENABLED) — the page offers the
    # form only when they do
    self_service: bool = True


class TrackerSourceNamespaceSpec(BaseModel):
    namespace: str = Field(..., description="qa-<team>; standard or restricted.")
    repos: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Org/repo prefixes of source_id this namespace may name; a repository "
        "not listed is never read.",
    )
    restricted: bool = Field(
        False, description="Private record: a token claim and a direct grant; join by approval."
    )
    join_policy: Literal["open", "approval"] | None = Field(
        None, description="For a standard namespace: open (default) or approval."
    )


class TrackerSourceAddInput(BaseModel):
    """Register a tracker source: a ClickHouse endpoint and database
    holding the record contract (docs/features/tracker/record-contract.md), a read-only
    credential, and the tracker namespaces that read it. Any signed-in person of
    the org may; they become the namespaces' first admin. Before anything is
    written the host must resolve to a public address, the credential must
    hold read access only, and the schema must meet the contract — each
    refusal names what to fix. The password is stored encrypted, never
    returned."""

    org: str = Field(..., description="The org registering the source.")
    name: str = Field(..., description="Source name: 2–32 lower-case letters, digits, - or _.")
    host: str = Field(
        ..., description="ClickHouse host (a URL is accepted; only the host is kept)."
    )
    port: int = Field(8443, ge=1, le=65535, description="HTTPS port (8443 on ClickHouse Cloud).")
    secure: bool = Field(True, description="TLS.")
    database: str = Field(..., description="The database holding the contract views.")
    user: str = Field(..., description="A READ-ONLY user (SELECT on the views and system.columns).")
    password: str = Field(..., description="Stored encrypted at rest; never returned.")
    namespaces: list[TrackerSourceNamespaceSpec] = Field(
        ..., min_length=1, max_length=8, description="The tracker namespaces reading this source."
    )
    # How the team's own tables map onto loom's logical model
    # (mapping.SourceMapping JSON); omitted = the database exposes the
    # logical tables directly as views
    mapping: dict[str, Any] | None = Field(
        None,
        description="Per logical table (github_issues, github_prs, edges, …): the team's table "
        "(`source`) and, per logical column, an expression over its columns — a column name, "
        "{lit}, {concat}, {coalesce}, {eq}, {in}, {has}, {cast}, {lower}. Omit for views.",
    )
    # The form's Check — every gate, nothing written
    dry_run: bool = Field(
        False,
        description="Run every check and write nothing; a schema that fails the contract "
        "is returned with view_templates instead of refused.",
    )


class TrackerSourceAddOutput(BaseModel):
    # The registered source's name, normalised.
    name: str
    # The stored host (only the host part of a URL).
    host: str
    # The database the source reads.
    database: str
    # [{namespace, repos, classification, join_policy}]
    namespaces: list[dict[str, Any]] = Field(default_factory=list)
    # The record-contract report (`ok`, missing tables and columns, type mismatches, …).
    contract: dict[str, Any] = Field(default_factory=dict)
    # roles the credential holds, shown so the registrant can see them; the
    # read-only check reads their grants too
    roles: list[str] = Field(default_factory=list)
    # `admin` when the caller becomes the namespaces' first admin; "" with auth off.
    my_role: str = ""
    # A dry run says what it would write and writes nothing;
    # view_templates = table → a CREATE OR REPLACE VIEW draft for each
    # required table the schema fails on ({} when it meets the contract)
    dry_run: bool = False
    # True when the source and its namespaces were stored; False on a dry run.
    written: bool = True
    # On a dry run: table -> a view draft for each required table the schema fails; `{}` when
    # it meets the contract or a `mapping` was given.
    view_templates: dict[str, str] = Field(default_factory=dict)


class TrackerContractInput(BaseModel):
    """The record contract as the register page shows it before anyone
    connects: every table loom reads, required or optional,
    each column's family, the type a view writes, whether a default is safe,
    what it holds — and a `CREATE OR REPLACE VIEW` template per table with
    every column at its typed default. No I/O; the same for every caller."""

    org: str = Field(..., description="The org.")
    database: str = Field(
        "<database>", description="The database the templates name (default a placeholder)."
    )


class TrackerContractOutput(BaseModel):
    # The database name the templates use (the placeholder `<database>` when none valid
    # was given).
    database: str
    # [{table, required, note, columns: [{name, family, type, must_map, note}]}]
    tables: list[dict[str, Any]] = Field(default_factory=list)
    # Table name -> a `CREATE OR REPLACE VIEW` template with every column at its typed default.
    templates: dict[str, str] = Field(default_factory=dict)


class TrackerSourceInspectInput(BaseModel):
    """Connect with a read-only credential and say what the
    database holds — every table the user can read, its columns and types —
    plus a first mapping of it onto loom's logical model. Writes nothing."""

    org: str = Field(..., description="The org.")
    # a registered tracker, by name: its stored endpoint and credential (the
    # registrant or a namespace admin) — the page edits a mapping without
    # ever seeing the password; otherwise the connection fields below
    name: str = Field("", description="A registered tracker to inspect with its own credential.")
    host: str = Field("", description="ClickHouse host (a URL is accepted).")
    # HTTPS port of the ClickHouse endpoint (8443 on ClickHouse Cloud); ignored with `name`.
    port: int = Field(8443, ge=1, le=65535)
    # Connect over TLS; ignored with `name`.
    secure: bool = True
    database: str = Field("", description="The database to inspect.")
    user: str = Field("", description="A read-only user.")
    password: str = Field("", description="Never stored by inspect.")
    repos: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="The repositories the tracker holds (Org/repo); one lets a table without a "
        "repository column still build 'Org/repo#number' references.",
    )


class TrackerSourceInspectOutput(BaseModel):
    # The host inspected (only the host part of a URL).
    host: str
    # The database inspected.
    database: str
    # [{table, engine, rows, columns: [{name, type}]}]
    tables: list[dict[str, Any]] = Field(default_factory=list)
    # roles the credential holds, shown so the caller can see them; the
    # read-only check reads their grants too
    roles: list[str] = Field(default_factory=list)
    # {mode: views|mapping, roles: {logical: table}, fields: {logical: {column: {from,
    #  confidence}}}, mapping: mapping.SourceMapping JSON | None}
    proposal: dict[str, Any] = Field(default_factory=dict)


class TrackerSourceSetMappingInput(BaseModel):
    """Change a registered tracker's mapping, with the same
    checks as add; `mapping: null` returns it to the views case. The
    registrant or an admin of one of its namespaces."""

    org: str = Field(..., description="The org.")
    name: str = Field(..., description="The tracker's name.")
    mapping: dict[str, Any] | None = Field(None, description="As tracker.source.add's `mapping`.")
    dry_run: bool = Field(False, description="Check and write nothing.")


class TrackerSourceSetMappingOutput(BaseModel):
    # The tracker source changed.
    name: str
    # The record-contract report for the new mapping (`ok`, missing tables and columns, …).
    contract: dict[str, Any] = Field(default_factory=dict)
    # True when the call only checked.
    dry_run: bool = False
    # True when the new mapping was stored; False on a dry run.
    written: bool = True


class TrackerSourceCheckInput(BaseModel):
    """Run the record-contract check on a configured tracker source now:
    the same read the boot gate runs, its verdict cached."""

    org: str = Field(..., description="The org.")
    name: str = Field(..., description="The source name.")


class TrackerSourceCheckOutput(BaseModel):
    # The source checked, normalised.
    name: str
    # The database the source reads.
    database: str = ""
    # False when the contract read failed or took over 8 s; `contract` then says why.
    reachable: bool = True
    # The record-contract report; `{ok: None, summary}` when unreachable.
    contract: dict[str, Any] = Field(default_factory=dict)
    # The view drafts for each required table the schema fails on
    view_templates: dict[str, str] = Field(default_factory=dict)


class TrackerSourceRotateInput(BaseModel):
    """Replace a registered source's credential. The
    registrant or an admin of one of its namespaces may; the new credential
    passes the same read-only and contract checks first."""

    org: str = Field(..., description="The org.")
    name: str = Field(..., description="The source name.")
    user: str = Field(..., description="The new read-only user.")
    password: str = Field(..., description="Stored encrypted at rest; never returned.")


class TrackerSourceRotateOutput(BaseModel):
    # The source whose credential was replaced.
    name: str
    # The record-contract report read with the new credential.
    contract: dict[str, Any] = Field(default_factory=dict)
    # When the credential was replaced, ISO UTC.
    rotated_at: str = ""


class TrackerSourceRemoveInput(BaseModel):
    """Retire a registered tracker source and its namespaces. The
    registrant or an admin of one of its namespaces may; `confirm` must equal
    the name. A source the deployment's configuration owns is changed there."""

    org: str = Field(..., description="The org.")
    name: str = Field(..., description="The source name.")
    confirm: str = Field(..., description="Type the source name to confirm.")


class TrackerSourceRemoveOutput(BaseModel):
    # The source retired.
    name: str
    # True when the source's registry row was marked inactive; False when unknown.
    deactivated: bool = False
    # The source's tracker namespaces that were deleted; one that failed is left out.
    namespaces_retired: list[str] = Field(default_factory=list)


class TrackerLinksInput(_TrackerIdentity):
    """Which PR probably fixed this issue — or which issue this PR probably
    fixed — when nobody wrote the link down. Candidates
    computed daily by the `tracker_links` pass: the merged trunk PRs of the
    window after the issue ranked by BM25F over title and body, the issue's
    stack frames in the PR's diff and files, the closing mention and the
    component; every hit carries `score`, `features` and `why`, and
    `epistemic` is always `candidate`. With `include_record` the tracker graph's
    own `fixes` edges are read live beside them and a candidate the record
    confirms says so. Give `issue_number` or `pr_number` (one is required,
    checked after the namespace gate)."""

    # The issue whose probable fixing PRs to list; wins over `pr_number`. 0 = none.
    issue_number: int = Field(default=0, ge=0)
    # The PR whose probably-fixed issues to list, when `issue_number` is 0.
    pr_number: int = Field(default=0, ge=0)
    # At most this many candidates; the server caps it at 50.
    limit: int = Field(default=10, ge=1, le=50)
    # Also read the tracker graph's `fixes` edges live and mark candidates it confirms.
    include_record: bool = True


class TrackerLinksOutput(BaseModel):
    # {kind: issue | pr, number}
    subject: dict[str, Any] = Field(default_factory=dict)
    # candidates | none | not_computed — none: a list was computed and holds
    # no row above the floor (never "no fix exists"); not_computed: the pass
    # has not covered this number (outside its window, or never ran)
    verdict: str = "not_computed"
    # [{issue_number, pr_number, rank, score, why: [closes|mentions, lexical,
    #   frames, component], features {bm25_title, bm25_body, bm25_norm,
    #   window_hours, component_match, frames_symbols, frames_files,
    #   frames_total, closes_mention}, terms, pr_title, pr_url, pr_merged_at,
    #   pr_minor_version, issue_title, issue_created_at, computed_at,
    #   epistemic candidate, source candidate, confirmed_by_record?}]
    items: list[dict[str, Any]] = Field(default_factory=list)
    # Always `candidate`: these are computed guesses, not recorded links.
    epistemic: str = "candidate"
    # the tracker graph's fixes edges for the subject: {status served | empty |
    # skipped | tracker_unavailable, numbers: [...]}
    record: dict[str, Any] = Field(default_factory=dict)
    # the table's last write for this namespace (what `not_computed` is
    # relative to)
    computed_at: str = ""
    # the record leg could not be read; the candidates above still stand
    tracker_unavailable: bool = False
