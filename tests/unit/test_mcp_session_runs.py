"""One implicit run per MCP session.

The transport mints a run on a session's first run-scoped tool call, stamps
it into writes' `run_id`, labels reads through the ledger ContextVar (never
the filtering `run_id` field), closes it on DELETE /mcp or idle, and never
fails a tool call on a ledger error. Handlers are stubbed; no ClickHouse.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import run_context
from server.observability.ledger import run_id_of
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import mcp_runs

DEFAULTS = {"org": "clickhouse", "namespace": "ns-a", "agent": "cursor"}


def rid(session_id: str) -> str:
    """The implicit run id of `session_id` under DEFAULTS' org, for an unpinned caller."""
    return mcp_runs.run_id_for("clickhouse", "-", session_id)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_session_run_idle_seconds", 1800, raising=False)
    mcp_runs.reset()
    run_context.set_ledger_run("")
    yield
    mcp_runs.reset()
    run_context.set_ledger_run("")


class _Ledger:
    """Records begin/complete calls; mints predictable ids."""

    def __init__(self) -> None:
        self.begins: list[Any] = []
        self.completes: list[Any] = []
        self.fail_begin = False
        # run_id -> the namespace its row was begun in.
        self.active: dict[str, str] = {}
        # Runs a sibling web process called within the idle window.
        self.served_elsewhere: set[str] = set()

    async def active_run(self, org: str, run_id: str) -> dict[str, Any] | None:
        return {"namespace": self.active[run_id]} if run_id in self.active else None

    async def calls(self, org: str, run_id: str) -> Any:
        if run_id not in self.active:
            return None
        from server.core.datetimes import naive_utc_now

        last = naive_utc_now() if run_id in self.served_elsewhere else None
        return self.active[run_id], 7, last  # the run's namespace, the ledger's count

    async def begin(self, inp: Any) -> bool:
        if self.fail_begin:
            raise RuntimeError("ledger down")
        self.begins.append(inp)
        self.active[inp.run_id] = inp.namespace
        return True

    async def complete(self, inp: Any) -> Any:
        self.completes.append(inp)
        self.active.pop(inp.run_id, None)
        from server.tasks.api.models_run import RunCompleteOutput

        return RunCompleteOutput(run_id=inp.run_id, status=inp.status, duration_ms=inp.duration_ms)


@pytest.fixture
def ledger(monkeypatch) -> _Ledger:
    import server.tasks.app.run as h

    led = _Ledger()
    monkeypatch.setattr(h, "begin_session_run", led.begin)
    monkeypatch.setattr(h, "handle_run_complete", led.complete)
    monkeypatch.setattr(h, "session_run_calls", led.calls)
    monkeypatch.setattr(h, "active_session_run", led.active_run)
    return led


# ---- which ops take / stamp the session run ----


def test_a_viewer_op_that_writes_is_stamped_like_any_write() -> None:
    """The read/write split is `Operation.read_only`, the rule readOnlyHint
    reads: an idempotent VIEWER op that is audited or marked `writes`
    changes state, so its `run_id` is provenance, not a filter."""
    import dataclasses

    search = OPERATIONS_BY_NAME["memory.search"]
    assert not mcp_runs.stamps_run(search)
    assert mcp_runs.stamps_run(dataclasses.replace(search, audit=True))
    assert mcp_runs.stamps_run(dataclasses.replace(search, writes=True))


def test_writes_stamp_reads_label_ledger_and_code_never() -> None:
    ops = OPERATIONS_BY_NAME
    assert mcp_runs.stamps_run(ops["memory.set"])
    assert mcp_runs.stamps_run(ops["memory.update"])
    assert mcp_runs.stamps_run(ops["observe.record"])
    # Reads take the run (labelled) but are never stamped: run_id filters them.
    for name in ("memory.search", "memory.ask", "memory.get"):
        assert mcp_runs.takes_run(ops[name]), name
        assert not mcp_runs.stamps_run(ops[name]), name
    # The ledger's own verbs and the code plane are outside the session run.
    for name in ("run.begin", "run.complete", "run.get"):
        assert not mcp_runs.takes_run(ops[name]), name
    code = [o for n, o in ops.items() if n.startswith("code.")]
    assert code and not any(mcp_runs.takes_run(o) for o in code)


