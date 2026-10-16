"""An MCP session's implicit run, as the ledger decides it, on live ClickHouse:
`active_session_runs` finds the run a sibling web process began, and the idle
sweep keeps open only a run its own token called lately, in any namespace.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now, to_naive_utc
from server.tasks.app import run as run_handlers
from server.tasks.domain.session_runs import session_run_id

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

ORG, AGENT = "itest-org", "itest-mcp"


def _at(ns: int) -> datetime:
    """A row's write time as its start, for a run begun that long ago."""
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _sweep_only(monkeypatch: pytest.MonkeyPatch, org: str) -> None:
    """The live tests share a database: the sweep closes this test's org only."""
    every_org = run_handlers.runs_store.idle_session_runs

    async def idle_in_org(ch: Any, *, idle_seconds: int, limit: int) -> list[Any]:
        rows = await every_org(ch, idle_seconds=idle_seconds, limit=1_000_000)
        return [r for r in rows if r["org"] == org][:limit]

    monkeypatch.setattr(run_handlers.runs_store, "idle_session_runs", idle_in_org)


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


async def _ledger_call(
    ch: Any, namespace: str, run_id: str, subject: str = "", org: str = ORG
) -> None:
    await ch.insert(
        "loom.agent_events",
        [
            {
                "org": org,
                "namespace": namespace,
                "agent": AGENT,
                "user_id": "-",
                "session_id": "s",
                "run_id": run_id,
                "subject": subject,
                "operation": "memory.search",
                "request_id": uuid.uuid4().hex,
                "latency_ms": 1,
                "success": True,
                "ts": datetime.now(UTC),  # the web process's clock, as the ledger writes it
            }
        ],
        settings={"async_insert": 0},
    )


@pytest.mark.asyncio
async def test_the_idle_sweep_closes_a_run_its_own_calls_did_not_keep_busy(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More busy runs than a pass closes do not hide a quiet one behind them, a
    run an earlier server opened under `mcp-<session id>` closes too, and a run
    begun by hand under the prefix stays open whatever its metadata says."""
    from server.tasks.app.run import active_session_runs, close_idle_session_runs
    from server.tasks.store.runs import fetch_run

    org = f"itest-sweep-{uuid.uuid4().hex[:8]}"
    namespace, elsewhere = (f"itest-{uuid.uuid4().hex[:12]}" for _ in range(2))
    busy = [session_run_id(org, "sa:a", uuid.uuid4().hex) for _ in range(3)]
    quiet, borrowed = (session_run_id(org, "sa:a", uuid.uuid4().hex) for _ in range(2))
    explicit = f"mcp-{uuid.uuid4().hex[:12]}"  # the prefix, but begun by hand
    handmade = f"mcp-nightly-{uuid.uuid4().hex[:8]}"  # by hand, and claims to be implicit
    legacy = f"mcp-{uuid.uuid4().hex}"
    metadata = {
        legacy: '{"implicit":true,"transport":"mcp"}',  # no subject recorded
        explicit: '{"implicit":false,"subject":"sa:a"}',
        handmade: '{"implicit":true}',
    }
    long_ago = time.time_ns() - 7200 * 10**9
    _sweep_only(monkeypatch, org)
    try:
        await ch.insert(
            "loom.agent_runs",
            [
                {
                    "org": org,
                    "namespace": namespace,
                    "agent": AGENT,
                    "run_id": run_id,
                    "goal": "g",
                    "environment": "mcp",
                    "metadata": metadata.get(run_id, '{"implicit":true,"subject":"sa:a"}'),
                    "version": long_ago,
                    "started_at": _at(long_ago),
                }
                for run_id in [*busy, quiet, borrowed, explicit, handmade, legacy]
            ],
            settings={"async_insert": 0},
        )
        for run_id in busy:  # calls in another namespace
            await _ledger_call(ch, elsewhere, run_id, subject="sa:a", org=org)
        await _ledger_call(ch, namespace, borrowed, subject="sa:eve", org=org)  # another token
        monkeypatch.setattr(run_handlers, "IDLE_CLOSE_BATCH", 2)
        assert await close_idle_session_runs(1800) == 2
        assert await close_idle_session_runs(1800) == 1
        assert all([await active_session_runs(org, run_id) for run_id in busy])
        assert await active_session_runs(org, quiet) == []
        assert await active_session_runs(org, borrowed) == []
        for run_id, status in ((legacy, "completed"), (explicit, "active"), (handmade, "active")):
            row = await fetch_run(ch, org, namespace, run_id)
            assert row is not None and row["status"] == status, run_id
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}}", params={"org": org}
            )


@pytest.mark.asyncio
async def test_the_idle_sweep_reads_any_number_of_active_runs(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidates stay inside ClickHouse: thousands of active runs are not
    one oversized parameter."""
    from server.tasks.app.run import close_idle_session_runs

    org = f"itest-many-{uuid.uuid4().hex[:8]}"
    long_ago = time.time_ns() - 7200 * 10**9
    ids = [session_run_id(org, "sa:a", str(i)) for i in range(3500)]
    _sweep_only(monkeypatch, org)
    try:
        await ch.insert(
            "loom.agent_runs",
            [
                {
                    "org": org,
                    "namespace": "ns",
                    "agent": AGENT,
                    "run_id": run_id,
                    "goal": "g",
                    "environment": "mcp",
                    "metadata": '{"implicit":true,"subject":"sa:a"}',
                    "version": long_ago,
                    "started_at": _at(long_ago),
                }
                for run_id in ids
            ],
            settings={"async_insert": 0},
        )
        monkeypatch.setattr(run_handlers, "IDLE_CLOSE_BATCH", 5)
        assert await close_idle_session_runs(60) == 5
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String}", params={"org": org}
        )


