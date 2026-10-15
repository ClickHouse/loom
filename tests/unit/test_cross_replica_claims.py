"""Consecutive requests on different replicas, inside ClickHouse's visibility lag.

Each test drives two replicas over one fake ClickHouse (`cross_replica_fake`): a plain
read on B misses what A wrote moments ago, a sequential read does not, and the
KeeperMap lock table is linearizable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import HTTPException

import server.auth.self_signup as ss
from server.auth import oauth
from server.auth.personal_namespace import PersonalNamespaceBusy, ensure_personal_namespace
from server.core import idempotency, leases, request_tail
from tests.unit.cross_replica_fake import FakeCluster, in_request, install


class _Req:
    headers = {"host": "loom.test"}
    client = type("C", (), {"host": "10.0.0.1"})()
    url = type("U", (), {"scheme": "http"})()


@pytest.fixture()
def cluster(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any) -> FakeCluster:
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")

    async def _hash(pw: str) -> str:
        return "h:" + pw

    monkeypatch.setattr(ss, "hash_password_off_loop", _hash)
    monkeypatch.setattr(ss, "mint_token", lambda **k: "tok:" + k["subject"])
    c = FakeCluster(lag_s=3.6)
    install(monkeypatch, c, ss, idempotency)
    return c


def _signup(namespace: str, email: str = "bob@x.io") -> Any:
    body = ss.SelfSignupInput(namespace=namespace, email=email, password="hunter2hunter2")
    return ss.self_signup(body, _Req())  # type: ignore[arg-type]


# ---- self-signup: a name or email claimed on one replica is taken on all ----


def test_a_name_signed_up_on_one_replica_is_claimed_on_the_next(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            await _signup("bob")
        with cluster.on(b), pytest.raises(HTTPException) as e:
            await _signup("bob", "other@x.io")
        assert e.value.detail["code"] == "NAMESPACE_CLAIMED"  # type: ignore[index]

    asyncio.run(run())


def test_an_email_used_on_one_replica_is_in_use_on_the_next(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            await _signup("bob")
        with cluster.on(b), pytest.raises(HTTPException) as e:
            await _signup("bobby")
        assert e.value.detail["code"] == "EMAIL_IN_USE"  # type: ignore[index]

    asyncio.run(run())


def test_a_signup_running_elsewhere_is_refused_409(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            assert await leases.take(ss.name_claim("dev", "bob"), ttl_s=60)
        with cluster.on(b), pytest.raises(HTTPException) as e:
            await _signup("bob")
        assert e.value.status_code == 409
        assert e.value.detail["code"] == "SIGNUP_IN_PROGRESS"  # type: ignore[index]

    asyncio.run(run())


def test_a_signup_slower_than_its_claim_allows_is_cut_off_and_releases_it(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work under a claim ends well inside its TTL, so a slow ClickHouse
    never lets a second sign-up take the claim over and write the same account."""
    a = cluster.replica("a")
    monkeypatch.setattr(leases, "_WORK_SHARE", 0.001)

    async def _slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(ss, "_claim_account", _slow)

    async def run() -> None:
        with cluster.on(a):
            with pytest.raises(HTTPException) as e:
                await _signup("bob")
            assert e.value.status_code == 503
            assert await leases.take(ss.name_claim("dev", "bob"), ttl_s=60), "released"

    asyncio.run(run())


