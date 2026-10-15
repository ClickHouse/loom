"""The Loom side of an AX task: open a run with a budget, check it between
steps, and close it. `step()` stands in for the agent's own work."""

from __future__ import annotations

import asyncio
import os

from loom import Agent

BUDGET_USD = 2.0
BUDGET_SECONDS = 30 * 60


async def step(agent: Agent, run_id: str, n: int) -> bool:
    """One unit of the agent's work; True when the task is done."""
    await agent.memory.set(
        f"ax/fix-flaky-test/step-{n}", f"step {n} ran in the AX sandbox", run_id=run_id
    )
    return n >= 3


async def main() -> None:
    org = os.environ["LOOM_ORG"]
    namespace = os.environ["LOOM_NAMESPACE"]
    async with Agent(org, namespace, "ax-fix-flaky-test") as agent:
        run = await agent.run.begin(
            goal="Fix the flaky test",
            environment="ax",
            budget_usd=BUDGET_USD,
            budget_seconds=BUDGET_SECONDS,
        )
        run_id = run["run_id"]
        status = "completed"
        n = 0
        while True:
            n += 1
            if await step(agent, run_id, n):
                break
            report = await agent.run.get(run_id)
            if report["over_budget"]:
                # Loom only reports the budget; stopping is the agent's call.
                print(f"over budget ({', '.join(report['over_budget'])}); stopping")
                status = "cancelled"
                break
        await agent.run.complete(run_id, status=status)


if __name__ == "__main__":
    asyncio.run(main())
