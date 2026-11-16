"""The collection tree's counts follow the document list through revisions and moves."""

from __future__ import annotations

from typing import Any

import httpx

from tests.integration._resources import unique_org


def test_collection_counts_follow_current_documents(
    control_plane_url: str, mint_test_token: Any
) -> None:
    org = unique_org("colcounts")
    token = mint_test_token(
        org=org, namespaces=["*"], roles=["writer", "admin", "org_admin"], subject="counter"
    )
    with httpx.Client(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ) as client:

        def call(op: str, **body: Any) -> dict[str, Any]:
            response = client.post(f"/v1/{op}", json={"org": org, **body})
            assert response.status_code == 200, response.text
            return response.json()

        call("collection.create", namespace="col-parent")
        call("collection.create", namespace="col-child", parent="col-parent")
        call("collection.create", namespace="col-empty")

        def counts() -> dict[str, int]:
            page = call("collection.list", include_document_counts=True)
            return {r["namespace"]: r["document_count"] for r in page["collections"]}

        assert counts() == {"col-parent": 0, "col-child": 0, "col-empty": 0}
        for ns in ("col-parent", "col-child"):
            call("knowledge.add", namespace=ns, source_uri="doc://a", content="First version")
        call("knowledge.add", namespace="col-child", source_uri="doc://a", content="Second version")
        assert counts() == {"col-parent": 1, "col-child": 1, "col-empty": 0}
        call(
            "knowledge.move",
            namespace="col-child",
            to_namespace="col-empty",
            source_uris=["doc://a"],
        )
        assert counts() == {"col-parent": 1, "col-child": 0, "col-empty": 1}
        call("knowledge.delete", namespace="col-parent", source_uri="doc://a")
        assert counts() == {"col-parent": 0, "col-child": 0, "col-empty": 1}
        call("knowledge.restore", namespace="col-parent", source_uri="doc://a")
        assert counts() == {"col-parent": 1, "col-child": 0, "col-empty": 1}
        for row in call("collection.list")["collections"]:
            assert row["document_count"] is None
