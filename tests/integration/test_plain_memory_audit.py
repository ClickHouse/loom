"""Plain memory writes persist the value and one hash-only audit receipt."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from server.core.hashing import sha256_value
from tests.integration._eventually import eventually
from tests.integration._resources import minted_identity


@pytest.mark.parametrize("kind,high", [("episodic", False), ("profile", False), ("semantic", True)])
async def test_plain_memory_write_has_hash_only_receipt(
    control_plane_url: str, mint_test_token: Any, ch_client: Any, kind: str, high: bool
) -> None:
    me = minted_identity(mint_test_token, "plain-audit")
    identity = {
        "org": me["org"],
        "namespace": me["namespace"],
        "agent": "writer",
        "memory_key": "decision",
    }
    value = {"private_note": "only the memory row may contain this value"}
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {me['token']}"},
        timeout=30.0,
    ) as client:
        response = await client.post(
            "/v1/memory.set",
            json={
                **identity,
                "value": value,
                "memory_type": kind,
                "sensitivity": "high" if high else "low",
                "source_class": "external",
                "pinned": True,
            },
        )
        assert response.status_code == 200, response.text
        written = response.json()
        response = await client.post("/v1/memory.get", json=identity)
        assert response.status_code == 200, response.text
        stored = response.json()
    assert stored["found"]
    assert stored["value"] == value
    assert stored["memory_id"] == written["memory_id"]
    assert stored["content_sha256"] == written["content_sha256"] == sha256_value(value)
    assert stored["memory_type"] == kind
    assert stored["sensitivity"] == ("high" if high else "low")
    assert stored["source_class"] == "external"
    assert stored["pinned"] is True

    async def receipts() -> Any:
        return ch_client.query(
            "SELECT event_type, actor, subject, row_hash FROM loom.audit_log "
            "WHERE org = {org:String} AND namespace = {namespace:String} "
            "AND event_type IN ('memory.set', 'memory.revision') "
            "AND JSONExtractString(subject, 'memory_key') = {memory_key:String} "
            "LIMIT 2",
            parameters=identity,
        ).result_rows

    rows = await eventually(receipts, description="plain write audit receipt")
    assert len(rows) == 1
    event, actor, raw_subject, row_hash = rows[0]
    subject = json.loads(raw_subject)
    assert subject["memory_key"] == identity["memory_key"]
    assert subject["memory_id"] == written["memory_id"]
    assert subject["content_sha256"] == sha256_value(value)
    assert subject["source_class"] == "external"
    assert subject["pinned"] is True
    assert actor == (stored["actor"] or "-")
    if event == "memory.set":
        assert subject["verdict"] == ""
        assert set(subject) == {
            "memory_key",
            "memory_id",
            "content_sha256",
            "source_class",
            "verdict",
            "pinned",
        }
    assert "value" not in subject and "serialized_value" not in subject
    assert value["private_note"] not in raw_subject
    if isinstance(row_hash, bytes):
        row_hash = row_hash.decode().rstrip("\x00")
    assert len(row_hash) == 64
