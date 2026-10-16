"""Attempt limits and key retirement across replicas, inside ClickHouse's visibility
lag (`cross_replica_fake`): a plain read on B misses what A wrote moments ago."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

import server.auth.self_signup as ss
from server.auth import attempts, keyring
from server.core import leases
from server.core.secrets_at_rest import seal
from server.settings import settings
from tests.unit.cross_replica_fake import FakeCluster, install


@pytest.fixture()
def cluster(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCluster]:
    monkeypatch.setattr(settings, "jwt_secret", "r" * 40)
    monkeypatch.setattr(attempts, "_known_over", OrderedDict())
    keyring._reset_for_tests()
    c = FakeCluster(lag_s=3.6)
    install(monkeypatch, c)
    yield c
    keyring._reset_for_tests()


def test_an_attempt_on_one_replica_counts_on_the_next(cluster: FakeCluster) -> None:
    """Two failed sign-ins were admitted against a limit of one."""
    gate = attempts.Attempts("login.test", 1, 3600)
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            assert await attempts.admit([(gate, "k")]) is not None
        with cluster.on(b):
            assert await attempts.admit([(gate, "k")]) is None

    asyncio.run(run())


def test_the_recount_sees_a_racing_attempt_on_another_replica(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both pass the first count before either records; the recount admits one."""
    gate = attempts.Attempts("login.test", 1, 3600)
    both_counted = asyncio.Barrier(2)
    insert = attempts._insert

    async def _after_both(kinds_keys: Any, at: datetime) -> None:
        if not kinds_keys[0][0].endswith(".ok"):
            await both_counted.wait()
        await insert(kinds_keys, at)

    monkeypatch.setattr(attempts, "_insert", _after_both)

    async def on(name: str) -> Any:
        with cluster.on(cluster.replica(name)):
            return await attempts.admit([(gate, "k")])

    async def run() -> list[Any]:
        return await asyncio.gather(on("a"), on("b"))

    admitted = [a for a in asyncio.run(run()) if a is not None]
    assert len(admitted) == 1


def test_the_signup_tally_counts_every_replica(cluster: FakeCluster) -> None:
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            for _ in range(ss.HUMAN_SIGNUPS_PER_HOUR):
                assert await ss._human_signups.allow("10.0.0.1")
        with cluster.on(b):
            assert not await ss._human_signups.allow("10.0.0.1")

    asyncio.run(run())


def _key_row(kid: str, *, retired: bool = False) -> dict[str, Any]:
    now = keyring._now()
    return {
        "kid": kid,
        "algorithm": "HS256",
        "sealed_key": keyring._root_record() if kid == "default" else seal("signing_key", "k" * 40),
        "public_pem": "",
        "created_at": now,
        "activate_at": now - timedelta(hours=1),
        "retired_at": now if retired else None,
    }


def _seed(cluster: FakeCluster) -> None:
    rows = [_key_row("default", retired=True), _key_row("k1"), _key_row("k2")]
    cluster.rows["loom.signing_keys"] = [(0.0, "seed", r) for r in rows]


def test_retiring_the_last_two_signers_on_two_replicas_keeps_one(cluster: FakeCluster) -> None:
    """Each replica's plain read still saw the other key active, so both
    retires passed and nothing could sign."""
    _seed(cluster)
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            assert await keyring.retire("k1", ch=a)
        with cluster.on(b), pytest.raises(ValueError, match="no key to sign with"):
            await keyring.retire("k2", ch=b)

    asyncio.run(run())


def test_a_retire_is_refused_while_another_holds_the_claim(cluster: FakeCluster) -> None:
    _seed(cluster)
    a, b = cluster.replica("a"), cluster.replica("b")

    async def run() -> None:
        with cluster.on(a):
            async with leases.holding(keyring.RETIRE_CLAIM, ttl_s=60, ch=a) as held:
                assert held
                with cluster.on(b), pytest.raises(ValueError, match="retry in a moment"):
                    await keyring.retire("k2", ch=b)
        with cluster.on(b):
            assert await keyring.retire("k2", ch=b)

    asyncio.run(run())
