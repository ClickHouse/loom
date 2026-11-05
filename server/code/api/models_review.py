"""Change review: code.pr_check (pre-PR duplicate check) and
code.review_brief.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from server.code.api.models_common import _CodeIdentity, _drop_unset_overlay, _PrPostImage


class CodePrCheckInput(_CodeIdentity):
    """A PR draft asks whether a PR like it already exists. Every field is
    defaulted; the handler raises ValidationError after the gate when
    neither a title, changed paths nor an issue number is given."""

    # The draft's title; its tokens (with the symbols') are matched against PR titles.
    title: str = Field(default="", max_length=500)
    # The draft's description: `Fixes #N` links and `#N` mentions add issues, and its words
    # (template boilerplate stripped) add match tokens.
    body: str = Field(default="", max_length=20000)
    # repository-relative paths the draft changes
    paths: list[str] = Field(default_factory=list, max_length=200)
    # symbols the draft adds or changes (function / class / method names)
    symbols: list[str] = Field(default_factory=list, max_length=100)
    # issues the draft closes, beside the ones its text names (`Fixes #123`, `#123`)
    issues: list[int] = Field(default_factory=list, max_length=10)
    # the PR already opened for this draft: excluded from the candidates
    pr_number: int = Field(default=0, ge=0)
    # At most this many candidates in `items` (1..20).
    top_k: int = Field(default=5, ge=1, le=20)


class CodePrCheckOutput(BaseModel):
    # duplicate_of | related | none — `none` carries no items (open it)
    verdict: str = "none"
    # [{pr_number, state: open|merged|closed, title, author, opened_at, merged_at,
    #   closed_at, relation: duplicate|related, score, evidence: [str], shared_paths,
    #   files_total, linked_issues, labels, found_by: [leg], features}]
    items: list[dict[str, Any]] = Field(default_factory=list)
    # the top candidate sits in the upper related band: a human should look
    undecided: bool = False
    # heuristic (every leg read) | lower_bound (a leg was unavailable) | unavailable
    epistemic: str = "heuristic"
    # The score cut-offs the verdict used: {duplicate, related}.
    thresholds: dict[str, float] = Field(default_factory=dict)
    # rows read per leg, or "unavailable: <reason>"
    legs: dict[str, Any] = Field(default_factory=dict)
    # what the draft became: title_tokens, paths, symbols, issues
    draft: dict[str, Any] = Field(default_factory=dict)
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.review_brief
# ============================================================


class CodeReviewBriefInput(_CodeIdentity, _PrPostImage):
    """A merged PR's review brief: the point read of what the ingest built at merge —
    touched symbols with callers and fan-in, coverage verdicts, setting facts,
    regression history, blame of the deleted lines, path classes, the tracker record and
    the calibrated risk priors (`jit_risk` and `author_revert_prior`, ranking signals,
    never a gate) — or a build on the first miss. `rationale`, read on every call and
    never stored, lists the repository's design decisions and constraints that govern the
    PR's files. `tier` selects sections and a token budget (trivial 700: paths + symbols;
    standard 2,500: + tests, settings, history, risk, rationale; full 4,000: + blame, qa);
    `sections` narrows further; `token_budget` overrides the tier's."""

    # The PR to brief; 0 = find it by `merge_sha`. Required with `open`.
    pr_number: int = Field(default=0, ge=0)
    # The PR's merge commit (hex, up to 40 chars); '' = find it by `pr_number`.
    merge_sha: str = Field(default="", max_length=40, pattern=r"^[0-9a-fA-F]{0,40}$")
    # Serve only these of the tier's sections; empty = every section the tier carries.
    sections: list[
        Literal[
            "paths",
            "symbols",
            "tests",
            "settings",
            "history",
            "blame",
            "qa",
            "risk",
            "do_not_flag",
            "rationale",
        ]
    ] = Field(default_factory=list)

    # Which sections and default budget: trivial (paths, symbols), standard (+ tests,
    # settings, history, risk, rationale) or full (+ blame, qa).
    tier: Literal["trivial", "standard", "full"] = "standard"
    # Token budget for the served sections; 0 = the tier's (700 / 2,500 / 4,000).
    token_budget: int = Field(default=0, ge=0, le=16_000)
    # Whether a brief this call builds on a miss is stored for the next reader (the
    # default). A measurement consumer (`eval-*`, the bench sentinels) never stores,
    # whatever it passes.
    store: bool = True
    # "pr_number or merge_sha" is checked by the handler, not a model validator: the
    # namespace gate answers a refused principal (403/404) before any shape complaint,
    # as the other code reads do.

    # The PR is OPEN — brief its head (`refs/pull/N/head` on the mirror) diffed against
    # its merge-base with the indexed branch, with the post-image overlay on the impact
    # read (added functions are seeds). Keyed by the head: a pushed head is a miss and a
    # rebuild. `merge_sha` is ignored; `pr_number` is required.
    open: bool = False


class CodeReviewBriefOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _drop_unset_overlay(handler(self))

    # The PR the brief is of (from the stored brief when the request named only `merge_sha`).
    pr_number: int = 0
    # The PR's merge commit; '' on an open brief or when no brief was found.
    merge_sha: str = ""
    # precomputed (the ingest built it) | built (this call built it from the ledger's
    # row) | built_from_mirror (no ledger row; the merge commit, title and files came
    # from the mirror's first-parent chain and the reviewers from the tracker record) |
    # missing (neither the ledger nor the mirror names the PR, or no mirror to build
    # from)
    source: Literal["precomputed", "built", "built_from_mirror", "missing"] = "missing"
    # what the ledger said: present (this build read its row) | absent (this or the
    # stored build came from the mirror because the ledger had no row) | "" (not
    # consulted: the ingest built the row)
    ledger: Literal["", "present", "absent"] = ""
    # True when this call built the brief and stored it
    stored: bool = False
    # A mirror-built brief's disclosure — source, ledger, merge_commit, files, title (qa
    # | mirror), qa status, reviewers, author, has_test, is_backport_pr; {} for a
    # ledger- or ingest-built brief
    provenance: dict[str, Any] = Field(default_factory=dict)
    # On the wire only for `open: true`. `overlay` true = the brief is of the PR's head
    # (`overlay_head_sha`) against its merge-base (`overlay_base_sha` in the payload's
    # `base_sha`); `overlay_note` names why it could not be (`head_not_in_mirror` |
    # `no_merge_base` | `no_repo_entry`). `merge_sha` is '' on an open brief.
    overlay: bool = False
    # The open PR's head the brief was read or built at; '' when the mirror lacks it.
    overlay_head_sha: str = ""
    # Functions the open PR adds (the symbols section's `via: added` rows).
    overlay_added: int = 0
    # Why no open brief exists (`head_not_in_mirror` | `no_merge_base` | `no_repo_entry`);
    # '' when one was served.
    overlay_note: str = ""
    # The tier served.
    tier: str = "standard"
    # When the brief was built (ISO-8601 UTC); '' when `source` is missing.
    built_at: str = ""
    # The fast-tier watermark the brief was built against; '' when it was unread.
    indexed_commit: str = ""
    # How long building the brief took, in milliseconds (at build time, not this call).
    build_ms: int = 0
    # Estimated tokens of the sections served.
    tokens_used: int = 0
    # The budget the sections were cut to: the request's, else the tier's.
    token_budget: int = 0
    # one-bit answers read off the section verdicts: test_only,
    # symbol_with_regressions, uncovered_symbol, randomized_setting,
    # deleted_lines_blamed
    decisions: dict[str, bool] = Field(default_factory=dict)
    # name -> {status, verdict, epistemic, causes, rows, rows_total, tokens, truncated}
    sections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
