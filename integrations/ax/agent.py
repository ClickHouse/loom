"""The Loom side of an AX task: open a run with a time budget, check it between
steps, and close it however the work ends. `step()` stands in for the agent's
own work."""

from __future__ import annotations

import asyncio
import os

from loom import Agent

# A time budget only: a cost budget needs a session whose turns Loom records,
# which a sandboxed agent without the Claude Code or Codex plugin does not have.
BUDGET_SECONDS = 30 * 60


async def step(agent: Agent, run_id: str, n: int) -> bool:
    """One unit of the agent's work; True when the task is done."""
    await agent.memory.set(
        f"ax/fix-flaky-test/step-{n}", f"step {n} ran in the AX sandbox", run_id=run_id
    )
    return n >= 3


async def work(agent: Agent, run_id: str) -> str:
    """Run steps until the task is done or over budget; return the run's status."""
    n = 0
    while True:
        n += 1
        if await step(agent, run_id, n):
            return "completed"
        report = await agent.run.get(run_id)
        if report["over_budget"]:
            # Loom only reports the budget; stopping is the agent's call.
            print(f"over budget ({', '.join(report['over_budget'])}); stopping")
            return "cancelled"


async def main() -> None:
    org = os.environ["LOOM_ORG"]
    namespace = os.environ["LOOM_NAMESPACE"]
    async with Agent(org, namespace, "ax-fix-flaky-test") as agent:
        run = await agent.run.begin(
            goal="Fix the flaky test", environment="ax", budget_seconds=BUDGET_SECONDS
        )
        run_id = run["run_id"]
        status = "failed"
        try:
            status = await work(agent, run_id)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            try:
                await agent.run.complete(run_id, status=status)
            except Exception as e:  # noqa: BLE001 — never hide why the work stopped
                print(f"could not close run {run_id}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
