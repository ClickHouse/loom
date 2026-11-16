"""Loom as tools for the OpenAI Agents SDK.

``loom_tools`` returns Loom's agent verbs (``loom_remember``, ``loom_recall``,
``loom_ask``, ``loom_offload``, ``loom_fetch``, ``loom_note``) as the OpenAI
Agents SDK's ``FunctionTool`` objects, so a model keeps and reads long-term
memory in Loom during a run::

    from agents import Agent as OpenAIAgent, Runner
    from loom import Agent
    from loom.integrations.openai_agents import loom_tools

    async with Agent(org="acme", namespace="support") as loom_agent:
        assistant = OpenAIAgent(
            name="assistant",
            instructions="Recall what you know about the user before answering.",
            tools=loom_tools(loom_agent, user_id="alice"),
        )
        result = await Runner.run(assistant, "Book my usual seat to Lisbon")

The tools are ``loom.tools.AgentTools`` (the same names, schemas and results
as ``agent.tools()``), bound to one user, session or run. Identity and
isolation come from the Loom agent. Install with
``pip install 'clickhouse-loom[openai-agents]'``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

try:
    from agents import FunctionTool
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.openai_agents needs openai-agents: "
        "pip install 'clickhouse-loom[openai-agents]'"
    ) from e

from loom.errors import LoomError
from loom.tools import AgentTools

if TYPE_CHECKING:
    from loom.agent import Agent


def loom_tools(
    agent: Agent,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    only: Iterable[str] | None = None,
    max_result_chars: int = 8_000,
) -> list[FunctionTool]:
    """Loom's agent verbs as ``FunctionTool``s, bound to one scope.

    ``only`` keeps a subset by name, e.g. ``["loom_remember",
    "loom_recall"]``; ``max_result_chars`` caps the string each call returns
    to the model (``loom_recall`` leaves out its lowest-ranked memories whole
    to fit, counted in ``omitted``). A call Loom refuses returns its error
    message to the model instead of failing the run.
    """
    tools = AgentTools(
        agent,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        only=only,
        max_result_chars=max_result_chars,
    )
    return [_function_tool(tools, spec) for spec in tools.definitions()]


def _function_tool(tools: AgentTools, spec: dict[str, Any]) -> FunctionTool:
    name = spec["name"]

    async def invoke(_ctx: Any, arguments: str) -> str:
        try:
            return await tools.execute(name, arguments)
        except LoomError as e:
            # openai-agents fails the whole run on an exception; the model can fix its call instead.
            return f"{name} failed: {e}"

    return FunctionTool(
        name=name,
        description=spec["description"],
        params_json_schema=spec["parameters"],
        on_invoke_tool=invoke,
        # Strict mode requires every property; the optional ones (top_k, memory_type) are not.
        strict_json_schema=False,
    )
