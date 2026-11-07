"""code.outcome, the feedback loop: the action and reason vocabularies and the
span rule the outcome rows share.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from server.code.api.models_common import _CodeIdentity

# ============================================================
# code.outcome (feedback loop)
# ============================================================


# Actions that describe the plugin's PreToolUse shunt rather than a
# retrieval: no trace_run_id exists (the model never called a code op), the
# row IS the production counter. Everything else still needs the trace id.
SHUNT_OUTCOME_ACTIONS: frozenset[str] = frozenset({"shunt_hit", "shunt_pass"})


# Why the loom-code-shunt hook let a call through. A shunt_pass row carries
# one of these in `source`, one row per reason per batch, so `GROUP BY source`
# answers: what share of pass decisions — and of the tokens they let into the context — is the
# modified-file rule the working-tree overlay would address. Closed set,
# LowCardinality-friendly; the plugin holds the same list
# (integrations/claude-code-plugin/lib/code-shunt.mjs PASS_REASONS), and
# tests/unit/test_code_observability.py asserts the two agree.
SHUNT_PASS_REASONS: frozenset[str] = frozenset(
    {
        "modified",  # tracked file differs from HEAD: the index cannot describe it
        "untracked",  # git does not know the file: never indexed
        "targeted_span",  # Read with offset/limit: the model knew where to look
        "under_max_lines",  # small file: cheaper to read than to redirect
        "not_indexed",  # path outside the indexed prefixes
        "binary",  # nothing to offer
        "single_file_grep",  # grep inside one file is targeted
        "file_grep_lines",  # one-file grep asking for lines, run as content
        "tree_content_grep",  # tree-wide grep for matching lines, cheaper than a redirect
        "file_list_grep",  # tree-wide grep listing files or counts: no matching lines
        # A ranged read longer than the read-window cap, let through because this file's
        # per-session denial budget is spent
        "cap_spent",
        # A single-file grep whose pattern names a symbol, let through because this
        # symbol's per-session denial budget is spent
        "symbol_grep_spent",
        # A denial whose per-session budget spend could not be written (the counts file
        # cannot be kept), let through rather than deny every such call
        "budget_unrecorded",
    }
)


# Why a retrieval outcome was negative. A `fell_back_grep` or `ignored` row carries one
# of these in `source`, the way a shunt_pass carries its pass reason: closed set,
# LowCardinality, one GROUP BY. An unknown reason normalises to `unattributed`, so
# telemetry never 400s.
OUTCOME_REASONS: frozenset[str] = frozenset(
    {
        "abstained",  # the answer said it could not tell (abstain / not_covered / no_match)
        "empty",  # zero rows and no verdict the client could act on
        "too_slow",  # the call ran past the client's budget; grep was faster
        "wrong_shape",  # rows came back but not the kind the question needed
        "not_indexed",  # the path or symbol is outside what the index holds
        "stale",  # the index lags the commit the client works on
        "preference",  # the agent grepped anyway (habit, no stated cause)
        "unattributed",  # reason not recorded
    }
)


# The actions a reason belongs to.
OUTCOME_REASON_ACTIONS: frozenset[str] = frozenset({"fell_back_grep", "ignored"})


OUTCOME_REASON_UNATTRIBUTED = "unattributed"


_SPAN_MAX_LEN = 600


def normalize_outcome_span(value: object) -> str:
    """One `spans` entry: `path:start-end`, `start-end`, a bare `path`, or ''.
    Telemetry never fails a request: a malformed entry becomes '' (unknown)
    so the array stays aligned with `targets`; tabs/newlines are dropped and
    a span with end < start is unknown too."""
    s = str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()
    if not s:
        return ""
    if len(s) > _SPAN_MAX_LEN:
        return ""
    head, sep, tail = s.rpartition(":")
    lines = tail if sep else s
    a, dash, b = lines.partition("-")
    if dash and a.isdecimal() and b.isdecimal():  # isdigit() admits "²", which int() refuses
        if int(a) < 1 or int(b) < int(a):
            return head if sep else ""
        return s
    return s  # a bare path (or path with a ':' in it): known file, unknown lines


class CodeOutcomeInput(_CodeIdentity):
    # Required for retrieval outcomes; optional (empty) for shunt_* only.
    trace_run_id: str = Field(default="", max_length=64)
    # What happened after the answer: opened_file (a returned file was read), used_in_answer,
    # fell_back_grep, ignored; or a batch of shunt-hook decisions (shunt_hit | shunt_pass).
    action: Literal[
        "opened_file",
        "used_in_answer",
        "fell_back_grep",
        "ignored",
        # loom-code-shunt hook decisions, batched: `targets` holds one tool
        # name per decision (length = count), tokens_saved sums the estimate.
        "shunt_hit",
        "shunt_pass",
    ]
    # opened_file: the paths read (matched to the trace's returned paths); shunt_*: one tool
    # name per decision. At most 50 are stored.
    targets: list[str] = Field(default_factory=list)
    # Who / why. Retrieval outcomes: the client (plugin_hook | clickgap |
    # manual). shunt_hit: the client (claude-code-shunt | bench-shunt-tokens).
    # shunt_pass: the PASS REASON from SHUNT_PASS_REASONS; any other value
    # reads as "reason not recorded" in the GROUP BY.
    source: str = Field(default="", max_length=64)
    # shunt_hit: bytes of the file/grep result the model did not ingest ÷ 4.
    # shunt_pass: the same estimate for what the pass LET IN (the addressable
    # tokens, summed per reason). 0 when unknowable (grep).
    tokens_saved: int = Field(default=0, ge=0, le=1_000_000_000)
    # Aligned with `targets`: the line span each decision / open covered, as
    # `path:start-end` (shunt rows: the file and the lines the Read would
    # cover), `start-end` (opened_file: the Read's offset/limit against the
    # target path) or '' when unknown / a whole-file read. Lets credit land
    # on the unit that was read rather than on every unit of the path;
    # malformed entries normalise to '', never reject.
    spans: list[str] = Field(default_factory=list, max_length=50)
    # fell_back_grep / ignored: WHY, from OUTCOME_REASONS. Stored in `source` on those
    # rows, as a shunt_pass stores its pass reason; anything outside the set is recorded
    # as `unattributed`, and a client that sends none keeps its `source`. Ignored on
    # every other action.
    reason: str = Field(default="", max_length=32)

    @field_validator("spans", mode="before")
    @classmethod
    def _normalize_spans(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return [normalize_outcome_span(x) for x in v[:50]]

    @field_validator("reason", mode="before")
    @classmethod
    def _reason_never_rejects(cls, v: object) -> str:
        # telemetry: a non-string or an over-long value is "not recorded"
        s = str(v or "").strip().lower() if isinstance(v, (str, int, float)) else ""
        return s[:32]

    @model_validator(mode="after")
    def _trace_id_required_unless_shunt(self) -> CodeOutcomeInput:
        if self.action not in SHUNT_OUTCOME_ACTIONS and not self.trace_run_id:
            raise ValueError("trace_run_id is required for action " + repr(self.action))
        return self

    @property
    def pass_reason(self) -> str:
        """The shunt_pass reason when it is from the fixed vocabulary, else
        'unattributed'."""
        if self.action != "shunt_pass":
            return ""
        return self.source if self.source in SHUNT_PASS_REASONS else "unattributed"

    @property
    def outcome_reason(self) -> str:
        """The fell_back_grep / ignored reason: `reason` when it is from
        OUTCOME_REASONS, else 'unattributed'. '' on every other action."""
        if self.action not in OUTCOME_REASON_ACTIONS:
            return ""
        if self.reason in OUTCOME_REASONS:
            return self.reason
        return OUTCOME_REASON_UNATTRIBUTED

    @property
    def stored_source(self) -> str:
        """What the row's `source` column holds: the outcome reason when the
        client sent one (or an unknown one — `unattributed`), otherwise the
        client's own `source`."""
        if self.action in OUTCOME_REASON_ACTIONS and self.reason:
            return self.outcome_reason
        return self.source


class CodeOutcomeOutput(BaseModel):
    # True when the row was written; false when the trace log is disabled or the write failed.
    recorded: bool
