"""memory.verify end to end against a live Loom + ClickHouse.

The attestation binds to content: set → verify reads `human`; an edit turns
it `stale` without anyone touching the attestation; namespace.validate
reports the stale attestation as a warning naming the key; re-verify makes
it `human` again against the new content; revoke returns it to `none`.
Under a person's JWT the actor is `human:<subject>` (the body's `actor` is
ignored); under a service-account token it is `process:<subject>` — a bot
cannot attest as a human.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("verify")


def _human_token(*, org: str, namespaces: list[str], subject: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=subject,
        org=org,
        namespaces=namespaces,
        roles=["writer"],
        ttl_seconds=600,
        is_service_account=False,
    )


# Every read here is loom.memory FINAL in the same process after a
# synchronous insert (async_insert=0, wait_for_async_insert=1): deterministic.
# The single search-free path means no embedding race to bridge.


@pytest.mark.asyncio
async def test_attestation_follows_the_content(control_plane_url, mint_test_token) -> None:
    from loom import Agent

    ns = f"vf-{uuid.uuid4().hex[:6]}"
    # A PERSON's token (the fixture mints service accounts, which attest as
    # `process:`); the human path is the one the dashboard button takes.
    tok = _human_token(org=ORG, namespaces=[ns], subject="alice")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        w = await a.memory.set(
            "deploy_window", "Tue and Thu only; never Friday.", memory_type="semantic"
        )
        assert w["memory_id"] and w["content_sha256"]

        got = await a.memory.get("deploy_window")
        assert got and got["verification"] == "none" and got["verified_by"] == ""

        # attest — the body's actor is ignored under auth; the token decides
        v = await a.memory.verify("deploy_window", actor="process:spoof")
        assert v["found"] and v["changed"] and v["verification"] == "human"
        assert v["verified_by"] == "human:alice"
        assert v["content_sha256"] == w["content_sha256"]

        got = await a.memory.get("deploy_window")
        assert got["verification"] == "human" and got["verified_by"] == "human:alice"
        assert got["verified_at"]
        # the attestation rides every version row: content hash unchanged
        assert got["content_sha256"] == w["content_sha256"]

        # re-verify by the same actor is a no-op
        again = await a.memory.verify("deploy_window")
        assert again["found"] and again["changed"] is False and again["verification"] == "human"

        # an edit voids it without touching the attestation columns
        upd = await a.memory.update_by_id(w["memory_id"], "Fridays are fine now.")
        assert upd["updated"] and upd["content_sha256"] != w["content_sha256"]
        got = await a.memory.get("deploy_window")
        assert got["verification"] == "stale"
        assert got["verified_by"] == "human:alice"  # who said so is kept
        assert got["content_sha256"] == upd["content_sha256"]

        # the integrity report names it
        rep = await a.namespaces.validate()
        stale = next(f for f in rep["findings"] if f["check"] == "stale_verification")
        assert stale["severity"] == "warning" and stale["count"] >= 1
        assert any(s["memory_key"] == "deploy_window" for s in stale["samples"])
        assert "memory.verify" in stale["detail"]
        assert rep["healthy"] is True  # a warning, not an error

        # re-verify binds to the new content; the report clears
        v2 = await a.memory.verify("deploy_window")
        assert v2["changed"] and v2["verification"] == "human"
        assert v2["content_sha256"] == upd["content_sha256"]
        rep = await a.namespaces.validate()
        stale = next(f for f in rep["findings"] if f["check"] == "stale_verification")
        assert not any(s["memory_key"] == "deploy_window" for s in stale["samples"])

        # revoke → none; revoking again is a no-op
        r = await a.memory.verify("deploy_window", revoke=True)
        assert r["changed"] and r["verification"] == "none" and r["verified_by"] == ""
        got = await a.memory.get("deploy_window")
        assert got["verification"] == "none" and got["verified_by"] == ""
        r = await a.memory.verify("deploy_window", revoke=True)
        assert r["found"] and r["changed"] is False


@pytest.mark.asyncio
async def test_list_carries_the_state_and_by_id_addressing_works(
    control_plane_url, mint_test_token
) -> None:
    from loom import Agent

    ns = f"vf-{uuid.uuid4().hex[:6]}"
    # the fixture mints a SERVICE-ACCOUNT token: it attests as `process:`
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="ci-reviewer")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        w = await a.memory.set(
            "db_engine", {"engine": "postgres", "major": 16}, memory_type="semantic"
        )
        v = await a.memory.verify(memory_id=w["memory_id"], actor="human:spoof")
        assert v["found"] and v["verification"] == "process" and v["memory_key"] == "db_engine"
        assert v["verified_by"] == "process:ci-reviewer"  # never human: from a service account

        by_id = await a.memory.get_by_id(w["memory_id"])
        assert by_id["verification"] == "process" and by_id["verified_by"] == "process:ci-reviewer"

        listed = await a.memory.list()
        entry = next(e for e in listed["entries"] if e["memory_key"] == "db_engine")
        assert entry["verification"] == "process" and entry["verified_by"] == "process:ci-reviewer"

        # bulk_get shares get's read shape: identity, lifecycle, attestation
        bulk = await a.memory.bulk_get(["db_engine", "nope"])
        row = bulk["results"]["db_engine"]
        assert row["memory_id"] == w["memory_id"] and row["status"] == "active"
        assert row["verification"] == "process" and row["verified_by"] == "process:ci-reviewer"
        assert row["content_sha256"] == w["content_sha256"] and row["value"] == {
            "engine": "postgres",
            "major": 16,
        }
        assert bulk["missing"] == ["nope"]

        missing = await a.memory.verify(memory_id="00000000-0000-0000-0000-000000000000")
        assert missing["found"] is False


@pytest.mark.asyncio
async def test_a_set_or_bulk_set_revision_keeps_the_attestation_as_stale(
    control_plane_url,
) -> None:
    """memory.set and memory.bulk_set revise a key the way memory.update does:
    the attestation stays on the new version, over the old bytes, so it reads
    `stale` and keeps who attested."""
    from loom import Agent

    ns = f"vf-{uuid.uuid4().hex[:6]}"
    tok = _human_token(org=ORG, namespaces=[ns], subject="alice")
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # distinct values throughout: bulk_set folds an entry whose content an
        # existing row of the identity already holds into that row
        await a.memory.set("via_set", "Deploys on Tue and Thu.", memory_type="semantic")
        await a.memory.set("via_bulk", "Backups run at 02:00 UTC.", memory_type="semantic")
        for key in ("via_set", "via_bulk"):
            v = await a.memory.verify(key)
            assert v["verification"] == "human"
        await a.memory.set("via_set", "Deploys on any weekday.", memory_type="semantic")
        await a.memory.bulk_set(
            [
                {
                    "memory_key": "via_bulk",
                    "value": "Backups run at 03:30 UTC.",
                    "memory_type": "semantic",
                }
            ]
        )
        for key in ("via_set", "via_bulk"):
            got = await a.memory.get(key)
            assert got["verification"] == "stale", key
            assert got["verified_by"] == "human:alice", key
