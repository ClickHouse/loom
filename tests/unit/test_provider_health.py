"""`providers_degraded` must mean "the providers are failing", not "fake ones are configured".

A flag computed as `fake_embedding or fake_llm` answers a configuration
question: during a total embedding outage (every OpenAI call 429
`insufficient_quota`, every `memory.set` 500) it would still say healthy.

The rule here is that the most recent outcome wins, with no TTL: a success clears
the flag, so recovery is reported as promptly as breakage, and a provider that is
merely idle is never called broken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.observability import provider_health as ph  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    ph.reset()
    yield
    ph.reset()


def test_unknown_provider_is_not_degraded() -> None:
    """Silence is not failure — a fresh process has called nothing yet."""
    assert ph.status()["degraded"] is False
    assert ph.status()["providers"] == {}


def test_failure_marks_degraded() -> None:
    ph.record_failure(
        "openai:embeddings", RuntimeError("429 insufficient_quota at https://gw.internal:8443/v1")
    )
    st = ph.status()
    assert st["degraded"] is True
    e = st["providers"]["openai:embeddings"]
    assert e["failing"] is True
    # `/v1/health` answers without a token: the kind, never a gateway's address.
    assert e["last_error"] == "RuntimeError"
    assert e["last_error_age_s"] >= 0


def test_success_after_failure_clears_it() -> None:
    """Recovery must be as visible as breakage, without waiting out a TTL."""
    ph.record_failure("openai:embeddings", "429")
    assert ph.status()["degraded"] is True
    ph.record_success("openai:embeddings")
    assert ph.status()["degraded"] is False
    assert ph.status()["providers"]["openai:embeddings"]["failing"] is False


def test_failure_after_success_marks_degraded_again() -> None:
    ph.record_success("openai:chat")
    ph.record_failure("openai:chat", "500")
    assert ph.status()["degraded"] is True


def test_one_failing_provider_degrades_the_verdict() -> None:
    """Embeddings can be dead while chat is fine; health must not average them."""
    ph.record_success("openai:chat")
    ph.record_failure("openai:embeddings", "429")
    st = ph.status()
    assert st["degraded"] is True
    assert st["providers"]["openai:chat"]["failing"] is False
    assert st["providers"]["openai:embeddings"]["failing"] is True


def test_error_text_is_bounded() -> None:
    """A provider error can carry a whole HTML error page; health is polled often."""
    ph.record_failure("openai:embeddings", "x" * 5000)
    assert len(ph.status()["providers"]["openai:embeddings"]["last_error"]) <= 200


def test_recording_never_raises() -> None:
    """This sits on the request path — a bug here must not fail a working call."""

    class Nasty(Exception):
        def __str__(self) -> str:
            raise ValueError("cannot stringify")

    ph.record_failure("weird", Nasty())
    ph.record_success("weird")
    ph.status()


def test_health_endpoint_consults_provider_health() -> None:
    """Structural: the flag must actually read this module, or it answers a configuration question again."""
    src = (ROOT / "server" / "health.py").read_text()
    assert "provider_health.status()" in src
    assert 'provider_status["degraded"]' in src, (
        "providers_degraded must include the observed-failure verdict, not only "
        "the fake_embedding/fake_llm configuration check"
    )


def test_outcomes_in_one_clock_tick_keep_their_order(monkeypatch) -> None:
    """A coarse clock gives both records one timestamp; the later one still wins."""
    monkeypatch.setattr(ph.time, "time", lambda: 1000.0)
    ph.record_success("openai:chat")
    ph.record_failure("openai:chat", "500")
    assert ph.status()["degraded"] is True
    ph.record_success("openai:chat")
    assert ph.status()["degraded"] is False


def test_recording_marks_the_outcome_and_ignores_a_cancellation() -> None:
    """The one wrapper every provider client puts around its call."""
    import asyncio

    with ph.recording("openai:chat"):
        pass
    assert ph.status()["providers"]["openai:chat"]["failing"] is False
    with pytest.raises(RuntimeError), ph.recording("openai:chat"):
        raise RuntimeError("503")
    assert ph.status()["providers"]["openai:chat"]["failing"] is True
    with pytest.raises(asyncio.CancelledError), ph.recording("gemini:embeddings"):
        raise asyncio.CancelledError
    assert "gemini:embeddings" not in ph.status()["providers"]


def test_every_provider_client_records_through_the_one_wrapper() -> None:
    import inspect

    from server.embedding import gemini as emb_gemini
    from server.embedding import openai as emb_openai
    from server.llm import anthropic as llm_anthropic
    from server.llm import openai as llm_openai

    for mod in (emb_openai, emb_gemini, llm_openai, llm_anthropic):
        src = inspect.getsource(mod)
        assert "recording(" in src, mod.__name__
        assert "record_failure(" not in src and "record_success(" not in src, mod.__name__


def test_a_providers_http_refusal_keeps_its_status() -> None:
    import httpx

    request = httpx.Request("POST", "https://gw.internal:8443/v1/embeddings")
    refused = httpx.HTTPStatusError(
        "Client error '429' for url 'https://gw.internal:8443/v1/embeddings'",
        request=request,
        response=httpx.Response(429, request=request),
    )
    ph.record_failure("openai:embeddings", refused)
    assert ph.status()["providers"]["openai:embeddings"]["last_error"] == (
        "HTTPStatusError (HTTP 429)"
    )


def test_a_loom_error_is_shown_by_its_code_alone() -> None:
    """`/v1/health` answers without a token, and a Loom error's message can name a
    host (a gateway refusal names the one it refused)."""
    from server.core.errors import GatewayRequiredError
    from server.observability import degradations

    ph.record_failure("openai:chat", GatewayRequiredError("gw.internal is not listed"))
    assert ph.status()["providers"]["openai:chat"]["last_error"] == "GATEWAY_REQUIRED"
    degradations.note("rerank", "input_order_kept", GatewayRequiredError("gw.internal x"))
    assert ph.status()["providers"]["rerank"]["last_error"] == "GATEWAY_REQUIRED"


def test_a_text_reason_stays_on_the_note_and_health_shows_the_effect() -> None:
    """A classifier's answer is a tenant's remote text: the request's own note may
    quote it, `/v1/health` (no token) shows only what the response did instead."""
    from server.observability import degradations

    degradations.note("classify", "caller_type_kept", "'secret-answer' not a registered type")
    assert ph.status()["providers"]["classify"]["last_error"] == "caller_type_kept"
