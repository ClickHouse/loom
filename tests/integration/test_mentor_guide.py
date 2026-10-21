"""POST /v1/mentor.guide — the agent guide the image ships, for a viewer token."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.integration._resources import unique_org

ROOT = Path(__file__).resolve().parents[2]

ORG = unique_org("mentor-guide")


@pytest.mark.parametrize("part", ["skill", "reference", "all"])
async def test_a_viewer_reads_each_part_of_the_guide(
    control_plane_url: str, mint_test_token, part: str
) -> None:
    token = mint_test_token(org=ORG, namespaces=["guide-reader"], roles=["viewer"])
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