@pytest.mark.asyncio
async def test_the_idle_sweep_closes_a_run_however_long_ago_it_was_written(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run an earlier server, or a month without workers, left open."""
    from server.tasks.app.run import active_session_runs, close_idle_session_runs

    org = f"itest-old-{uuid.uuid4().hex[:8]}"
    run_id = session_run_id(org, "sa:a", uuid.uuid4().hex)
    long_ago = time.time_ns() - 31 * 86400 * 10**9
    _sweep_only(monkeypatch, org)
    try:
        await ch.insert(
            "loom.agent_runs",
            [
                {
                    "org": org,
                    "namespace": "ns",
                    "agent": AGENT,
                    "run_id": run_id,
                    "goal": "g",
                    "environment": "mcp",
                    "metadata": '{"implicit":true,"subject":"sa:a"}',
                    "version": long_ago,
                    "started_at": _at(long_ago),
                }
            ],
            settings={"async_insert": 0},
        )
        assert await close_idle_session_runs(1800) == 1
        assert await active_session_runs(org, run_id) == []
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String}", params={"org": org}
        )


@pytest.mark.asyncio
async def test_a_run_that_records_no_subject_stays_open_while_it_is_called(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier server's run: its calls carry a subject it never recorded."""
    from server.tasks.app.run import close_idle_session_runs
    from server.tasks.store.runs import fetch_run

    org = f"itest-nosub-{uuid.uuid4().hex[:8]}"
    busy, quiet = f"mcp-{uuid.uuid4().hex}", f"mcp-{uuid.uuid4().hex}"
    long_ago = time.time_ns() - 7200 * 10**9
    _sweep_only(monkeypatch, org)
    try:
        await ch.insert(
            "loom.agent_runs",
            [
                {
                    "org": org,
                    "namespace": "ns",
                    "agent": AGENT,
                    "run_id": run_id,
                    "goal": "g",
                    "environment": "mcp",
                    "metadata": '{"implicit":true,"transport":"mcp"}',
                    "version": long_ago,
                    "started_at": _at(long_ago),
                }
                for run_id in (busy, quiet)
            ],
            settings={"async_insert": 0},
        )
        await _ledger_call(ch, "ns", busy, subject="sa:still-working", org=org)
        assert await close_idle_session_runs(1800) == 1
        assert (await fetch_run(ch, org, "ns", busy) or {}).get("status") == "active"
        assert (await fetch_run(ch, org, "ns", quiet) or {}).get("status") == "completed"
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}}", params={"org": org}
            )


