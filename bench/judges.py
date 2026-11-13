"""One chat call for bench readers and judges, on either provider.

The request is shaped the way Loom's own clients shape it: `server/llm/openai.py`
and `server/llm/anthropic.py` build the payload, and `server/llm/traits.py` learns
each model's refused fields, lowest reasoning effort, output limit and the room
its hidden reasoning needs from the provider's answers; there is no model list here.
The HTTP call itself is the harness's: its client (so tests can inject a
transport) and its retry policy (this sends one request per learned shape and
raises on an HTTP error).

A model named `claude*` goes to the Anthropic Messages API (ANTHROPIC_API_KEY);
every other name to an OpenAI-compatible endpoint (OPENAI_API_KEY,
`OPENAI_BASE_URL`). The reply comes back with an OpenAI-shaped usage block
(`prompt_tokens`, `completion_tokens`) for both providers.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from server.llm import anthropic as _anthropic
from server.llm import openai as _openai
from server.llm import traits

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def is_claude(model: str) -> bool:
    return model.startswith("claude")


def openai_chat_url() -> str:
    base = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    return base.rstrip("/") + "/chat/completions"


async def achat(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str = "",
    timeout_s: float = 120.0,
    api_key: str = "",
) -> tuple[str, dict[str, int]]:
    """One completion: (reply text, {"prompt_tokens", "completion_tokens"}).
    An HTTP error the model's traits do not explain raises `httpx.HTTPStatusError`."""
    claude = is_claude(model)
    if claude:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        url = ANTHROPIC_URL
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}

        def build() -> dict[str, Any]:
            return _anthropic._payload(model, system, prompt, max_tokens, 0.0)

        learn, starved = _anthropic._learn_from_refusal, _anthropic._thinking_starved
        hidden, cap_field = _anthropic._thinking_tokens, "max_tokens"
    else:
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        url = openai_chat_url()
        headers = {"Authorization": f"Bearer {key}"}

        def build() -> dict[str, Any]:
            return _openai._payload(
                model, system, prompt, max_tokens, None, 0.0, "", "max_completion_tokens"
            )

        learn, starved = _openai._learn_from_refusal, _openai._starved
        hidden, cap_field = _openai._reasoning_tokens, ""

    payload = build()
    r = await client.post(url, headers=headers, json=payload, timeout=timeout_s)
    for _ in range(traits.MAX_NEGOTIATION):
        if not learn(model, r, payload):
            break
        payload = build()
        r = await client.post(url, headers=headers, json=payload, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    spent = hidden(data)
    traits.note_hidden(model, spent)
    if starved(data):
        field = cap_field or next(
            k for k in ("max_completion_tokens", "max_tokens") if k in payload
        )
        payload[field] = traits.starved_cap(model, int(payload[field]), spent)
        r = await client.post(url, headers=headers, json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
    if claude:
        text = "".join(
            b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
        )
        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": int(u.get("input_tokens") or 0),
            "completion_tokens": int(u.get("output_tokens") or 0),
        }
    else:
        text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
        }
    return text.strip(), usage
