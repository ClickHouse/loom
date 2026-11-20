"""Lab 1: an agent that remembers.

The lab: https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-1-an-agent-that-remembers
The SDK calls it uses: https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md

Fill in the two functions marked TODO, then run `python labs/lab1.py`.
`python grading/grade.py` checks them.
"""

import asyncio

from loom import Agent

# The key your fact lives under. Keep it: the checks read it.
KEY = "lab1/fact/me"


def make_agent() -> Agent:
    # The server comes from LOOM_BASE_URL, else http://localhost:7777.
    return Agent(org="dev", namespace="local", agent="lab1-student", consumer="course-lab1")


async def save_and_revise(agent: Agent) -> None:
    """Save a fact about yourself under KEY, then revise it: write KEY again with
    a changed value, so memory.history lists two versions."""
    # TODO: two calls to agent.memory.set(KEY, ...)
    raise NotImplementedError("Lab 1: save_and_revise")


async def unknown_question(agent: Agent) -> str:
    """Search for something no saved fact answers and return the reply's
    abstain_code."""
    # TODO: agent.memory.search(...), then return the result's abstain_code
    raise NotImplementedError("Lab 1: unknown_question")


async def main() -> None:
    async with make_agent() as agent:
        await save_and_revise(agent)
        for version in await agent.memory.history(KEY):
            print("version", version["version"], version["status"])
        print("abstain_code:", await unknown_question(agent))


if __name__ == "__main__":
    asyncio.run(main())
