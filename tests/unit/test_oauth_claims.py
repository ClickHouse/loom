"""One exchange at a time per OAuth credential, claimed in a KeeperMap table:
a second claim of a live key is refused, a stale one is taken over, a
release drops only the releaser's own claim, and no table means unclaimed."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from server.auth import oauth_claims as oc


class _Keeper:
    """loom.oauth_claims as a dict; `stale` keys are older than STALE_S."""

    def __init__(self, table: bool = True) -> None:
        self.table = table
        self.rows: dict[str, str] = {}
        self.stale: set[str] = set()
        self.down = False

    async def query_one(self, sql: str, params: Any = None) -> dict[str, str] | None:
        return {"engine": "KeeperMap"} if self.table else None

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        if self.down:
            raise RuntimeError("Keeper away")
        key = params["k"]
        if sql.startswith("INSERT"):
            assert "keeper_map_strict_mode = 1" in sql
            if key in self.rows:
                raise RuntimeError("Code: 999. Coordination::Exception: Node exists")
            self.rows[key] = params["me"]
        elif "claimed_at <" in sql:
            if key in self.stale:
                self.rows.pop(key, None)
                self.stale.discard(key)
        elif self.rows.get(key) == params["me"]:
            del self.rows[key]


@pytest.fixture(autouse=True)
def _unprobed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(oc, "_atomic", None)
    yield


def _claim(keeper: _Keeper, key: str = "code:abc") -> tuple[oc.Claim, str]:
    return asyncio.run(oc.claim(keeper, key))


def test_a_live_claim_keeps_a_second_exchange_out_until_released() -> None:
    keeper = _Keeper()
    first, holder = _claim(keeper)
    assert first is oc.Claim.HELD
    assert _claim(keeper)[0] is oc.Claim.TAKEN
    asyncio.run(oc.release(keeper, "code:abc", holder))
    assert _claim(keeper)[0] is oc.Claim.HELD


def test_a_claim_a_crashed_exchange_left_is_taken_over() -> None:
    keeper = _Keeper()
    _claim(keeper)
    keeper.stale.add("code:abc")
    assert _claim(keeper)[0] is oc.Claim.HELD


def test_a_release_leaves_a_claim_that_was_taken_over() -> None:
    keeper = _Keeper()
    _, old = _claim(keeper)
    keeper.stale.add("code:abc")
    _, new = _claim(keeper)
    asyncio.run(oc.release(keeper, "code:abc", old))
    assert keeper.rows == {"code:abc": new}


def test_no_claims_table_runs_the_exchange_unclaimed() -> None:
    keeper = _Keeper(table=False)
    assert _claim(keeper) == (oc.Claim.UNCLAIMED, "")
    assert oc.claims_atomic() is False


def test_keeper_failing_is_raised_for_a_retryable_refusal() -> None:
    keeper = _Keeper()
    keeper.down = True
    with pytest.raises(RuntimeError):
        _claim(keeper)
