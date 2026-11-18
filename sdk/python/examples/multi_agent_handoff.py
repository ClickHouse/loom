"""A planner hands a task to a worker and reads its result with provenance.

Needs an existing namespace and distinct planner/admin and worker/writer tokens.
The agents use deterministic Python without an LLM or extraction API call.
Memory writes use the server's configured embedding provider.
The result memory is deleted in finally. The inbox and task remain as history.
"""

from __future__ import annotations

import asyncio
import os
import secrets

from loom import Agent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def cleanup(planner: Agent, worker: Agent, task_id: str, key: str) -> None:
    try:
        if task_id:
            task = await planner.tasks.get(task_id)
            require(task is not None, "The created task is missing during cleanup")
            assert task is not None
            if task["status"] not in {"done", "failed", "wont_do", "cancelled"}:
                if task["owner"] == worker.agent:
                    await worker.tasks.cancel(
                        task_id, fence=task["fence"], reason="The handoff example did not finish"
                    )
                else:
                    await planner.tasks.cancel(task_id, reason="The handoff example did not finish")
    finally:
        # Attempt deletion even if checking or cancelling the task fails.
        await worker.memory.delete(key)
        require(await worker.memory.get(key) is None, "The result memory is still readable")
        print("cleanup: result memory is missing")


async def handoff(planner: Agent, worker: Agent, inbox: str, key: str) -> None:
    await planner.tasks.register_inbox(inbox, owner_agent=worker.agent)
    print(f"inbox: {inbox} (retained)")
    task_id = ""
    try:
        created = await planner.tasks.create(
            inbox,
            kind="calculate",
            title="Square a number",
            brief={"number": 7},
            idempotency_key=inbox,
        )
        task = created["task"]
        task_id = task["task_id"]
        require(
            task["from_subject"] == "handoff-planner",
            "Mint the planner token with subject handoff-planner",
        )
        require(task["status"] == "todo", "The example task was not admitted for work")
        task = (await worker.tasks.claim(task_id))["task"]
        require(task["owner"] == worker.agent, "The worker does not own the task")
        fence = task["fence"]
        await worker.tasks.heartbeat(task_id, fence=fence)
        number = task["brief"]["number"]
        result = f"{number} squared is {number * number}"
        # A result belongs to this task, even when another task calculates the same value.
        value = {"task_id": task_id, "result": result}
        written = await worker.memory.set(key, value)
        require(not written.get("noop"), "The result memory was not written")
        await worker.tasks.complete(task_id, fence=fence, outcome={"memory_key": key})

        finished = await planner.tasks.get(task_id)
        require(finished is not None, "The planner cannot read the task")
        assert finished is not None
        require(finished["status"] == "done", "The task did not finish")
        require(finished["outcome"].get("memory_key") == key, "The outcome lost the memory key")
        memory = await planner.memory.get(key)
        require(memory is not None, "The planner cannot read the worker's memory")
        assert memory is not None
        require(memory.get("value") == value, "The result differs from the worker's calculation")
        require(memory.get("agent") == worker.agent, "The memory names another agent")
        actor = str(memory.get("actor") or "")
        require(actor == "process:handoff-worker", "The memory names another writing principal")
        require(memory.get("source_class") == "asserted", "The memory has another source class")
        print(f"task: {finished['status']}")
        print(f"result: {memory['value']['result']}")
        print(f"writer agent: {memory['agent']}")
        print(f"writer actor: {actor}")
        print(f"source class: {memory['source_class']}")
    finally:
        await cleanup(planner, worker, task_id, key)


async def main() -> None:
    org = os.environ["LOOM_ORG"]
    namespace = os.environ["LOOM_NAMESPACE"]
    planner_token = os.environ["LOOM_PLANNER_API_KEY"]
    worker_token = os.environ["LOOM_WORKER_API_KEY"]
    require(
        bool(org.strip() and namespace.strip()), "Set the organization and registered namespace"
    )
    require(bool(planner_token and worker_token), "Set both tokens")
    require(planner_token != worker_token, "Use a separate token for each principal")
    suffix = secrets.token_hex(8)
    async with (
        Agent(org, namespace, "handoff-planner", api_key=planner_token, auto_run=False) as planner,
        Agent(org, namespace, "handoff-worker", api_key=worker_token, auto_run=False) as worker,
    ):
        await handoff(planner, worker, f"handoff-{suffix}", f"example/handoff/{suffix}/square")


if __name__ == "__main__":
    asyncio.run(main())
