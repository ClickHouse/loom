"""One memory revision has the same guidance in search and file recall."""

from __future__ import annotations

import asyncio

import pytest

from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_search_and_file_follow_reviewed_content(control_plane_url, mint_test_token):
    from loom import Agent

    org = unique_org("retrieved-guidance")
    ns = "decisions"
    token = mint_test_token(org=org, namespaces=[ns], roles=["writer"])
    key = "refresh_catalog_index"
    path = "catalog/refresh.py"
    async with Agent(
        org=org, namespace=ns, agent="writer", base_url=control_plane_url, api_key=token
    ) as agent:
        written = await agent.memory.set(
            key, "Refresh the catalog index after imports.", files=[path]
        )

        async def check(review: str, step: str) -> None:
            for _ in range(25):
                file = await agent.call("memory.for_file", {"path": path})
                search = await agent.memory.search(
                    key, recipe="fast", auto_touch=False, namespace_scope="this"
                )
                file_hits = [hit for hit in file["hits"] if hit["memory_key"] == key]
                found = [hit for hit in search if hit.memory_key == key]
                if file_hits and found and found[0].use_guidance:
                    guidance = found[0].use_guidance
                    if guidance["review"] == review:
                        assert guidance["next_step"] == step
                        assert guidance == file_hits[0]["use_guidance"]
                        return
                await asyncio.sleep(0.2)
            pytest.fail(f"{key} did not reach {review} on both read paths")

        await check("none", "validate_lead")
        await agent.memory.verify(memory_id=written["memory_id"])
        await check("process", "validate_lead")
        await agent.memory.update_by_id(written["memory_id"], "Refresh the catalog before imports.")
        await check("stale", "obtain_review")
