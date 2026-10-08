"""A read-replica's LLM client: the main Loom's, over `llm.complete`.

Chosen when a read-replica knows the main Loom's URL and has no key for the
provider a model needs (`llm_via_main`), so a replica runs with no provider
key. It reports the real provider and model, so callers treat its answers as
they would a direct client's; the exact-prompt response cache wraps it at the
call site like any other client. It opens no generation span: the main
Loom's provider client records the generation, its cost and tokens, under the
stage this call names, and a second span here would count them twice.
"""

from __future__ import annotations

from server.llm.client import LLMResult


class MainLoomLLMClient:
    def __init__(self, *, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        temperature: float | None = 0.0,
    ) -> LLMResult:
        from server.clients import main_loom
        from server.observability.genai import current_stage
        from server.settings import settings

        data = await main_loom.call(
            "llm.complete",
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "temperature": temperature,
                "model": self.model,
                "stage": current_stage(),
            },
            timeout_s=float(settings.read_replica_llm_timeout_seconds),
            idempotent=False,
        )
        return LLMResult(
            text=str(data.get("text") or ""),
            tokens_in=int(data.get("tokens_in") or 0),
            tokens_out=int(data.get("tokens_out") or 0),
            provider=str(data.get("provider") or self.provider),
            model=str(data.get("model") or self.model),
            cached_input_tokens=int(data.get("cached_input_tokens") or 0),
            cache_write_input_tokens=int(data.get("cache_write_input_tokens") or 0),
            stop_reason=str(data.get("stop_reason") or ""),
        )
