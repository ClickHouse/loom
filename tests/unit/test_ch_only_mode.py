"""Tests for the CH-only deployment tier.

These pin two contracts the CH-only positioning depends on:

  1. `/v1/health` reports a structured `mode` token that names the
     active integrations (`ch`, `ch+langfuse`, `ch+clickstack`, etc.).
     Operators and the dashboard render this to know the tier.

  2. `prompts.evaluate` returns 501 INTEGRATION_NOT_CONFIGURED — not
     503 BACKEND_UNAVAILABLE — when Langfuse is unconfigured. 503
     reads as "backend is down, retry later"; the real semantic is
     "this deployment did not enable that integration."

A regression on either contract would silently break the headline
"Loom runs on ClickHouse alone" story.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import boot
from server.insights.app.admin_routes import admin_router

# ----------------------------------------------------------------------
# /v1/health mode field
# ----------------------------------------------------------------------


@pytest.fixture
def health_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Health endpoint with a stubbed CH client. The fixture sets the
    integration env to unset by default; individual tests opt in."""
    import server.main as main_module
    from server.settings import settings

    class _StubCH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"one": 1}

    monkeypatch.setattr(
        "server.core.ch.get_client",
        lambda: _StubCH(),
    )
    # `status` reads the provider breaker and the boot
    # snapshot of pending migrations; a failure another test recorded
    # (the classify handler's, for one) must not reach this assertion.
    from server.observability import provider_health

    provider_health.reset()
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ())
    monkeypatch.setattr("server.embedding.embedder._probed_dim", None)
    # Default: pure CH-only — every integration off.
    monkeypatch.setattr(settings, "langfuse_url", "")
    monkeypatch.setattr(settings, "langfuse_admin_token", "")
    monkeypatch.setattr(settings, "clickstack_database", "")
    monkeypatch.setattr(settings, "otel_exporter", "")

    return TestClient(main_module.app)


def test_health_mode_ch_only_when_nothing_configured(
    health_client: TestClient,
) -> None:
    """No Langfuse or ClickStack env means mode is ch."""
    r = health_client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "ch"


def test_health_mode_includes_langfuse_when_configured(
    health_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "langfuse_url", "https://langfuse.example.com")
    monkeypatch.setattr(settings, "langfuse_admin_token", "pk-x:sk-y")

    r = health_client.get("/v1/health")
    body = r.json()
    parts = set(body["mode"].split("+"))
    assert "ch" in parts
    assert "langfuse" in parts


def test_health_langfuse_carries_the_score_and_judge_counters(
    health_client: TestClient,
) -> None:
    body = health_client.get("/v1/health?include=langfuse").json()["langfuse"]
    assert {"posted", "failed", "dropped_queue_full", "dropped_breaker_open"} <= set(body["scores"])
    assert {"judged", "dropped_queue_full"} <= set(body["retrieval_judge"])
    assert "dropped_shutdown" in body["scores"] and "dropped_shutdown" in body["retrieval_judge"]


def test_health_events_carries_the_writers_loss_counters(health_client: TestClient) -> None:
    body = health_client.get("/v1/health?include=events").json()["events"]
    assert set(body["agent_events"]) == {
        "queued",
        "dropped_queue_full",
        "dropped_flush_failed",
        "dropped_batch_repeat",
        "batch_repeats",
    }
    assert set(body["stream"]) == {"queued", "dropped"}


