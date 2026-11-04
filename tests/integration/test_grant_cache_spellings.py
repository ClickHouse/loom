"""The claim cap's grant read on ClickHouse: which tied spellings set the rank.

An active account whose email is verified (no password, as an SSO account's)
ties its login handle and email as one person, so a grant under either counts;
a password account's unverified email only caps. A deactivated account's pair,
and a released account's `released_aliases`, still tie the spellings, but only
to cap: their revoked rows cap a token's claim, their live grants never admit it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core import grant_cache
from server.core.repo_base import _default_client


@pytest.fixture
async def org(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    client = _default_client()
    await client.connect()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    name = f"it-spellings-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    account = {
        "org": name,
        "created_at": now,
        "updated_at": now,
        "released_aliases": [],
        "password_hash": "",
    }

    def _account(namespace: str, owner: str, contact: str, **over: Any) -> dict[str, Any]:
        row = {**account, "namespace": namespace, "owner_email": owner, "contact_email": contact}
        return {**row, "active": True, **over}

    await client.insert(
        "loom.namespaces",
        [
            _account("h", "h", "h@x"),
            # A password account, never linked: its email is unverified.
            _account("u", "u", "u@x", password_hash="x"),
            _account("d", "d", "d@x", active=False),
            _account("r", "", "", active=False, released_aliases=["r", "r@x"]),
            # Released, then re-registered by an admin: its owner is not the released person.
            _account("q", "admin", "admin@x", released_aliases=["q", "q@x"]),
        ],
    )
    grants: list[dict[str, Any]] = []
    for spelling in ("h@x", "d@x", "r@x", "admin@x", "u@x"):
        for namespace, revoked in (("live", None), ("gone", now)):
            grants.append(
                {
                    "org": name,
                    "namespace": namespace,
                    "subject": spelling,
                    "role": "writer",
                    "status": "active",
                    "granted_at": now,
                    "granted_by": "it",
                    "revoked_at": revoked,
                    "version": time.time_ns(),
                }
            )
    # The same spelling in another org, where nothing ties it.
    grants.append({**grants[0], "org": f"{name}-other"})
    await client.insert(
        "loom.namespace_grants",
        grants,
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    try:
        yield name
    finally:
        for table in ("loom.namespace_grants", "loom.namespaces"):
            for partition in (name, f"{name}-other"):
                await client.execute(
                    f"ALTER TABLE {table} DROP PARTITION {{p:String}}", params={"p": partition}
                )
        await client.disconnect()


@pytest.mark.parametrize(
    ("subject", "ranks"),
    [
        ("h", {"live": 2, "gone": 0}),  # an active account: one person
        ("u", {"live": 0, "gone": 0}),  # an unverified email: caps, never admits
        ("d", {"live": 0, "gone": 0}),  # deactivated: caps, never admits
        ("r", {"live": 0, "gone": 0}),  # released: caps, never admits
        ("r@x", {"live": 2, "gone": 0}),  # a spelling's own grants count
        ("q", {}),  # the row's current owner is not the released person
        ("admin", {"live": 2, "gone": 0}),  # nor is the released person its owner
    ],
)
async def test_which_tied_spellings_set_the_rank(
    org: str, subject: str, ranks: dict[str, int]
) -> None:
    assert (await grant_cache._read(org, subject)).ranks == ranks


async def test_a_spelling_tied_in_one_org_is_not_tied_in_another(org: str) -> None:
    # h@x holds a grant in the other org, but h is tied to h@x only in this one.
    assert (await grant_cache._read(f"{org}-other", "h")).ranks == {}
    assert (await grant_cache._read(f"{org}-other", "h@x")).ranks == {"live": 2}
