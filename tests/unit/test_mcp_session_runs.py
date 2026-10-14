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
from server.tasks.domain.session_runs import session_run_id
from server.transport import mcp_runs

DEFAULTS = {"org": "clickhouse", "namespace": "ns-a", "agent": "cursor"}


CALLER = mcp_runs.Caller()


def rid(session_id: str) -> str:
    """The implicit run id of `session_id` under DEFAULTS' org, for an unpinned caller."""
    return session_run_id("clickhouse", CALLER.subject, session_id)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_session_run_idle_seconds", 1800)
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

    async def active_runs(self, org: str, run_id: str) -> list[dict[str, Any]]:
        if run_id not in self.active:
            return []
        return [{"org": org, "namespace": self.active[run_id], "run_id": run_id}]

    async def begin(self, inp: Any, *, started_at: Any) -> None:
        if self.fail_begin:
            raise RuntimeError("ledger down")
        self.begins.append(inp)
        self.active[inp.run_id] = inp.namespace

    async def fetch(self, ch: Any, org: str, ns: str, run_id: str) -> dict[str, Any] | None:
        return {"status": "active", "metadata": "{}"} if run_id in self.active else None

    async def complete(self, ch: Any, inp: Any, existing: Any, **_: Any) -> None:
        self.completes.append(inp)
        self.active.pop(inp.run_id, None)


@pytest.fixture
def ledger(monkeypatch) -> _Ledger:
    import server.tasks.app.run as h

    led = _Ledger()
    monkeypatch.setattr(h, "begin_session_run", led.begin)
    monkeypatch.setattr(h, "active_session_runs", led.active_runs)
    monkeypatch.setattr(h, "_complete", led.complete)
    monkeypatch.setattr(h.runs_store, "fetch_run", led.fetch)
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


# ---- ensure: one run per session, begun once ----


def test_ensure_mints_one_run_per_session_and_reuses_it(ledger: _Ledger) -> None:
    async def _run() -> None:
        r1 = await mcp_runs.ensure("sess-1", DEFAULTS, CALLER, now=100.0)
        r2 = await mcp_runs.ensure("sess-1", DEFAULTS, CALLER, now=101.0)
        r3 = await mcp_runs.ensure("sess-2", DEFAULTS, CALLER, now=102.0)
        assert r1 is not None and r2 is r1 and r1.run_id == rid("sess-1")
        assert r3 is not None and r3.run_id == rid("sess-2")
        assert list(mcp_runs._RUNS) == [rid("sess-1"), rid("sess-2")]  # least recent first
        await mcp_runs.drain_ledger_writes()  # run.begin lands off the call's path
        b = ledger.begins[0]
        assert b.run_id == rid("sess-1")
        assert (b.org, b.namespace, b.agent, b.session_id) == (
            "clickhouse",
            "ns-a",
            "cursor",
            "sess-1",
        )
        # A label: a read's query may reach any namespace the caller reads.
        assert b.goal == "mcp session (cursor)"
        assert b.environment == "mcp" and b.consumer == "cursor"
        assert b.metadata == {"implicit": True, "transport": "mcp", "subject": ""}
        assert len(mcp_runs._RUNS) == 2

    asyncio.run(_run())


def test_a_sibling_worker_does_not_begin_the_sessions_run_again(ledger: _Ledger) -> None:
    """Another web process serving the same session finds the run active and
    joins it; once the run is closed, a later call reopens it."""

    async def _run() -> None:
        await mcp_runs.ensure("sess-1", DEFAULTS, CALLER, now=100.0)
        await mcp_runs.drain_ledger_writes()
        mcp_runs.reset()  # the next call lands on a sibling worker
        await mcp_runs.ensure("sess-1", DEFAULTS, CALLER, now=101.0)
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 1
        await mcp_runs.close_session("sess-1", "clickhouse", "-")
        await mcp_runs.ensure("sess-1", DEFAULTS, CALLER, now=103.0)
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 2

    asyncio.run(_run())