def test_goal_is_the_first_query_then_key_then_fallback() -> None:
    assert mcp_runs.goal_of({"query": "  what broke CI  "}, "x") == "what broke CI"
    assert mcp_runs.goal_of({"memory_key": "ci:flaky"}, "x") == "ci:flaky"
    assert mcp_runs.goal_of({"value": 1}, "mcp session (cursor)") == "mcp session (cursor)"
    assert len(mcp_runs.goal_of({"query": "q" * 5000}, "x")) == mcp_runs.GOAL_MAX_CHARS


# ---- ensure: one run per session, begun once ----


def test_ensure_mints_one_run_per_session_and_reuses_it(ledger: _Ledger) -> None:
    async def _run() -> None:
        r1 = await mcp_runs.ensure("sess-1", DEFAULTS, goal="what broke CI", now=100.0)
        r2 = await mcp_runs.ensure("sess-1", DEFAULTS, goal="second call", now=101.0)
        r3 = await mcp_runs.ensure("sess-2", DEFAULTS, goal="other session", now=102.0)
        assert r1 is not None and r2 is r1 and r1.run_id == rid("sess-1")
        assert r3 is not None and r3.run_id == rid("sess-2")
        assert r1.last_seen == 101.0
        await mcp_runs.drain_ledger_writes()  # run.begin lands off the call's path
        b = ledger.begins[0]
        assert b.run_id == rid("sess-1")
        assert (b.org, b.namespace, b.agent, b.session_id) == (
            "clickhouse",
            "ns-a",
            "cursor",
            "sess-1",
        )
        assert b.goal == "what broke CI" and b.environment == "mcp" and b.consumer == "cursor"
        assert b.metadata == {"implicit": True, "transport": "mcp"}
        assert mcp_runs.live_count() == 2

    asyncio.run(_run())


def test_a_sibling_worker_does_not_begin_the_sessions_run_again(ledger: _Ledger) -> None:
    """Another web process serving the same session finds the run active and
    leaves its goal alone; once the run is closed, a later call reopens it."""

    async def _run() -> None:
        await mcp_runs.ensure("sess-1", DEFAULTS, goal="first call", now=100.0)
        await mcp_runs.drain_ledger_writes()
        mcp_runs.reset()  # the next call lands on a sibling worker
        await mcp_runs.ensure("sess-1", DEFAULTS, goal="third call", now=101.0)
        await mcp_runs.drain_ledger_writes()
        assert [b.goal for b in ledger.begins] == ["first call"]
        await mcp_runs.close_session("sess-1")
        await mcp_runs.ensure("sess-1", DEFAULTS, goal="after close", now=103.0)
        await mcp_runs.drain_ledger_writes()
        assert [b.goal for b in ledger.begins] == ["first call", "after close"]

    asyncio.run(_run())


def test_ensure_needs_a_session_and_an_identity(ledger: _Ledger) -> None:
    async def _run() -> None:
        assert await mcp_runs.ensure(None, DEFAULTS, goal="g") is None
        assert await mcp_runs.ensure("s", None, goal="g") is None
        assert (
            await mcp_runs.ensure("s", {"org": "-", "namespace": "-", "agent": "-"}, goal="g")
            is None
        )
        assert ledger.begins == []

    asyncio.run(_run())


def test_begin_failure_is_swallowed_and_not_retried_every_call(ledger: _Ledger) -> None:
    async def _run() -> None:
        ledger.fail_begin = True
        # The call gets its run at once; the failed begin forgets it.
        assert await mcp_runs.ensure("s", DEFAULTS, goal="g", now=10.0) is not None
        await mcp_runs.drain_ledger_writes()
        assert mcp_runs.current("s") is None
        ledger.fail_begin = False
        # Within the idle window the failed session is not retried (no
        # run.begin per tool call while the ledger is down) …
        assert await mcp_runs.ensure("s", DEFAULTS, goal="g", now=20.0) is None
        assert ledger.begins == []
        # … and after it, it is.
        r = await mcp_runs.ensure("s", DEFAULTS, goal="g", now=20.0 + mcp_runs.idle_seconds())
        assert r is not None and r.run_id == rid("s")

    asyncio.run(_run())


