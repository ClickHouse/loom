"""A namespace restored from another's snapshot keeps the memory ids under fresh versions.

The ops addressed by an id alone answer from the one live namespace holding it, and a
redaction names its version, which only one namespace holds. Runs against a server of
either storage authority.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
def call(control_plane_url: str, mint_test_token: Any) -> Any:
    org = unique_org("restids")
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer", "admin", "org_admin"])

    async def _call(op: str, body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
            return await c.post(
                f"/v1/{op}",
                json={"org": org, **body},
                headers={"Authorization": f"Bearer {token}"},
            )

    return _call


def _set(ns: str, value: str) -> dict[str, Any]:
    return {
        "namespace": ns,
        "agent": "a",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": value,
    }


async def _restored_copy(call: Any, source: str) -> str:
    copy = f"copy-{uuid.uuid4().hex[:8]}"
    snapshot = await call("namespace.snapshot", {"namespace": source})
    assert snapshot.status_code == 200, snapshot.text
    restored = await call(
        "namespace.restore", {"target_namespace": copy, "payload": snapshot.json()}
    )
    assert restored.status_code == 200, restored.text
    return copy


async def test_a_deleted_copy_leaves_the_source_addressable_by_id(call: Any) -> None:
    source = f"src-{uuid.uuid4().hex[:8]}"
    out = (await call("memory.set", _set(source, "the deploy window is tuesday"))).json()
    copy = await _restored_copy(call, source)
    gone = await call("memory.delete", {"namespace": copy, "agent": "a", "memory_key": "k"})
    assert gone.status_code == 200, gone.text

    got = await call("memory.get_by_id", {"memory_id": out["memory_id"]})
    assert got.json()["found"] is True and got.json()["namespace"] == source, got.text
    updated = await call(
        "memory.update_by_id",
        {"memory_id": out["memory_id"], "value": "the deploy window is wednesday"},
    )
    assert updated.json()["updated"] is True, updated.text
    head = await call("memory.get", {"namespace": source, "agent": "a", "memory_key": "k"})
    assert head.json()["value"] == "the deploy window is wednesday", head.text


async def test_two_live_holders_are_not_chosen_between(call: Any) -> None:
    source = f"src-{uuid.uuid4().hex[:8]}"
    out = (await call("memory.set", _set(source, "the build cache lives on disk"))).json()
    await _restored_copy(call, source)
    got = await call("memory.get_by_id", {"memory_id": out["memory_id"]})
    assert got.json()["found"] is False, got.text


async def test_a_redaction_of_the_sources_version_finds_the_source(call: Any) -> None:
    source = f"src-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(source, "first text"))).json()
    assert (await call("memory.set", _set(source, "second text"))).status_code == 200
    copy = await _restored_copy(call, source)
    r = await call("memory.redact", {"memory_id": first["memory_id"], "version": first["version"]})
    assert r.json()["redacted"] is True, r.text
    # The copy's versions are its own; its head is untouched.
    head = await call("memory.get", {"namespace": copy, "agent": "a", "memory_key": "k"})
    assert head.json()["value"] == "second text", head.text


async def test_a_replay_is_found_by_its_whole_snapshot_id(call: Any) -> None:
    """A restore is found again by its snapshot id in any spelling of the UUID, and only by
    it: an id that is not a UUID is refused."""
    source = f"src-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(source, "a fact"))).status_code == 200
    snapshot = (await call("namespace.snapshot", {"namespace": source})).json()
    target = f"copy-{uuid.uuid4().hex[:8]}"

    async def _restore(snapshot_id: str) -> httpx.Response:
        payload = {**snapshot, "snapshot_id": snapshot_id}
        return await call("namespace.restore", {"target_namespace": target, "payload": payload})

    sid = snapshot["snapshot_id"]
    first = await _restore(sid)
    assert first.status_code == 200 and first.json()["restored"] is True, first.text
    again = await _restore("{" + sid.upper() + "}")
    assert again.status_code == 200 and again.json()["restored"] is False, again.text
    assert again.json()["counts"] == first.json()["counts"]
    assert (await _restore(sid[:8])).status_code == 422
