"""Loom as a Pydantic AI toolset.

``loom_toolset`` returns Loom's agent verbs (``loom_remember``,
``loom_recall``, ``loom_ask``, ``loom_offload``, ``loom_fetch``,
``loom_note``) as a Pydantic AI ``FunctionToolset``, so a model keeps and reads
long-term memory in Loom during a run::

    from loom import Agent
    from loom.integrations.pydantic_ai import loom_toolset
    from pydantic_ai import Agent as PydanticAgent

    async with Agent(org="acme", namespace="support") as loom_agent:
        assistant = PydanticAgent(
            "anthropic:claude-sonnet-5-5",
            instructions="Recall what you know about the user before answering.",
            toolsets=[loom_toolset(loom_agent, user_id="alice")],
        )
        result = await assistant.run("Book my usual seat to Lisbon")

The tools are ``loom.tools.AgentTools`` (the same names, schemas and results
as ``agent.tools()``), bound to one user, session or run. Identity and
isolation come from the Loom agent. Install with
``pip install 'clickhouse-loom[pydantic-ai]'``, plus the Pydantic AI extra for
the model's provider (``pydantic-ai-slim[anthropic]`` for the example).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

try:
    from pydantic_ai import FunctionToolset, Tool, ToolFailed
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.pydantic_ai needs pydantic-ai-slim: "
        "pip install 'clickhouse-loom[pydantic-ai]'"
    ) from e

from loom.errors import LoomError
from loom.sync import SyncAgent
from loom.tools import AgentTools

if TYPE_CHECKING:
    from loom.agent import Agent


def loom_toolset(
    agent: Agent,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    only: Iterable[str] | None = None,
    max_result_chars: int = 8_000,
) -> FunctionToolset[Any]:
    """Loom's agent verbs as a ``FunctionToolset``, bound to one scope.

    ``only`` keeps a subset by name, e.g. ``["loom_remember", "loom_recall"]``;
    ``max_result_chars`` caps the string each call returns to the model
    (``loom_recall`` leaves out its lowest-ranked memories whole to fit). A call
    Loom refuses reaches the model as a failed tool result, and the run goes on.
    """
    if isinstance(agent, SyncAgent):
        raise TypeError("loom_toolset needs a loom.Agent: its tools await the agent's calls")
    tools = AgentTools(
        agent,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        only=only,
        max_result_chars=max_result_chars,
    )
    return FunctionToolset(
        [
            Tool.from_schema(
                _runner(tools, spec["name"]),
                name=spec["name"],
                description=spec["description"],
                json_schema=spec["parameters"],
            )
            for spec in tools.definitions()
        ]
    )


def _runner(tools: AgentTools, name: str) -> Callable[..., Awaitable[str]]:
    async def run(**arguments: Any) -> str:
        try:
            return await tools.execute(name, arguments)
        except LoomError as e:
            # A failed tool result the model reads and the run goes on; any other
            # exception fails the run, and ModelRetry would spend the tool's retries.
            raise ToolFailed(f"{name} failed: {e}") from e

    return run
