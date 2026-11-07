"""A source's delete reaches the summaries built from it, on either authority: one with
another live source is downgraded on both tables, `stale-source:<key>` included; one
with none left is tombstoned with it."""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org


@pytest.fixture
def org() -> str:
    return unique_org("summaries")


@pytest.fixture
def call(control_plane_url: str, mint_test_token: Any, org: str) -> Any:
    os.environ.get("LOOM_BASE_URL") or pytest.skip("LOOM_BASE_URL not set")
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer"])

    async def _call(op: str, body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
            return await c.post(
                f"/v1/{op}", json={"org": org, **body}, headers={"Authorization": f"Bearer {token}"}
            )

    return _call


def _set(ns: str, key: str, value: str, **extra: Any) -> dict[str, Any]:
    return {
        "namespace": ns,
        "agent": "a",
        "memory_key": key,
        "memory_type": "semantic",
        "value": value,
        **extra,
    }


async def test_a_summary_is_downgraded_then_tombstoned_as_its_sources_go(
    call: Any, ch_client: Any, org: str
) -> None:
    ns = f"sum-{uuid.uuid4().hex[:8]}"
    for key in ("k1", "k2"):
        assert (await call("memory.set", _set(ns, key, f"a source fact {key}"))).status_code == 200
    summary = _set(
        ns, "summary", "a fact both sources support", tags=["consolidated", "src:k1", "src:k2"]
    )
    assert (await call("memory.set", summary)).status_code == 200

    first = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k1"})
    assert first.status_code == 200, first.text
    assert (first.json()["dependents_downgraded"], first.json()["dependents_tombstoned"]) == (1, 0)

    def live_vectors() -> list[tuple[list[str], str]]:
        return ch_client.query(
            "SELECT tags, status FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = 'summary' AND tombstoned_at IS NULL",
            settings={"select_sequential_consistency": 1},
            parameters={"o": org, "n": ns},
        ).result_rows

    # search reads the vector rows: they carry the downgrade's tag, not only its status
    rows = live_vectors()
    assert rows and all(status == "conflicted" for _, status in rows), rows
    assert all("stale-source:k1" in tags for tags, _ in rows), rows

    second = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k2"})
    assert second.status_code == 200, second.text
    assert second.json()["dependents_tombstoned"] == 1, second.text
    got = await call("memory.get", {"namespace": ns, "agent": "a", "memory_key": "summary"})
    assert got.status_code == 200 and not got.json().get("found"), got.text
    assert live_vectors() == []  # its vectors retired with it
