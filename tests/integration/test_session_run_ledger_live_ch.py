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
