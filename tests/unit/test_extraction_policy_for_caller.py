"""extraction_policy.for_caller — the policy in force, as a report shows it.

An org admin reads the effective policy; anyone else is told only an org admin reads it;
a grant read that fails is reported as unreadable, not as a refusal; a process copy too
old to trust withholds the policy and names why.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.auth import fail_closed
from server.llm import customer
from server.memory.app import extraction_policy as ep


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"admin": True, "unreadable": False, "policy": "Never store PINs."}

    async def row_in_scope(*a: Any, **k: Any) -> bool:
        if state["unreadable"]:
            fail_closed.note_unreadable()
        return bool(state["admin"]) and not state["unreadable"]

    async def effective(org: str, namespace: str) -> str:
        if isinstance(state["policy"], Exception):
            raise state["policy"]
        return str(state["policy"])

    monkeypatch.setattr(ep, "row_in_scope", row_in_scope)
    monkeypatch.setattr(ep, "effective", effective)
    return state


@pytest.mark.asyncio
async def test_an_org_admin_reads_the_policy_in_force(admin: dict[str, Any]) -> None:
    assert await ep.for_caller("o", "ns") == ep.CallerPolicy("Never store PINs.", "", "")


@pytest.mark.asyncio
async def test_anyone_else_is_told_only_an_org_admin_reads_it(admin: dict[str, Any]) -> None:
    admin["admin"] = False
    read = await ep.for_caller("o", "ns")
    assert read.policy is None and "only an org admin" in read.unavailable and read.skipped == ""


@pytest.mark.asyncio
async def test_an_unreadable_grant_is_not_a_refusal(admin: dict[str, Any]) -> None:
    admin["unreadable"] = True
    read = await ep.for_caller("o", "ns")
    assert read.policy is None
    assert read.unavailable == "the caller's access could not be read"
    assert read.skipped == "the caller's access was unreadable"


@pytest.mark.asyncio
async def test_a_stale_copy_withholds_the_policy(admin: dict[str, Any]) -> None:
    admin["policy"] = customer.SwitchUnread("stale")
    read = await ep.for_caller("o", "ns")
    assert read == ep.CallerPolicy(None, "the policy could not be read", "SwitchUnread")
