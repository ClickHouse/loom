"""A live setting's override is stored in loom.runtime_overrides, listed as the value in
force, and removed by a reset."""

from __future__ import annotations

import json

import httpx
import pytest

# Read only when the mentor's cache is refilled: no other test sees the override.
NAME = "mentor_ttl_seconds"


def _stored(ch_client) -> list[tuple[str, int]]:
    return ch_client.query(
        "SELECT value, deleted FROM loom.runtime_overrides FINAL "
        "WHERE kind = 'setting' AND key = {key:String}",
        parameters={"key": json.dumps([NAME])},
    ).result_rows


async def _listed(c: httpx.AsyncClient) -> dict:
    r = await c.post("/v1/admin/settings.list", json={"prefix": NAME})
    assert r.status_code == 200, r.text
    (row,) = [s for s in r.json()["settings"] if s["name"] == NAME]
    return row


@pytest.mark.asyncio
async def test_set_list_and_reset_a_live_setting(control_plane_url: str, ch_client) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as c:
        before = await _listed(c)
        assert before["live"] is True and before["source"] != "override"
        try:
            r = await c.post("/v1/admin/settings.set", json={"name": NAME, "value": "1234"})
            assert r.status_code == 200, r.text
            assert r.json()["setting"]["value"] == 1234
            listed = await _listed(c)
            assert (listed["value"], listed["source"]) == (1234, "override")
            assert _stored(ch_client) == [("1234", 0)]
        finally:
            r = await c.post("/v1/admin/settings.set", json={"name": NAME, "clear": True})
            assert r.status_code == 200, r.text
        after = await _listed(c)
        assert (after["value"], after["source"]) == (before["value"], before["source"])
        assert [d for _, d in _stored(ch_client)] == [1]


@pytest.mark.asyncio
async def test_a_setting_read_at_startup_is_refused(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as c:
        r = await c.post("/v1/admin/settings.set", json={"name": "ch_host", "value": "x"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"
