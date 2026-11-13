"""Cost plane — cost.summary over agent_cost_rollup_daily and the live agent_events tail."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# `memory_key` and `user_id` live only on loom.agent_events, not the MV grain —
# grouping or filtering by either switches cost.summary to a direct
# events-table scan (slower, but the only place those columns exist).
# `instance_label` and `build_commit` name
# the PROCESS that wrote the row — the deployment (`deploy:web:blue`) against
# every isolated bench server — which `traffic_class` (a claim on the token)
# does not: a bench replaying product-shaped questions with a service-account
# token is `production` by class and a bench by label. Events-scan only.
GroupDim = Literal[
    "namespace",
    "agent",
    "day",
    "operation",
    "memory_key",
    "user_id",
    "model",
    "traffic_class",
    "instance_label",
    "build_commit",
]


def _default_group_by() -> list[GroupDim]:
    return ["operation"]


class CostSummaryInput(BaseModel):
    """Cost rollup over a date window.

    Set `namespace=None` (or omit) to aggregate across all the org's
    namespaces — CFO-facing view. Use `group_by=["namespace"]` to keep
    the per-namespace breakdown in that case.
    """

    org: str
    namespace: str | None = None
    # Inclusive lower bound of the window, compared against each event's UTC day.
    from_date: date
    to_date: date = Field(..., description="Inclusive upper bound")
    group_by: list[GroupDim] = Field(
        default_factory=_default_group_by,
        description="Dimensions to GROUP BY. Defaults to operation-level rollup.",
    )
    agent: str | None = None
    # Only this op name (for example 'memory.ask'); None counts every op.
    operation: str | None = None
    # One end user only (forces an events scan); None or "-" = every user.
    user_id: str | None = None
    # One class only (production | interactive | bench);
    # forces an events scan like `user_id`.
    traffic_class: str | None = None
    # One launcher's processes (`deploy:web:blue`,
    # `iso:w6m1:7864`, …); forces an events scan like `user_id`.
    instance_label: str | None = None
    # Read the exact-prompt response cache's totals off the
    # ledger (`out_llm_cache_hits` / `out_llm_cache_saved_usd` on memory.ask
    # rows). Free on the events-scan path; on the roll-up path it is one
    # bounded statement over the window's memory.ask rows (the roll-up has no
    # args). Off, `cache_hits` / `cache_saved_usd` read 0 and nothing is read.
    include_cache: bool = True


class CostSummaryRow(BaseModel):
    """One row of the result. Dimensions populated only when grouped by."""

    namespace: str | None = None
    agent: str | None = None
    day: date | None = None
    operation: str | None = None
    memory_key: str | None = None
    user_id: str | None = None
    # LLM model that carried the op ('' = no LLM call). Events-scan only.
    model: str | None = None
    # Production | interactive | bench ('' when unrecorded). Events-scan only.
    traffic_class: str | None = None
    # The writing process's launcher label and build ('' when unrecorded).
    # Events-scan only.
    instance_label: str | None = None
    build_commit: str | None = None
    ops: int
    errors: int
    tokens: int
    embed_tokens: int
    cost_usd: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    # Response-cache hits and the dollars they did not spend
    # on this row's asks. Events-scan only (the roll-up carries no args); 0
    # on roll-up rows — read the totals instead.
    cache_hits: int = 0
    cache_saved_usd: float = 0.0


class CostSummaryOutput(BaseModel):
    # One row per distinct value of the `group_by` dimensions, most ops first, at most 5,000;
    # empty when the caller can read no namespace in the org.
    rows: list[CostSummaryRow]
    # Sum of `ops` over the returned rows.
    total_ops: int
    # Sum of `errors` (ops that failed) over the returned rows.
    total_errors: int
    # Sum of the upstream provider spend (`upstream_cost_usd`) over the returned rows, in USD.
    total_cost_usd: float
    # Exact-prompt response cache over the window —
    # LLM calls served from the table and the price-table dollars they did
    # not spend. `total_cost_usd` is what WAS spent; the two never overlap.
    cache_hits: int = 0
    # USD the response-cache hits did not spend, rounded to 6 places; 0 when `include_cache`
    # is off or the filter excludes `memory.ask`.
    cache_saved_usd: float = 0.0


# namespace=None returns an org-wide rollup across every namespace in the org.
class BudgetStatusInput(BaseModel):
    org: str
    namespace: str | None = None


class BudgetStatusOutput(BaseModel):
    namespace: str
    # Upstream spend over the last 24 hours (rolling, not calendar day), in USD.
    spent_24h_usd: float
    # Effective cap: per-namespace override if set, else global default.
    # 0.0 means uncapped; org-wide rollups are always uncapped.
    cap_usd: float
    # Where `cap_usd` came from: `namespace` (its own cap), `global` (the server default),
    # `uncapped` (neither is set) or `org` (an org-wide rollup, which has no cap).
    cap_source: Literal["namespace", "global", "uncapped", "org"]
    # True when a cap is set and `spent_24h_usd` has reached it; always false org-wide.
    over_cap: bool
    pct_of_cap: float | None = None  # 0..1, None when uncapped
    namespaces_with_spend: int = 0  # org-wide mode only


class TurnUsage(BaseModel):
    """One assistant turn's raw usage counters, as the transcript carries
    them. `input_tokens` is the uncached share; cache reads and writes are
    separate, and writes are split by TTL because they price differently."""

    message_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    input_tokens: int = Field(default=0, ge=0, le=100_000_000)
    cache_read_tokens: int = Field(default=0, ge=0, le=100_000_000)
    cache_creation_5m_tokens: int = Field(default=0, ge=0, le=100_000_000)
    cache_creation_1h_tokens: int = Field(default=0, ge=0, le=100_000_000)
    output_tokens: int = Field(default=0, ge=0, le=100_000_000)
    # ISO-8601 time of the turn (the transcript's timestamp). Empty or not a
    # time: the time this message_id is recorded with, else now.
    turn_at: str = Field(default="", max_length=40)


class CostRecordTurnsInput(BaseModel):
    """The coding agent's own spend for one session, a batch of turns per
    call. Posted by the plugin's Stop hook from the transcript tail; the
    server prices each turn and writes
    loom.agent_turn_usage, ReplacingMergeTree on (session, message_id) so a
    turn re-sent on the next Stop overwrites itself."""

    org: str
    namespace: str
    agent: str = "-"
    user_id: str = "-"
    session_id: str = Field(min_length=1, max_length=128)
    consumer: str = Field(default="", max_length=64)
    # The turns to record, 1 to 200 per call; a turn re-sent with the same `message_id`
    # replaces its earlier row.
    turns: list[TurnUsage] = Field(min_length=1, max_length=200)


class CostRecordTurnsOutput(BaseModel):
    # Turns written to the ledger; 0 when the write failed (the call still succeeds).
    recorded: int
    # Turns whose model the price table knows; 0 when the write failed.
    priced: int
    # Models the price table does not know: their rows carry cost_usd=0 and
    # priced=false; add them to PRICE_PER_1K_TOKENS rather than trusting $0.
    unpriced_models: list[str] = Field(default_factory=list)
    # Total price of the priced turns in this batch, in USD, rounded to 6 places.
    cost_usd: float = 0.0
    # Turns dated over 5 s after the server's clock: the client's clock runs
    # ahead, so a run's cost window may place them late. They are stored as
    # sent. The plugins post fire-and-forget and do not read this.
    turns_dated_ahead: int = 0


AgentGroupDim = Literal["namespace", "agent", "day", "model", "consumer", "session_id"]


class CostAgentSummaryInput(BaseModel):
    """The coding agent's own spend (loom.agent_turn_usage)
    over a date window — the series `cost.summary` does not cover, which
    prices what LOOM spends upstream. `namespace=None` aggregates the
    caller's accessible namespaces. Default grouping is by model, the
    dimension the price depends on."""

    org: str
    namespace: str | None = None
    # Inclusive lower bound of the window, compared against each turn's UTC day.
    from_date: date
    to_date: date = Field(..., description="Inclusive upper bound")
    group_by: list[AgentGroupDim] = Field(
        default=["model"],
        description="Dimensions to GROUP BY. Defaults to a per-model rollup.",
    )
    agent: str | None = None
    consumer: str | None = None


class CostAgentSummaryRow(BaseModel):
    namespace: str | None = None
    agent: str | None = None
    day: date | None = None
    model: str | None = None
    consumer: str | None = None
    session_id: str | None = None
    turns: int
    sessions: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    cost_usd: float
    # turns whose model the price table did not know: their cost is $0 here,
    # and the honest number is "unknown", not "free"
    unpriced_turns: int


class CostAgentSummaryOutput(BaseModel):
    # One row per distinct value of the `group_by` dimensions, highest cost first, at most
    # 5,000; empty when the caller can read no namespace in the org.
    rows: list[CostAgentSummaryRow]
    # Sum of `turns` over the returned rows.
    total_turns: int
    # Distinct sessions: the sum over rows when grouped by `session_id`, else the largest
    # row's count (a lower bound when a session spans several rows).
    total_sessions: int
    # Sum of `cost_usd` over the returned rows, in USD; unpriced turns count as 0.
    total_cost_usd: float
    # Turns whose model the price table did not know, summed over the returned rows.
    unpriced_turns: int
