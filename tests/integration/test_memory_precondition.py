"""Content-hash precondition loop on memory.set / memory.update_by_id.

Verifies the optimistic-concurrency primitive end-to-end through the
HTTP transport:

  - set returns content_sha256 of the just-written value
  - get / get_by_id return matching content_sha256 for the live row
  - set with matching if_match_sha256 → succeeds
  - set with stale if_match_sha256 → 409 PRECONDITION_FAILED
  - read-then-conditional-update (the canonical bug-class scenario:
    two agents racing on the same key — the loser sees 409 and can
    re-read + retry instead of silently clobbering the winner)

The race-resolution behavior is what makes the primitive load-bearing
for multi-agent pipelines where a detector and a validator
may both touch the same memory_key.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("precondition")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.mark.asyncio
async def test_set_returns_canonical_hash_matching_get(control_plane_url) -> None:
    from loom import Agent

    ns = f"sha-roundtrip-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url) as a:
        s = await a.memory.set("k", {"a": 1, "b": [2, 3]}, memory_type="semantic")
        g = await a.memory.get("k")
        assert g is not None
        assert g["content_sha256"] == s["content_sha256"]
        assert g["content_sha256"], "content_sha256 should be a non-empty hex digest"


@pytest.mark.asyncio
async def test_matching_precondition_accepts_write(control_plane_url) -> None:
    from loom import Agent

    ns = f"match-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url) as a:
        s1 = await a.memory.set("k", {"v": 1}, memory_type="semantic")
        s2 = await a.memory.set(
            "k",
            {"v": 2},
            memory_type="semantic",
            if_match_sha256=s1["content_sha256"],
        )
        assert int(s2["version"]) > int(s1["version"])
        assert s2["content_sha256"] != s1["content_sha256"]


@pytest.mark.asyncio
async def test_stale_precondition_rejects_with_409(control_plane_url) -> None:
    """Two writes between read+update: second update with the original
    hash sees 409 PRECONDITION_FAILED instead of silently clobbering."""
    from loom import Agent, PreconditionFailed

    ns = f"stale-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url) as a:
        s1 = await a.memory.set("k", {"v": 1}, memory_type="semantic")
        # Second writer races in and updates without precondition.
        s2 = await a.memory.set("k", {"v": 2}, memory_type="semantic")
        # First writer's conditional update with the now-stale hash fails.
        with pytest.raises(PreconditionFailed) as excinfo:
            await a.memory.set(
                "k",
                {"v": 3},
                memory_type="semantic",
                if_match_sha256=s1["content_sha256"],
            )
        # The exception carries the server's current hash so the caller
        # can retry conditionally against it without an extra get round-trip.
        assert excinfo.value.current_sha256 == s2["content_sha256"]


@pytest.mark.asyncio
async def test_read_then_conditional_update_loop(control_plane_url) -> None:
    """The canonical safe-update pattern.

    Demonstrates the intended use: read the row, conditionally update
    against its hash. Should succeed when nothing changed in between.
    """
    from loom import Agent

    ns = f"read-update-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="t", base_url=control_plane_url) as a:
        await a.memory.set("counter", {"n": 1}, memory_type="semantic")
        current = await a.memory.get("counter")
        assert current is not None
        new_val = {"n": current["value"]["n"] + 1}
        s = await a.memory.set(
            "counter",
            new_val,
            memory_type="semantic",
            if_match_sha256=current["content_sha256"],
        )
        assert int(s["version"]) > 0
        # Confirm the live value reflects the update.
        final = await a.memory.get("counter")
        assert final is not None
        assert final["value"] == {"n": 2}


@pytest.fixture
async def conditional_client(
    control_plane_url, mint_test_token
) -> AsyncIterator[httpx.AsyncClient]:
    token = mint_test_token(org=ORG, namespaces=["*"], roles=["writer", "admin"])
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ) as client:
        yield client


@pytest.mark.parametrize(
    "memory_type,sensitivity", [("episodic", ""), ("semantic", ""), ("episodic", "high")]
)
async def test_first_conditional_creators_retry_against_the_winner(
    conditional_client: httpx.AsyncClient,
    memory_type: str,
    sensitivity: str,
) -> None:
    c = conditional_client
    ident = {
        "org": ORG,
        "namespace": f"create-{uuid.uuid4().hex[:8]}",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "resource",
    }
    patches = [{"workingMemory": "notes"}, {"metadata": {"flag": True}}]
    preview = await c.post(
        "/v1/memory.set",
        json={
            **ident,
            "value": patches[0],
            "memory_type": memory_type,
            "sensitivity": sensitivity,
            "if_match_sha256": "absent",
            "clear_expiry": True,
            "dry_run": True,
        },
    )
    assert preview.status_code == 200 and preview.json()["version"] == 0, preview.text
    assert not (await c.post("/v1/memory.get", json=ident)).json()["found"]
    replies = await asyncio.gather(
        *(
            c.post(
                "/v1/memory.set",
                json={
                    **ident,
                    "value": value,
                    "memory_type": memory_type,
                    "sensitivity": sensitivity,
                    "if_match_sha256": "absent",
                    "clear_expiry": True,
                },
            )
            for value in patches
        )
    )
    assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
    loser = next(i for i, reply in enumerate(replies) if reply.status_code == 409)
    assert replies[loser].json()["detail"]["code"] == "PRECONDITION_FAILED"
    current = (await c.post("/v1/memory.get", json=ident)).json()
    assert current["value"] == patches[1 - loser]
    merged = {**current["value"], **patches[loser]}
    retry = await c.post(
        "/v1/memory.set",
        json={
            **ident,
            "value": merged,
            "memory_type": memory_type,
            "sensitivity": sensitivity,
            "if_match_sha256": current["content_sha256"],
            "clear_expiry": True,
        },
    )
    assert retry.status_code == 200, retry.text
    final = (await c.post("/v1/memory.get", json=ident)).json()
    assert final["value"] == {"workingMemory": "notes", "metadata": {"flag": True}}
    assert (final["agent"], final["user_id"], final["session_id"]) == ("a", "u", "s")
    assert final["memory_id"] == current["memory_id"]
    assert final["content_sha256"] == retry.json()["content_sha256"]


@pytest.mark.parametrize("expired", [False, True])
async def test_absent_set_recreates_a_deleted_or_expired_key(
    conditional_client: httpx.AsyncClient,
    expired: bool,
) -> None:
    c = conditional_client
    ident = {
        "org": ORG,
        "namespace": f"recreate-{uuid.uuid4().hex[:8]}",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "resource",
    }
    initial = await c.post(
        "/v1/memory.set",
        json={
            **ident,
            "value": {"old": True},
            "memory_type": "episodic",
            **({"ttl_seconds": -1} if expired else {}),
        },
    )
    assert initial.status_code == 200, initial.text
    if not expired:
        deleted = await c.post("/v1/memory.delete", json=ident)
        assert deleted.status_code == 200, deleted.text
    assert not (await c.post("/v1/memory.get", json=ident)).json()["found"]
    created = await c.post(
        "/v1/memory.set",
        json={
            **ident,
            "value": {"metadata": {"fresh": True}},
            "memory_type": "episodic",
            "if_match_sha256": "absent",
            "clear_expiry": True,
        },
    )
    assert created.status_code == 200, created.text
    final = (await c.post("/v1/memory.get", json=ident)).json()
    assert final["found"] and final["value"] == {"metadata": {"fresh": True}}
    assert final["expires_at"] is None
    assert final["content_sha256"] == created.json()["content_sha256"]
