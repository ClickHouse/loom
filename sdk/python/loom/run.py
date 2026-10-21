"""Run ledger client — one agent execution as a first-class object.

    run = await agent.run.begin(goal="Fix the flaky CI job", user_request=prompt)
    await agent.memory.set("ci:fix", "Pin the runner image.", run_id=run["run_id"])
    ...
    await agent.run.complete(run["run_id"], status="completed",
                             memories_used=["ci:flaky:2026-09"],
                             final_response="Pinned the runner image.")

`agent.run_scope(...)` does the same as an `async with` block, and also labels
the block's reads with the run (see `RunScope`).

`memories_used` is the credit-assignment signal: each key gets a `used`
usage event (search-side reinforcement), and on a completed run a positive
`evidence` event as well. The server derives what the run wrote and
recalled from the run_id, and its cost from the session.

Pass the run_id on writes (memory.set, memory.update, observe.record). On a read
(memory.search, memory.ask) a body run_id filters to the run's own rows;
`run_scope` labels reads with the `X-Loom-Run-Id` header instead.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

# The run the calling task is inside, if any: `credit_answer` notes cited
# keys on it, writes carry its run_id, the scope completes it on exit.
_ACTIVE_RUN: ContextVar[RunScope | None] = ContextVar("loom_active_run", default=None)


def current_run() -> RunScope | None:
    return _ACTIVE_RUN.get()


def current_run_id() -> str | None:
    scope = _ACTIVE_RUN.get()
    return scope.run_id if scope is not None and scope.run_id else None


def credit_answer(result: Mapping[str, Any]) -> None:
    """Inside a run scope, the keys a grounded `memory.ask` answer cited
    become the run's `memories_used`; an abstention credits nothing."""
    scope = _ACTIVE_RUN.get()
    if (
        scope is not None
        and not result.get("abstain")
        and result.get("source") in ("synthesized", "precedent", "record")
    ):
        scope.note_cited(list(result.get("memory_keys") or []))


# The write paths an ambient run stamps its id on. Reads are
# deliberately absent — `run_id` on a read is a filter to the run's own rows,
# so the ambient run labels them with the `X-Loom-Run-Id` header the
# server's ledger reads instead.
AMBIENT_WRITE_PATHS: frozenset[str] = frozenset(
    {
        "/v1/memory.set",
        "/v1/memory.pin",
        "/v1/memory.update",
        "/v1/memory.set_from_messages",
        "/v1/memory.bulk_set",
        "/v1/memory.link",
        "/v1/observe.record",
        "/v1/working_memory.set",
        "/v1/working_memory.append",
    }
)
# Only calls on these planes open the ambient run; a grants or namespace
# call never does.
AMBIENT_PLANE_PREFIXES: tuple[str, ...] = (
    "/v1/memory.",
    "/v1/observe.",
    "/v1/working_memory.",
    "/v1/context.",
)
AMBIENT_RUN_HEADER = "X-Loom-Run-Id"


def stamp_write(op_path: str, payload: dict[str, Any], run_id: str | None) -> None:
    """Carry `run_id` on a write body that names no run of its own."""
    if run_id and op_path in AMBIENT_WRITE_PATHS and str(payload.get("run_id") or "") in ("", "-"):
        payload["run_id"] = run_id


# Recall reads where a body `run_id` filters to the run's own rows (see
# `scope_read_label`). Run-keyed ledger reads (outcome_run, search_trace_log)
# are absent on purpose: there the run_id is the parameter.
RECALL_READ_PATHS: frozenset[str] = frozenset(
    {
        "/v1/memory.search",
        "/v1/memory.search_trace",
        "/v1/memory.ask",
        "/v1/memory.list",
    }
)


def scope_read_label(op_path: str, payload: dict[str, Any]) -> dict[str, str]:
    """Keep a RunScope's run_id off recall bodies (where it would be a filter).

    Under an active `run_scope`, the memory builders inject `current_run_id()`
    into every body. On a recall read that is a FILTER (search only the run's own
    rows) and quietly returns nothing; a run is meant to LABEL reads, the
    same split the ambient run applies. So on a recall read, move the
    scope-injected run_id from the body to the `X-Loom-Run-Id` header. An
    explicit run_id the caller passed to deliberately scope the read (one that
    differs from the active run) is left in the body; writes and ledger ops are
    untouched. Returns the header(s) to add, or {}."""
    rid = current_run_id()
    if not rid or op_path not in RECALL_READ_PATHS:
        return {}
    if str(payload.get("run_id") or "") == rid:
        payload.pop("run_id", None)
    return {AMBIENT_RUN_HEADER: rid}


