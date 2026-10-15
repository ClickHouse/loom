"""The Loom side of an AX task: open a run with a time budget, check it between
steps, and close it however the work ends. `step()` stands in for the agent's
own work."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Literal

from loom import Agent

# A time budget only: a cost budget needs a session whose turns Loom records,
# which a sandboxed agent without the Claude Code or Codex plugin does not have.
BUDGET_SECONDS = 30 * 60


async def step(agent: Agent, n: int) -> bool:
    """One unit of the agent's work; True when the task is done. Inside the run
    scope, the write carries the run's id."""
    await agent.memory.set(f"ax/fix-flaky-test/step-{n}", f"step {n} ran in the AX sandbox")
    return n >= 3


async def work(agent: Agent, run_id: str) -> Literal["completed", "cancelled"]:
    """Run steps until the task is done or over budget; return the run's status."""
    n = 0
    while True:
        n += 1
        if await step(agent, n):
            return "completed"
        report = await agent.run.get(run_id)
        over = report.get("over_budget") or []  # absent on a server without budgets
        if over:
            # Loom only reports the budget; stopping is the agent's call.
            print(f"over budget ({', '.join(over)}); stopping")
            return "cancelled"


async def main() -> None:
    # AX stops a task with SIGTERM; cancelling lets the run close as cancelled.
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)

    org = os.environ["LOOM_ORG"]
    namespace = os.environ["LOOM_NAMESPACE"]
    # AX kills the task 10 s after SIGTERM: one 5 s attempt per Loom call keeps
    # the run's close inside that.
    retry = {"max_attempts": 1}
    async with Agent(org, namespace, "ax-fix-flaky-test", timeout_s=5.0, retry=retry) as agent:
        # The scope closes the run: as `run.status` on a clean exit, `failed` on
        # an error and `cancelled` on a cancellation, re-raising either.
        async with agent.run_scope(
            goal="Fix the flaky test", environment="ax", budget_seconds=BUDGET_SECONDS
        ) as run:
            run.status = await work(agent, run.run_id)
            # The work is done and the close is next: a SIGTERM now must not
            # cancel the close, which fits in the 10 s AX allows.
            loop.add_signal_handler(signal.SIGTERM, lambda: None)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        sys.exit(128 + signal.SIGTERM)
