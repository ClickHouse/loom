"""One task as a ledger run — the loop every headless Loom agent should have.

`examples/claude_agent_loop.py` shows recall and write-back; this file shows
the third leg, the run ledger: a task opens a run, reads memory inside it,
writes inside it, and closes it naming the memories it relied on. That
`memories_used` list is the credit-assignment signal the server folds into
`used` usage events; reads and writes outside a run carry no run_id, so the
ledger has nothing to credit. The shape:

  1. `agent.run_scope(goal=...)`  — `run.begin` on entry, `run.complete` on exit.
  2. reads inside the block        — `memory.search` / `memory.ask` are
                                     labelled with the run (`X-Loom-Run-Id`),
                                     not filtered by it; `ask` credits the
                                     keys its answer cited automatically.
  3. `run.used(key, ...)`          — name what the task actually relied on
                                     (search hits the agent read and acted on).
  4. writes inside the block       — `memory.set` stamps the run_id, so the
                                     server derives `memories_written`.
  5. `run.final_response = ...`    — what the task answered; the exit writes
                                     the row.

Run against any Loom:
    export LOOM_BASE_URL=http://localhost:7777
    export LOOM_API_KEY=$(cat ~/.loom/token)
    python examples/ledger_run.py

`tests/integration/test_sdk_ledger_example.py` runs `run_task` in CI and
asserts the `agent_runs` row it produces carries a non-empty `memories_used`.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from loom import Agent

DEPLOY_KEY = "ops:deploy-window"
DEPLOY_FACT = "We deploy on Tuesdays and Thursdays, never on a Friday."


async def run_task(
    *,
    org: str,
    namespace: str,
    agent_name: str = "ledger-example",
    base_url: str | None = None,
    api_key: str | None = None,
    goal: str = "Pick a ship day for the release",
) -> dict[str, Any]:
    """Seed one fact, then run a task that reads it, writes a decision and
    closes the run naming what it relied on. Returns the run's ids and the
    completed ledger response."""
    async with Agent(org, namespace, agent_name, base_url=base_url, api_key=api_key) as agent:
        # A fact the task will rely on — outside the run: it is prior
        # knowledge, not something this run wrote.
        await agent.memory.set(DEPLOY_KEY, DEPLOY_FACT, tags=["ops"])

        async with agent.run_scope(goal=goal, user_request="When can we ship?") as run:
            # READ — inside the block reads are labelled with run.run_id, writes carry it.
            hits = await agent.memory.search("when do we deploy?", top_k=5)
            # What the task relied on: the hits it read. (`memory.ask` does
            # this step itself for the keys its answer cites.)
            run.used(*[h.memory_key for h in hits])
            if DEPLOY_KEY not in run.used_keys:
                # Retrieval on a toy corpus can miss; the task still read
                # the fact by key, so it still counts as relied on.
                got = await agent.memory.get(DEPLOY_KEY)
                if got is not None:
                    run.used(DEPLOY_KEY)

            # WRITE — stamped with the run_id; the server lists it under the
            # run's `memories_written`.
            await agent.memory.set(
                f"decision:ship-day:{run.run_id}",
                "Ship on Thursday.",
                tags=["decision"],
            )
            run.final_response = "Ship on Thursday."
        # On exit `run.complete` was called with memories_used = run.used_keys
        # and status completed (failed if the block raised).
        return {
            "run_id": run.run_id,
            "memories_used": list(run.used_keys),
            "completed": run.completed,
        }


async def main() -> None:
    out = await run_task(
        org=os.environ.get("LOOM_ORG", "dev"),
        namespace=os.environ.get("LOOM_NAMESPACE", "examples"),
    )
    print(f"run {out['run_id']} relied on {out['memories_used']}")
    print(f"ledger: {out['completed']}")


if __name__ == "__main__":
    asyncio.run(main())
