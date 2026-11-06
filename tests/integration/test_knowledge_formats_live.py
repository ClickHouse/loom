"""`knowledge.formats` against a running server: every format it routes with the
names that select it, the upload's limits, and as available the formats its
configured extractors declare, as the server's own routing reads them.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_the_formats_the_deployment_reads_and_the_upload_limits(
    control_plane_url: str,
) -> None:
    org, ns = unique_org("formats"), f"docs-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        r = await c.post("/v1/knowledge.formats", json={"org": org, "namespace": ns})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    by = {f["format"]: f for f in out["formats"]}
    assert {"markdown", "pdf", "csv", "text"} <= set(by)
    assert "md" in by["markdown"]["extensions"] and "application/pdf" in by["pdf"]["media_types"]
    assert out["max_upload_bytes"] > 0 and out["refused_extensions"] == ["doc", "ppt", "xls"]
    # What the server's own routing reads, asked of the same extractors.
    from server.knowledge.app.extractors import available_formats

    served = await available_formats()
    if not served:
        pytest.skip("no configured extractor declares a format: nothing to compare")
    assert {f for f, v in by.items() if v["available"]} == set(served)