def test_ensure_needs_a_session_and_an_identity(ledger: _Ledger) -> None:
    async def _run() -> None:
        assert await mcp_runs.ensure(None, DEFAULTS, CALLER) is None
        assert await mcp_runs.ensure("s", None, CALLER) is None
        assert (
            await mcp_runs.ensure("s", {"org": "-", "namespace": "-", "agent": "-"}, CALLER) is None
        )
        assert ledger.begins == []

    asyncio.run(_run())


def test_begin_failure_is_swallowed_and_not_retried_every_call(ledger: _Ledger) -> None:
    async def _run() -> None:
        ledger.fail_begin = True
        # The call gets its run at once; the failed begin forgets it.
        assert await mcp_runs.ensure("s", DEFAULTS, CALLER, now=10.0) is not None
        await mcp_runs.drain_ledger_writes()
        assert mcp_runs.current(rid("s")) is None
        ledger.fail_begin = False
        # Within a check interval the failed session is not retried (no
        # run.begin per tool call while the ledger is down) …
        assert await mcp_runs.ensure("s", DEFAULTS, CALLER, now=20.0) is None
        assert ledger.begins == []
        # … and after it, it is.
        r = await mcp_runs.ensure("s", DEFAULTS, CALLER, now=10.0 + mcp_runs.CHECK_INTERVAL_S)
        assert r is not None and r.run_id == rid("s")

    asyncio.run(_run())


def test_failed_begins_are_capped_like_the_runs(ledger: _Ledger, monkeypatch) -> None:
    monkeypatch.setattr(mcp_runs, "SESSION_RUN_CAP", 2)

    async def _run() -> None:
        ledger.fail_begin = True
        for session in ("a", "b", "c"):
            await mcp_runs.ensure(session, DEFAULTS, CALLER, now=10.0)
        await mcp_runs.drain_ledger_writes()
        assert list(mcp_runs._BEGIN_FAILED_AT) == [rid("b"), rid("c")]

    asyncio.run(_run())


# ---- close: DELETE /mcp; the cache cap ----


def test_close_session_completes_with_outcome_none(ledger: _Ledger) -> None:
    async def _run() -> None:
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=100.0)
        assert await mcp_runs.close_session("s", "clickhouse", "-") is True
        assert await mcp_runs.close_session("s", "clickhouse", "-") is False  # already gone
        assert await mcp_runs.close_session("never-seen", "clickhouse", "-") is False
        c = ledger.completes[0]
        assert (c.org, c.namespace, c.run_id) == ("clickhouse", "ns-a", rid("s"))
        assert c.status == "completed" and c.outcome == "none" and c.memories_used == []
        assert c.duration_ms == 0  # from the run's start, which every process shares
        assert c.metadata == {"closed_by": "mcp_session_end"}  # the ledger holds the calls
        assert len(mcp_runs._RUNS) == 0

    asyncio.run(_run())


