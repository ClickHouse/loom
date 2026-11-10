"""`/v1/health` answers anyone, so its `code_tier_failing` problem names each failing
entry that is not restricted (repository@branch, org and namespace) and its error
only to a platform admin's token without per-namespace roles; any other caller, or a token that does not verify, reads the reasons and fixes
without the entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server import boot
from server.auth.jwt import mint_token
from server.code.app.index import tier_health as recorder
from server.code.domain.index import tier_health

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LOCKED = (
    "ToolOutputError: git checkout failed: fatal: Unable to create "
    "'/srv/repos/checkout-a/.git/index.lock': File exists."
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import server.main as main_module
    from server.observability import provider_health

    class _StubCH:
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"one": 1}

    monkeypatch.setattr("server.core.ch.get_client", lambda: _StubCH())
    provider_health.reset()
    monkeypatch.setattr(boot, "SKIPPED_PENDING_MIGRATIONS", ())
    health = None
    for i in range(4):
        health = tier_health.failed(health, "clang_full", LOCKED, T0 + timedelta(minutes=30 * i))
    assert health is not None
    recorder._FAILING = (recorder.FailingTier("o", "code-a", health, 0, "o/a", "main", False),)
    yield TestClient(main_module.app)
    recorder.reset_for_tests()


def _tier_message(client: TestClient, headers: dict[str, str]) -> tuple[str, str | None]:
    r = client.get("/v1/health", headers=headers)
    assert r.status_code == 200
    (p,) = [p for p in r.json()["problems"] if p["code"] == "code_tier_failing"]
    return str(p["message"]), r.headers.get("Cache-Control")


def _bearer(roles: list[str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {mint_token(subject='ops', org='o', namespaces=['*'], roles=roles)}"
    }


def test_a_platform_admin_reads_each_entry_and_its_error(client: TestClient) -> None:
    message, cache = _tier_message(client, _bearer(["platform_admin"]))
    assert "o/a@main (o/code-a) clang_full: git lock held, 4 failed passes" in message
    assert '"ToolOutputError: git checkout failed' in message
    assert cache == "no-store"  # the answer names entries: no shared cache keeps it


def test_a_scoped_token_is_no_platform_admin(client: TestClient) -> None:
    token = mint_token(
        subject="ops",
        org="o",
        namespaces=["*"],
        roles=["platform_admin"],
        ns_roles={"code-a": "admin"},
    )
    message, _ = _tier_message(client, {"Authorization": f"Bearer {token}"})
    assert "code-a" not in message


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-token"}, "writer"],
    ids=["anonymous", "bad-token", "writer"],
)
def test_any_other_caller_reads_the_reason_without_the_entry(
    client: TestClient, headers: Any
) -> None:
    message, cache = _tier_message(client, _bearer(["writer"]) if headers == "writer" else headers)
    assert "clang_full: git lock held, 4 failed passes" in message
    assert cache != "no-store"
    assert not any(s in message for s in ("o/a", "code-a", "/srv", "ToolOutputError"))


def test_an_admin_token_that_cannot_be_checked_reads_no_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the revocation list is not loaded: the token may be revoked, so it names nothing
    monkeypatch.setattr("server.auth.revocation.loaded", lambda: False)
    message, cache = _tier_message(client, _bearer(["platform_admin"]))
    assert "code-a" not in message and cache != "no-store"
