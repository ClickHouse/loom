"""Stale digest candidates cannot hide a later page of proven facts."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from server.core.datetimes import naive_utc_now
from tests.integration._resources import unique_org


@pytest.mark.parametrize("op", ["memory.digests", "memory.outline", "memory.export_evidence"])
async def test_digest_listings_scan_past_withheld_sources(
    control_plane_url, mint_test_token, ch_client, op
):
    org = unique_org("digest-pages")
    token = mint_test_token(org=org, namespaces=["facts"], roles=["writer"])
    identity = dict(org=org, namespace="facts", agent="a", user_id="subject")
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        for i in (2, 3):
            response = await client.post(
                "/v1/memory.set",
                json={**identity, "session_id": f"s{i}", "memory_key": "k", "value": f"fact {i}"},
            )
            assert response.status_code == 200, response.text
        now = naive_utc_now()
        columns = [
            "org",
            "namespace",
            "agent",
            "user_id",
            "session_id",
            "embedding_model",
            "summary_text",
            "key_facts",
            "derived_from",
            "created_at",
            "version",
            "summary_embedding",
        ]
        ch_client.insert(
            "loom.session_summaries",
            [
                [
                    org,
                    "facts",
                    "a",
                    "subject",
                    f"s{i}",
                    "test",
                    f"fact {i}",
                    [f"fact {i}"],
                    ["k"] if i >= 2 else [],
                    now + timedelta(seconds=-i if op == "memory.outline" else i),
                    1,
                    [0.0] * 3072,
                ]
                for i in range(4)
            ],
            column_names=columns,
            settings={"async_insert": 0},
        )
        fields = (
            {"sessions_limit": 1, "hubs_limit": 0, "communities_limit": 0}
            if op == "memory.outline"
            else {"limit": 1}
        )
        if op == "memory.export_evidence":
            fields["kind"] = "digests"
        response = await client.post(f"/v1/{op}", json={**identity, **fields})
        assert response.status_code == 200, response.text
        result = response.json()
        field = {
            "memory.digests": "rows",
            "memory.outline": "sessions",
            "memory.export_evidence": "digests",
        }[op]
        assert [row["session_id"] for row in result[field]] == ["s2"], result
        if op == "memory.digests":
            assert result["truncated"] is True
        if op == "memory.export_evidence":
            assert result["has_more"] is True
