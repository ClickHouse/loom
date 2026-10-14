"""An MCP session's implicit run, as the ledger decides it, on live ClickHouse:
`active_session_runs` finds the run a sibling web process began, and the idle
sweep keeps open only a run its own token called lately, in any namespace.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.tasks.app import run as run_handlers
from server.tasks.domain.session_runs import session_run_id

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

ORG, AGENT = "itest-org", "itest-mcp"


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
            }
        ],
        settings={"async_insert": 0},
    )


@pytest.mark.asyncio
async def test_the_idle_sweep_closes_a_run_its_own_calls_did_not_keep_busy(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More busy runs than a pass closes do not hide a quiet one behind them, and
    a run an earlier server opened under `mcp-<session id>` closes too."""
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import active_session_runs, begin_session_run, close_idle_session_runs

    org = f"itest-sweep-{uuid.uuid4().hex[:8]}"
    namespace, elsewhere = (f"itest-{uuid.uuid4().hex[:12]}" for _ in range(2))
    busy = [session_run_id(org, "sa:a", uuid.uuid4().hex) for _ in range(3)]
    quiet, borrowed = (session_run_id(org, "sa:a", uuid.uuid4().hex) for _ in range(2))
    explicit = f"itest-run-{uuid.uuid4().hex[:12]}"
    legacy = f"mcp-{uuid.uuid4().hex}"
    try:
        for run_id in [*busy, quiet, borrowed, explicit, legacy]:
            await begin_session_run(
                RunBeginInput(
                    org=org,
                    namespace=namespace,
                    agent=AGENT,
                    run_id=run_id,
                    goal="g",
                    environment="mcp",
                    metadata=(
                        {"implicit": True, "transport": "mcp"}  # no subject recorded
                        if run_id == legacy
                        else {"implicit": run_id != explicit, "subject": "sa:a"}
                    ),
                ),
                started_at=naive_utc_now(),
            )
        await asyncio.sleep(2)
        for run_id in busy:  # calls in another namespace
            await _ledger_call(ch, elsewhere, run_id, subject="sa:a", org=org)
        await _ledger_call(ch, namespace, borrowed, subject="sa:eve", org=org)  # another token
        monkeypatch.setattr(run_handlers, "IDLE_CLOSE_BATCH", 2)
        assert await close_idle_session_runs(1, org=org) == 2
        assert await close_idle_session_runs(1, org=org) == 1
        assert all([await active_session_runs(org, run_id) for run_id in busy])
        assert await active_session_runs(org, quiet) == []
        assert await active_session_runs(org, borrowed) == []
        assert await active_session_runs(org, legacy) == []
        assert await active_session_runs(org, explicit) != []  # not an implicit run
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
    long_ago = int((naive_utc_now().timestamp() - 7200) * 1e9)
    ids = [session_run_id(org, "sa:a", str(i)) for i in range(3500)]
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
                }
                for run_id in ids
            ],
            settings={"async_insert": 0},
        )
        monkeypatch.setattr(run_handlers, "IDLE_CLOSE_BATCH", 5)
        assert await close_idle_session_runs(60, org=org) == 5
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String}", params={"org": org}
        )


@pytest.mark.asyncio
async def test_the_idle_sweep_closes_a_run_however_long_ago_it_was_written(ch: Any) -> None:
    """A run an earlier server, or a month without workers, left open."""
    from server.tasks.app.run import active_session_runs, close_idle_session_runs

    org = f"itest-old-{uuid.uuid4().hex[:8]}"
    run_id = session_run_id(org, "sa:a", uuid.uuid4().hex)
    long_ago = int((naive_utc_now().timestamp() - 31 * 86400) * 1e9)
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
                }
            ],
            settings={"async_insert": 0},
        )
        assert await close_idle_session_runs(1800, org=org) == 1
        assert await active_session_runs(org, run_id) == []
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String}", params={"org": org}
        )


@pytest.mark.asyncio
async def test_a_run_that_records_no_subject_stays_open_while_it_is_called(ch: Any) -> None:
    """An earlier server's run: its calls carry a subject it never recorded."""
    from server.tasks.app.run import active_session_runs, close_idle_session_runs

    org = f"itest-nosub-{uuid.uuid4().hex[:8]}"
    busy, quiet = f"mcp-{uuid.uuid4().hex}", f"mcp-{uuid.uuid4().hex}"
    long_ago = int((naive_utc_now().timestamp() - 7200) * 1e9)
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
                }
                for run_id in (busy, quiet)
            ],
            settings={"async_insert": 0},
        )
        await _ledger_call(ch, "ns", busy, subject="sa:still-working", org=org)
        assert await close_idle_session_runs(1800, org=org) == 1
        assert await active_session_runs(org, busy) != []
        assert await active_session_runs(org, quiet) == []
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}}", params={"org": org}
            )


@pytest.mark.asyncio
async def test_closes_racing_for_one_session_run_complete_it_once(ch: Any) -> None:
    """With Keeper, the close claim lets one of them through."""
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import begin_session_run, close_session_run
    from server.tasks.store.runs import claim_close, drop_close_claim

    org = f"itest-race-{uuid.uuid4().hex[:8]}"
    run_id = session_run_id(org, "sa:a", uuid.uuid4().hex)
    probe = await claim_close(ch, {"org": org, "namespace": "ns", "run_id": run_id})
    if probe is None:
        pytest.skip("no Keeper claims table on this server")
    await drop_close_claim(ch, {"org": org, "namespace": "ns", "run_id": run_id}, probe)
    try:
        for _ in range(5):
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
            closed = await asyncio.gather(
                *(close_session_run(org, run_id, reason="session_end") for _ in range(3))
            )
            assert sorted(closed) == [False, False, True]
    finally:
        await ch.execute(
            "DELETE FROM loom.agent_runs WHERE org = {org:String}", params={"org": org}
        )


@pytest.mark.asyncio
async def test_a_stale_close_claim_is_taken_over_once_and_its_holder_cannot_drop_it(
    ch: Any,
) -> None:
    from server.tasks.store.runs import CLOSE_CLAIM_STALE_S, claim_close, drop_close_claim

    org = f"itest-claim-{uuid.uuid4().hex[:8]}"
    row = {"org": org, "namespace": "ns", "run_id": session_run_id(org, "sa:a", "s")}
    old = await claim_close(ch, row)
    if old is None:
        pytest.skip("no Keeper claims table on this server")
    try:
        assert old.held and not (await claim_close(ch, row)).held  # fresh: held elsewhere
        await ch.execute(
            "ALTER TABLE loom.agent_run_claims UPDATE claimed_at = claimed_at - "
            "INTERVAL {s:UInt32} SECOND WHERE org = {org:String}",
            params={"s": CLOSE_CLAIM_STALE_S + 60, "org": org},
        )
        claims = await asyncio.gather(*(claim_close(ch, row) for _ in range(6)))
        (new,) = [c for c in claims if c is not None and c.held]
        await drop_close_claim(ch, row, old)  # the dead closer comes back late
        assert not (await claim_close(ch, row)).held  # the taker still holds it
        await drop_close_claim(ch, row, new)
        assert (await claim_close(ch, row)).held
    finally:
        await ch.execute(
            "ALTER TABLE loom.agent_run_claims DELETE WHERE org = {org:String}",
            params={"org": org},
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
        assert row is not None and row["started_at"] <= call["ts"].replace(tzinfo=None)
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