def test_the_cap_forgets_the_idlest_run_without_closing_it(ledger: _Ledger, monkeypatch) -> None:
    """A cap, not a sign the session ended: the evicted session's next call joins
    its run, which only the idle sweep or a DELETE closes."""
    monkeypatch.setattr(mcp_runs, "SESSION_RUN_CAP", 2)

    async def _run() -> None:
        await mcp_runs.ensure("a", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.ensure("b", DEFAULTS, CALLER, now=5.0)
        await mcp_runs.ensure("a", DEFAULTS, CALLER, now=10.0)  # a is now the fresher one
        await mcp_runs.ensure("c", DEFAULTS, CALLER, now=20.0)
        assert len(mcp_runs._RUNS) == 2 and mcp_runs.current(rid("b")) is None
        await mcp_runs.ensure("b", DEFAULTS, CALLER, now=30.0)
        await mcp_runs.drain_ledger_writes()
        assert ledger.completes == [] and len(ledger.begins) == 3  # b joined, not begun again

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
    stamp_at = call.index("_stamp_session_run(op, arguments, defaults, mcp_session_id)")
    validate_at = call.index("_validated_input(")
    admitted_at = call.index("refused = await _admission_refusal(call)")
    begin_at = call.index("await _bind_session_run(op, defaults, mcp_session_id, run_caller)")
    handler_at = call.index("result = await op.handler(input_obj)")
    # Stamped before validation; begun only once the call is admitted.
    assert stamp_at < validate_at < admitted_at < begin_at < handler_at
    src = inspect.getsource(mcp_call._stamp_session_run)
    assert 'arguments["run_id"] = run_id' in src
    assert "set_ledger_run(session_run_id)" in inspect.getsource(mcp_call._bind_session_run)
    # Reads are never stamped: the only assignment is guarded by stamps_run.
    guard = src[src.index("if run_id and _mcp_runs.stamps_run(op)") :].split("\n", 1)[0]
    assert "stamps_run(op)" in guard and 'arguments.get("run_id")' in guard


def test_a_refused_call_begins_no_run(ledger: _Ledger, monkeypatch) -> None:
    """A call the gates refuse (here: a rate limit) carries the stamped id but
    writes no run row, so rotating Mcp-Session-Id cannot fill the ledger."""
    from server.transport import mcp_call

    stamped: list[str] = []

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return DEFAULTS

    async def _refuse(call: Any) -> dict[str, Any]:
        stamped.append(call.arguments.get("run_id"))
        return {"jsonrpc": "2.0", "id": call.req_id, "result": {"isError": True}}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    monkeypatch.setattr(mcp_call, "_admission_refusal", _refuse)
    params = {"name": "loom_memory_set", "arguments": {"memory_key": "k", "value": "v"}}

    async def _run() -> None:
        await mcp_call._call_tool(1, params, mcp_session_id="s")
        await mcp_runs.drain_ledger_writes()

    asyncio.run(_run())
    assert stamped == [rid("s")]
    assert ledger.begins == [] and mcp_runs.current(rid("s")) is None


def test_delete_route_is_wired() -> None:
    import inspect

    import server.transport.mcp as mcp

    src = inspect.getsource(mcp.attach)
    assert '@router.delete("/mcp"' in src
    assert "_mcp_runs.close_session(" in src and "_mcp_runs.subject_of(auth)" in src
    assert src.index("set_current_auth(auth)") < src.index("_mcp_runs.close_session(")


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
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()
        mcp_runs.reset()  # the DELETE lands on a web process that never served it
        assert await mcp_runs.close_session("s", "", "-") is False  # no org, no lookup
        assert await mcp_runs.close_session("s", "clickhouse", "-") is True
        assert [(c.run_id, c.namespace) for c in ledger.completes] == [(rid("s"), "ns-a")]

    asyncio.run(_run())


def test_a_cached_run_is_checked_with_the_ledger_once_a_minute(ledger: _Ledger) -> None:
    """The idle sweep or a DELETE on another web process may close the run: the
    next check re-opens it, and joins it while it is active."""

    async def _run() -> None:
        first = await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()
        ledger.active.pop(rid("s"))  # a sibling closed it
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=10.0)  # within the minute
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 1
        again = await mcp_runs.ensure("s", DEFAULTS, CALLER, now=61.0)
        await mcp_runs.drain_ledger_writes()
        assert again is first and len(ledger.begins) == 2 and rid("s") in ledger.active
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=130.0)
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 2  # still active: joined

    asyncio.run(_run())


