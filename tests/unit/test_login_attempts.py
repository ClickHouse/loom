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

from server.auth import attempts, login, revocation
from server.auth.passwords import hash_password

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
def _env(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any) -> None:
    monkeypatch.setattr(login, "get_client", lambda: _CH())
    monkeypatch.setattr(revocation, "_loaded", True)

    async def no_aliases(org: str, subject: str, **_: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", no_aliases)


def _resolve(ident: str, password: str, address: str = "10.0.0.1") -> Any:
    return asyncio.run(login.resolve_user_credentials(ident, password, address=address))


def _median_ms(ident: str, attempts: Any) -> float:
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        _resolve(ident, "wrong-password", address="")
        samples.append(time.perf_counter() - start)
        attempts.rows.clear()
    return sorted(samples)[2] * 1000


def test_an_unknown_account_costs_a_full_hash(auth_attempts: Any) -> None:
    _resolve("nobody@acme.io", "x", address="")  # warm the cached dummy hash
    known = _median_ms("alice@acme.io", auth_attempts)
    unknown = _median_ms("nobody@acme.io", auth_attempts)
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


def test_an_ipv6_net_counts_as_one_address() -> None:
    # One host holds a whole /64 to spread a spray over.
    for i in range(50):
        _resolve(f"user{i}@acme.io", "spray", address=f"2001:db8::{i:x}")
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "correct horse battery staple", address="2001:db8::ffff")


def test_failures_are_counted_by_kind_and_keyed_hash(auth_attempts: Any) -> None:
    """The rows every process counts: one per failure under each limit, the
    identifier and the address stored as an HMAC keyed by the root secret —
    not as written, and not as a plain sha256 anyone can recompute."""
    import hashlib
    import hmac

    from server.settings import settings

    _resolve("Alice@acme.io", "wrong", address="203.0.113.7")
    key = hashlib.sha256(b"loom.auth_attempts:" + settings.jwt_secret.encode()).digest()
    assert sorted((r["kind"], r["key_sha256"]) for r in auth_attempts.rows) == [
        ("login.address", hmac.new(key, b"203.0.113.7", hashlib.sha256).hexdigest()),
        ("login.identifier", hmac.new(key, b"alice@acme.io", hashlib.sha256).hexdigest()),
    ]
    plain = {
        hashlib.sha256(b"203.0.113.7").hexdigest(),
        hashlib.sha256(b"alice@acme.io").hexdigest(),
    }
    assert not plain & {r["key_sha256"] for r in auth_attempts.rows}
    assert "alice" not in repr(auth_attempts.rows) and "203.0.113.7" not in repr(auth_attempts.rows)


def test_the_lock_lifts_when_the_failures_leave_the_window(auth_attempts: Any) -> None:
    for i in range(10):
        _resolve("alice@acme.io", f"guess-{i}")
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "correct horse battery staple")
    auth_attempts.now += 901
    assert _resolve("alice@acme.io", "correct horse battery staple") is not None


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