@pytest.mark.asyncio
async def test_a_run_reopened_or_called_within_the_window_is_busy(ch: Any) -> None:
    """The sweep's re-check before a close: a run re-opened or called since the
    pick stays open."""
    from server.tasks.store.runs import busy_lately

    org = f"itest-busy-{uuid.uuid4().hex[:8]}"
    run_id = session_run_id(org, "sa:a", uuid.uuid4().hex)
    row = {
        "org": org,
        "namespace": "ns",
        "agent": AGENT,
        "run_id": run_id,
        "goal": "g",
        "environment": "mcp",
        "metadata": '{"implicit":true,"subject":"sa:a"}',
    }
    long_ago = time.time_ns() - 7200 * 10**9
    try:
        await ch.insert(
            "loom.agent_runs",
            [{**row, "version": long_ago, "started_at": _at(long_ago)}],
            settings={"async_insert": 0},
        )
        assert not await busy_lately(ch, org, "ns", run_id, "sa:a", 60)
        await _ledger_call(ch, "elsewhere", run_id, subject="sa:eve", org=org)
        assert not await busy_lately(ch, org, "ns", run_id, "sa:a", 60)  # another token
        await _ledger_call(ch, "elsewhere", run_id, subject="sa:a", org=org)
        assert await busy_lately(ch, org, "ns", run_id, "sa:a", 60)
        await ch.execute(
            "DELETE FROM loom.agent_events WHERE org = {org:String}", params={"org": org}
        )
        # Re-opened now: a new version that keeps the old start.
        reopened = {**row, "started_at": _at(long_ago)}
        await ch.insert("loom.agent_runs", [reopened], settings={"async_insert": 0})
        assert await busy_lately(ch, org, "ns", run_id, "sa:a", 60)
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}}", params={"org": org}
            )


@pytest.mark.asyncio
async def test_closes_racing_for_one_session_run_leave_it_completed(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closes that judge nothing may both get through; the run ends completed."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set
    from server.settings import settings
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import active_session_runs, begin_session_run, close_session_run

    # The server default would age the run's memories; a session's run keeps them.
    monkeypatch.setattr(settings, "memory_run_complete_retain", "demote")

    org = f"itest-race-{uuid.uuid4().hex[:8]}"
    run_id = session_run_id(org, "sa:a", uuid.uuid4().hex)
    try:
        await begin_session_run(
            RunBeginInput(
                org=org,
                namespace="ns",
                agent=AGENT,
                run_id=run_id,
                goal="mcp session (itest)",
                environment="mcp",
                metadata={"implicit": True, "subject": "sa:a"},
            ),
            started_at=naive_utc_now(),
        )
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace="ns",
                agent=AGENT,
                run_id=run_id,
                memory_key="t/fact/1",
                value="the session noted a fact",
            )
        )
        closed = await asyncio.gather(
            *(close_session_run(org, run_id, reason="session_end") for _ in range(3))
        )
        assert any(closed)
        assert await active_session_runs(org, run_id) == []
        row = await ch.query_one(
            "SELECT confidence, expires_at FROM loom.memory FINAL WHERE org = {org:String}",
            params={"org": org},
        )
        assert row == {"confidence": 1.0, "expires_at": None}
    finally:
        for table in ("agent_runs", "memory"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}}", params={"org": org}
            )


