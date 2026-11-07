"""Audit v1 lifecycle against a live Loom + CH, under a token minted for a fresh org."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import minted_identity  # noqa: E402


def _post(url: str, path: str, body: dict, token: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url=url, timeout=8.0) as c:
        return c.post(path, json=body, headers=headers)


@pytest.mark.asyncio
async def test_hybrid_search_lexical_catches_exact_symbol(
    control_plane_url,
    mint_test_token,
) -> None:
    """Lexical search returns only rows containing the query substring."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"ax-{suffix}")
    body = {
        "org": me["org"],
        "namespace": me["namespace"],
        "agent": "a",
        "user_id": "-",
        "memory_key": "crash",
        "memory_type": "preference",
        "value": "the crash happens in ColumnObject getExtremes",
    }
    assert _post(control_plane_url, "/v1/memory.set", body, me["token"]).status_code == 200

    r = _post(
        control_plane_url,
        "/v1/memory.search",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "query": "ColumnObject",
            "search_mode": "lexical",
        },
        me["token"],
    )
    assert r.status_code == 200, r.text
    keys = [h["memory_key"] for h in r.json()["results"]]
    assert "crash" in keys

    r = _post(
        control_plane_url,
        "/v1/memory.search",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "query": "completely-absent-token",
            "search_mode": "lexical",
        },
        me["token"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"] == []


@pytest.mark.asyncio
async def test_per_type_retention_materializes_expires_at(
    control_plane_url,
    mint_test_token,
    ch_client,
) -> None:
    """Per-type retention from tag_registry materializes expires_at on memory.set."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"bx-{suffix}")

    r = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "owner_email": me["subject"],
            "tag_registry": [
                {"memory_type": "preference", "retention_days": 7},
            ],
        },
        me["token"],
    )
    assert r.status_code == 200, r.text

    body = {
        "org": me["org"],
        "namespace": me["namespace"],
        "agent": "a",
        "user_id": "-",
        "memory_key": "kx",
        "memory_type": "preference",
        "value": "vx",
    }
    set_resp = _post(control_plane_url, "/v1/memory.set", body, me["token"])
    assert set_resp.status_code == 200, set_resp.text
    memory_id = set_resp.json()["memory_id"]

    row = ch_client.query(
        f"SELECT expires_at FROM loom.memory FINAL WHERE memory_id = toUUID('{memory_id}')"
    ).first_item
    assert row is not None
    assert row["expires_at"] is not None, "expires_at should be set by per-type retention"


@pytest.mark.asyncio
async def test_memory_outcome_bumps_evidence_count(
    control_plane_url,
    mint_test_token,
    ch_client,
) -> None:
    """memory.outcome positive bumps evidence_count."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"cx-{suffix}")
    set_resp = _post(
        control_plane_url,
        "/v1/memory.set",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "user_id": "-",
            "memory_key": "k",
            "memory_type": "preference",
            "value": "V",
        },
        me["token"],
    )
    assert set_resp.status_code == 200
    memory_id = set_resp.json()["memory_id"]

    last_evidence_count = 0
    for _ in range(3):
        r = _post(
            control_plane_url,
            "/v1/memory.outcome",
            {
                "org": me["org"],
                "namespace": me["namespace"],
                "memory_id": memory_id,
                "outcome": "positive",
                "weight": 1,
            },
            me["token"],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["recorded"] is True
        last_evidence_count = int(body.get("evidence_count_after") or 0)

    # MV propagation can lag the API return; poll the totals table.
    deadline = time.monotonic() + 30
    totals_count = last_evidence_count
    while time.monotonic() < deadline:
        row = ch_client.query(
            "SELECT sum(evidence_count) AS evidence_count "
            "FROM loom.memory_usage_totals "
            f"WHERE memory_id = toUUID('{memory_id}')"
        ).first_item
        if (
            row is not None
            and row["evidence_count"] is not None
            and int(row["evidence_count"]) >= 3
        ):
            totals_count = int(row["evidence_count"])
            break
        await asyncio.sleep(0.5)
    assert totals_count >= 3, (
        f"evidence_count should reflect the 3 positive outcomes; "
        f"saw totals={totals_count}, api-returned={last_evidence_count}"
    )


@pytest.mark.asyncio
async def test_memory_list_sort_by_score(
    control_plane_url,
    mint_test_token,
) -> None:
    """memory.list sort_by=score returns usage-signal fields per row."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"dx-{suffix}")

    for i, key in enumerate(["a", "b", "c"]):
        _post(
            control_plane_url,
            "/v1/memory.set",
            {
                "org": me["org"],
                "namespace": me["namespace"],
                "agent": "a",
                "user_id": "-",
                "memory_key": key,
                "memory_type": "preference",
                "value": f"v{i}",
            },
            me["token"],
        )

    r = _post(
        control_plane_url,
        "/v1/memory.list",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "sort_by": "score",
        },
        me["token"],
    )
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    assert len(entries) == 3
    for e in entries:
        assert "evidence_count" in e
        assert "recall_count" in e
        assert "last_recalled_at" in e


@pytest.mark.asyncio
async def test_namespace_get_returns_audit_v1_fields(
    control_plane_url,
    mint_test_token,
) -> None:
    """namespace.get surfaces the audit v1 config fields."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"ex-{suffix}")

    reg = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "owner_email": me["subject"],
            # A rerank choice is a name, never a URL (a URL is a 422).
            "rerank_endpoint": "builtin:cohere",
            "classify_endpoint": "https://classifier.example/c",
            "tag_registry": [
                {
                    "memory_type": "principle",
                    "retention_days": 0,
                    "dedup_threshold": 0.85,
                    "hot_quota": 5,
                    "conflict_policy": "manual_review",
                },
            ],
        },
        me["token"],
    )
    assert reg.status_code == 200, reg.text

    r = _post(
        control_plane_url,
        "/v1/namespace.get",
        {
            "org": me["org"],
            "namespace": me["namespace"],
        },
        me["token"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] is True
    assert body["rerank_endpoint"] == "builtin:cohere"
    assert body["classify_endpoint"] == "https://classifier.example/c"
    tag_registry = body["tag_registry"]
    assert len(tag_registry) == 1
    assert tag_registry[0]["memory_type"] == "principle"
    # CH Float32 round-trips with single-precision error (0.85 → 0.85000…)
    assert abs(tag_registry[0]["dedup_threshold"] - 0.85) < 1e-4
    assert tag_registry[0]["conflict_policy"] == "manual_review"
