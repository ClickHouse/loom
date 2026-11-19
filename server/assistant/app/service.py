"""Native assistant configuration and readiness; no durable runtime provisioning."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from server.assistant.domain.instructions import INSTRUCTIONS
from server.core import readiness
from server.core.provider_keys import provider_key
from server.llm import gateway
from server.observability import cost_accumulator
from server.settings import settings


@dataclass(frozen=True)
class Definition:
    provider: str
    model: str
    instructions: str = INSTRUCTIONS

    @property
    def price_model(self) -> str:
        return str(settings.assistant_price_model) or self.model


def current_definition() -> Definition:
    provider = str(settings.assistant_provider) or "anthropic"
    default = {"anthropic": "claude-haiku-4-5", "fake": "fake-echo-1"}.get(provider, "")
    return Definition(provider, str(settings.assistant_model) or default)


def configuration_error() -> str:
    if not settings.assistant_enabled:
        return "not_configured"
    if not settings.auth_actually_required():
        return "auth_off"
    definition = current_definition()
    if not definition.model:
        return "model_required"
    if settings.assistant_answer_ceiling_usd <= 0:
        return "budget_required"
    if not cost_accumulator.is_priced(definition.price_model):
        return "model_price_unknown"
    if definition.provider != "fake" and not any(
        cost_accumulator.PRICE_PER_1K_TOKENS[definition.price_model]
    ):
        return "model_price_unknown"
    if definition.provider in ("anthropic", "openai"):
        if not provider_key(definition.provider) and not gateway.keyless():
            return "provider_key_missing"
    if definition.provider == "bedrock":
        if not settings.assistant_bedrock_region or importlib.util.find_spec("botocore") is None:
            return "bedrock_configuration_missing"
        if settings.gateway_required() or gateway.keyless():
            return "bedrock_requires_direct_route"
    return ""


def assistant_state() -> tuple[bool | None, str]:
    if not settings.assistant_enabled:
        return None, ""
    error = configuration_error()
    return not error, error


def unready_reason() -> str:
    return configuration_error() or "failing"


def readiness_check() -> readiness.CheckResult:
    error = configuration_error()
    return readiness.result(not error, error)
