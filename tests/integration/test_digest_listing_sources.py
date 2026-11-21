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


@pytest.mark.parametrize("page_size", [1, 2])
async def test_period_pages_choose_latest_model_before_applying_the_cursor(ch_client, page_size):
    from server.core.ch import get_client
    from server.memory.app.period_digest import _fetch_digests

    org, now = unique_org("period-pages"), naive_utc_now()
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
    rows = [
        [
            org,
            "facts",
            "a",
            "subject",
            sid,
            model,
            text,
            [text],
            ["k"],
            now + timedelta(seconds=offset),
            version,
            [0.0] * 3072,
        ]
        for sid, model, text, offset, version in (
            ("s1", "old", "retired fact", 10, 1),
            ("s1", "middle", "intermediate fact", 5, 2),
            ("s1", "current", "current fact", 0, 3),
            ("s2", "current", "second fact", 1, 1),
            ("s3", "current", "third fact", 2, 1),
        )
    ]
    ch_client.insert(
        "loom.session_summaries", rows, column_names=columns, settings={"async_insert": 0}
    )
    ch = get_client()
    await ch.connect()
    try:
        identity = dict(org=org, namespace="facts", agent="a", user_id="subject")
        found = [
            row async for page in _fetch_digests(ch, identity, page_size=page_size) for row in page
        ]
    finally:
        await ch.disconnect()
    assert [row["session_id"] for row in found] == ["s1", "s2", "s3"]
    assert found[0]["summary_text"] == "current fact" and found[0]["version"] == 3
