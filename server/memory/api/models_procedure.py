"""Procedural memory plane — typed surface over memory_type='procedural'.

Each procedure is a memory row: memory_key='procedure_<slug>', value=JSON of
{situation, name, steps, tags, metadata}. The `situation` (the precondition)
drives the embedding so procedure.match retrieves by situation-similarity; the
full value is the procedure body the agent consumes. No schema changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProcedureAddInput(BaseModel):
    org: str
    namespace: str
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"

    # Procedure name; the memory key is `procedure_<slug of name>`, so the same name rewrites it.
    name: str = Field(min_length=1, max_length=128)
    # When the procedure applies (its precondition); this text drives `procedure.match`.
    situation: str = Field(min_length=1, max_length=2048)
    # Two at least: a single step is a lesson, not a procedure (the miner
    # holds the same line).
    steps: list[str] = Field(min_length=2, max_length=64)
    # Caller tags stored on the procedure; the row also gets the tag `procedure`.
    tags: list[str] = Field(default_factory=list, max_length=16)
    # Optional structured metadata (preconditions as key-value, expected
    # outputs, ...). Stored verbatim on the procedure record so callers
    # can attach their own conventions without us prescribing a schema.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional executable graph (nodes = steps / memory / consolidation /
    # decision, edges = dep / dataflow / branch). When omitted, a linear graph
    # is synthesised from `steps` so every procedure has a graph substrate that
    # record_run and evolve operate on uniformly. See memory.domain.procedure_graph.
    graph: dict[str, Any] | None = None


class ProcedureAddOutput(BaseModel):
    # Key of the procedure row (`procedure_<slug>`).
    memory_key: str
    # Id of the memory version written.
    memory_id: str
    # Version number of the written row.
    version: int


class ProcedureMatchInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"  # default fan-out — procedures from any agent in scope
    user_id: str = "-"
    session_id: str = "-"

    # The state the agent is in; compared against each procedure's stored situation.
    situation: str = Field(min_length=1)
    # At most this many candidates are searched (1–50); filters may return fewer.
    top_k: int = Field(default=5, ge=1, le=50)
    # Only procedures carrying at least one of these tags; empty = no tag filter.
    tags: list[str] = Field(default_factory=list)
    # `all_mine` searches every namespace the caller belongs to; `this` only `namespace`.
    namespace_scope: Literal["this", "all_mine"] = "all_mine"
    # When true, only return procedures promoted to reusable templates
    # (proven, well-exercised workflow topologies).
    templates_only: bool = False
    # Candidates are procedures nothing has exercised yet AND that no human
    # authored. They are returned last and labelled; pass False to drop them
    # entirely.
    include_candidates: bool = True
    # A quarantined procedure (its last `strike_limit` runs all failed) is
    # dropped unless asked for; returned last, labelled.
    include_quarantined: bool = False
    # The tools the agent has called recently in this task. Procedures whose
    # action nodes cover them are boosted: "which procedure am I in" answered
    # from the trajectory, fused with the situation similarity.
    recent_tools: list[str] = Field(default_factory=list, max_length=32)
    # Weight of `tool_overlap` in the fused `score`; 0 ignores `recent_tools`.
    tool_boost: float = Field(default=0.2, ge=0.0, le=1.0)
    # Weight on the ledger-derived utility term of the fused score:
    # score = similarity + tool_boost*overlap + weight * ((ok+1)/(total+2) - 0.5).
    # None = 0 (similarity + tool overlap only).
    utility_weight: float | None = Field(default=None, ge=0.0, le=5.0)
    # The objective the agent is pursuing, as distinct from the state it is
    # in. When given, matching is two-factor: the situation is scored against
    # the procedure's situation (precondition), the goal against its name
    # (the key axis), and the two cosines combine as a geometric mean so a
    # goal match with the wrong precondition cannot win. Omitted, matching
    # is by situation alone.
    goal: str | None = Field(default=None, max_length=2048)


ProcedureStatus = Literal["template", "exercised", "authored", "candidate", "quarantined"]


class _ProcedureMatch(BaseModel):
    name: str
    situation: str  # the stored situation that matched
    steps: list[str]
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    similarity: float  # cosine similarity vs query situation
    memory_key: str
    namespace: str
    graph: dict[str, Any] | None = None  # executable graph, when present
    template: bool = False  # promoted to a reusable workflow template
    run_stats: dict[str, int] = Field(default_factory=dict)  # {total, ok}
    # template / exercised / authored / candidate / quarantined — see
    # _procedure_status.
    status: ProcedureStatus = "authored"
    # {at, strikes, reason} when the procedure is struck out.
    quarantined: dict[str, Any] | None = None
    # Steps evolve flagged as disproportionately on failed runs: read as
    # pitfalls for this procedure, not as steps to skip.
    fail_prone_steps: list[str] = Field(default_factory=list)
    revision: int = 1
    # Topologies the acceptance gate reverted (digest, reason, before/after
    # success). An agent proposing an edit should not re-propose one of these.
    rejected_revisions: list[dict[str, Any]] = Field(default_factory=list)
    # Share of the caller's `recent_tools` this procedure's action nodes
    # cover (0..1), and the fused ranking score similarity + boost*overlap.
    tool_overlap: float = 0.0
    score: float = 0.0
    # What the ledger proved: Laplace-smoothed success rate minus 0.5, in
    # (-0.5, 0.5); 0 for an unrun procedure. Enters `score` scaled by the
    # request's `utility_weight` (0 when unset).
    utility: float = 0.0
    # Cosine of the caller's `goal` against the procedure's key axis (its
    # name); None when no goal was given or the row has no key vector.
    goal_similarity: float | None = None
    # {total, ok} for the calling agent (when it is a named agent) and per
    # agent, folded from the ledger on evolve.
    for_agent: dict[str, int] = Field(default_factory=dict)
    run_stats_by_agent: dict[str, dict[str, int]] = Field(default_factory=dict)
    # Who wrote the procedure (`human:<subject>` / `process:<subject>`); "" when unrecorded.
    # context.pack and memory.stack auto-inject only the caller's own.
    actor: str = ""


class ProcedureMatchOutput(BaseModel):
    # Matching procedures, by status tier (candidates and quarantined last), then `score`.
    matches: list[_ProcedureMatch]
    # The hit rate on the ledger — how many matched, the top
    # procedure and its score (out_matched / out_procedure_key / out_score).
    matched: int = 0
    # Memory key of the top match; "" when nothing matched.
    procedure_key: str = ""
    # Fused score of the top match; 0 when nothing matched.
    score: float = 0.0


class ProcedureListInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"
    user_id: str = "-"
    session_id: str = "-"

    # Only procedures whose key starts with `procedure_` + this; "" lists all.
    prefix: str = ""
    # Only procedures carrying at least one of these tags; empty = no tag filter.
    tags: list[str] = Field(default_factory=list)
    # At most this many procedure rows are read (the server caps it at 500).
    limit: int = Field(default=50, ge=1, le=500)


class _ProcedureSummary(BaseModel):
    name: str
    situation: str
    tags: list[str]
    memory_key: str
    created_at: datetime


class ProcedureListOutput(BaseModel):
    # The namespace's procedures (name, situation, tags, key, creation time).
    procedures: list[_ProcedureSummary]


class ProcedureRecordRunInput(BaseModel):
    org: str
    namespace: str
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"

    # Key of the procedure that was run (`procedure_<slug>`).
    memory_key: str = Field(min_length=1)
    # Ordered node ids the agent actually visited this run.
    path: list[str] = Field(default_factory=list, max_length=128)
    # Whether the run succeeded.
    ok: bool = True
    # Free-text note on the run (why it failed, …); `procedure.locate` shows failed runs' notes.
    note: str = Field(default="", max_length=512)
    # Not stored: the ledger stamps each run with the server's time.
    ts: str = ""
    # What the run cost. `steps_taken` defaults to the path
    # length when a path is given; all three are 0 when unknown, and the
    # gate ignores a revision until enough successful runs reported one.
    steps_taken: int | None = Field(default=None, ge=0, le=65535)
    # How long the run took, in milliseconds; 0 = unknown.
    duration_ms: int = Field(default=0, ge=0)
    # Tokens the run spent; 0 = unknown.
    tokens: int = Field(default=0, ge=0)


class ProcedureRecordRunOutput(BaseModel):
    # Key of the procedure the run was recorded against.
    memory_key: str
    # Always 0: the run goes to the ledger, the procedure row is not rewritten.
    version: int
    # `{total, ok, unfolded}` including this run; `unfolded` counts ledger runs
    # `procedure.evolve` has not folded yet.
    run_stats: dict[str, int]


class ProcedureEvolveInput(BaseModel):
    org: str
    namespace: str
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"

    # Key of the procedure to evolve (`procedure_<slug>`).
    memory_key: str = Field(min_length=1)
    # Minimum folded-in runs before pruning acts on the evidence.
    min_runs: int = Field(default=3, ge=1, le=1000)
    # Acceptance gate for a pending revision. `sprt` (default) is Wald's
    # sequential probability ratio test: it decides as soon as the runs
    # establish or rule out a drop of `gate_min_drop`, and waits otherwise.
    # `margin` is a fixed comparison of success rates.
    gate_mode: Literal["sprt", "margin"] = "sprt"
    # Runs needed on the revision under test before any verdict.
    gate_min_runs: int = Field(default=3, ge=1, le=1000)
    # margin mode: the success-rate drop that reverts.
    gate_margin: float = Field(default=0.05, ge=0.0, le=1.0)
    # sprt mode: the drop worth reverting over, error rates, and the run count
    # past which the point estimate decides.
    gate_min_drop: float = Field(default=0.15, ge=0.01, le=0.9)
    # sprt mode: false-revert error rate; lower needs more evidence before reverting.
    gate_alpha: float = Field(default=0.05, gt=0.0, lt=0.5)
    # sprt mode: missed-drop error rate; lower needs more evidence before accepting.
    gate_beta: float = Field(default=0.20, gt=0.0, lt=0.5)
    # sprt mode: runs on the revision after which the point estimate decides.
    gate_max_runs: int = Field(default=50, ge=2, le=10000)
    # Consecutive failed runs that quarantine the procedure;
    # None = LOOM_PROCEDURE_STRIKE_LIMIT (3), 0 = never.
    strike_limit: int | None = Field(default=None, ge=0, le=100)
    # Cost tie-break on an accepted revision: revert when its
    # successful runs cost more than this fraction above the prior revision's
    # (first of duration, steps, tokens both reported); both sides need
    # `gate_cost_min_samples` costed successes (default: gate_min_runs).
    # 0 disables the cost check.
    gate_cost_tolerance: float = Field(default=0.25, ge=0.0, le=10.0)
    # Costed successful runs each side needs for the cost check; `None` =
    # `gate_min_runs`.
    gate_cost_min_samples: int | None = Field(default=None, ge=1, le=1000)


class ProcedureEvolveOutput(BaseModel):
    # Key of the evolved procedure.
    memory_key: str
    # Version of the procedure row this call wrote.
    version: int
    # The procedure's topology revision after this call (a revert opens a new one).
    revision: int
    # True when this call changed the graph's flags or quarantine state.
    changed: bool
    # True when the procedure is now a reusable template (stable, mostly succeeds,
    # not quarantined).
    template: bool
    # Ledger rows folded into the graph by this call.
    folded_runs: int = 0
    # Acceptance gate on a pending revision: none (nothing
    # pending), undecided (not enough runs yet), accept, revert.
    gate: Literal["none", "undecided", "accept", "revert"] = "none"
    # The gate mode used (`sprt` or `margin`); "" when no revision was pending.
    gate_mode: str = ""
    # sprt: accumulated log-likelihood ratio (positive favours "the revision
    # made things worse"); the verdict fires when it crosses a boundary.
    gate_llr: float = 0.0
    gate_before: dict[str, int] = Field(default_factory=dict)  # {total, ok} prior revision
    gate_after: dict[str, int] = Field(default_factory=dict)  # {total, ok} revision under test
    # Median cost of successful runs per side, and why a
    # revision that passed the success gate was still reverted ("cost").
    gate_cost_before: dict[str, float] = Field(default_factory=dict)
    # Median cost of the successful runs on the revision under test.
    gate_cost_after: dict[str, float] = Field(default_factory=dict)
    # `success` or `cost` on a revert; "" otherwise.
    gate_reason: str = ""
    # Step texts no folded run ever traversed (demoted from the linear `steps`).
    dead_end_steps: list[str] = Field(default_factory=list)
    # Outcome contrast: steps / transitions disproportionately on failed runs
    # (failure share >= procedure base failure rate + margin, enough runs).
    # Transitions are "src -> dst" node ids.
    fail_prone_steps: list[str] = Field(default_factory=list)
    # Transitions disproportionately on failed runs, as `src -> dst` node ids.
    fail_prone_transitions: list[str] = Field(default_factory=list)
    # The procedure's folded `{total, ok}` after this call.
    run_stats: dict[str, int] = Field(default_factory=dict)
    # Consecutive failed runs at the end of the trace log, and whether the
    # procedure is struck out.
    strikes: int = 0
    # True when the procedure is struck out (see `strike_limit`).
    quarantined: bool = False
    # Success retention over the trace log: pairs of
    # consecutive runs whose first succeeded, how many of their seconds did,
    # and the rate. A stable procedure reproduces its successes.
    retention: dict[str, float] = Field(default_factory=dict)


class ProcedureReviseInput(BaseModel):
    org: str
    namespace: str
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"

    # Key of the procedure to revise (`procedure_<slug>`).
    memory_key: str = Field(min_length=1)
    # The proposed topology (full graph; nodes keep their ids where unchanged
    # so their statistics carry over).
    graph: dict[str, Any]
    # Why: what the failed runs showed and what this edit changes.
    reason: str = Field(default="", max_length=1024)
    # Propose a topology the gate already rejected anyway.
    force: bool = False


class ProcedureReviseOutput(BaseModel):
    # Key of the revised procedure.
    memory_key: str
    # Version of the procedure row this call wrote.
    version: int
    # The new revision number; runs recorded from now on count against it.
    revision: int
    # The revision it replaces (restored if the gate reverts).
    prior_revision: int
    # Hash of the proposed topology; the gate uses it to recognise a rejected one.
    digest: str
    # The linear step list derived from the new graph.
    steps: list[str] = Field(default_factory=list)


class ProcedureForkInput(BaseModel):
    """Copy a procedure from a namespace the caller can read into the
    request namespace, with its evidence reset. The org-wide template
    library is a convention on top of this: one namespace holding proven
    templates, viewer grants for everyone, `procedure.match
    templates_only=true namespace_scope=all_mine` to find them, fork to adopt."""

    org: str
    namespace: str  # target (the request namespace; WRITER enforced by the op)
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"

    # Namespace to copy from; the caller needs read access to it.
    from_namespace: str = Field(min_length=1)
    from_org: str = ""  # defaults to `org`
    # Key of the procedure to copy in `from_namespace`.
    memory_key: str = Field(min_length=1)
    # Rename on the way in (the memory key derives from the name).
    new_name: str = Field(default="", max_length=128)
    # Why the procedure was adopted; kept in `forked_from`.
    reason: str = Field(default="", max_length=1024)


class ProcedureForkOutput(BaseModel):
    # Key of the new procedure in the target namespace.
    memory_key: str
    # Version of the row written.
    version: int
    # Name of the new procedure (`new_name` or the source's).
    name: str
    # The copied steps.
    steps: list[str] = Field(default_factory=list)
    # Lineage: source org, namespace, key, revision, digest, template flag, run stats,
    # who forked it, why, and when.
    forked_from: dict[str, Any] = Field(default_factory=dict)


class ProcedureLocateInput(BaseModel):
    org: str
    namespace: str
    agent: str = "*"
    user_id: str = "-"
    session_id: str = "-"

    # Key of the procedure the agent is following (`procedure_<slug>`).
    memory_key: str = Field(min_length=1)
    # Where the agent is: a node id or the tool name of its most recent call.
    # Empty + `recent_path` given → the last element of recent_path.
    at: str = Field(default="", max_length=128)
    # The agent's recent node ids or tool names in this run, oldest first; feeds `revisits`
    # and `looping`.
    recent_path: list[str] = Field(default_factory=list, max_length=128)
    # How far ahead to expose. 2 is enough for the next decision; more is more
    # tokens.
    hops: int = Field(default=2, ge=1, le=4)
    # Attach recent failure notes from the ledger for transitions ahead.
    include_notes: bool = True
    # At most this many failure notes; 0 attaches none.
    max_notes: int = Field(default=5, ge=0, le=20)


class ProcedureLocateOutput(BaseModel):
    # Key of the procedure read.
    memory_key: str
    # The procedure's name.
    name: str = ""
    # `template`, `exercised`, `authored`, `candidate` or `quarantined`.
    status: ProcedureStatus = "authored"
    # True when `at` (or the last of `recent_path`) names a node of the graph.
    matched: bool
    # The node the agent is at; `None` when not matched.
    active: dict[str, Any] | None = None
    # hops[0] = one step ahead, hops[1] = two ahead, ... (dead ends omitted).
    hops: list[list[dict[str, Any]]] = Field(default_factory=list)
    # When not matched: node ids with no incoming edge, where the procedure starts.
    entry_nodes: list[str] = Field(default_factory=list)
    # Next node ids along the proven path from the active node.
    dominant_next: list[str] = Field(default_factory=list)
    # Trajectory window: visits per node in this run, whether the active node
    # is being revisited, and the untaken exits from it.
    revisits: dict[str, int] = Field(default_factory=dict)
    # True when the active node already appears earlier in `recent_path`.
    looping: bool = False
    # When looping: node ids reachable from the active node not yet visited in this run;
    # empty otherwise.
    exits: list[str] = Field(default_factory=list)
    # Notes agents left on failed runs that died on a transition ahead.
    failure_notes: list[dict[str, Any]] = Field(default_factory=list)
    # Deterministic text of the above, ready to drop into a prompt.
    rendered: str = ""
    # The procedure's folded `{total, ok}`.
    run_stats: dict[str, int] = Field(default_factory=dict)
    # `{total, ok}` for the calling agent; empty for `*`, `-` or no runs.
    for_agent: dict[str, int] = Field(default_factory=dict)