def test_health_mode_includes_clickstack_when_otel_or_db_set(
    health_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either clickstack_database (for the erasure leg) or an OTel
    exporter (for span emission) marks the deployment as clickstack."""
    from server.settings import settings

    monkeypatch.setattr(settings, "clickstack_database", "default")

    r = health_client.get("/v1/health")
    parts = set(r.json()["mode"].split("+"))
    assert "clickstack" in parts


def test_health_mode_full_tier(
    health_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All integrations on → mode contains every leg, ch first."""
    from server.settings import settings

    monkeypatch.setattr(settings, "langfuse_url", "https://lf")
    monkeypatch.setattr(settings, "langfuse_admin_token", "x:y")
    monkeypatch.setattr(settings, "clickstack_database", "default")

    r = health_client.get("/v1/health")
    parts = r.json()["mode"].split("+")
    # CH always leads; rest are appended in fixed order.
    assert parts[0] == "ch"
    assert set(parts) == {"ch", "langfuse", "clickstack"}


def test_health_mode_partial_langfuse_does_not_count(
    health_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL without token (or token without URL) is misconfigured, not
    configured — mode must not claim the integration is active."""
    from server.settings import settings

    monkeypatch.setattr(settings, "langfuse_url", "https://lf")
    # admin_token left empty

    r = health_client.get("/v1/health")
    parts = set(r.json()["mode"].split("+"))
    assert "langfuse" not in parts


# ----------------------------------------------------------------------
# prompts.evaluate 501 contract
# ----------------------------------------------------------------------


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Admin router with Langfuse explicitly unconfigured.

    Auth off: the router-wide bearer+ADMIN gate has its own coverage in
    tests/unit/test_transport_security.py; these tests target the
    handlers behind it."""
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "langfuse_url", "")
    monkeypatch.setattr(settings, "langfuse_admin_token", "")

    app = FastAPI()
    app.include_router(admin_router)
    return TestClient(app)


def test_prompts_evaluate_501_when_langfuse_unconfigured(
    admin_client: TestClient,
) -> None:
    """The CH-only deployment must not 503 on prompts.evaluate. 503
    triggers transient-error retry; this is a permanent "feature not
    enabled" signal, so 501 INTEGRATION_NOT_CONFIGURED is correct."""
    r = admin_client.post(
        "/v1/admin/prompts.evaluate",
        json={
            "prompt_name": "system",
            "prompt_version": 1,
            "rubric": "correctness",
            "min_evals": 3,
            "threshold": 0.7,
        },
    )
    assert r.status_code == 501, r.text
    body = r.json()
    # FastAPI wraps HTTPException.detail under "detail".
    detail = body.get("detail", body)
    assert detail.get("code") == "INTEGRATION_NOT_CONFIGURED"
    assert "langfuse" in detail.get("message", "").lower()


# ----------------------------------------------------------------------
# /v1/admin/workers.status disabled_reason surface
# ----------------------------------------------------------------------


def test_workers_status_marks_disabled_by_setting(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker the operator chose to turn off must say so, distinct
    from `integration_not_configured`."""
    from server.settings import settings

    monkeypatch.setattr(settings, "profile_consolidation_enabled", False)

    r = admin_client.get("/v1/admin/workers.status")
    fold = r.json()["workers"]["profile_consolidation"]
    assert fold["enabled"] is False
    assert fold["effective"] is False
    assert fold["disabled_reason"] == "disabled_by_setting"


def test_workers_status_includes_keepers(
    admin_client: TestClient,
) -> None:
    """The status surface must cover every worker the registry knows
    about."""
    r = admin_client.get("/v1/admin/workers.status")
    names = set(r.json()["workers"].keys())
    must_have = {
        # Default-on (no extra credentials)
        "embedding_worker",
        "audit_verifier",
        "erasure_recovery",
        "projection_repair",
        # Default-off (require LLM credentials or opt-in by design)
        "entity_extractor",
    }
    missing = must_have - names
    assert not missing, f"workers.status omits: {missing}"


# ----------------------------------------------------------------------
# The status code says whether the data plane can serve
# ----------------------------------------------------------------------


def test_health_is_200_when_clickhouse_answers(health_client: TestClient) -> None:
    r = health_client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["ch_ok"] is True and r.json()["status"] == "ok"


def test_health_is_503_when_clickhouse_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that reads only the status code must see the outage: a degraded
    service answers /v1/health with a non-200 status, not 200 + `degraded` in
    the body. The body names the error kind; its text is only logged."""
    import server.main as main_module

    class _DownCH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            raise OSError(24, "Too many open files")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _DownCH())
    r = TestClient(main_module.app).get("/v1/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ch_ok"] is False and body["status"] == "degraded"
    assert body["ch_error"] == "OSError"


def test_health_names_the_migrations_a_skipping_process_lacks(
    health_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/v1/health carries the boot-migrate switch and
    the migration files the service lacked when this process skipped the step."""

    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ("800_namespace_classification.sql",))
    body = health_client.get("/v1/health").json()
    assert body["pending_migrations"] == ["800_namespace_classification.sql"]
    assert isinstance(body["boot_migrate"], bool)
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ())
    assert health_client.get("/v1/health").json()["pending_migrations"] == []
