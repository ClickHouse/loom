"""An MCP session's implicit run, as the ledger decides it, on live ClickHouse:
`active_session_run` finds the run a sibling web process began, in whichever
namespace, and `session_run_calls` counts the calls labelled with it in every
namespace.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

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


async def _ledger_call(ch: Any, namespace: str, run_id: str) -> None:
    await ch.insert(
        "loom.agent_events",
        [
            {
                "org": ORG,
                "namespace": namespace,
                "agent": AGENT,
                "user_id": "-",
                "session_id": "s",
                "run_id": run_id,
                "operation": "memory.search",
                "request_id": uuid.uuid4().hex,
                "latency_ms": 1,
                "success": True,
            }
        ],
        settings={"async_insert": 0},
    )


@pytest.mark.asyncio
async def test_the_ledger_decides_when_a_session_run_ends(ch: Any) -> None:
    from server.core.datetimes import naive_utc_now
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import active_session_run, handle_run_begin, session_run_calls

    first, other = (f"itest-{uuid.uuid4().hex[:12]}" for _ in range(2))
    run_id = f"mcp-{uuid.uuid4().hex[:12]}"
    try:
        assert await active_session_run(ORG, run_id) is None
        await handle_run_begin(
            RunBeginInput(
                org=ORG, namespace=first, agent=AGENT, run_id=run_id, session_id="s", goal="g"
            )
        )
        run = await active_session_run(ORG, run_id)
        assert run is not None and run["namespace"] == first

        # Nothing ledgered yet.
        assert await session_run_calls(ORG, run_id) == (first, 0, None)
        # A call the session made in another namespace counts.
        await _ledger_call(ch, other, run_id)
        namespace, calls, last = await session_run_calls(ORG, run_id) or ("", 0, None)
        assert (namespace, calls) == (first, 1)
        assert last is not None and (naive_utc_now() - last).total_seconds() < 120
        assert await session_run_calls(ORG, "mcp-never-begun") is None
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} "
                "AND namespace IN {ns:Array(String)}",
                params={"org": ORG, "ns": [first, other]},
            )


@pytest.mark.asyncio
async def test_a_scoped_callers_session_run_is_its_own(ch: Any) -> None:
    """A token pinned to one user: the run the transport begins is filed under
    that user and the id its calls carry, so the caller reads and closes it
    through the ownership checks run.get and run.complete apply."""
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
        defaults = {"org": ORG, "namespace": namespace, "agent": AGENT, "user_id": "review-user"}
        run = await mcp_runs.ensure(session, defaults, goal="first call")
        await mcp_runs.drain_ledger_writes()
        assert run is not None and run.run_id == mcp_runs.run_id_for(ORG, "review-user", session)
        got = await handle_run_get(RunGetInput(org=ORG, namespace=namespace, run_id=run.run_id))
        assert got.found and got.run is not None and got.run.user_id == "review-user"
        mcp_runs.reset()  # the DELETE reaches a web process that never served it
        assert await mcp_runs.close_session(session, ORG, "review-user") is True
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