@pytest.mark.asyncio
async def test_a_scoped_callers_session_run_is_its_own(ch: Any) -> None:
    """A token pinned to one user: the run the transport begins is filed under
    that user, and its DELETE completes it on a web process that never served
    it."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.tasks.api.models_run import RunGetInput
    from server.tasks.app.run import handle_run_get
    from server.transport import mcp_runs

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    session = uuid.uuid4().hex
    mcp_runs.reset()
    set_current_auth(
        AuthInfo(
            subject="sa:bot",
            org=ORG,
            namespaces=(namespace,),
            roles=("writer",),
            is_service_account=True,
            read_user_id="review-user",
        )
    )
    try:
        from server.auth.context import current_auth

        caller = mcp_runs.Caller.of_call(current_auth(), {})
        assert caller is not None
        assert (caller.subject, caller.user_id) == ("sa:bot", "review-user")
        defaults = {"org": ORG, "namespace": namespace, "agent": AGENT}
        run = await mcp_runs.ensure(session, defaults, caller)
        await mcp_runs.drain_ledger_writes()
        assert run is not None and run.run_id == session_run_id(ORG, caller.subject, session)
        got = await handle_run_get(RunGetInput(org=ORG, namespace=namespace, run_id=run.run_id))
        assert got.found and got.run is not None and got.run.user_id == "review-user"
        mcp_runs.reset()  # the DELETE reaches a web process that never served it
        assert await mcp_runs.close_session(session, ORG, caller.subject) is True
        got = await handle_run_get(RunGetInput(org=ORG, namespace=namespace, run_id=run.run_id))
        assert got.run is not None and got.run.status == "completed"
    finally:
        set_current_auth(None)
        mcp_runs.reset()
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
                params={"org": ORG, "ns": namespace},
            )


@pytest.mark.asyncio
async def test_two_callers_sending_one_session_id_keep_their_own_runs(ch: Any) -> None:
    """A client picks its session id: two tokens sending the same one hold two
    runs, and one's DELETE leaves the other's open."""
    from server.tasks.app.run import active_session_runs
    from server.transport import mcp_runs

    first, other = (f"itest-{uuid.uuid4().hex[:12]}" for _ in range(2))
    session = uuid.uuid4().hex
    mcp_runs.reset()
    try:
        runs = {}
        for who, namespace in (("alice", first), ("bob", other)):
            defaults = {"org": ORG, "namespace": namespace, "agent": AGENT}
            caller = mcp_runs.Caller(subject=who)
            runs[who] = await mcp_runs.ensure(session, defaults, caller)
        await mcp_runs.drain_ledger_writes()
        assert runs["alice"] is not None and runs["bob"] is not None
        assert runs["alice"].run_id != runs["bob"].run_id
        mcp_runs.reset()
        assert await mcp_runs.close_session(session, ORG, "bob") is True
        assert await active_session_runs(ORG, runs["bob"].run_id) == []
        alice = await active_session_runs(ORG, runs["alice"].run_id)
        assert [r["namespace"] for r in alice] == [first]
    finally:
        mcp_runs.reset()
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} "
                "AND namespace IN {ns:Array(String)}",
                params={"org": ORG, "ns": [first, other]},
            )


@pytest.mark.asyncio
async def test_a_session_run_starts_before_a_call_that_lands_before_its_begin(ch: Any) -> None:
    """The run starts when the transport registered it, so the idle sweep sees a
    first call that answers before the begin's write."""
    from server.tasks.store.runs import fetch_run
    from server.transport import mcp_runs

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    mcp_runs.reset()
    try:
        defaults = {"org": ORG, "namespace": namespace, "agent": AGENT}
        run = await mcp_runs.ensure(uuid.uuid4().hex, defaults, mcp_runs.Caller())
        assert run is not None
        await _ledger_call(ch, namespace, run.run_id)  # before the begin lands
        await mcp_runs.drain_ledger_writes()
        row = await fetch_run(ch, ORG, namespace, run.run_id)
        call = await ch.query_one(
            "SELECT max(ts) AS ts FROM loom.agent_events WHERE run_id = {rid:String}",
            params={"rid": run.run_id},
        )
        assert row is not None and row["started_at"] <= to_naive_utc(call["ts"])
    finally:
        mcp_runs.reset()
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
                params={"org": ORG, "ns": namespace},
            )


@pytest.mark.asyncio
async def test_a_reopened_session_run_keeps_its_owner(ch: Any) -> None:
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import begin_session_run
    from server.tasks.store.runs import fetch_run

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    run_id = session_run_id(ORG, "sub", uuid.uuid4().hex)

    def begin(user: str) -> RunBeginInput:
        return RunBeginInput(
            org=ORG,
            namespace=namespace,
            agent=AGENT,
            run_id=run_id,
            user_id=user,
            goal="mcp session (itest)",
            environment="mcp",
        )

    try:
        await begin_session_run(begin("u1"), started_at=naive_utc_now())
        await begin_session_run(begin("u2"), started_at=naive_utc_now())
        row = await fetch_run(ch, ORG, namespace, run_id)
        assert row is not None and row["user_id"] == "u1"
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": ORG, "ns": namespace},
        )


@pytest.mark.asyncio
async def test_a_close_completes_every_row_under_the_run_id(ch: Any) -> None:
    """One subject's tokens default to different namespaces, so the run has a
    row in each: the session's close completes both."""
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import active_session_runs, begin_session_run
    from server.transport import mcp_runs

    first, other = (f"itest-{uuid.uuid4().hex[:12]}" for _ in range(2))
    session = uuid.uuid4().hex
    run_id = session_run_id(ORG, "sub", session)
    try:
        for namespace in (first, other):
            await begin_session_run(
                RunBeginInput(
                    org=ORG,
                    namespace=namespace,
                    agent=AGENT,
                    run_id=run_id,
                    goal="g",
                    environment="mcp",
                ),
                started_at=naive_utc_now(),
            )
        assert len(await active_session_runs(ORG, run_id)) == 2
        mcp_runs.reset()
        assert await mcp_runs.close_session(session, ORG, "sub") is True
        assert await active_session_runs(ORG, run_id) == []
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String} "
            "AND namespace IN {ns:Array(String)}",
            params={"org": ORG, "ns": [first, other]},
        )


