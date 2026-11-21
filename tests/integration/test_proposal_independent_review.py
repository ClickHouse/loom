"""Only an independent human administrator can approve a fact proposal."""

from __future__ import annotations

import httpx
import pytest

from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_proposal_acceptance_requires_an_independent_human_administrator(
    control_plane_url: str,
) -> None:
    from server.auth.jwt import mint_token

    org, ns = unique_org("proposal-review"), "facts"
    identity = {"org": org, "namespace": ns, "agent": "author", "memory_key": "candidate"}

    def token(subject: str, *, human: bool, role: str = "admin") -> str:
        return mint_token(
            subject=subject,
            org=org,
            namespaces=[ns],
            roles=[role],
            ttl_seconds=600,
            is_service_account=not human,
        )

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as client:

        async def post(op: str, body: dict, bearer: str | None):
            headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
            return await client.post(f"/v1/{op}", json=body, headers=headers)

        author = token("proposer", human=False)
        wrote = await post(
            "memory.set",
            {**identity, "value": "A candidate fact awaiting review", "proposed": True},
            author,
        )
        assert wrote.status_code == 200 and wrote.json()["status"] == "proposed", wrote.text
        promoted = await post("memory.promote", identity, author)
        assert promoted.status_code == 403, promoted.text
        decision = {
            "org": org,
            "namespace": ns,
            "memory_keys": ["candidate"],
            "decision": "accept",
            "approved_sha256": {"candidate": wrote.json()["content_sha256"]},
        }
        for bearer in (None, author, token("reviewer", human=True, role="writer")):
            refused = await post("memory.proposal_decide", decision, bearer)
            assert refused.status_code == 403, refused.text
        self_approval = await post(
            "memory.proposal_decide", decision, token("proposer", human=True)
        )
        assert self_approval.status_code == 200 and self_approval.json()["decided"] == 0, (
            self_approval.text
        )
        accepted = await post("memory.proposal_decide", decision, token("reviewer", human=True))
        assert accepted.status_code == 200 and accepted.json()["decided"] == 1, accepted.text
        got = await post("memory.get", identity, author)
        assert got.status_code == 200, got.text
        assert got.json()["status"] == "active"
        assert got.json()["verified_by"] == "human:reviewer"
        assert got.json()["content_sha256"] == wrote.json()["content_sha256"]