def test_a_session_run_lives_in_the_callers_org() -> None:
    """A DELETE knows only the caller, never a call's arguments, so the run sits
    in the caller's org and default namespace whatever a call names."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    arguments = {"org": "other", "namespace": "ns-b", "memory_key": "k", "value": "v"}
    mcp_call._stamp_session_run(OPERATIONS_BY_NAME["memory.set"], arguments, DEFAULTS, "s")
    assert arguments["run_id"] == rid("s")  # the caller's org, not the call's


def test_two_callers_sending_one_session_id_hold_two_runs(ledger: _Ledger) -> None:
    """The id binds the caller's principal: one's writes never carry the other's
    run, and one's DELETE never closes the other's."""

    async def _run() -> None:
        alice, bob = mcp_runs.Caller(subject="alice"), mcp_runs.Caller(subject="bob")
        a = await mcp_runs.ensure("shared", DEFAULTS, alice, now=0.0)
        b = await mcp_runs.ensure("shared", {**DEFAULTS, "namespace": "ns-b"}, bob, now=1.0)
        await mcp_runs.drain_ledger_writes()
        assert a is not None and b is not None and a.run_id != b.run_id
        assert [x.namespace for x in ledger.begins] == ["ns-a", "ns-b"]
        assert await mcp_runs.close_session("shared", "clickhouse", "bob") is True
        assert [c.run_id for c in ledger.completes] == [b.run_id]
        assert mcp_runs.current(a.run_id) is a

    asyncio.run(_run())


def test_a_failed_check_keeps_the_run_and_tries_again(ledger: _Ledger, monkeypatch) -> None:
    """Only a failed first begin waits a check interval; a check that cannot
    reach the ledger leaves the session's run in place."""
    import server.tasks.app.run as h

    async def _run() -> None:
        run = await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()

        async def _down(org: str, run_id: str) -> Any:
            raise RuntimeError("ledger down")

        monkeypatch.setattr(h, "active_session_runs", _down)
        assert await mcp_runs.ensure("s", DEFAULTS, CALLER, now=61.0) is run
        await mcp_runs.drain_ledger_writes()
        assert await mcp_runs.ensure("s", DEFAULTS, CALLER, now=62.0) is run
        monkeypatch.setattr(h, "active_session_runs", ledger.active_runs)
        assert await mcp_runs.ensure("s", DEFAULTS, CALLER, now=125.0) is run
        await mcp_runs.drain_ledger_writes()
        assert len(ledger.begins) == 1  # still active: joined, never begun twice

    asyncio.run(_run())


def test_a_delete_after_this_processs_begin_failed_still_asks_the_ledger(
    ledger: _Ledger,
) -> None:
    """A sibling began the run for the session's later calls; the client's DELETE
    lands on the process whose own begin failed."""

    async def _run() -> None:
        ledger.fail_begin = True
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()
        ledger.fail_begin = False
        ledger.active[rid("s")] = "ns-a"  # the sibling's begin
        assert await mcp_runs.close_session("s", "clickhouse", "-") is True
        assert [c.run_id for c in ledger.completes] == [rid("s")]

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("claims", "arguments", "expected"),
    [
        ({}, {"user_id": "u"}, ("sa:t", "-", "-")),  # namespace-wide: no owner
        ({"read_user_id": "u"}, {}, ("sa:t", "u", "-")),  # a pinned user
        (
            {"read_scope": "own_session", "read_user_id": "u"},
            {"session_id": "app-1"},
            ("sa:t", "u", "app-1"),  # the session the call names, not the MCP one
        ),
        ({"read_scope": "own_session", "read_user_id": "u"}, {}, None),  # refused at the gate
    ],
)
def test_the_caller_is_the_one_the_calls_read_scope_resolves(
    claims: dict[str, str], arguments: dict[str, str], expected: Any
) -> None:
    from server.auth.jwt import AuthInfo

    auth = AuthInfo(
        subject="sa:t",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        **claims,
    )
    caller = mcp_runs.Caller.of_call(auth, arguments)
    got = None if caller is None else (caller.subject, caller.user_id, caller.session_id)
    assert got == expected