class AmbientRun:
    """The run an `Agent` binds when no `run_scope` is open.

    Lazy: nothing is sent until the first memory-plane call, which begins a
    run whose goal is that call's query or key; every later call inside the
    Agent's lifetime is threaded — writes by `run_id` (provenance), reads by
    the `X-Loom-Run-Id` header (a label the ledger reads, never a filter).
    `Agent.close()` / `__aexit__` completes it (status `completed`, or
    `failed` when the block raised; outcome `none` — the SDK cannot say how
    the work went and never guesses). An explicit `run_scope` inside the
    block wins for the calls it covers. Ledger failures never fail a call.
    """

    def __init__(self, client: RunClient, *, goal: str = "") -> None:
        self._client = client
        self._goal = goal
        self.run_id: str = ""
        self.completed: dict[str, Any] | None = None
        self._lock: Any = None
        self._begin_failed = False
        self._t0: float = 0.0
        self.calls: int = 0

    @staticmethod
    def goal_of(payload: dict[str, Any], fallback: str) -> str:
        for k in ("query", "goal", "memory_key", "text"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:2000]
        return fallback[:2000]

    async def _ensure(self, payload: dict[str, Any]) -> str:
        if self.run_id or self._begin_failed:
            return self.run_id
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.run_id or self._begin_failed:
                return self.run_id
            agent = self._client._agent
            try:
                res = await self._client.begin(
                    goal=self._goal or self.goal_of(payload, f"sdk ambient run ({agent.agent})"),
                    environment="sdk",
                    # The Agent's consumer when it has one; the
                    # agent label is the fallback.
                    consumer=(str(getattr(agent, "consumer", "") or "") or str(agent.agent))[:64],
                    session_id=str(payload.get("session_id") or "") or None,
                    metadata={"implicit": True, "transport": "sdk"},
                )
                self.run_id = str(res.get("run_id") or "")
                self._t0 = time.perf_counter()
            except Exception:  # noqa: BLE001 — the ledger never fails a call
                self._begin_failed = True
        return self.run_id

    async def before_post(self, op_path: str, payload: dict[str, Any]) -> dict[str, str]:
        """The transport hook: stamp a write, label a read, skip the rest."""
        if op_path.startswith("/v1/run.") or not op_path.startswith(AMBIENT_PLANE_PREFIXES):
            return {}
        if current_run_id():
            return {}  # an explicit scope covers this call
        run_id = await self._ensure(payload)
        if not run_id:
            return {}
        self.calls += 1
        if op_path in AMBIENT_WRITE_PATHS:
            stamp_write(op_path, payload, run_id)
            return {}
        return {AMBIENT_RUN_HEADER: run_id}

    async def complete(self, *, failed: bool = False) -> dict[str, Any] | None:
        if not self.run_id or self.completed is not None:
            return self.completed
        try:
            self.completed = await self._client.complete(
                self.run_id,
                status="failed" if failed else "completed",
                outcome="negative" if failed else "none",
                duration_ms=int((time.perf_counter() - self._t0) * 1000) if self._t0 else 0,
                metadata={"closed_by": "sdk_close", "calls": self.calls},
            )
        except Exception:  # noqa: BLE001 — never masks the caller's exit
            self.completed = None
        return self.completed


if TYPE_CHECKING:
    from loom.agent import Agent


class RunClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def begin(
        self,
        *,
        goal: str,
        user_request: str = "",
        expected_outcome: str = "",
        run_id: str = "",
        session_id: str | None = None,
        user_id: str | None = None,
        environment: str = "",
        model: str = "",
        consumer: str = "",
        role: str = "",
        metadata: dict[str, Any] | None = None,
        budget_usd: float = 0.0,
        budget_seconds: int = 0,
    ) -> dict[str, Any]:
        """Open a run. Returns {run_id, status, started_at, next}. Pass the
        run_id on writes (memory.set, memory.update, observe.record); on
        memory.search or memory.ask a body run_id filters to the run's rows,
        so label reads with `run_scope` (the `X-Loom-Run-Id` header) instead.
        `role` (planner / executor / critic / your own word) tags the run's
        reflection `role:<role>` so a same-role memory.search(role=...)
        prefers it. `budget_usd` / `budget_seconds` (0 = none) set a budget
        that `get` reports as `over_budget` once the run goes past it;
        `budget_usd` needs `session_id`, whose turns are the run's spend."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": self._agent.agent,
            "goal": goal,
            "user_request": user_request,
            "expected_outcome": expected_outcome,
            "environment": environment,
            "model": model,
            "consumer": consumer,
            "role": role,
            "metadata": metadata or {},
        }
        if budget_usd:
            payload["budget_usd"] = budget_usd
        if budget_seconds:
            payload["budget_seconds"] = budget_seconds
        if run_id:
            payload["run_id"] = run_id
        if session_id is not None:
            payload["session_id"] = session_id
        if user_id is not None:
            payload["user_id"] = user_id
        return await self._agent.transport.post("/v1/run.begin", payload)

    async def complete(
        self,
        run_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"] = "completed",
        final_response: str = "",
        confidence: float = 0.0,
        memories_used: list[str] | None = None,
        recalled_keys: list[str] | None = None,
        outcome: Literal["", "positive", "negative", "none"] = "",
        weight: int = 1,
        duration_ms: int = 0,
        metadata: dict[str, Any] | None = None,
        reflection: dict[str, Any] | None = None,
        retain: Literal["", "keep", "demote", "expire"] = "",
    ) -> dict[str, Any]:
        """Close a run once. `memories_used` = memory_keys you relied on;
        `recalled_keys` = memory_keys your searches RETURNED (recorded,
        never credited — a served row is not a relied-on row;
        ≤500, the plugin's hooks send the same list).
        `reflection` = {what_worked, what_failed, assumptions, lessons,
        improvements: list[str], confidence} — process learning stored as a
        `reflection` memory, excluded from plain recall (search it with
        memory_type="reflection"). Returns {run_id, status, duration_ms,
        used_applied, evidence_applied, memories_written, recalled,
        observations, reflection_key}."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "run_id": run_id,
            "status": status,
            "final_response": final_response,
            "confidence": confidence,
            "memories_used": list(memories_used or []),
            "outcome": outcome,
            "weight": weight,
            "duration_ms": duration_ms,
            "metadata": metadata or {},
        }
        if reflection:
            payload["reflection"] = reflection
        if retain:
            # Keep (default) | demote (confidence now) | expire
            # (expiry + on_expire=demote, the sweep does it later).
            payload["retain"] = retain
        if recalled_keys:
            payload["recalled_keys"] = list(recalled_keys)[:500]
        return await self._agent.transport.post("/v1/run.complete", payload)

    def scope(self, **begin_kwargs: Any) -> RunScope:
        """`async with agent.run.scope(goal=...) as run:` — see RunScope."""
        return RunScope(self, **begin_kwargs)

    async def get(self, run_id: str) -> dict[str, Any]:
        """Replay one run: the row plus live derivations (recalled keys,
        memories written, observations, turns, cost_usd, elapsed_seconds and
        the `over_budget` list)."""
        return await self._agent.transport.post(
            "/v1/run.get",
            {"org": self._agent.org, "namespace": self._agent.namespace, "run_id": run_id},
        )

    async def list(
        self,
        *,
        agent: str = "",
        session_id: str = "",
        status: str = "",
        limit: int = 50,
        memory_activity: str = "",
        grade: str = "",
        offset: int = 0,
        order_by: Literal[
            "started_desc",
            "started_asc",
            "duration_desc",
            "duration_asc",
            "relied_on_desc",
            "relied_on_asc",
        ] = "started_desc",
        q: str = "",
    ) -> dict[str, Any]:
        """Runs in `order_by` order (newest first by default).

        `memory_activity="with"` keeps only runs that relied on or wrote a
        memory, `"without"` only the hollow ones; `grade="ungraded"` the closed
        runs still owed a verdict (`"graded"`, `"pass"`, `"fail"` also work).
        `q` matches the goal, run id or agent, in any case; `offset` skips at
        most 100,000 runs. Filters apply before `limit`.

        `total` is None on a full page after the first."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": agent,
            "session_id": session_id,
            "status": status,
            "limit": limit,
            "memory_activity": memory_activity,
            "grade": grade,
        }
        if offset:
            payload["offset"] = offset
        if order_by != "started_desc":
            payload["order_by"] = order_by
        if q.strip():
            payload["q"] = q
        return await self._agent.transport.post("/v1/run.list", payload)

    async def grade(
        self,
        grade: str,
        *,
        run_id: str = "",
        session_id: str = "",
        source: str = "caller",
        reason: str = "",
        ref: str = "",
    ) -> dict[str, Any]:
        """Record whether a closed run's result held up (`"pass"` / `"fail"`),
        usually once the verdict arrives: a maintainer confirmed the finding,
        CI passed, a reviewer rejected it. Pass `run_id` for one run or
        `session_id` for every closed run of a session. Each memory the runs
        relied on (`memories_used`) is counted against the grade; under an
        owner-set proposal policy, proposals among them are decided."""
        return await self._agent.transport.post(
            "/v1/run.grade",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "run_id": run_id,
                "session_id": session_id,
                "grade": grade,
                "source": source,
                "reason": reason,
                "ref": ref,
            },
        )


class RunScope:
    """One agent execution as an `async with` block.

        async with agent.run_scope(goal="Fix the flaky CI job") as run:
            answer = await agent.memory.ask("what broke CI last week?")
            ...
            run.final_response = "Pinned the runner image."

    `run.begin` on entry; on exit `run.complete` with the keys the block's
    `memory.ask` answers cited (plus anything passed to `used()`), status
    `completed` (or `run.status` when the block set it), `failed` on an
    exception or `cancelled` on a cancellation (either propagates after the
    ledger row is written), and outcome `positive` when memories were used on
    a clean exit, `negative` on failure, unless set explicitly. Inside the block,
    writes without a run_id carry the run's, typed or through `Agent.call`
    (Session, AgentTools); recalls are labelled with it (`scope_read_label`).
    The ledger write is best-effort: a failed complete() never masks the
    block's own exception."""

    def __init__(self, client: RunClient, **begin_kwargs: Any) -> None:
        self._client = client
        self._begin_kwargs = begin_kwargs
        self.run_id: str = str(begin_kwargs.get("run_id") or "")
        self.used_keys: list[str] = []
        self.final_response: str = ""
        self.confidence: float = 0.0
        self.outcome: Literal["", "positive", "negative", "none"] = ""
        # How a block that ends without an exception closes the run.
        self.status: Literal["completed", "failed", "cancelled"] = "completed"
        self.metadata: dict[str, Any] = {}
        self.completed: dict[str, Any] | None = None
        self._token: Any = None
        self._t0: float = 0.0

    def used(self, *keys: str) -> None:
        """Name memories the run relied on (in addition to what recall cited)."""
        for k in keys:
            if k and k not in self.used_keys:
                self.used_keys.append(k)

    async def note(self, content: str, *, block_key: str = "scratchpad") -> dict[str, Any]:
        """Write the run's scratchpad block: the task's current
        state as the agent sees it. context.pack and memory.ask calls that
        carry this run_id read it back before the memories."""
        return await self._client._agent.working_memory.set(
            content, block_key=block_key, run_id=self.run_id
        )

    async def append_note(
        self, addition: str, *, block_key: str = "scratchpad", separator: str = "\n"
    ) -> dict[str, Any]:
        """Append to the run's scratchpad block."""
        return await self._client._agent.working_memory.append(
            addition, block_key=block_key, separator=separator, run_id=self.run_id
        )

    async def trace(
        self,
        actor: str,
        messages: list[dict[str, object]],
        **kwargs: Any,
    ) -> Any:
        """Write an actor's trace under the actor's identity:
        `memory.set_from_messages` with `user_id=actor` (lowercased) and
        this run's id. The extractor's dedup context is then that actor's own
        memories — another actor's plan in the context measured 0 of 9 of this
        actor's decided values stored, 9 of 9 under its own identity — and a
        namespace-wide read (the default `read_scope`) still sees every actor.
        Any `set_from_messages` keyword passes through."""
        return await self._client._agent.memory.set_from_messages(
            messages,
            user_id=str(actor).strip().lower() or None,
            run_id=self.run_id or None,
            **kwargs,
        )

    def note_cited(self, keys: list[str] | tuple[str, ...]) -> None:
        self.used(*[str(k) for k in keys if k])

    async def __aenter__(self) -> RunScope:
        res = await self._client.begin(**self._begin_kwargs)
        self.run_id = str(res.get("run_id") or self.run_id)
        self._t0 = time.perf_counter()
        self._token = _ACTIVE_RUN.set(self)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._token is not None:
            _ACTIVE_RUN.reset(self._token)
            self._token = None
        status = self.status
        if exc_type is not None:
            status = "cancelled" if issubclass(exc_type, asyncio.CancelledError) else "failed"
        outcome = self.outcome
        if not outcome:
            if status == "failed":
                outcome = "negative"
            elif status == "completed" and self.used_keys:
                outcome = "positive"
        try:
            self.completed = await self._client.complete(
                self.run_id,
                status=status,
                final_response=self.final_response,
                confidence=self.confidence,
                memories_used=list(self.used_keys),
                outcome=outcome,
                duration_ms=int((time.perf_counter() - self._t0) * 1000),
                metadata=self.metadata or None,
            )
        except Exception:  # noqa: BLE001 — the ledger never masks the block's outcome
            self.completed = None
        return False
