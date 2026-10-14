"""Password sign-in: an unknown account costs what a wrong password does,
and repeated failures are throttled per identifier and per address.

Hermetic: ClickHouse is faked; one account `alice@acme.io` exists.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import login, revocation
from server.auth.passwords import hash_password

pytestmark = pytest.mark.usefixtures("ended_grant_claims")

_HASH = hash_password("correct horse battery staple")
_ROW = {
    "namespace": "alice",
    "owner_email": "alice",
    "org": "acme",
    "password_hash": _HASH,
    "contact_email": "alice@acme.io",
}


class _CH:
    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "namespace_grants" in sql:
            return []
        return [_ROW] if (params or {}).get("ident") in ("alice@acme.io", "alice") else []


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login, "get_client", lambda: _CH())
    monkeypatch.setattr(revocation, "_loaded", True)

    async def no_aliases(org: str, subject: str, **_: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", no_aliases)
    login._failures_by_identifier.clear()
    login._failures_by_address.clear()


def _resolve(ident: str, password: str, address: str = "10.0.0.1") -> Any:
    return asyncio.run(login.resolve_user_credentials(ident, password, address=address))


def _median_ms(ident: str) -> float:
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        _resolve(ident, "wrong-password", address="")
        samples.append(time.perf_counter() - start)
        login._failures_by_identifier.clear()
    return sorted(samples)[2] * 1000


def test_an_unknown_account_costs_a_full_hash() -> None:
    _resolve("nobody@acme.io", "x", address="")  # warm the cached dummy hash
    known, unknown = _median_ms("alice@acme.io"), _median_ms("nobody@acme.io")
    assert unknown > known / 3, (known, unknown)  # the placeholder answered in ~1 µs


def test_the_right_password_signs_in() -> None:
    assert _resolve("Alice@acme.io ", "correct horse battery staple")[0] == "alice"


def test_ten_failures_lock_the_identifier_not_the_address() -> None:
    for i in range(10):
        assert _resolve("alice@acme.io", f"guess-{i}", address=f"10.0.0.{i}") is None
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "correct horse battery staple", address="10.9.9.9")
    assert _resolve("alice", "wrong", address="10.9.9.9") is None  # another identifier


def test_fifty_failures_lock_the_address_across_identifiers() -> None:
    for i in range(50):
        _resolve(f"user{i}@acme.io", "spray", address="203.0.113.7")
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "correct horse battery staple", address="203.0.113.7")


def test_success_does_not_count_against_the_limit() -> None:
    for _ in range(12):
        assert _resolve("alice@acme.io", "correct horse battery staple") is not None


def test_the_route_answers_429_with_retry_after() -> None:
    app = FastAPI()
    app.include_router(login.router)
    client = TestClient(app)
    for i in range(10):
        client.post("/v1/auth/login", json={"identifier": "alice@acme.io", "password": f"g{i}"})
    r = client.post("/v1/auth/login", json={"identifier": "alice@acme.io", "password": "g"})
    assert r.status_code == 429 and r.headers["retry-after"] == "900"
    assert r.json()["detail"]["code"] == "RATE_LIMITED"


def test_a_password_signs_in_to_its_own_row_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two password rows share an email (a sign-up race, another org): the
    password names the row it verifies, and the token carries that row's
    namespaces, never the other credential's."""
    theirs = {
        "namespace": "victim",
        "owner_email": "victim",
        "org": "acme",
        "password_hash": _HASH,
        "contact_email": "shared@acme.io",
    }
    mine = {
        "namespace": "mallory",
        "owner_email": "mallory",
        "org": "acme",
        "password_hash": hash_password("mallory's own password"),
        "contact_email": "shared@acme.io",
    }
    elsewhere = {**theirs, "namespace": "victim-b", "org": "other"}

    class _Shared(_CH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "namespace_grants" in sql:
                return []
            return [mine, theirs, elsewhere]

    monkeypatch.setattr(login, "get_client", lambda: _Shared())
    subject, org, namespaces, _roles = _resolve("shared@acme.io", "mallory's own password")
    assert (subject, org, namespaces) == ("mallory", "acme", ["mallory"])
    subject, org, namespaces, _roles = _resolve("shared@acme.io", "correct horse battery staple")
    assert (subject, org, namespaces) == ("victim", "acme", ["victim"])


def test_the_password_hash_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """One argon2id verify is tens of ms of CPU; on the loop it stalls every
    other request the process serves."""
    import threading

    from server.auth import passwords

    seen: list[int] = []
    real = passwords.verify_password

    def spy(stored_hash: str, password: str) -> bool:
        seen.append(threading.get_ident())
        return real(stored_hash, password)

    monkeypatch.setattr(passwords, "verify_password", spy)
    loop_thread = threading.get_ident()
    assert _resolve("alice@acme.io", "correct horse battery staple") is not None
    assert _resolve("nobody@acme.io", "x") is None
    assert len(seen) == 2 and loop_thread not in seen