@pytest.mark.parametrize("warm", [True, False])
def test_a_delete_closes_a_run_whose_calls_named_another_org(
    ledger: _Ledger, monkeypatch, warm: bool
) -> None:
    """The run sits in the caller's org, which the DELETE names too, whatever org
    the calls named; the ledger counts those calls by run id."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    async def _run() -> None:
        op = OPERATIONS_BY_NAME["memory.set"]
        arguments = {"org": "other", "namespace": "ns-b", "memory_key": "k", "value": "v"}
        caller, _ = mcp_call._stamp_session_run(op, arguments, DEFAULTS, "s")
        await mcp_call._bind_session_run(op, DEFAULTS, "s", caller)
        await mcp_runs.drain_ledger_writes()
        assert arguments["run_id"] == rid("s")
        if not warm:
            mcp_runs.reset()  # the DELETE lands on a process that never served it
        assert await mcp_runs.close_session("s", "clickhouse", "-") is True
        assert [(c.org, c.run_id) for c in ledger.completes] == [("clickhouse", rid("s"))]

    asyncio.run(_run())


def test_a_delete_the_ledger_did_not_answer_leaves_the_run_open(
    ledger: _Ledger, monkeypatch
) -> None:
    """The idle sweep closes it later, from the ledger."""
    import server.tasks.app.run as h

    async def _run() -> None:
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()

        async def _down(org: str, run_id: str) -> Any:
            raise ConnectionError("ledger down")

        monkeypatch.setattr(h, "active_session_runs", _down)
        assert await mcp_runs.close_session("s", "clickhouse", "-") is False
        assert ledger.completes == [] and rid("s") in ledger.active
        assert mcp_runs.current(rid("s")) is None

    asyncio.run(_run())


def test_the_run_names_the_subject_the_idle_sweep_counts(ledger: _Ledger) -> None:
    async def _run() -> None:
        await mcp_runs.ensure("s", DEFAULTS, mcp_runs.Caller(subject="sa:t"), now=0.0)
        await mcp_runs.ensure("t", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()
        assert [b.metadata["subject"] for b in ledger.begins] == ["sa:t", ""]

    asyncio.run(_run())


def test_the_detached_begin_counts_against_the_callers_quota(ledger: _Ledger, monkeypatch) -> None:
    import server.tasks.app.run as h
    from server.core.ch import current_query_tag, quota_key_var, set_quota_key

    seen: list[tuple[str, str]] = []

    async def begin(inp: Any, *, started_at: Any) -> None:
        seen.append((quota_key_var.get(), current_query_tag()))

    monkeypatch.setattr(h, "begin_session_run", begin)

    async def _run() -> None:
        set_quota_key("tenant-a")
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()

    asyncio.run(_run())
    assert seen == [("tenant-a", "mcp.session_run")]


def test_a_delete_closes_against_the_callers_quota(ledger: _Ledger, monkeypatch) -> None:
    import server.tasks.app.run as h
    from server.core.ch import quota_key_var, set_quota_key

    seen: list[str] = []
    complete = ledger.complete

    async def counted(ch: Any, inp: Any, existing: Any, **_: Any) -> None:
        seen.append(quota_key_var.get())
        await complete(ch, inp, existing)

    monkeypatch.setattr(h, "_complete", counted)

    async def _run() -> None:
        await mcp_runs.ensure("s", DEFAULTS, CALLER, now=0.0)
        await mcp_runs.drain_ledger_writes()
        set_quota_key("tenant-a")  # as the DELETE route sets it from the token
        assert await mcp_runs.close_session("s", "clickhouse", "-") is True

    asyncio.run(_run())
    assert seen == ["tenant-a"]


def test_the_idle_window_is_no_shorter_than_a_processs_check() -> None:
    """A run closed sooner than a web process re-checks it would still be stamped."""
    from pydantic import ValidationError

    from server.settings import Settings

    assert Settings(mcp_session_run_idle_seconds=60).mcp_session_run_idle_seconds == 60
    with pytest.raises(ValidationError):
        Settings(mcp_session_run_idle_seconds=59)
    assert mcp_runs.CHECK_INTERVAL_S == 60
