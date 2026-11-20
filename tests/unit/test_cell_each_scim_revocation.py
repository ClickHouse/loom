"""Descendant revocation discards provisioning decisions that its write overtakes."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

from server.auth import login, scim, service_accounts
from server.core import ch_bounded
from server.settings import settings
from tests.unit.test_scim_people import people as people


@pytest.mark.parametrize("bounded", [False, True], ids=["ordinary", "bounded"])
@pytest.mark.parametrize("outcome", ["success", "error", "lost-reply", "cancel"])
async def test_descendant_write_drops_warm_and_overtaken_provisioning_permits(
    people, monkeypatch, bounded, outcome
):
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 240)
    owner, subject = "alice@example.test", "sa:personal:child"
    people.provision(owner)
    original = {
        "org": "acme",
        "name": "child",
        "sa_subject": subject,
        "namespaces": ["private"],
        "roles": ["writer"],
        "ttl_days": 0,
        "created_by": owner,
        "created_at": datetime(2020, 1, 1),
        "expires_at": None,
        "revoked_at": None,
        "revoked_by": "",
        "version": 1,
        "kind": "personal",
        "client": "unit-client",
        "inherit_creator_grants": False,
        "ns_roles": "{}",
        "lineage": [],
        "saves_to": "private",
    }
    people.services[subject] = dict(original)
    assert await scim.refuses_subject(subject, org="acme") is False

    started, release = asyncio.Event(), asyncio.Event()
    pending = None
    pause_ancestry = False
    stored = []
    query_one = people.query_one
    error = {
        "error": RuntimeError("identity write rejected"),
        "lost-reply": ConnectionError("identity write landed without its reply"),
        "cancel": asyncio.CancelledError("identity write cancelled after landing"),
    }.get(outcome)

    async def read(sql, params=None):
        nonlocal pause_ancestry
        if "SELECT * FROM loom.service_accounts" in sql:
            return dict(people.services[subject])
        row = await query_one(sql, params=params)
        if pause_ancestry and params is not None and params.get("sub") == subject:
            pause_ancestry = False
            started.set()
            await release.wait()
        return row

    async def read_rows(sql, params, **kwargs):
        assert "FROM loom.service_accounts FINAL" in sql
        assert params["org"] == "acme" and params["name"] == "child"
        row = dict(people.services[subject])
        row["created_at"] = int(
            (original["created_at"] - datetime(1970, 1, 1)).total_seconds() * 1000
        )
        return [row]

    async def insert(table, rows, **kwargs):
        nonlocal pending, pause_ancestry
        assert table == "loom.service_accounts" and len(rows) == 1
        people.unavailable = True
        try:
            with pytest.raises(login.SignInUnavailable):
                await scim.refuses_subject(subject, org="acme")
        finally:
            people.unavailable = False
        pause_ancestry = True
        pending = asyncio.create_task(scim.refuses_subject(subject, org="acme"))
        await asyncio.wait_for(started.wait(), 2)
        if outcome != "error":
            stored.extend(dict(row) for row in rows)
            people.services[subject] = dict(rows[0])
        if error is not None:
            raise error

    monkeypatch.setattr(people, "query_one", read)
    monkeypatch.setattr(people, "insert", insert, raising=False)
    monkeypatch.setattr(ch_bounded, "read_rows", read_rows)
    work = (
        service_accounts._RevocationWork(ch=people, deadline=time.monotonic() + 30, roots={owner})
        if bounded
        else None
    )
    try:
        call = service_accounts._revoke_named(
            "acme",
            "child",
            [owner],
            {owner: None},
            set(),
            "operator",
            {subject},
            ch=people,
            work=work,
        )
        if error is None:
            assert await call == subject
        else:
            with pytest.raises(type(error)) as caught:
                await call
            assert caught.value is error
        release.set()
        assert pending is not None
        assert await pending is False
        people.unavailable = True
        try:
            with pytest.raises(login.SignInUnavailable):
                await scim.refuses_subject(subject, org="acme")
        finally:
            people.unavailable = False
        assert await scim.refuses_subject(subject, org="acme") is (outcome != "error")
        if outcome == "error":
            assert stored == [] and people.services[subject] == original
        else:
            assert len(stored) == 1
            row = people.services[subject]
            assert isinstance(row["revoked_at"], datetime)
            assert row["revoked_by"] == "operator" and row["version"] > original["version"]
            assert row == {
                **original,
                "revoked_at": row["revoked_at"],
                "revoked_by": "operator",
                "version": row["version"],
            }
        if work is not None:
            assert work.confirmed_dead == ({subject} if outcome == "success" else set())
    finally:
        release.set()
        if pending is not None and not pending.done():
            await pending