@pytest.mark.parametrize("seed", range(5))
def test_a_concurrent_burst_reaches_the_password_check_at_most_limit_times(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """50 concurrent guesses against a limit of 10, every ClickHouse call
    taking a random few ms so counts and records interleave: at most 10 of
    them reach the password check, and the identifier is locked after."""
    import random

    rng = random.Random(seed)
    insert, query_all = auth_attempts.insert, auth_attempts.query_all

    async def slow_insert(*a: Any, **k: Any) -> None:
        await asyncio.sleep(rng.uniform(0, 0.01))
        await insert(*a, **k)

    async def slow_query_all(*a: Any, **k: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(rng.uniform(0, 0.01))
        return await query_all(*a, **k)

    monkeypatch.setattr(auth_attempts, "insert", slow_insert)
    monkeypatch.setattr(auth_attempts, "query_all", slow_query_all)
    checked = 0
    real_check = login._check_credentials

    async def counting_check(ident: str, password: str) -> Any:
        nonlocal checked
        checked += 1
        return await real_check(ident, password)

    monkeypatch.setattr(login, "_check_credentials", counting_check)

    async def burst() -> list[Any]:
        guesses = [
            login.resolve_user_credentials("alice@acme.io", f"g{i}", address=f"10.1.0.{i}")
            for i in range(50)
        ]
        return await asyncio.gather(*guesses, return_exceptions=True)

    results = asyncio.run(burst())
    assert checked <= 10, checked
    assert sum(isinstance(r, login.LoginThrottled) for r in results) == 50 - checked
    # The refused ones never reached the check, so they do not count: the
    # identifier takes the rest of its 10 failures, then locks.
    burst_checked = checked
    # A key refused during the burst is refused here without a query for a few seconds.
    auth_attempts.now += login.attempts._KNOWN_OVER_S
    for i in range(10 - burst_checked):
        assert _resolve("alice@acme.io", f"late-{i}") is None
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "correct horse battery staple")
    assert checked == 10


def test_a_burst_from_one_address_reaches_the_check_at_most_its_limit(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The address limit is gated the same way: a spray of 80 concurrent
    guesses at distinct accounts from one address checks at most 50."""
    insert, query_all = auth_attempts.insert, auth_attempts.query_all

    async def slow_insert(*a: Any, **k: Any) -> None:
        await asyncio.sleep(0.005)
        await insert(*a, **k)

    async def slow_query_all(*a: Any, **k: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0.005)
        return await query_all(*a, **k)

    monkeypatch.setattr(auth_attempts, "insert", slow_insert)
    monkeypatch.setattr(auth_attempts, "query_all", slow_query_all)
    checked = 0

    async def no_account(ident: str, password: str) -> None:
        nonlocal checked
        checked += 1

    monkeypatch.setattr(login, "_check_credentials", no_account)

    async def spray() -> list[Any]:
        guesses = [
            login.resolve_user_credentials(f"u{i}@acme.io", "spray", address="203.0.113.9")
            for i in range(80)
        ]
        return await asyncio.gather(*guesses, return_exceptions=True)

    asyncio.run(spray())
    assert checked <= 50, checked


def test_a_success_from_an_address_does_not_buy_more_guesses() -> None:
    """A sign-in to one's own account cancels its own attempt only."""
    for i in range(49):
        _resolve(f"user{i}@acme.io", "spray", address="203.0.113.7")
    for _ in range(3):
        assert _resolve("alice@acme.io", "correct horse battery staple", address="203.0.113.7")
    assert _resolve("user99@acme.io", "spray", address="203.0.113.7") is None
    with pytest.raises(login.LoginThrottled):
        _resolve("user98@acme.io", "spray", address="203.0.113.7")


def test_a_refused_signup_is_not_recorded_so_a_retrying_caller_gets_back_in(
    auth_attempts: Any,
) -> None:
    from server.auth.attempts import Attempts

    signups = Attempts("signup.agent", 3, 3600)
    assert [asyncio.run(signups.allow("10.0.0.9")) for _ in range(5)] == [True] * 3 + [False] * 2
    assert len(auth_attempts.rows) == 3
    auth_attempts.now += 3601
    assert asyncio.run(signups.allow("10.0.0.9"))


def test_a_key_known_over_its_limit_is_refused_without_a_query(auth_attempts: Any) -> None:
    """A refused sign-in used to cost a count query each; a spray at a locked
    key now costs none until the remembered refusal lapses."""
    from server.auth import attempts

    for i in range(10):
        _resolve("alice@acme.io", f"guess-{i}")
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "x")
    queries = 0
    query_all = auth_attempts.query_all

    async def counting(*a: Any, **k: Any) -> list[dict[str, Any]]:
        nonlocal queries
        queries += 1
        return await query_all(*a, **k)

    auth_attempts.query_all = counting  # type: ignore[method-assign]
    for _ in range(20):
        with pytest.raises(login.LoginThrottled):
            _resolve("alice@acme.io", "x")
    assert queries == 0
    auth_attempts.now += attempts._KNOWN_OVER_S + 1
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "x")
    assert queries == 1


def test_attempts_are_written_as_waiting_async_inserts(auth_attempts: Any) -> None:
    """One part per attempt let a spray of sign-ins flood the shared server
    with parts; a waiting async insert still lands before the recount."""
    _resolve("alice@acme.io", "wrong")
    assert auth_attempts.settings == {"async_insert": 1, "wait_for_async_insert": 1}


