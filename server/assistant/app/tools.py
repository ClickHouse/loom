"""Answer-owned calls through the existing authenticated MCP transport."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from server.core import shielded
from server.core.errors import AssistantUpstreamError, AuthInsufficientError
from server.observability.cost_accumulator import CostWatch, watch_costs
from server.operations.toolsets import (
    ASSISTANT_DROPPED_ARGS,
    ASSISTANT_FORCED_ARGS,
    assistant_ops,
)
from server.settings import settings

_app: Any = None

_INPUTS = frozenset(
    {
        "query",
        "question",
        "memory_key",
        "name",
        "symbol_id",
        "path",
        "section",
        "anchor",
        "source_uri",
        "sql",
        "top_k",
        "limit",
        "agent",
        "include_body",
        "include_history",
        "include_examples",
    }
)


def set_app(app: Any) -> None:
    global _app
    _app = app


@dataclass
class Receipt:
    op: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    origin: str
    duration_ms: int
    cost_usd: float = 0.0


class Tools:
    def __init__(self, org: str, namespace: str, token: str, run_id: str) -> None:
        self.org, self.namespace, self.token, self.run_id = org, namespace, token, run_id
        self.operations = {op.mcp_tool_name: op for op in assistant_ops()}
        self.receipts: list[Receipt] = []
        self.seen: set[str] = set()

    def descriptors(self, names: set[str]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for name, op in self.operations.items():
            if op.name not in names:
                continue
            schema = op.input_model.model_json_schema()
            hidden = {
                "org",
                "namespace",
                "consumer",
                *ASSISTANT_DROPPED_ARGS,
                *ASSISTANT_FORCED_ARGS.get(op.name, {}),
            }
            # Present the task's useful inputs, without provider overrides or
            # secondary synthesis knobs. Validation still uses the registry model.
            schema["properties"] = {
                k: v
                for k, v in schema.get("properties", {}).items()
                if k not in hidden and k in _INPUTS
            }
            schema["required"] = [
                k for k in schema.get("required", []) if k in schema["properties"]
            ]
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": (op.handler.__doc__ or op.name).strip(),
                        "parameters": schema,
                    },
                }
            )
        return tools

    async def call(self, name: str, raw: dict[str, Any], *, origin: str) -> Receipt:
        op = self.operations.get(name)
        if op is None:
            raise AuthInsufficientError("this answer cannot call that tool")
        hidden = {
            "org",
            "namespace",
            "consumer",
            *ASSISTANT_DROPPED_ARGS,
            *ASSISTANT_FORCED_ARGS.get(op.name, {}),
        }
        if set(raw) - _INPUTS - hidden:
            raise AssistantUpstreamError("a tool requested an input outside its bounded schema")
        if len(self.receipts) >= settings.assistant_max_tool_calls:
            raise AssistantUpstreamError("the answer exceeded its tool budget")
        if any(
            k in raw and raw[k] != v for k, v in (("org", self.org), ("namespace", self.namespace))
        ):
            raise AuthInsufficientError("a tool cannot change this answer's scope")
        args = {k: v for k, v in raw.items() if k not in ASSISTANT_DROPPED_ARGS}
        args.update(ASSISTANT_FORCED_ARGS.get(op.name, {}))
        args.update(
            {
                k: v
                for k, v in (("org", self.org), ("namespace", self.namespace))
                if k in op.input_model.model_fields
            }
        )
        dedup = args
        if op.name.endswith(".search") and isinstance(args.get("query"), str):
            dedup = {"query": " ".join(args["query"].casefold().split())}
        signature = json.dumps([op.name, dedup], sort_keys=True, separators=(",", ":"))
        if signature in self.seen:
            raise AssistantUpstreamError("the answer repeated a completed tool request")
        self.seen.add(signature)
        started = time.monotonic()

        # ASGI requests bind fresh auth, work and cost context. A child task prevents
        # their ContextVars replacing those of the answer after the request returns.
        async def requested() -> tuple[dict[str, Any], CostWatch]:
            with watch_costs() as watch:
                result = await self._request(name, args)
            return result, watch

        task = asyncio.create_task(requested())
        try:
            result, watch = await task
        finally:

            async def joined() -> None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            await shielded.to_its_end(joined(), name="assistant.tool.close")
        if watch.unknown:
            raise AssistantUpstreamError("an assistant tool used an unpriced model")
        receipt = Receipt(
            op.name, args, result, origin, int((time.monotonic() - started) * 1000), watch.cost_usd
        )
        self.receipts.append(receipt)
        return receipt

    async def _request(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app, raise_app_exceptions=False),
            base_url="http://loom",
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Loom-Run-Id": self.run_id,
                "Accept": "application/json, text/event-stream",
            },
        ) as client:
            response = await client.post(
                "/mcp?toolset=assistant",
                json={
                    "jsonrpc": "2.0",
                    "id": len(self.receipts) + 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
        if response.status_code != 200:
            raise AssistantUpstreamError("an assistant tool was refused")
        if len(response.content) > settings.assistant_max_tool_result_chars * 4:
            raise AssistantUpstreamError("a tool exceeded its response bound")
        try:
            envelope = response.json()
            result = envelope["result"]
            if result.get("isError") or envelope.get("error"):
                raise AssistantUpstreamError("an assistant tool failed or was refused")
            structured = result["structuredContent"]
            if not isinstance(structured, dict):
                raise ValueError("not an object")
            if len(json.dumps(structured)) > settings.assistant_max_tool_result_chars:
                raise AssistantUpstreamError("a tool exceeded its response bound")
            return structured
        except (ValueError, TypeError, KeyError) as e:
            raise AssistantUpstreamError("an assistant tool returned a malformed result") from e