@pytest.mark.asyncio
async def test_a_close_under_an_unpinned_scoped_token_counts_all_the_runs_writes(
    ch: Any,
) -> None:
    """The run id binds the org, token and session, so a close counts every write
    under it whatever auth its context carries: here an `own_user` token pinned to
    no user, which has no read scope of its own."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import begin_session_run, close_session_run
    from server.tasks.store.runs import fetch_run

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    run_id = session_run_id(ORG, "sa:bot", uuid.uuid4().hex)
    try:
        await begin_session_run(
            RunBeginInput(
                org=ORG,
                namespace=namespace,
                agent=AGENT,
                run_id=run_id,
                goal="mcp session (itest)",
                environment="mcp",
                metadata={"implicit": True, "subject": "sa:bot"},
            ),
            started_at=naive_utc_now(),
        )
        for user in ("u1", "u2"):
            await handle_memory_set(
                MemorySetInput(
                    org=ORG,
                    namespace=namespace,
                    agent=AGENT,
                    user_id=user,
                    run_id=run_id,
                    memory_key=f"t/fact/{user}",
                    value=f"{user} noted a fact",
                )
            )
        set_current_auth(
            AuthInfo(
                subject="sa:bot",
                org=ORG,
                namespaces=(namespace,),
                roles=("writer",),
                is_service_account=True,
                read_scope="own_user",
            )
        )
        assert await close_session_run(ORG, run_id, reason="session_end") is True
        row = await fetch_run(ch, ORG, namespace, run_id)
        assert row is not None and row["status"] == "completed"
        assert sorted(row["memories_written"]) == ["t/fact/u1", "t/fact/u2"]
    finally:
        set_current_auth(None)
        for table in ("agent_runs", "memory"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
                params={"org": ORG, "ns": namespace},
            )


@pytest.mark.asyncio
async def test_two_first_begins_that_both_found_no_run_keep_the_earlier(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cold web processes read "no run" before either writes: the run keeps
    the earlier start and its owner, and a close still supersedes both."""
    from datetime import timedelta

    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import begin_session_run, close_session_run
    from server.tasks.store import runs as runs_store

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    run_id = session_run_id(ORG, "sa:a", uuid.uuid4().hex)
    first = naive_utc_now().replace(microsecond=0)  # DateTime64(3) keeps it exactly

    def begin(user: str) -> RunBeginInput:
        return RunBeginInput(
            org=ORG,
            namespace=namespace,
            agent=AGENT,
            run_id=run_id,
            user_id=user,
            goal="mcp session (itest)",
            environment="mcp",
            metadata={"implicit": True, "subject": "sa:a"},
        )

    async def absent(*_: Any) -> None:
        return None

    try:
        with monkeypatch.context() as m:
            m.setattr(runs_store, "fetch_run", absent)  # both read before either writes
            await begin_session_run(begin("u1"), started_at=first)
            await begin_session_run(begin("u2"), started_at=first + timedelta(milliseconds=240))
        row = await runs_store.fetch_run(ch, ORG, namespace, run_id)
        assert row is not None
        assert (row["user_id"], to_naive_utc(row["started_at"])) == ("u1", first)
        assert await runs_store.busy_lately(ch, ORG, namespace, run_id, "sa:a", 60)
        assert await close_session_run(ORG, run_id, reason="session_end")
        row = await runs_store.fetch_run(ch, ORG, namespace, run_id)
        assert row is not None and row["status"] == "completed"
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": ORG, "ns": namespace},
        )