def test_failed_begins_are_forgotten_while_no_run_is_live(ledger: _Ledger) -> None:
    """With the ledger down no run is ever live; the sweep still runs and
    forgets each failed session after the idle window."""

    async def _run() -> None:
        ledger.fail_begin = True
        for i in range(3):
            await mcp_runs.ensure(f"s{i}", DEFAULTS, goal="g", now=10.0)
        await mcp_runs.drain_ledger_writes()
        assert mcp_runs.live_count() == 0
        later = 10.0 + mcp_runs.idle_seconds() + 1.0
        assert mcp_runs.sweep_due(now=later)
        await mcp_runs.sweep_idle(now=later)
        assert mcp_runs._BEGIN_FAILED_AT == {}
        assert not mcp_runs.sweep_due(now=later + mcp_runs.SWEEP_MIN_INTERVAL_S)

    asyncio.run(_run())


# ---- close: DELETE /mcp, idle sweep, cap eviction ----


def test_close_session_completes_with_outcome_none(ledger: _Ledger) -> None:
    async def _run() -> None:
        await mcp_runs.ensure("s", DEFAULTS, goal="g", now=100.0)
        assert await mcp_runs.close_session("s") is True
        assert await mcp_runs.close_session("s") is False  # already gone
        assert await mcp_runs.close_session("never-seen") is False
        c = ledger.completes[0]
        assert (c.org, c.namespace, c.run_id) == ("clickhouse", "ns-a", rid("s"))
        assert c.status == "completed" and c.outcome == "none" and c.memories_used == []
        assert c.duration_ms == 0  # from the run's start, which every process shares
        assert c.metadata == {"closed_by": "mcp_session_end", "calls": 7}
        assert mcp_runs.live_count() == 0

    asyncio.run(_run())


