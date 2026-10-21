"""A user erasure's identity steps (server/erasure/app/saga_steps.py): each
holds the claims an online writer of the same rows holds, in the documented
order. The group step moves the groups watermark when it changed something
or a write failed (one whose reply was lost may have landed); the owner
rewrite moves none, since no cached value holds an owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.core.principals import personal_name
from server.erasure.app import saga_steps

ORG = "acme"


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every claim taken and every watermark move, in order."""
    events: list[str] = []

    def _claim(name: str) -> Any:
        @asynccontextmanager
        async def _held(*args: str) -> Any:
            events.append(f"{name} {' '.join(args[1:]) or args[0]}")
            yield

        return _held

    async def _changed(org: str, *_a: Any, kind: str = "grants") -> None:
        events.append(f"changed {kind}")

    monkeypatch.setattr(saga_steps, "group_change", _claim("group"))
    monkeypatch.setattr(saga_steps, "account_change", _claim("account"))
    monkeypatch.setattr(saga_steps, "collections_change", _claim("tree"))
    monkeypatch.setattr(saga_steps.grant_cache, "changed", _changed)
    return events


def _returns(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    async def _fake(*_a: Any, **_k: Any) -> Any:
        return value

    monkeypatch.setattr(saga_steps.principal_steps, name, _fake)


def test_groups_are_left_one_claim_each_and_moved_once(
    monkeypatch: pytest.MonkeyPatch, held: list[str]
) -> None:
    _returns(monkeypatch, "groups_of_user", ["eng", "ops"])
    _returns(monkeypatch, "remove_from_group", 1)
    assert asyncio.run(saga_steps._step_remove_from_groups_by_user(ORG, "bob")) == 2
    assert held == ["group eng", "group ops", "changed groups"]


def test_a_removal_whose_reply_is_lost_still_moves_the_watermark(
    monkeypatch: pytest.MonkeyPatch, held: list[str]
) -> None:
    """The saga's retry finds the user in no group and would announce nothing."""
    _returns(monkeypatch, "groups_of_user", ["eng", "ops"])
    calls: list[str] = []

    async def _remove(org: str, group_id: str, user_id: str) -> int:
        calls.append(group_id)
        if group_id == "ops":
            raise TimeoutError("reply lost")
        return 1

    monkeypatch.setattr(saga_steps.principal_steps, "remove_from_group", _remove)
    with pytest.raises(TimeoutError):
        asyncio.run(saga_steps._step_remove_from_groups_by_user(ORG, "bob"))
    assert calls == ["eng", "ops"] and held == ["group eng", "group ops", "changed groups"]


def test_no_group_listing_the_user_moves_no_watermark(
    monkeypatch: pytest.MonkeyPatch, held: list[str]
) -> None:
    _returns(monkeypatch, "groups_of_user", [])
    assert asyncio.run(saga_steps._step_remove_from_groups_by_user(ORG, "bob")) == 0
    assert held == []


def test_the_personal_collection_retires_under_its_directory_claim(
    monkeypatch: pytest.MonkeyPatch, held: list[str]
) -> None:
    _returns(monkeypatch, "retire_personal_collection_by_user", 1)
    assert asyncio.run(saga_steps._step_retire_personal_collection_by_user(ORG, "bob")) == 1
    assert held == [f"account {personal_name(ORG, 'user:bob')}"]


@pytest.mark.parametrize(("created", "rewritten"), [(["col-a", "col-b"], 2), ([], 0)])
def test_the_creators_id_goes_under_each_directory_claim_then_the_trees(
    monkeypatch: pytest.MonkeyPatch, held: list[str], created: list[str], rewritten: int
) -> None:
    _returns(monkeypatch, "collections_created_by", created)
    _returns(monkeypatch, "forget_collection_owner_by_user", rewritten)
    assert asyncio.run(saga_steps._step_forget_collection_owner_by_user(ORG, "bob")) == rewritten
    claims = [f"account {ns}" for ns in created] + [f"tree {ORG}"]
    # The tree's claim even with none listed: a create racing the erasure is
    # rewritten under it. No watermark: the cached tree holds no owner.
    assert held == claims