def test_a_jit_sign_up_slower_than_its_claim_allows_is_cut_off(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import jit, login
    from server.settings import settings

    a = cluster.replica("a")
    monkeypatch.setattr(leases, "_WORK_SHARE", 0.001)
    monkeypatch.setattr(settings, "oidc_jit_provision", True)

    async def _slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(jit, "_provision_jit", _slow)

    async def run() -> None:
        with cluster.on(a):
            with pytest.raises(login.SignInUnavailable):
                await jit.jit_provision_for_email("bob@example.com")
            claim = ss.email_claim(settings.oidc_jit_org, "bob@example.com")
            assert await leases.take(claim, ttl_s=60), "released"

    asyncio.run(run())


def test_an_agent_name_signed_up_on_one_replica_is_claimed_on_the_next(
    cluster: FakeCluster,
) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            await ss.agent_signup(ss.AgentSignupInput(name="Bot"), _Req())  # type: ignore[arg-type]
        with cluster.on(b), pytest.raises(HTTPException) as e:
            await ss.agent_signup(ss.AgentSignupInput(name="bot"), _Req())  # type: ignore[arg-type]
        assert e.value.detail["code"] == "NAMESPACE_CLAIMED"  # type: ignore[index]

    asyncio.run(run())


def test_a_dead_holders_claim_is_taken_over_after_its_ttl(cluster: FakeCluster) -> None:
    a = cluster.replica("a")

    async def run() -> None:
        with cluster.on(a):
            assert await leases.take("x", ttl_s=5)
            assert await leases.take("x", ttl_s=5) is None
            cluster.advance(6)
            assert await leases.take("x", ttl_s=5)

    asyncio.run(run())


# ---- personal namespace: one name per caller across replicas ----------------


def test_a_personal_name_taken_on_one_replica_is_not_reused_on_the_next(
    cluster: FakeCluster,
) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            assert await ensure_personal_namespace(a, "dev", "bob@a.io") == "personal-bob"
        with cluster.on(b):
            got = await ensure_personal_namespace(b, "dev", "bob@b.io")
        assert got is not None and got.startswith("personal-bob-")

    asyncio.run(run())


def test_a_personal_name_being_provisioned_elsewhere_is_left_for_the_next_request(
    cluster: FakeCluster,
) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            assert await leases.take("personal-ns:dev:personal-bob", ttl_s=30)
        with cluster.on(b), pytest.raises(PersonalNamespaceBusy):
            await ensure_personal_namespace(b, "dev", "bob@a.io")

    asyncio.run(run())


def test_a_personal_provision_slower_than_its_claim_allows_publishes_nothing(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner lookup stalls past the claim's work bound: the provision is cut off
    before it writes, and the claim is free for the next request."""
    import server.auth.personal_namespace as pn

    a = cluster.replica("a")
    monkeypatch.setattr(leases, "_WORK_SHARE", 0.001)
    written: list[str] = []

    async def _slow_owner(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(5)

    async def _provision(**kw: Any) -> None:
        written.append(kw["namespace"])

    monkeypatch.setattr(pn, "_owner", _slow_owner)
    monkeypatch.setattr(ss, "provision_account", _provision)

    async def run() -> None:
        with cluster.on(a):
            with pytest.raises(leases.ClaimExpired):
                await ensure_personal_namespace(a, "dev", "bob@a.io")
            assert await leases.take("personal-ns:dev:personal-bob", ttl_s=30), "released"

    asyncio.run(run())
    assert written == []


# ---- OAuth: state, codes and refresh tokens spent once across replicas ------


def _grant(**over: Any) -> dict[str, Any]:
    g = {
        "client_id": "c1",
        "redirect_uri": "http://127.0.0.1/cb",
        "code_challenge": "x",
        "subject": "alice",
        "org": "dev",
        "namespaces": ["n"],
        "roles": ["writer"],
        "exp": int(time.time()) + 60,
    }
    return g | over


async def _spend(kind: str, credential: str) -> dict[str, Any] | None:
    """Exchange `credential` as /oauth/token does; None when it is refused."""

    async def accept(grant: dict[str, Any]) -> dict[str, Any]:
        return grant

    try:
        return await oauth._exchange(kind, credential, client_id="c1", decide=accept)  # type: ignore[arg-type]
    except HTTPException:
        return None


def test_oauth_state_written_on_one_replica_is_read_on_the_next(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")
    store = oauth._CHStore()

    async def run() -> None:
        with cluster.on(a):
            await store.save_client("c1", ["http://127.0.0.1/cb"], "cli")
            await store.save_code("code-1", _grant())
        with cluster.on(b):
            assert await store.get_client("c1") is not None
            assert await _spend("code", "code-1") is not None

    asyncio.run(run())


def test_a_code_replayed_on_another_replica_inside_the_lag_is_refused(
    cluster: FakeCluster,
) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")
    store = oauth._CHStore()

    async def run() -> None:
        with cluster.on(a):
            await store.save_code("code-1", _grant())
            cluster.advance(5)  # the code is visible everywhere; its consume will not be
            assert await _spend("code", "code-1") is not None
        with cluster.on(b):
            assert await _spend("code", "code-1") is None

    asyncio.run(run())


def test_a_refresh_token_presented_twice_at_once_is_spent_once(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")
    store = oauth._CHStore()

    async def take(rep: Any) -> Any:
        with cluster.on(rep):
            return await _spend("refresh", "rt-1")

    async def run() -> list[Any]:
        with cluster.on(a):
            await store.save_refresh("rt-1", _grant(exp=int(time.time()) + 3600))
        cluster.advance(5)
        return list(await asyncio.gather(take(a), take(b)))

    assert sum(r is not None for r in asyncio.run(run())) == 1


def test_a_consume_beats_a_row_stamped_by_a_clock_ahead(cluster: FakeCluster) -> None:
    a = cluster.replica("a")

    async def run() -> None:
        with cluster.on(a):
            ahead = time.time_ns() + 30 * 10**9
            await a.insert(
                "loom.oauth_state",
                [{"record_type": "code", "id": oauth._token_id("c"), **_grant(), "version": ahead}],
            )
            cluster.rows["loom.oauth_state"][-1][2]["expires_at"] = _grant()["exp"]
            assert await _spend("code", "c") is not None
            assert await _spend("code", "c") is None

    asyncio.run(run())


# ---- idempotency: a running key is in progress on every replica -------------


def test_a_key_running_on_one_replica_is_in_progress_on_the_next(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def on_a() -> Any:
        with in_request() as tail, cluster.on(a):
            assert await idempotency.check("k1", "h") == idempotency.CacheResult(None, False)
        return tail

    async def on_b() -> idempotency.CacheResult:
        with in_request(), cluster.on(b):
            return await idempotency.check("k1", "h")

    async def run() -> None:
        tail_a = await on_a()
        busy = await on_b()
        assert busy.conflict and busy.in_progress
        with cluster.on(a):
            await idempotency.store("k1", "h", "memory.set", 200, {"ok": 1}, org="o", subject="s")
            await request_tail.settle(tail_a)  # the answer starts: the claim is released
        replay = await on_b()
        assert replay.hit is not None and replay.hit.body == {"ok": 1}

    asyncio.run(run())


def test_a_keys_claim_is_one_insert_released_after_the_answer(cluster: FakeCluster) -> None:
    """A client's key is a fresh UUID: its claim is inserted without a read first, and
    released beside the answer, not before it (the stored row answers a duplicate)."""
    a = cluster.replica("a")

    async def run() -> None:
        with in_request() as tail, cluster.on(a):
            await idempotency.check("k1", "h")
            claims = [s for _, s in cluster.statements if "_migration_lock" in s]
            assert len(claims) == 1 and claims[0].startswith("INSERT")
            await request_tail.settle(tail)
            assert cluster.locks, "the answer does not wait for the release"
            await asyncio.gather(*idempotency._releases)
            assert cluster.locks == {}

    asyncio.run(run())


def test_a_check_cancelled_during_its_read_gives_its_claim_up(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = cluster.replica("a")
    reading = asyncio.Event()

    async def _slow_read(key: str) -> Any:
        reading.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(idempotency, "_stored", _slow_read)

    async def run() -> None:
        with in_request(), cluster.on(a):
            check = asyncio.ensure_future(idempotency.check("k1", "h"))
            await reading.wait()
            await asyncio.sleep(0.05)  # the claim has landed
            check.cancel()
            with pytest.raises(asyncio.CancelledError):
                await check
            await asyncio.gather(*idempotency._releases)
        assert cluster.locks == {}

    asyncio.run(run())
