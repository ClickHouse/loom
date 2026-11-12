"""Run ledger — one agent execution as a first-class object.

`run.begin` opens a run (goal, original request, expected outcome) and hands
back a `run_id` the agent passes on its writes (`memory.set`, `memory.update`,
`observe.record`); its reads are labelled through `X-Loom-Run-Id` or the MCP
session's run, since a body `run_id` on a read filters. `run.complete` closes it with the
terminal status, the memories the agent RELIED on (`memories_used` → a `used`
usage event, distinct from `recall` = returned) and an outcome that feeds the
evidence counters. `run.get` / `run.list` render the story: goal → recalled →
used → written → cost → outcome.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from server.core.naming import SENTINEL, named, names_session
from server.operations.base import MCP_HIDDEN
from server.operations.read_projection import ProjectedRow
from server.tasks.domain.budgets import BudgetKind
from server.tasks.domain.states import RunStatus


class RunBeginInput(BaseModel):
    org: str
    namespace: str
    agent: str
    user_id: str = Field(default=SENTINEL)
    # The conversation this run belongs to. Joins the run to the memories,
    # observations and model turns (loom.agent_turn_usage) already keyed by
    # session — cost per run comes from here without another write. Kept on a
    # re-sent begin of the same run_id that names none.
    session_id: str = Field(default=SENTINEL)
    # The run's id; empty or "-" = the server mints one. A caller-supplied id
    # serves end-to-end tracing (re-sending the same id is a replace, not a
    # duplicate). The shape of an MCP session's implicit run (`mcp-` and 40 hex
    # digits) is the transport's: a begin naming one is answered with an id of
    # its own.
    run_id: str = Field(default="", max_length=128)
    # What the run sets out to do, in the agent's words; stored on the run row.
    goal: str = Field(min_length=1, max_length=2000)
    # The original request that started the run, verbatim; empty when not given.
    user_request: str = Field(default="", max_length=8000)
    # What a successful run should produce; stored for later review, empty when not given.
    expected_outcome: str = Field(default="", max_length=2000)
    # Free-text label of where the run executes (e.g. `ci`, `local`); stored as given.
    environment: str = Field(default="", max_length=128)
    # The model the agent runs on; stored as given, empty when not given.
    model: str = Field(default="", max_length=128)
    consumer: str = Field(default="", max_length=64)
    # The agent's role in a multi-agent run — planner, executor, critic, or
    # the team's own word. Stored in metadata.role; a
    # reflection written at run.complete carries the tag `role:<role>` so a
    # same-role agent's memory.search(role=...) prefers it.
    role: str = Field(default="", max_length=64)
    # Free-form key/values stored on the run; `role`, when set, is added to it.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Model spend in USD the run may reach; needs a session_id (0 = none, kept on a
    # re-sent begin of the same run_id). Rounded to 6 places, so the smallest
    # budget is 0.000001. Hidden over MCP, which hides session_id.
    budget_usd: float = Field(default=0.0, ge=0.0, le=100_000.0, json_schema_extra=MCP_HIDDEN)
    # Seconds the run may take (0 = none, kept on a re-sent begin of the same run_id).
    budget_seconds: int = Field(default=0, ge=0, le=30 * 86400)

    @field_validator("budget_usd")
    @classmethod
    def _to_six_places(cls, v: float) -> float:
        # run.get rounds cost to 6 places, so the budget is kept to the same.
        rounded = round(v, 6)
        if v > 0 and rounded == 0:
            raise ValueError("budget_usd is kept to 6 places; the smallest is 0.000001")
        return rounded

    @model_validator(mode="after")
    def _cost_budget_has_a_session(self) -> RunBeginInput:
        if self.budget_usd > 0 and not names_session(self.session_id):
            raise ValueError("budget_usd needs a session_id: a run's spend is its session's turns")
        return self


class RunBeginOutput(BaseModel):
    run_id: str
    # Always `active` for a run just opened.
    status: RunStatus = "active"
    # When the run started, ISO UTC with a `Z`; a re-sent `run_id` keeps the first start.
    started_at: str
    # Protocol nudge: what to do with the run_id. Descriptions are read once;
    # a field on the response is read every call.
    next: str = ""


class RunReflection(BaseModel):
    """Process learning from a run — hypotheses, not facts. Stored as ONE
    memory of memory_type="reflection" (key `reflection:<run_id>`), embedded
    so a later run can find it by similarity, but excluded from an unfiltered
    search or memory.ask: a lesson must be re-validated before it is
    stated as a claim. Each list is capped at 20 items of
    500 characters."""

    what_worked: list[str] = Field(default_factory=list, max_length=20)
    what_failed: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    lessons: list[str] = Field(default_factory=list, max_length=20)
    improvements: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # Reflection grounding: the memory_keys (or evidence ids)
    # this lesson rests on. A `what_failed` or `lessons` item without any
    # evidence is a generalisation nobody can audit, so those two lists
    # require at least one key; the reflection memory records the keys and
    # a `derived_from` edge to each so the grounding grade sees them.
    evidence: list[str] = Field(default_factory=list, max_length=50)
    # Unvalidated lessons fade: the reflection memory expires after this many
    # days unless memory.verify or a positive outcome renews it. 0 = never.
    expires_in_days: int = Field(default=90, ge=0, le=3650)

    @model_validator(mode="after")
    def _grounded(self) -> RunReflection:
        cleaned = [str(k).strip() for k in self.evidence if str(k).strip()]
        if len(cleaned) != len(self.evidence):
            self.evidence = cleaned
        generalises = any(str(x).strip() for x in (*self.what_failed, *self.lessons))
        if generalises and not cleaned:
            raise ValueError(
                "reflection.evidence is required when what_failed or lessons is "
                "given: name the memory_keys the lesson rests on"
            )
        return self

    def is_empty(self) -> bool:
        return not any(
            (self.what_worked, self.what_failed, self.assumptions, self.lessons, self.improvements)
        )


class RunCompleteInput(BaseModel):
    org: str
    namespace: str
    # The run's user and conversation, as run.begin took them: a token narrowed to
    # one session resolves its scope from these, and completes only its own run.
    user_id: str = Field(default=SENTINEL)
    session_id: str = Field(default=SENTINEL)
    run_id: str = Field(min_length=1, max_length=128)
    # How the run ended: `completed`, `failed` or `cancelled`; also picks the default `outcome`.
    status: Literal["completed", "failed", "cancelled"]
    # The agent's final answer; stored on the run row.
    final_response: str = Field(default="", max_length=8000)
    # The agent's confidence in the result (0–1), stored on the run; 0 records the
    # session outcome label at full confidence.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # memory_keys the agent actually relied on (a subset of what it recalled).
    # Each gets a `used` usage event; on a positive outcome, an `evidence`
    # event too — the credit-assignment signal a returned-but-ignored row
    # never earns; on a negative outcome, a `harm` event.
    memories_used: list[str] = Field(default_factory=list, max_length=500)
    # The memory keys the session's Loom tools SERVED (the
    # plugin's PostToolUse hook stashes them from loom_search / memory.get
    # results and hands them over at SessionEnd). Recalled, not relied on:
    # `memories_used` above stays the agent's own reliance label;
    # this list only feeds run.get's live `recalled`, whose trace-log
    # derivation cannot see MCP reads ledgered under the connection's own
    # implicit run.
    recalled_keys: list[str] = Field(default_factory=list, max_length=500)
    # Outcome applied to memories_used. "" derives from status: completed →
    # positive, failed → negative, cancelled → none. Negative charges `harm`
    # to the memories used (never to the merely recalled); the fold floors
    # the search-side reinforcement at the write-time base, so one bad run
    # takes back earned boost, never the cosine rank, and never the row.
    outcome: Literal["", "positive", "negative", "none"] = ""
    # Event count each usage, evidence or harm event for `memories_used` carries (1–100).
    weight: int = Field(default=1, ge=1, le=100)
    # Wall-clock override; 0 = computed from started_at.
    duration_ms: int = Field(default=0, ge=0)
    # Key/values merged into the run's stored metadata; keys given here win.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional process learning, written as a `reflection` memory (see
    # RunReflection). Omit when nothing reusable was learned.
    reflection: RunReflection | None = None
    # What happens to the rows this run wrote. '' = the
    # server default (settings.memory_run_complete_retain, `keep`). `demote`
    # lowers their confidence now; `expire` sets an expiry with on_expire=
    # demote so the expiry sweep does it when the expiry passes. Pinned,
    # verified, promoted, locked, episodic and reflection rows are retained
    # whatever the mode. Never a tombstone.
    retain: Literal["", "keep", "demote", "expire"] = ""


class RunCompleteOutput(BaseModel):
    run_id: str
    # The status the run was closed with.
    status: RunStatus
    # Run length in milliseconds: `duration_ms` from the call, else now minus `started_at`.
    duration_ms: int
    # memories_used keys that resolved to a live memory row (each got `used`).
    used_applied: int = 0
    # rows that also received a positive-outcome evidence event.
    evidence_applied: int = 0
    # rows that received a negative-outcome harm event.
    harm_applied: int = 0
    # memory_keys written under this run_id (derived from loom.memory).
    memories_written: list[str] = Field(default_factory=list)
    # distinct memory_keys memory.search returned under this run_id — needs
    # LOOM_SEARCH_TRACE_LOG_ENABLED; -1 when the trace log is off.
    recalled: int = -1
    # episodic observations (observe.record) tagged with this run_id.
    observations: int = 0
    # procedural memories in memories_used that got a procedure.record_run
    # row (path empty, ok = the run's outcome) — the ledger procedure.evolve
    # reads. 0 on a `none` outcome.
    procedure_runs_recorded: int = 0
    # memory_key of the reflection written from `reflection`; "" when none.
    reflection_key: str = ""
    # Retention counts over the rows this run wrote.
    retain: str = "keep"
    # Rows this run wrote that the `demote`/`expire` mode left alone (pinned, verified, …); 0
    # under `keep`.
    retained: int = 0
    # Rows whose confidence was lowered by `retain=demote`.
    demoted: int = 0
    # Rows given an expiry by `retain=expire`.
    expiring: int = 0


class RunRow(ProjectedRow):
    run_id: str
    agent: str = ""
    user_id: str = "-"
    session_id: str = "-"
    goal: str = ""
    user_request: str = ""
    expected_outcome: str = ""
    environment: str = ""
    model: str = ""
    consumer: str = ""
    status: RunStatus = "active"
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    final_response: str = ""
    confidence: float = 0.0
    # The graded outcome run.grade set after close ('' =
    # not graded). `status` says the run ended; `grade` says whether its
    # result held up.
    grade: str = ""
    grade_source: str = ""
    grade_reason: str = ""
    grade_ref: str = ""
    graded_at: str = ""
    memories_used: list[str] = Field(default_factory=list)
    memories_written: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # The budget run.begin set; 0 = none.
    budget_usd: float = 0.0
    budget_seconds: int = 0


class RunGradeInput(BaseModel):
    """A verdict on a CLOSED run, usually days after it —
    a maintainer confirmed the finding, CI passed, a reviewer rejected it.

    `run.complete`'s status says the run ended; the grade says whether its
    result held up. Address one run by `run_id`, or every closed run of a
    session by `session_id` (e.g. `pr_<number>`). A regrade replaces the
    run's verdict. Every memory in the run's `memories_used` is counted
    against the grade — the counts `memory.proposals` shows — and, under an
    owner-set `proposal_policy` of mode `auto`, proposals among them are
    accepted or rejected on those counts."""

    org: str
    namespace: str
    # The run to grade; "" or "-" = none. Name exactly one of run_id and session_id.
    run_id: str = Field(default="", max_length=128)
    # Every closed run of this session; "" or "-" = none.
    session_id: str = Field(default="", max_length=256)
    # The verdict: `pass` (the result held up) or `fail`.
    grade: Literal["pass", "fail"]
    # Who graded: `maintainer`, `ci`, `reviewer`, `judge`… (free text, short).
    source: str = Field(default="caller", min_length=1, max_length=64)
    # Why the grade was given; stored on the run as `grade_reason`.
    reason: str = Field(default="", max_length=2000)
    # What the grade rests on: a URL, a finding key, a check run id.
    ref: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def _at_most_one_target(self) -> RunGradeInput:
        # Naming neither is refused by the handler, after the read-scope gate.
        self.run_id = named(self.run_id.strip()) or ""
        self.session_id = named(self.session_id.strip()) or ""
        if self.run_id and self.session_id:
            raise ValueError("pass exactly one of run_id or session_id")
        return self


class RunGradeProposal(BaseModel):
    memory_key: str
    decision: Literal["accept", "reject"]
    passes: int
    fails: int


class RunGradeOutput(BaseModel):
    # Runs whose grade was written (a closed run whose verdict changed).
    graded: int = 0
    # Ids of the runs whose grade was written.
    run_ids: list[str] = Field(default_factory=list)
    # Runs of the target left alone because they are still active (not closed).
    skipped_active: int = 0
    # Closed runs that already carried this grade, source, reason and ref.
    unchanged: int = 0
    # Distinct memories the graded runs relied on (memories_used).
    memories_counted: int = 0
    # Proposals the owner-set policy decided on this grade (auto mode only).
    proposals_decided: list[RunGradeProposal] = Field(default_factory=list)


class RunGetInput(BaseModel):
    org: str
    namespace: str
    run_id: str = Field(min_length=1, max_length=128)


class RunGetOutput(BaseModel):
    # False when no run with this id exists in the namespace; every other field is then empty.
    found: bool
    # The run row; `None` when not found.
    run: RunRow | None = None
    # Live derivations (not stored on the row): what the run recalled, wrote,
    # observed and cost. Recalled needs the trace log (-1 = off).
    recalled: int = -1
    # Keys memory.search returned under the run plus keys the hooks reported as served;
    # empty when neither is known.
    recalled_keys: list[str] = Field(default_factory=list)
    # Keys written under this run's id, read live from `loom.memory`.
    memories_written: list[str] = Field(default_factory=list)
    # Episodic observations (`observe.record`) tagged with this run's id.
    observations: int = 0
    # The session's turns in loom.agent_turn_usage (the plugin's Stop hook writes
    # them) made while the run was open; 0 when the run has no session or the
    # hook is not installed.
    turns: int = 0
    # Those turns whose model the price table does not know: each costs 0, so
    # `cost_usd` and a cost budget leave them out.
    unpriced_turns: int = 0
    # Summed model cost in USD of those turns; 0 as for `turns`.
    cost_usd: float = 0.0
    # Seconds from started_at to completed_at (to now while active), on the
    # server's clock; not duration_ms.
    elapsed_seconds: int = 0
    # The budgets the run has gone past: `cost` (cost_usd > budget_usd) and
    # `time` (elapsed_seconds > budget_seconds). Empty when within budget or
    # when run.begin set none.
    over_budget: list[BudgetKind] = Field(default_factory=list)


class RunListInput(BaseModel):
    org: str
    namespace: str
    # Only this agent's runs; "", "*" or "-" = every agent.
    agent: str = ""
    # Only this session's runs; "", "*" or "-" = every session.
    session_id: str = ""
    # Only runs in this status; "" lists every status.
    status: Literal["", "active", "completed", "failed", "cancelled"] = ""
    # At most this many runs, in `order_by` order; the server caps it at 200.
    limit: int = Field(default=50, ge=1, le=200)
    # Runs to skip before the page (0-100,000); default 0, the first page.
    offset: int = Field(default=0, ge=0, le=100_000)
    # Sort by start time, duration (0 while active) or memories relied on; default newest first.
    order_by: Literal[
        "started_desc",
        "started_asc",
        "duration_desc",
        "duration_asc",
        "relied_on_desc",
        "relied_on_asc",
    ] = "started_desc"
    # Only runs whose goal, run id or agent holds this text, in any case; "" is no filter.
    q: str = Field(default="", max_length=200)
    # The Runs page folds runs that neither relied on nor wrote a memory.
    # Folding on the client, after `limit`, can leave a chatty consumer's page
    # empty: its newest runs are all hollow while filled runs sit further
    # back. `with`
    # keeps only runs with a relied-on or written memory, `without` only the
    # hollow ones, "" (default) every run. It is a WHERE clause, so `limit`
    # counts the rows the caller will actually see.
    memory_activity: Literal["", "with", "without"] = ""
    # `ungraded` / `graded` / a verdict (`pass`, `fail`);
    # "" = every run.
    grade: Literal["", "ungraded", "graded", "pass", "fail"] = ""
    # The output fields the caller reads (a whitelist
    # resolved against the row model; an unknown name is a 422); unset, every
    # field. Every list operation is projectable.
    fields: list[str] | None = None


class RunListOutput(BaseModel):
    # Matching runs in `order_by` order (newest `started_at` first by default),
    # projected to `fields` when given.
    runs: list[RunRow]
    # How many runs match the filters; null on a full page after the first.
    total: int | None = None
