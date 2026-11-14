"""The lessons a code answer carries, read from live ClickHouse: rows written through
memory.set and matched to a result's file by its path, never by another directory's file
of the same name, a decision served whole."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("code-result-lessons")
PATH = "server/code/app/search/handler.py"
DECISION = "Decided: the search memo never stores a caller's memories, " * 6


def _set(base: str, token: str, key: str, value: str, files: list[str], **kw: Any) -> None:
    body = {"org": ORG, "namespace": "dev", "agent": "claude-code", "memory_key": key}
    body |= {"value": value, "files": files, **kw}
    r = httpx.post(
        f"{base}/v1/memory.set", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert r.status_code == 200, r.text


async def test_lessons_match_the_result_file_by_path(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    from server.core.ch import get_client
    from server.memory.app import path_lessons

    await get_client().connect()
    token = mint_test_token(org=ORG, namespaces=["*"], roles=["writer"])
    _set(control_plane_url, token, "notes/exact", "Pages are cut by tokens.", [PATH])
    _set(control_plane_url, token, "notes/bare", "Every handler logs its route.", ["handler.py"])
    _set(
        control_plane_url,
        token,
        "notes/other",
        "The memory handler drops weak hits.",
        ["server/memory/app/loom_search/handler.py"],
    )
    _set(control_plane_url, token, "design/memo", DECISION, [PATH], memory_type="decision")
    # More rows about other directories' handler.py than a small read would keep: the
    # exact path's rows still come back.
    for i in range(12):
        _set(
            control_plane_url,
            token,
            f"notes/elsewhere-{i}",
            f"Handler {i} retries twice.",
            [f"server/plane{i}/app/handler.py"],
            confidence=1.0,
        )

    rows: list[dict[str, Any]] = []
    for _ in range(50):
        rows = (
            await path_lessons.lessons_for_paths(ORG, ["dev"], "", [PATH], token_budget=2000)
        ).rows
        if len(rows) >= 3:
            break
        await asyncio.sleep(0.2)
    keys = {r["memory_key"] for r in rows}
    assert keys == {"notes/exact", "notes/bare", "design/memo"}, rows
    by_key = {r["memory_key"]: r for r in rows}
    assert by_key["notes/exact"] == {
        "path": PATH,
        "memory_key": "notes/exact",
        "namespace": "dev",
        "excerpt": "Pages are cut by tokens.",
        "code_state": "",
    }
    assert by_key["design/memo"]["excerpt"] == " ".join(DECISION.split())
    # a cap below the decision's size leaves it out, never cut
    small = (await path_lessons.lessons_for_paths(ORG, ["dev"], "", [PATH], token_budget=60)).rows
    assert "design/memo" not in {r["memory_key"] for r in small}
