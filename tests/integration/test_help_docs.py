"""POST /v1/help.search and /v1/help.get: the docs the image ships, for a viewer token."""

from __future__ import annotations

import httpx

from tests.integration._resources import unique_org

ORG = unique_org("help-docs")


async def test_a_viewer_finds_a_section_and_reads_it_back(
    control_plane_url: str, mint_test_token
) -> None:
    token = mint_test_token(org=ORG, namespaces=["help-reader"], roles=["viewer"])
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as c:
        found = await c.post("/v1/help.search", json={"query": "revoke and rotate a token"})
        assert found.status_code == 200, found.text
        body = found.json()
        assert body["available"], body
        hit = next(h for h in body["hits"] if h["path"] == "guides/tokens-and-grants.md")
        got = await c.post("/v1/help.get", json={"path": hit["path"], "section": hit["section"]})
        missing = await c.post("/v1/help.get", json={"path": "../.env"})
    assert got.status_code == 200, got.text
    section = got.json()
    assert section["found"] and section["anchor"] == hit["anchor"]
    assert section["text"].startswith("#")
    assert missing.status_code == 200 and not missing.json()["found"]
