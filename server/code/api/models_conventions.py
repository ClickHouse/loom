"""What a repository expects of a change: code.setting_facts and
code.conventions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from server.code.api.models_common import _CodeIdentity


class CodeSettingFactsInput(_CodeIdentity):
    """One ClickHouse setting, the four places that define it: its
    declaration and tier (production / important / beta / experimental /
    obsolete), every default change with the release it shipped in, whether
    `tests/clickhouse-test` randomizes it in CI (and how), and how many test
    files name it. A failure that shows up only with one value of a
    randomized setting is the randomizer's, not the PR's; a behaviour that
    changed with a release is a default change, not a regression."""

    # The setting name as declared (`max_threads`); a name that cannot be a setting answers
    # `not_a_setting`.
    name: str = Field(min_length=1, max_length=128)
    # At most this many default changes in `history`, newest release first (1..50).
    history_limit: int = Field(default=10, ge=1, le=50)


class CodeSettingFactsOutput(BaseModel):
    # The setting asked about, trimmed.
    name: str
    # known | not_a_setting | unknown
    verdict: str = "unknown"
    # control_failed | no_declaration | invalid_name | history_unread
    causes: list[str] = Field(default_factory=list)
    # {file, line, type, default, description, tier, alias}
    declaration: dict[str, Any] = Field(default_factory=dict)
    # The declaration's tier (production | important | beta | experimental | obsolete); ''
    # without a declaration.
    tier: str = ""
    # [{version, old, new, reason, line}], newest release first
    history: list[dict[str, Any]] = Field(default_factory=list)
    # {randomized, file, line, sampler, randomizer}
    randomized_in_ci: dict[str, Any] = Field(default_factory=dict)
    # test files under tests/ whose chunks name the setting (a floor)
    tests_total: int = 0
    # {setting, ok}: the positive control this call ran; ok=False ⇒ unknown
    control: dict[str, Any] = Field(default_factory=dict)
    # Id of this call's trace row ('' for an invalid name); pass it to `code.outcome`.
    trace_run_id: str = ""


class CodeConventionsInput(_CodeIdentity):
    """The repository's recurring maintainer checks as facts over one PR's
    diff: a setting added or re-defaulted without a SettingsChangesHistory
    entry; the same setting's docs mention; a new
    `ErrorCodes::NAME` use without its `extern const int` declaration; a
    bug fix that touches no test; a new stateless test whose name a
    previous PR already added, or that lacks its `.reference`; plus two
    leads — a changelog category unusual for the touched area, and a
    widely-called touched function no test names. Give `pr_number` (the
    diff comes from the ledger's merge commit on the mirror), or
    `head_sha` the mirror knows, or `files` + `diff` for an open PR.
    `rules` narrows the set."""

    # A merged PR in the ledger: its title, labels and merge commit's diff are checked; 0 = none.
    pr_number: int = Field(default=0, ge=0)
    # A commit the mirror holds: the diff from its merge-base with the branch tip is checked;
    # falls back to the ledger's merge commit when the mirror lacks it.
    head_sha: str = Field(default="", max_length=40)
    # An open PR's changed paths; with `diff` they replace any ledger file list.
    files: list[str] = Field(default_factory=list, max_length=400)
    # An open PR's unified diff; its hunks are what the rules check (with `files`).
    diff: str = Field(default="", max_length=1_500_000)
    # The rules to run (names from the conventions rule table); empty = every rule.
    rules: list[str] = Field(default_factory=list, max_length=9)
    # "pr_number, head_sha, or files/diff is required" is the handler's
    # check, raised after the namespace gate

    @field_validator("rules")
    @classmethod
    def _known_rules(cls, v: list[str]) -> list[str]:
        from server.code.domain.index.conventions import RULES

        bad = [r for r in v if r not in RULES]
        if bad:
            raise ValueError(f"unknown rules: {bad}; known: {list(RULES)}")
        return v


class CodeConventionsOutput(BaseModel):
    # The PR asked about; 0 when none was named.
    pr_number: int = 0
    # ledger | head_sha | supplied
    source: str = ""
    # The ledger PR's title; '' when the PR is not in the ledger or none was named.
    title: str = ""
    # The ledger PR's labels.
    labels: list[str] = Field(default_factory=list)
    # bug_fix | improvement | new_feature | performance | not_for_changelog |
    # build | ci | documentation | backward_incompatible | experimental | ''
    category: str = ""
    # Where `category` came from: `label:<name>`, `changelog_bullet` (the PR body), or ''.
    category_source: str = ""
    # When the ledger PR merged; '' without a ledger row.
    merged_at: str = ""
    # The ledger PR's merge commit; '' without a ledger row.
    merge_commit_sha: str = ""
    # The commit the diff was read at when `source` is head_sha; otherwise the `head_sha` sent.
    head_sha: str = ""
    # Changed paths known for the change (name-status list plus the diff's files).
    files_total: int = 0
    # Files whose hunks were parsed from the diff; fewer than `files_total` when it was cut.
    files_in_diff: int = 0
    # Size of the diff text read, in characters; 0 without a diff.
    diff_bytes: int = 0
    # True when the mirror diff hit `code_conventions_diff_max_bytes` and its last file was
    # dropped; `notes` says how many files were parsed.
    diff_truncated: bool = False
    # [{rule, verdict: violated|satisfied|not_applicable|unknown|unusual,
    #   evidence: [{path, line, text, ...}], epistemic: exact|lower_bound, note}]
    rules: list[dict[str, Any]] = Field(default_factory=list)
    # verdict -> count over `rules`
    summary: dict[str, int] = Field(default_factory=dict)
    # {coverage_symbol, ok, ran}: the coverage control rule 7 depends on
    control: dict[str, Any] = Field(default_factory=dict)
    # Why part of the answer is weaker: PR not in the ledger, merged into another branch, commit
    # not in the mirror, diff cut, supplied-diff pre-image; empty when none applies.
    notes: list[str] = Field(default_factory=list)
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