def test_every_count_reads_with_sequential_consistency(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain read on another replica misses attempts it acknowledged
    seconds ago: the count and the recount must both be sequential."""
    from server.auth import attempts
    from server.core.ch import sequential_reads_active

    seen: list[bool] = []
    count = auth_attempts.query_all

    async def recording(sql: str, params: Any = None) -> list[dict[str, Any]]:
        seen.append(sequential_reads_active())
        return await count(sql, params)

    monkeypatch.setattr(auth_attempts, "query_all", recording)
    _resolve("alice@acme.io", "wrong")
    assert asyncio.run(attempts.Attempts("signup", 3, 60).allow("10.0.0.1"))
    assert seen == [True] * 4  # first count and recount, per path


def test_a_full_process_answers_busy_and_records_nothing(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past `auth_attempts_concurrency` checks in flight, a sign-in waits a
    moment and is refused 429 with a short Retry-After, before it costs
    ClickHouse anything; the slots it waited on still finish."""
    from server.auth import attempts

    monkeypatch.setattr(attempts, "_SLOT_WAIT_S", 0.05)
    from server.settings import settings

    monkeypatch.setattr(settings, "auth_attempts_concurrency", 2)
    gate = asyncio.Event()
    real_check = login._check_credentials

    async def slow_check(ident: str, password: str) -> Any:
        await gate.wait()
        return await real_check(ident, password)

    monkeypatch.setattr(login, "_check_credentials", slow_check)

    async def scenario() -> list[Any]:
        held = [
            asyncio.create_task(login.resolve_user_credentials(f"u{i}@acme.io", "x"))
            for i in range(2)
        ]
        await asyncio.sleep(0.01)
        with pytest.raises(login.LoginBusy):
            await login.resolve_user_credentials("alice@acme.io", "x")
        rows = len(auth_attempts.rows)
        gate.set()
        done = await asyncio.gather(*held)
        return [rows, *done]

    rows, *done = asyncio.run(scenario())
    assert rows == 2 and done == [None, None]  # the busy one wrote nothing
    assert login.LoginBusy.retry_after_s == 1


def test_a_check_that_crashes_releases_its_attempt(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ClickHouse timeout inside the check judged no password: ten of them
    must not lock the account."""
    real_check = login._check_credentials

    async def crash(ident: str, password: str) -> Any:
        raise TimeoutError("clickhouse")

    monkeypatch.setattr(login, "_check_credentials", crash)
    for _ in range(12):
        with pytest.raises(TimeoutError):
            _resolve("alice@acme.io", "correct horse battery staple")
    monkeypatch.setattr(login, "_check_credentials", real_check)
    assert _resolve("alice@acme.io", "correct horse battery staple") is not None


def test_a_release_leaves_the_window_with_its_attempt(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release stamped later than its attempt outlived it in the window and
    cancelled a newer failure: one extra guess at the window's edge."""
    real_check = login._check_credentials

    async def slow_success(ident: str, password: str) -> Any:
        auth_attempts.now += 800  # the check took a long time
        return await real_check(ident, password)

    monkeypatch.setattr(login, "_check_credentials", slow_success)
    assert _resolve("alice@acme.io", "correct horse battery staple") is not None
    ok = [r for r in auth_attempts.rows if r["kind"] == "login.identifier.ok"]
    attempt = [r for r in auth_attempts.rows if r["kind"] == "login.identifier"]
    assert [r["at"] for r in ok] == [r["at"] for r in attempt]
    monkeypatch.setattr(login, "_check_credentials", real_check)
    auth_attempts.now += 101  # the attempt and its release have left the window
    for i in range(10):
        assert _resolve("alice@acme.io", f"guess-{i}") is None
    with pytest.raises(login.LoginThrottled):
        _resolve("alice@acme.io", "guess-10")


def test_the_keys_known_over_their_limit_stay_bounded(
    auth_attempts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import attempts

    monkeypatch.setattr(attempts, "_KNOWN_OVER_MAX", 3)
    for i in range(5):
        attempts._remember_over(("login.address", f"k{i}"))
    assert list(attempts._known_over) == [("login.address", f"k{i}") for i in (2, 3, 4)]
    # Seen over again: it is now the newest, and the oldest goes first.
    attempts._remember_over(("login.address", "k2"))
    attempts._remember_over(("login.address", "k5"))
    assert list(attempts._known_over) == [("login.address", f"k{i}") for i in (4, 2, 5)]


def test_a_limit_this_process_is_too_busy_to_check_still_refuses(
    auth_attempts, monkeypatch
) -> None:
    monkeypatch.setattr(attempts, "_semaphore", lambda: asyncio.Semaphore(0))
    monkeypatch.setattr(attempts, "_SLOT_WAIT_S", 0.01)
    assert asyncio.run(attempts.Attempts("k", 1, 60).allow("x")) is False
    assert auth_attempts.rows == []
