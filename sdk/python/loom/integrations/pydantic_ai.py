"""Loom as a Pydantic AI toolset.

``loom_toolset`` returns Loom's agent verbs (``loom_remember``,
``loom_recall``, ``loom_ask``, ``loom_offload``, ``loom_fetch``,
``loom_note``) as a Pydantic AI ``FunctionToolset``, so a model keeps and reads
long-term memory in Loom during a run::

    from pydantic_ai import Agent as PydanticAgent
    from loom import Agent
    from loom.integrations.pydantic_ai import loom_toolset

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
``pip install 'clickhouse-loom[pydantic-ai]'``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

try:
    from pydantic_ai import FunctionToolset, Tool
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
    ``max_result_chars`` caps the string each call returns to the model. A call
    Loom refuses returns its error message to the model instead of failing the run.
    """
    if isinstance(agent, SyncAgent):
        raise TypeError("loom_toolset needs a loom.Agent: Pydantic AI awaits its tools")
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
            # Pydantic AI fails the run on any exception but ModelRetry, which spends
            # the tool's retries; the message lets the model fix its call either way.
            return f"{name} failed: {e}"

    return run
