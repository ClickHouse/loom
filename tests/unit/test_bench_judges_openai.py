"""The benchmark judge builds its OpenAI request with the server's own payload helper."""

from __future__ import annotations

import asyncio
import json

import httpx

from bench import judges


def test_an_openai_judge_call_sends_the_servers_payload() -> None:
    sent: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        reply = {"choices": [{"message": {"content": "yes"}}], "usage": {"prompt_tokens": 3}}
        return httpx.Response(200, json=reply)

    async def call() -> tuple[str, dict[str, int]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await judges.achat(client, "gpt-x", "Is it?", max_tokens=8, api_key="k")

    text, usage = asyncio.run(call())
    assert (text, usage["prompt_tokens"]) == ("yes", 3)
    assert sent[0]["model"] == "gpt-x" and sent[0]["max_completion_tokens"] == 8
