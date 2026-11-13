"""LLM provider and model resolution from the configured names and the keys present.

Pure: `server.settings` resolves the provider at load with it, and the LLM client
factory, `/v1/health` and `loom doctor` read the same tables.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# The API-key environment variable of each keyed provider.
KEY_ENV_BY_PROVIDER = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

# The model each provider runs when LOOM_LLM_MODEL is unset: the cheapest current
# model of each provider that does the extraction job well.
DEFAULT_MODEL_BY_PROVIDER = {
    "openai": "gpt-6-luna",
    "anthropic": "claude-haiku-4-5",
    # A server the operator runs (`local`): a 3B model Ollama pulls in about 2 GB that
    # follows the extraction prompt's JSON shape.
    "local": "qwen2.5:3b",
    "fake": "fake-echo-1",
}


@dataclass(frozen=True)
class ResolvedLLM:
    """The (provider, model) pair the process runs on and where it came from.

    `source` is the one phrase the boot log and `loom doctor` print:
    "LOOM_LLM_PROVIDER" when the operator set the provider (never overridden),
    otherwise the evidence the inference used — "LOOM_LLM_MODEL=gpt-6-luna",
    "OPENAI_API_KEY is set", or "nothing set" for the keyless fake default.
    """

    provider: str
    model: str
    source: str

    @property
    def inferred(self) -> bool:
        return self.source != "LOOM_LLM_PROVIDER"


def resolve_llm_provider(
    provider_env: str, model_env: str, env: Mapping[str, str] | None = None
) -> ResolvedLLM:
    """Infer the LLM provider the way the embedder infers its
    own from `LOOM_EMBEDDING_MODEL`.

    Precedence: an explicit `LOOM_LLM_PROVIDER` always wins (so `=fake` stays
    fake with a key in the environment); else the model name decides
    (`gpt*`/`o1*`/`o3*`/`o4*`/`chatgpt*` -> openai, `claude*` -> anthropic,
    `fake*` -> fake); else the key that is present decides (OpenAI before Anthropic, matching the
    calibrated default); else fake. An empty model takes the provider's
    default model. Pure: reads only its arguments and `env` (os.environ when
    None), so `Settings` and the tests call it without side effects.
    """
    e = os.environ if env is None else env
    provider = (provider_env or "").strip().lower()
    model = (model_env or "").strip()
    if provider:
        default = DEFAULT_MODEL_BY_PROVIDER.get(provider, "")
        return ResolvedLLM(provider, model or default, "LOOM_LLM_PROVIDER")
    if model:
        by_name = provider_for_model(model, "")
        if by_name:
            return ResolvedLLM(by_name, model, f"LOOM_LLM_MODEL={model}")
    for keyed, key_env in KEY_ENV_BY_PROVIDER.items():
        if e.get(key_env):
            default = DEFAULT_MODEL_BY_PROVIDER[keyed]
            return ResolvedLLM(keyed, model or default, f"{key_env} is set")
    return ResolvedLLM("fake", model or DEFAULT_MODEL_BY_PROVIDER["fake"], "nothing set")


def provider_of(model: str, main_model: str, main_provider: str) -> str:
    """The provider a job's `model` runs on: the deployment's own model on its resolved
    provider (a local server may serve a model whose name looks hosted, `gpt-oss`), any
    other model on the provider its name names, else the deployment's."""
    if model == main_model:
        return main_provider
    return provider_for_model(model, main_provider)


def provider_for_model(model: str, default: str) -> str:
    """Infer the provider from a model-override name so a stage can run on a
    DIFFERENT provider than the global LOOM_LLM_PROVIDER (e.g. opus synthesis
    while extraction stays on gpt-6-luna). `claude*` -> anthropic;
    `gpt*`/`o1..o4*`/`chatgpt*` -> openai; otherwise keep the default provider.
    """
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if m.startswith("fake"):
        return "fake"
    return default
