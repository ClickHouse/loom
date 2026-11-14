"""A chat-completions server the operator runs (Ollama, llama.cpp's server, vLLM).

The request and reply are OpenAI's chat-completions shape, so this is the OpenAI client
pointed at `LOOM_LOCAL_LLM_BASE_URL`, sending no key. Its calls cost nothing in the cost
ledger, which lists no price for the model.
"""

from __future__ import annotations

from server.llm import gateway
from server.llm.openai import OpenAILLMClient


class LocalLLMClient(OpenAILLMClient):
    provider = "local"
    _source = "local:chat"
    # Ollama ignores max_completion_tokens without refusing it, so the cap goes as
    # max_tokens, which every chat-completions server reads.
    _cap_name = "max_tokens"
    _needs_key = False

    def _root(self) -> str:
        from server.settings import settings

        return settings.local_llm_base_url

    async def _headers(self, url: str) -> dict[str, str]:
        # No key of its own: behind an inference gateway the workload's token, which
        # also refuses a host off the gateway; otherwise no bearer at all.
        bearer = await gateway.bearer("", url)
        return {"Authorization": f"Bearer {bearer}"} if bearer else {}
