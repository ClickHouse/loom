"""A native answer replays Google's signed tool history within the active turn."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from server.assistant.app import agent, runtime
from server.assistant.app.tools import Tools
from server.llm import agent_vertex, customer, vertex_auth
from server.settings import settings


def _sse(parts: list[dict[str, Any]], usage: dict[str, int]) -> httpx.Response:
    frames = [
        {"candidates": [{"content": {"parts": parts}}]},
        {
            "candidates": [{"finishReason": "STOP"}],
            "usageMetadata": usage,
        },
    ]
    return httpx.Response(200, text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames))


@pytest.mark.parametrize(
    ("provider_ids", "sparse_tool_usage"),
    [
        pytest.param(({}, {}), False, id="match-by-order"),
        pytest.param(({"id": "vertex-first"}, {"id": "vertex-second"}), False, id="match-by-id"),
        pytest.param(({}, {}), True, id="tool-only-usage"),
    ],
)
async def test_vertex_assistant_replays_parallel_and_sequential_calls(
    monkeypatch: pytest.MonkeyPatch,
    provider_ids: tuple[dict[str, str], dict[str, str]],
    sparse_tool_usage: bool,
) -> None:
    monkeypatch.setattr(settings, "assistant_provider", "vertex")
    monkeypatch.setattr(settings, "assistant_model", "vertex:gemini-3.1-flash-lite")
    monkeypatch.setattr(settings, "vertex_project", "test-project")
    monkeypatch.setattr(settings, "vertex_location", "eu")
    monkeypatch.setattr(settings, "assistant_max_steps", 4)
    monkeypatch.setattr(runtime, "_afford", AsyncMock())
    monkeypatch.setattr(
        vertex_auth, "headers", AsyncMock(return_value={"authorization": "Bearer test"})
    )

    tool_reads: list[tuple[str, str]] = []

    async def read_tool(self: Tools, name: str, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("memory_key", args.get("query", ""))
        tool_reads.append((name, query))
        return {"value": query}

    monkeypatch.setattr(Tools, "_request", read_tool)
    first_id, second_id = provider_ids
    parallel = [
        {"text": "private thought", "thought": True, "thoughtSignature": "dGhvdWdodA=="},
        {
            "functionCall": {
                "name": "loom_memory_get",
                "args": {"memory_key": "first"},
                **first_id,
            },
            "thoughtSignature": "Zmlyc3Q=",
        },
        {
            "functionCall": {
                "name": "loom_memory_get",
                "args": {"memory_key": "second"},
                **second_id,
            }
        },
    ]
    sequential = [
        {
            "functionCall": {"name": "loom_memory_get", "args": {"memory_key": "third"}},
            "thoughtSignature": "dGhpcmQ=",
        },
    ]
    output = "Use the deployment policy. [memory:third@v1]"
    requests: list[dict[str, Any]] = []
    normal_usage = {"promptTokenCount": 100, "candidatesTokenCount": 10}
    parallel_usage = (
        {"promptTokenCount": 100, "thoughtsTokenCount": 3} if sparse_tool_usage else normal_usage
    )
    sequential_usage = (
        {"promptTokenCount": 100, "candidatesTokenCount": 0, "thoughtsTokenCount": 5}
        if sparse_tool_usage
        else normal_usage
    )
    replies = iter(
        [
            _sse(parallel, parallel_usage),
            _sse(sequential, sequential_usage),
            _sse([{"text": output}], normal_usage),
        ]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "aiplatform.eu.rep.googleapis.com"
        assert request.url.path == (
            "/v1/projects/test-project/locations/eu/publishers/google/models/"
            "gemini-3.1-flash-lite:streamGenerateContent"
        )
        assert request.url.params["alt"] == "sse"
        requests.append(json.loads(request.content))
        return next(replies)

    state = agent.State()
    question = "What is our deployment policy?"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(agent_vertex, "get_model_client", lambda: client)
        with customer.system():
            frames = [
                frame
                async for frame in agent.run(
                    "acme",
                    "sandbox",
                    "test-token",
                    "viewer",
                    [{"role": "user", "content": question}],
                    state,
                )
            ]

    first, second, third = requests
    [tools] = first["tools"]
    [memory_get] = [fn for fn in tools["functionDeclarations"] if fn["name"] == "loom_memory_get"]
    assert "memory_key" in memory_get["parametersJsonSchema"]["properties"]
    [user_content] = first["contents"]
    assert user_content["role"] == "user"
    assert user_content["parts"][0] == {"text": question}
    host_parts = user_content["parts"][1:]
    assert len(host_parts) == 2 and all(
        p["text"].startswith("Loom retrieved context:") for p in host_parts
    )
    assert [json.loads(p["text"].split("\n", 1)[1])["tool"] for p in host_parts] == [
        "loom_memory_search",
        "loom_knowledge_search",
    ]
    host, model_content, results = second["contents"]
    assert host == user_content
    assert model_content == {"role": "model", "parts": parallel}
    assert results == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "loom_memory_get",
                    "response": {"output": {"value": key}},
                    **provider_id,
                }
            }
            for key, provider_id in zip(("first", "second"), provider_ids, strict=True)
        ],
    }
    assert third["contents"][:-2] == second["contents"]
    assert third["contents"][-2] == {"role": "model", "parts": sequential}
    assert third["contents"][-1]["parts"][0]["functionResponse"]["response"] == {
        "output": {"value": "third"}
    }
    assert tool_reads == [
        ("loom_memory_search", question),
        ("loom_knowledge_search", question),
        ("loom_memory_get", "first"),
        ("loom_memory_get", "second"),
        ("loom_memory_get", "third"),
    ]
    assert [
        choice["delta"]["content"]
        for frame in frames
        for choice in frame["choices"]
        if "content" in choice["delta"]
    ] == [output]
    output_tokens = 18 if sparse_tool_usage else 30
    assert frames[-1]["usage"] == {
        "prompt_tokens": 300,
        "completion_tokens": output_tokens,
        "total_tokens": 300 + output_tokens,
    }
