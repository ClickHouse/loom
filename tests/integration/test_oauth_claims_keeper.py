"""The OAuth exchange claim against a real KeeperMap table.

The unit tests drive `oauth_claims` through a fake that matches its SQL; this
runs the same statements on ClickHouse, so strict-mode inserts, the
compare-and-swap takeover of a stale claim and the boot sweep are checked on
the server. Skips without a Keeper (no KeeperMap `loom.oauth_claims`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.auth import oauth_claims
from server.core.repo_base import _default_client

_OLD_CLAIM = (
    "INSERT INTO loom.oauth_claims (claim_key, holder, claimed_at) "
    "SETTINGS keeper_map_strict_mode = 1 "
    "VALUES ({k:String}, 'dead', now64(3) - toIntervalSecond({age:UInt32}))"
)


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = _default_client()
    await client.connect()
    try:
        row = await client.query_one(
            "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'oauth_claims'"
        )
        if not oauth_claims.is_keeper_table(row):
            pytest.skip("needs loom.oauth_claims (a ClickHouse Keeper)")
        oauth_claims.CLAIMS.atomic = None
        yield client
    finally:
        oauth_claims.CLAIMS.atomic = None
        await client.disconnect()


def _key() -> str:
    return f"code:test-{uuid.uuid4().hex}"


async def test_of_concurrent_claims_on_one_credential_exactly_one_holds(ch: Any) -> None:
    key = _key()
    outcomes = await asyncio.gather(*(oauth_claims.claim(ch, key) for _ in range(8)))
    held = [holder for outcome, holder in outcomes if outcome is oauth_claims.Claim.HELD]
    assert len(held) == 1
    assert all(
        o is oauth_claims.Claim.TAKEN for o, _ in outcomes if o is not oauth_claims.Claim.HELD
    )
    await oauth_claims.release(ch, key, held[0])
    outcome, holder = await oauth_claims.claim(ch, key)
    assert outcome is oauth_claims.Claim.HELD
    await oauth_claims.release(ch, key, holder)


async def test_a_stale_claim_is_taken_over_and_a_release_keeps_the_takers(ch: Any) -> None:
    key = _key()
    await ch.execute(_OLD_CLAIM, params={"k": key, "age": oauth_claims.STALE_S + 60})
    outcome, holder = await oauth_claims.claim(ch, key)
    assert outcome is oauth_claims.Claim.HELD
    await oauth_claims.release(ch, key, "dead")  # the dead exchange's late release
    assert (await oauth_claims.claim(ch, key))[0] is oauth_claims.Claim.TAKEN
    await oauth_claims.release(ch, key, holder)


async def test_the_boot_sweep_drops_only_stale_claims(ch: Any) -> None:
    stale, live = _key(), _key()
    await ch.execute(_OLD_CLAIM, params={"k": stale, "age": oauth_claims.STALE_S + 60})
    _, holder = await oauth_claims.claim(ch, live)
    await oauth_claims.probe(ch)
    rows = await ch.query_all(
        "SELECT claim_key FROM loom.oauth_claims WHERE claim_key IN {ks:Array(String)}",
        params={"ks": [stale, live]},
    )
    assert [r["claim_key"] for r in rows] == [live]
    await oauth_claims.release(ch, live, holder)


async def test_of_concurrent_takers_of_a_stale_claim_exactly_one_holds(ch: Any) -> None:
    key = _key()
    await ch.execute(_OLD_CLAIM, params={"k": key, "age": oauth_claims.STALE_S + 60})
    outcomes = await asyncio.gather(*(oauth_claims.claim(ch, key) for _ in range(8)))
    held = [holder for outcome, holder in outcomes if outcome is oauth_claims.Claim.HELD]
    assert len(held) == 1
    await oauth_claims.release(ch, key, held[0])
