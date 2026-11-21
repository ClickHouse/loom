"""Deletion refuses later intake while preserving unrelated identities and values."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_wildcard_forgetting_refuses_only_the_requested_users_value(
    control_plane_url: str, mint_test_token
) -> None:
    org = unique_org("refusal-users")
    token = mint_test_token(org=org, namespaces=["facts"], roles=["writer"])
    identity = {"org": org, "namespace": "facts", "agent": "writer"}
    values = {"alice": "A copper lark stays near the lake", "bob": "A silver heron stays upstream"}
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        for uid, value in values.items():
            wrote = await client.post(
                "/v1/memory.set",
                json={
                    **identity,
                    "user_id": uid,
                    "memory_key": "shared-key",
                    "value": value,
                    "memory_type": "episodic",
                },
            )
            assert wrote.status_code == 200, wrote.text
        deleted = await client.post(
            "/v1/memory.delete",
            json={
                **identity,
                "agent": "*",
                "user_id": "alice",
                "memory_key": "shared-key",
            },
        )
        assert deleted.status_code == 200 and deleted.json()["tombstoned"], deleted.text
        for uid, expected in (("alice", 422), ("bob", 200)):
            got = await client.post(
                "/v1/memory.get",
                json={
                    **identity,
                    "user_id": uid,
                    "memory_key": "shared-key",
                },
            )
            assert got.status_code == 200, got.text
            assert bool(got.json()["found"]) is (uid == "bob")
            replay = await client.post(
                "/v1/memory.set",
                json={
                    **identity,
                    "user_id": uid,
                    "memory_key": "replayed",
                    "value": values[uid],
                    "memory_type": "episodic",
                },
            )
            assert replay.status_code == expected, replay.text


@pytest.mark.asyncio
async def test_deleted_content_cannot_return_through_another_intake(
    control_plane_url: str, mint_test_token
) -> None:
    org = unique_org("refusal")
    ns = "facts"
    token = mint_test_token(org=org, namespaces=[ns], roles=["writer"])
    identity = {"org": org, "namespace": ns, "agent": "writer", "user_id": "subject"}
    value = f"The retired phrase is copper lark {uuid.uuid4().hex}"
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:

        async def call(op: str, **fields):
            return await client.post(f"/v1/{op}", json={**identity, **fields})

        wrote = await call("memory.set", memory_key="source", value=value)
        assert wrote.status_code == 200, wrote.text
        deleted = await call("memory.delete", memory_key="source")
        assert deleted.status_code == 200 and deleted.json()["tombstoned"], deleted.text
        restored = await call("memory.restore", memory_key="source")
        assert restored.status_code == 422, restored.text
        assert restored.json()["detail"]["reason"] == "forgotten"
        for new_value in (value, f"Reminder: {value.upper()}.", {"wrapped": value}):
            refused = await call("memory.set", memory_key="returned", value=new_value)
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"]["reason"] == "forgotten"
        deferred = await call(
            "memory.set_from_messages",
            defer=True,
            messages=[{"role": "user", "content": value}],
        )
        assert deferred.status_code == 422, deferred.text
        unrelated = await call(
            "memory.set", memory_key="unrelated", value="A separate current fact"
        )
        assert unrelated.status_code == 200, unrelated.text
        other = await call("memory.set", memory_key="other", user_id="another", value=value)
        assert other.status_code == 200, other.text


@pytest.mark.asyncio
async def test_structured_deleted_values_are_refused_canonically(
    control_plane_url: str, mint_test_token
) -> None:
    org = unique_org("refusal-object")
    token = mint_test_token(org=org, namespaces=["facts"], roles=["writer"])
    identity = {"org": org, "namespace": "facts", "agent": "writer", "user_id": "subject"}
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        for op, fields, expected in (
            ("memory.set", {"memory_key": "source", "value": {"a": "one", "b": "two"}}, 200),
            ("memory.delete", {"memory_key": "source"}, 200),
            ("memory.set", {"memory_key": "returned", "value": {"b": "two", "a": "one"}}, 422),
        ):
            result = await client.post(f"/v1/{op}", json={**identity, **fields})
            assert result.status_code == expected, result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["user_id", "namespace"])
@pytest.mark.parametrize("residue", [False, True])
async def test_delayed_shared_retirement_cannot_recreate_erased_content(
    control_plane_url, mint_test_token, scope_type, residue
):
    from server.core.ch import get_client
    from server.core.datetimes import naive_utc_now
    from server.memory.store import shared
    from server.memory.store.rows import next_version

    org, namespace = unique_org("refusal-shared-retirement"), "facts"
    token = mint_test_token(org=org, namespaces=["*"], roles=["org_admin", "admin", "writer"])
    identity = {"org": org, "namespace": namespace, "agent": "writer", "user_id": "alice"}
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        for op, fields in (
            (
                "memory.set",
                {"memory_key": "source", "value": f"Private copper lark {uuid.uuid4().hex}"},
            ),
            ("memory.promote", {"memory_key": "source"}),
        ):
            result = await client.post(f"/v1/{op}", json={**identity, **fields})
            assert result.status_code == 200, result.text
        ch = get_client()
        await ch.connect()
        try:
            original = await shared.fetch_shared(org, namespace, "source", strong=True, ch=ch)
            assert original is not None
            original = {
                "superseded_by": None,
                **original,
                "org": org,
                "namespace": namespace,
                "memory_key": "source",
            }
            erased = await client.post(
                "/v1/erasure.request",
                json={
                    "org": org,
                    "scope_type": "user_id" if scope_type == "user_id" else "namespace_list",
                    "scope_value": "alice"
                    if scope_type == "user_id"
                    else json.dumps({"namespaces": [namespace], "purge": True}),
                    "requester": "operator",
                    "purge": True,
                },
            )
            assert erased.status_code == 200 and erased.json()["status"] == "completed", erased.text
            assert await shared.fetch_shared(org, namespace, "source", strong=True, ch=ch) is None
            if residue:
                # An old writer's admitted send leaves a copy for the retirement to remove.
                await ch.insert("loom.shared", [original], settings={"async_insert": 0})
            await shared.insert_shared_row(
                {**original, "version": next_version(), "tombstoned_at": naive_utc_now()}, ch=ch
            )
            assert await shared.fetch_shared(org, namespace, "source", strong=True, ch=ch) is None
        finally:
            await ch.disconnect()