@pytest.mark.asyncio
async def test_a_check_after_an_erasure_begins_a_new_run_not_the_erased_one(ch: Any) -> None:
    """The minute check re-opens a closed run with its own start; once an erasure
    has deleted the row, it begins a new run started now, not one dated before it."""
    from server.tasks.app.run import close_session_run
    from server.tasks.store.runs import fetch_run
    from server.transport import mcp_runs

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    defaults = {"org": ORG, "namespace": namespace, "agent": AGENT}
    session = uuid.uuid4().hex
    mcp_runs.reset()
    try:
        run = await mcp_runs.ensure(session, defaults, mcp_runs.Caller(), now=10.0)
        assert run is not None
        await mcp_runs.drain_ledger_writes()
        first = await fetch_run(ch, ORG, namespace, run.run_id)
        assert first is not None
        started = to_naive_utc(first["started_at"])

        assert await close_session_run(ORG, run.run_id, reason="idle")
        await mcp_runs.ensure(session, defaults, mcp_runs.Caller(), now=100.0)
        await mcp_runs.drain_ledger_writes()
        reopened = await fetch_run(ch, ORG, namespace, run.run_id)
        assert reopened is not None and reopened["status"] == "active"
        assert to_naive_utc(reopened["started_at"]) == started

        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": ORG, "ns": namespace},
        )
        erased_at = naive_utc_now()
        await asyncio.sleep(0.01)  # a new start is a later millisecond than the erasure
        await mcp_runs.ensure(session, defaults, mcp_runs.Caller(), now=200.0)
        await mcp_runs.drain_ledger_writes()
        begun = await fetch_run(ch, ORG, namespace, run.run_id)
        assert begun is not None and begun["status"] == "active"
        assert started < erased_at < to_naive_utc(begun["started_at"])
    finally:
        mcp_runs.reset()
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": ORG, "ns": namespace},
        )


@pytest.mark.asyncio
async def test_a_reopened_session_run_keeps_what_its_close_recalled(ch: Any) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.tasks.api.models_run import RunBeginInput, RunCompleteInput
    from server.tasks.app.run import _complete, _parse_metadata, begin_session_run
    from server.tasks.store.runs import fetch_run

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    run_id = session_run_id(ORG, "sub", uuid.uuid4().hex)
    begin = RunBeginInput(
        org=ORG,
        namespace=namespace,
        agent=AGENT,
        run_id=run_id,
        goal="mcp session (itest)",
        environment="mcp",
        metadata={"implicit": True, "subject": "sub"},
    )
    try:
        await begin_session_run(begin, started_at=naive_utc_now())
        row = await fetch_run(ch, ORG, namespace, run_id)
        assert row is not None
        await _complete(
            ch,
            RunCompleteInput(
                org=ORG,
                namespace=namespace,
                run_id=run_id,
                status="completed",
                outcome="none",
                recalled_keys=["user/fact/one"],
                metadata={"closed_by": "mcp_idle"},
            ),
            row,
            drop_claim=False,
            scope=WHOLE_NAMESPACE,
        )
        await begin_session_run(begin, started_at=naive_utc_now())
        row = await fetch_run(ch, ORG, namespace, run_id)
        assert row is not None and row["status"] == "active"
        meta = _parse_metadata(row["metadata"])
        assert meta["recalled_keys"] == ["user/fact/one"]
        assert "closed_by" not in meta  # the reopen undid that close
        assert meta["subject"] == "sub"
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": ORG, "ns": namespace},
        )


@pytest.mark.asyncio
async def test_the_run_cost_window_keeps_milliseconds_in_utc(ch: Any) -> None:
    # A driver that binds datetimes to whole seconds would read this window as
    # 12:00:00 to 12:00:00, and one that binds a naive datetime as local time
    # would move it to 06:30 on this host; either misses the turn.
    namespace = f"ns-{uuid.uuid4().hex[:8]}"
    await ch.insert(
        "loom.agent_turn_usage",
        [
            {
                "org": ORG,
                "namespace": namespace,
                "agent": AGENT,
                "session_id": "s",
                "message_id": "m1",
                "model": "claude-opus-5-5",
                "cost_usd": 2,
                "priced": True,
                "turn_at": datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=UTC),
            }
        ],
        settings={"async_insert": 0},
    )
    since = datetime(2026, 1, 1, 12, 0, 0, 100000)
    until = datetime(2026, 1, 1, 12, 0, 0, 900000)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TZ", "Asia/Kolkata")
            time.tzset()
            turns, _, cost = await run_handlers.runs_store.run_cost(
                ch, ORG, namespace, "s", since, until
            )
    finally:
        time.tzset()
        await ch.execute(
            "DELETE FROM loom.agent_turn_usage WHERE namespace = {ns:String}",
            params={"ns": namespace},
        )
    assert (turns, cost) == (1, 2.0)
