"""POST /v1/mentor.guide — the agent guide the image ships, for a viewer token."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["skill", "reference", "all"])
async def test_a_viewer_reads_each_part_of_the_guide(control_plane_url: str, part: str) -> None:
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="sa-guide-reader", org="clickhouse", namespaces=["eng-tooling"], roles=["VIEWER"]
    )
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers={"Authorization": f"Bearer {token}"}
    ) as c:
        r = await c.post("/v1/mentor.guide", json={"part": part})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["part"] == part and body["version"]
    expected = ROOT / "skills" / "loom" / ("reference.md" if part == "reference" else "SKILL.md")
    first_heading = next(
        line for line in expected.read_text().splitlines() if line.startswith("# ")
    )
    assert body["text"].lstrip().startswith(first_heading)
