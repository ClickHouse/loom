"""An MCP session's implicit run, as the ledger decides it, on live ClickHouse:
`active_session_run` finds the run a sibling web process began, in whichever
namespace, and `idle_session_run` closes it only once no call labelled with it,
in any namespace, is recent.

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
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import active_session_run, handle_run_begin, idle_session_run

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

        # Nothing ledgered yet: idle at once, with no calls.
        assert await idle_session_run(ORG, run_id, idle_s=1800) == (first, 0)
        # A call the session made in another namespace keeps the run open.
        await _ledger_call(ch, other, run_id)
        assert await idle_session_run(ORG, run_id, idle_s=1800) is None
        assert await idle_session_run(ORG, run_id, idle_s=0) == (first, 1)
        assert await idle_session_run(ORG, run_id, idle_s=None) == (first, 1)
        assert await idle_session_run(ORG, "mcp-never-begun", idle_s=None) is None
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} "
                "AND namespace IN {ns:Array(String)}",
                params={"org": ORG, "ns": [first, other]},
            )
