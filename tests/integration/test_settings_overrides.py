"""A live setting's override is stored in loom.runtime_overrides, listed as the value in
force, recorded in the change history, and removed by a reset."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually

# Read only when the mentor's cache is refilled: no other test sees the override.
NAME = "mentor_ttl_seconds"


def _client(control_plane_url: str) -> httpx.AsyncClient:
    tok = os.environ.get("LOOM_API_KEY")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    return httpx.AsyncClient(base_url=control_plane_url, timeout=10.0, headers=headers)


def _stored(ch_client) -> list[tuple[str, int]]:
    return ch_client.query(
        "SELECT value, deleted FROM loom.runtime_overrides FINAL "
        "WHERE kind = 'setting' AND key = {key:String}",
        parameters={"key": json.dumps([NAME])},
    ).result_rows


async def _listed(c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.post("/v1/admin/settings.list", json={"prefix": NAME})
    assert r.status_code == 200, r.text
    (row,) = [s for s in r.json()["settings"] if s["name"] == NAME]
    return row


async def _listed_as(c: httpx.AsyncClient, value: Any, source: str) -> dict[str, Any]:
    """The listing once the process that answers has adopted the change: with
    several processes, the one that took the write may not answer the read."""

    async def _seen() -> dict[str, Any] | None:
        row = await _listed(c)
        return row if (row["value"], row["source"]) == (value, source) else None

    return await eventually(_seen, max_wait=30, description=f"{NAME} = {value} ({source})")


@pytest.mark.asyncio
async def test_set_list_and_reset_a_live_setting(control_plane_url: str, ch_client) -> None:
    async with _client(control_plane_url) as c:
        before = await _listed(c)
        assert before["live"] is True and before["source"] != "override"
        try:
            r = await c.post("/v1/admin/settings.set", json={"name": NAME, "value": "1234"})
            assert r.status_code == 200, r.text
            assert r.json()["setting"]["value"] == 1234
            await _listed_as(c, 1234, "override")
            assert _stored(ch_client) == [("1234", 0)]
        finally:
            r = await c.post("/v1/admin/settings.set", json={"name": NAME, "clear": True})
            assert r.status_code == 200, r.text
        await _listed_as(c, before["value"], before["source"])
        assert [d for _, d in _stored(ch_client)] == [1]


@pytest.mark.asyncio
async def test_a_change_names_the_value_it_replaces_and_is_recorded(
    control_plane_url: str,
) -> None:
    async with _client(control_plane_url) as c:
        before = (await _listed(c))["value"]
        try:
            r = await c.post(
                "/v1/admin/settings.set", json={"name": NAME, "value": 999, "expected": before + 1}
            )
            assert r.status_code == 409 and r.json()["detail"]["current"] == before
            r = await c.post(
                "/v1/admin/settings.set", json={"name": NAME, "value": 999, "expected": before}
            )
            assert r.status_code == 200, r.text
        finally:
            await c.post("/v1/admin/settings.set", json={"name": NAME, "clear": True})

        async def _two() -> list[dict[str, Any]] | None:
            r = await c.post("/v1/admin/settings.history", json={"name": NAME, "limit": 2})
            assert r.status_code == 200, r.text
            changes = r.json()["changes"]
            return changes if len(changes) == 2 and changes[0]["cleared"] else None

        reset, made = await eventually(_two, max_wait=30, description="two recorded changes")
        assert (made["previous"], made["value"], made["cleared"]) == (before, 999, False)
        assert (reset["previous"], reset["value"]) == (999, before)


@pytest.mark.asyncio
async def test_a_setting_read_at_startup_is_refused(control_plane_url: str) -> None:
    async with _client(control_plane_url) as c:
        r = await c.post("/v1/admin/settings.set", json={"name": "ch_host", "value": "x"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"
