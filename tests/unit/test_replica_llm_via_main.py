"""A read-replica with no provider key completes through the main Loom's
`llm.complete`, called with its own service token; the op serves only it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.clients import main_loom
from server.llm import client as llm
from server.llm.main_loom import MainLoomLLMClient
from server.settings import settings

MAIN = "https://main.loom.test"


@pytest.fixture
def keyless_replica(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import server.core.provider_keys as pk

    monkeypatch.setattr(settings, "read_replica", True)
    monkeypatch.setattr(settings, "read_replica_main_url", MAIN)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-6-luna")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    monkeypatch.setattr(pk, "provider_key", lambda provider: "")
    llm.reset()
    yield
    llm.reset()


@pytest.mark.usefixtures("keyless_replica")
def test_a_keyless_replica_completes_through_the_main_loom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.provider_keys as pk

    got = llm.get_llm_client()
    assert isinstance(got, MainLoomLLMClient)
    assert (got.provider, got.model) == ("openai", "gpt-6-luna")
    # A stage override routes the same way, under its own model.
    over = llm.get_llm_client_for_model("claude-haiku-4-5")
    assert isinstance(over, MainLoomLLMClient) and over.provider == "anthropic"
    # Not a fallback to the fake: diagnostics see a working provider.
    assert not llm.provider_key_missing("openai")
    # Its own key wins; without the URL the fake fallback is still reported.
    monkeypatch.setattr(pk, "provider_key", lambda provider: "sk-test")
    assert not llm.llm_via_main("openai")
    monkeypatch.setattr(pk, "provider_key", lambda provider: "")
    monkeypatch.setattr(settings, "read_replica_main_url", "")
    assert not llm.llm_via_main("openai") and llm.provider_key_missing("openai")


async def test_the_replica_client_sends_the_call_and_returns_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any], float | None]] = []

    async def fake_call(
        op: str, body: dict[str, Any], *, timeout_s: float | None = None, idempotent: bool = True
    ) -> dict[str, Any]:
        assert not idempotent, "a completion must never be retried after it was sent"
        sent.append((op, body, timeout_s))
        return {
            "text": '{"answer": 1}',
            "tokens_in": 120,
            "tokens_out": 8,
            "provider": "openai",
            "model": "gpt-6-luna",
            "cached_input_tokens": 100,
            "stop_reason": "stop",
        }

    monkeypatch.setattr(main_loom, "call", fake_call)
    monkeypatch.setattr(settings, "read_replica_llm_timeout_seconds", 240.0)
    out = await MainLoomLLMClient(provider="openai", model="gpt-6-luna").complete(
        "sys", "usr", max_tokens=64, response_format={"type": "json_object"}, temperature=0.0
    )
    assert (out.text, out.tokens_in, out.tokens_out, out.cached_input_tokens) == (
        '{"answer": 1}',
        120,
        8,
        100,
    )
    assert out.stop_reason == "stop" and not out.from_cache
    ((op, body, timeout),) = sent
    assert op == "llm.complete" and timeout == 240.0
    assert body == {
        "system": "sys",
        "user": "usr",
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "model": "gpt-6-luna",
        "stage": "other",
    }


@pytest.fixture
def main_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "read_replica", False)
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_model", "fake-echo-1")
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    llm.reset()
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    yield TestClient(app)
    llm.reset()


def _bearer(subject: str, *, service: bool = True) -> dict[str, str]:
    token = mint_token(
        subject=subject,
        org="dev",
        namespaces=[],
        roles=["viewer"],
        ttl_seconds=300,
        is_service_account=service,
    )
    return {"Authorization": f"Bearer {token}"}


def test_the_main_loom_completes_for_a_replica_token(main_client: TestClient) -> None:
    r = main_client.post(
        "/v1/llm.complete",
        json={"system": "Answer briefly.", "user": "Which index?", "max_tokens": 32},
        headers=_bearer("loom-replica:1234"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "fake" and isinstance(body["text"], str)


@pytest.mark.parametrize(
    ("subject", "service"),
    [("agent-x", True), ("loom-replica:1234", False)],
    ids=["other-service-account", "user-token-with-replica-name"],
)
def test_the_main_loom_refuses_every_other_llm_caller(
    main_client: TestClient, subject: str, service: bool
) -> None:
    r = main_client.post(
        "/v1/llm.complete",
        json={"system": "s", "user": "spend someone else's budget"},
        headers=_bearer(subject, service=service),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"


def test_llm_complete_is_not_an_mcp_tool() -> None:
    from server.operations.reach import mcp_reach

    assert mcp_reach("llm.complete") == set()


@pytest.mark.usefixtures("keyless_replica")
def test_a_keyless_replica_reports_no_llm_key_problem() -> None:
    from server.observability.problems import HealthState, collect_problems

    codes = {p.code for p in collect_problems(settings, HealthState(env={}))}
    assert "llm_key_missing" not in codes and "embedder_key_missing" not in codes
