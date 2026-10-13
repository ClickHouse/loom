"""An MCP session's implicit run, as the ledger decides it, on live ClickHouse:
`run.begin(keep_active=True)` keeps the run a sibling web process began, and
`idle_session_run` closes it only once no call labelled with it is recent.

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
    from server.tasks.app.run import handle_run_begin, idle_session_run
    from server.tasks.store.runs import fetch_run

    namespace = f"itest-{uuid.uuid4().hex[:12]}"
    run_id = f"mcp-{uuid.uuid4().hex[:12]}"

    def begin(goal: str) -> RunBeginInput:
        return RunBeginInput(
            org=ORG, namespace=namespace, agent=AGENT, run_id=run_id, session_id="s", goal=goal
        )

    try:
        await handle_run_begin(begin("first call"), keep_active=True)
        await handle_run_begin(begin("a sibling's first call"), keep_active=True)
        run = await fetch_run(ch, ORG, namespace, run_id)
        assert run is not None and run["goal"] == "first call"

        # Nothing ledgered yet: idle at once, with no calls.
        assert await idle_session_run(ORG, namespace, AGENT, run_id, idle_s=1800) == 0
        await _ledger_call(ch, namespace, run_id)
        assert await idle_session_run(ORG, namespace, AGENT, run_id, idle_s=1800) is None
        assert await idle_session_run(ORG, namespace, AGENT, run_id, idle_s=0) == 1
        assert await idle_session_run(ORG, namespace, AGENT, run_id, idle_s=None) == 1
        assert await idle_session_run(ORG, namespace, AGENT, "mcp-never-begun", idle_s=None) is None
    finally:
        for table in ("agent_runs", "agent_events"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
                params={"org": ORG, "ns": namespace},
            )