def test_a_run_a_sibling_still_serves_or_closed_is_not_completed(ledger: _Ledger) -> None:
    async def _run() -> None:
        await mcp_runs.ensure("busy", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.ensure("done", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.drain_ledger_writes()
        ledger.served_elsewhere.add(rid("busy"))
        ledger.active.pop(rid("done"))  # a sibling's DELETE /mcp closed it
        assert await mcp_runs.sweep_idle(now=mcp_runs.idle_seconds() + 1.0, force=True) == 0
        # the closed run is forgotten; the busy one stays for the next sweep
        assert ledger.completes == [] and mcp_runs.current("busy") is not None
        assert mcp_runs.current("done") is None
        ledger.served_elsewhere.clear()  # the sibling went quiet too
        assert await mcp_runs.sweep_idle(now=mcp_runs.idle_seconds() + 61.0) == 1
        assert [c.run_id for c in ledger.completes] == [rid("busy")] and mcp_runs.live_count() == 0

    asyncio.run(_run())


def test_idle_sweep_closes_only_stale_runs_and_is_throttled(ledger: _Ledger) -> None:
    async def _run() -> None:
        await mcp_runs.ensure("old", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.ensure("fresh", DEFAULTS, goal="g", now=1000.0)
        idle = mcp_runs.idle_seconds()
        assert mcp_runs.sweep_due(now=idle + 1.0)
        closed = await mcp_runs.sweep_idle(now=idle + 1.0)
        assert closed == 1 and [c.run_id for c in ledger.completes] == [rid("old")]
        assert ledger.completes[0].metadata["closed_by"] == "mcp_idle"
        assert mcp_runs.live_count() == 1
        # Throttled: a second sweep inside the interval does nothing even
        # when forced=False, and sweep_due says so.
        assert not mcp_runs.sweep_due(now=idle + 2.0)
        assert await mcp_runs.sweep_idle(now=idle + 2.0) == 0
        # Forced past the fresh run's window it closes too.
        assert await mcp_runs.sweep_idle(now=1000.0 + idle + 1.0, force=True) == 1
        assert mcp_runs.live_count() == 0 and not mcp_runs.sweep_due()

    asyncio.run(_run())


def test_cap_evicts_the_idlest_run_by_closing_it(ledger: _Ledger, monkeypatch) -> None:
    monkeypatch.setattr(mcp_runs, "SESSION_RUN_CAP", 2)

    async def _run() -> None:
        await mcp_runs.ensure("a", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.ensure("b", DEFAULTS, goal="g", now=5.0)
        await mcp_runs.ensure("a", DEFAULTS, goal="g", now=10.0)  # a is now the fresher one
        await mcp_runs.ensure("c", DEFAULTS, goal="g", now=20.0)
        assert mcp_runs.live_count() == 2 and mcp_runs.current("b") is None
        await mcp_runs.drain_ledger_writes()  # the close runs off the call's path
        assert [c.run_id for c in ledger.completes] == [rid("b")]
        assert ledger.completes[0].metadata["closed_by"] == "mcp_evicted"

    asyncio.run(_run())


# ---- the ledger ContextVar: label without filtering ----


def test_ledger_run_labels_agent_events_and_exposures_but_not_the_hint() -> None:
    from server.memory.api.models_common import run_next_hint
    from server.memory.app import exposure_ledger

    assert run_id_of({"query": "q"}) == ""
    token = run_context.set_ledger_run("run-9")
    try:
        # A read with no run of its own is labelled …
        assert run_id_of({"query": "q"}) == "run-9"
        assert run_id_of({"run_id": "-"}) == "run-9"
        # … an explicit run still wins …
        assert run_id_of({"run_id": "explicit"}) == "explicit"
        assert run_context.ledger_run_for("-") == "run-9"
        assert run_context.ledger_run_for("explicit") == "explicit"
        # … and the run nudge is not produced by the implicit run: the read's
        # own run_id is what the handlers hand run_next_hint.
        assert run_next_hint(None) == "" and run_next_hint("-") == ""
        row = exposure_ledger.build_row(
            operation="memory.search",
            org="o",
            namespace="n",
            keys=["k"],
            run_id=run_context.ledger_run_for(None),
        )
        assert row["run_id"] == "run-9"
    finally:
        run_context.reset_ledger_run(token)
    assert run_id_of({"query": "q"}) == ""
    assert (
        run_context.clean_run_id(" x" * 200).startswith("x")
        and len(run_context.clean_run_id("y" * 500)) == run_context.RUN_ID_MAX_CHARS
    )


# ---- _call_tool wiring (source-level, like the identity-order guard) ----


def test_call_tool_stamps_writes_only_and_binds_the_ledger_run() -> None:
    import inspect

    import server.transport.mcp_call as mcp_call

    call = inspect.getsource(mcp_call._call_tool)
    stamp_at = call.index("await _bind_session_run(op, arguments, defaults, mcp_session_id)")
    handler_at = call.index("result = await op.handler(input_obj)")
    assert stamp_at < handler_at, "the session run must be bound before the handler runs"
    src = inspect.getsource(mcp_call._bind_session_run)
    assert 'arguments["run_id"] = session_run_id' in src
    assert "set_ledger_run(session_run_id)" in src
    # Reads are never stamped: the only assignment is guarded by stamps_run.
    guard = src[src.index("if _mcp_runs.stamps_run(op)") :].split("\n", 1)[0]
    assert "stamps_run(op)" in guard and 'arguments.get("run_id")' in guard


def test_delete_route_and_sweep_are_wired() -> None:
    import inspect

    import server.transport.mcp as mcp

    src = inspect.getsource(mcp.attach)
    assert '@router.delete("/mcp"' in src
    assert "_mcp_runs.close_session(" in src and 'ledger_identity({})["user_id"]' in src
    assert "_mcp_runs.sweep_due()" in src and "_mcp_runs.sweep_idle()" in src


def test_loom_search_labels_the_read_and_emits_the_run() -> None:
    """`search.query` (the loom_search tool) has no `run_id` field, yet the
    session run labels it: it is a read, so the run is never stamped."""
    import inspect

    op = OPERATIONS_BY_NAME["search.query"]
    assert mcp_runs.takes_run(op) and not mcp_runs.stamps_run(op)
    assert "run_id" not in op.input_model.model_fields
    # The search input itself never carries the session run (it would filter).
    from server.memory.app.loom_search import fanout

    fan = inspect.getsource(fanout.search_sections)
    mem_input = fan[fan.index("mem_input = MemorySearchInput(") : fan.index("know_input = ")]
    assert "run_id" not in mem_input
    assert "run_id" not in fan[: fan.index("mem_input = ")].split(")", 1)[0]


def test_working_memory_is_labelled_but_never_stamped() -> None:
    """A working-memory block's run_id is its scope. Stamped
    with the MCP session's run, `set`/`append` would land at `mcp-<session>`,
    where `replace` addressed without it answers 404 and no other connection
    or primer reads the note."""
    ops = OPERATIONS_BY_NAME
    wm = [n for n in ops if n.startswith("working_memory.")]
    assert {"working_memory.set", "working_memory.append", "working_memory.replace"} <= set(wm)
    for name in wm:
        if mcp_runs.takes_run(ops[name]):
            assert not mcp_runs.stamps_run(ops[name]), name
    assert mcp_runs.takes_run(ops["working_memory.set"])  # still labelled on the ledger


def test_a_delete_reaching_a_sibling_closes_the_run_by_id(ledger: _Ledger) -> None:
    async def _run() -> None:
        await mcp_runs.ensure("s", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.drain_ledger_writes()
        mcp_runs.reset()  # the DELETE lands on a web process that never served it
        assert await mcp_runs.close_session("s") is False  # no org, no lookup
        assert await mcp_runs.close_session("s", org="clickhouse") is True
        assert [(c.run_id, c.namespace) for c in ledger.completes] == [(rid("s"), "ns-a")]

    asyncio.run(_run())


def test_a_cached_run_is_checked_with_the_ledger_once_a_minute(ledger: _Ledger) -> None:
    """A sibling web process may close the run (its sweep, a DELETE, a cap
    eviction): the next check re-opens it, and joins it while it is active."""

    async def _run() -> None:
        first = await mcp_runs.ensure("s", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.drain_ledger_writes()
        ledger.active.pop(rid("s"))  # a sibling closed it
        await mcp_runs.ensure("s", DEFAULTS, goal="g2", now=10.0)  # within the minute
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 1
        again = await mcp_runs.ensure("s", DEFAULTS, goal="g3", now=61.0)
        await mcp_runs.drain_ledger_writes()
        assert again is first and len(ledger.begins) == 2 and rid("s") in ledger.active
        await mcp_runs.ensure("s", DEFAULTS, goal="g4", now=130.0)
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 2  # still active: joined

    asyncio.run(_run())


def test_an_eviction_closes_a_run_a_sibling_then_reopens(ledger: _Ledger, monkeypatch) -> None:
    """The cap bounds this process's memory; a sibling still serving the session
    re-opens the run on its next check."""
    monkeypatch.setattr(mcp_runs, "SESSION_RUN_CAP", 1)

    async def _run() -> None:
        await mcp_runs.ensure("a", DEFAULTS, goal="g", now=0.0)
        await mcp_runs.drain_ledger_writes()
        sibling = dict(mcp_runs._RUNS)  # the same session, warm on another process
        await mcp_runs.ensure("b", DEFAULTS, goal="g", now=1.0)
        await mcp_runs.drain_ledger_writes()
        assert [c.run_id for c in ledger.completes] == [rid("a")]
        mcp_runs._RUNS.clear()
        mcp_runs._RUNS.update(sibling)
        await mcp_runs.ensure("a", DEFAULTS, goal="g", now=70.0)
        await mcp_runs.drain_ledger_writes()
        assert rid("a") in ledger.active and len(ledger.begins) == 3

    asyncio.run(_run())


def test_a_call_naming_an_org_binds_the_run_there(monkeypatch) -> None:
    """The ledger labels the call's rows with the org it named, so the run is
    looked up there too."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    seen: list[dict[str, str]] = []

    async def _ensure(session_id: Any, defaults: Any, *, goal: str) -> None:
        seen.append(defaults)
        return None

    monkeypatch.setattr(mcp_call._mcp_runs, "ensure", _ensure)
    arguments = {"org": "other", "namespace": "ns-b", "memory_key": "k", "value": "v"}
    asyncio.run(
        mcp_call._bind_session_run(OPERATIONS_BY_NAME["memory.set"], arguments, DEFAULTS, "s")
    )
    assert (seen[0]["org"], seen[0]["namespace"]) == ("other", "ns-b")
