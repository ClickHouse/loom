"""Category search ranks only vectors in the query's embedding space."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

ORG = unique_org("category-search")


@pytest.mark.asyncio
@pytest.mark.parametrize("incompatible", ["model", "dimension"])
async def test_category_search_excludes_incompatible_embeddings(
    control_plane_url: str, mint_test_token: Any, ch_client: Any, incompatible: str
) -> None:
    ns = f"cats-{uuid.uuid4().hex[:8]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"], subject="writer")
    base = {"org": ORG, "namespace": ns}
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as client:
        registered = await client.post(
            "/v1/namespace.register_category", json={**base, "name": "billing"}
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["embedded"] is True
        (row,) = list(
            ch_client.query(
                "SELECT * FROM loom.categories FINAL "
                "WHERE org = {org:String} AND namespace = {ns:String}",
                parameters={"org": ORG, "ns": ns},
            ).named_results()
        )
        other = {**row, "category_id": uuid.uuid4().hex, "name": "incompatible"}
        if incompatible == "model":
            other["embedding_model"] = f"other-{row['embedding_model']}"
        else:
            other["embedding"] = [*row["embedding"], 0.5]
            other["embedding_dim"] = len(other["embedding"])
        ch_client.insert("loom.categories", [list(other.values())], column_names=list(other))

        response = await client.post(
            "/v1/namespace.search_categories", json={**base, "query": "billing", "top_k": 20}
        )
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert [hit["category_id"] for hit in results] == [registered.json()["category_id"]]
        assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-5)
