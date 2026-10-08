"""memory.touch — extend TTL without rewriting value."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("mtouch")


@pytest.mark.asyncio
async def test_touch_extends_ttl(control_plane_url: str, mint_test_token) -> None:
    """A memory written with ttl_seconds=3600, then touched with
    ttl_seconds=7200, has a later expires_at."""
    from loom import Agent

    ns = f"mt-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic", ttl_seconds=3600)
        first = await a.memory.get("k")
        result = await a.memory.touch("k", ttl_seconds=7200)
        second = await a.memory.get("k")

    assert result["touched"] is True
    assert result["version"] > first["version"]
    # expires_at should be later after the touch
    assert second["expires_at"] > first["expires_at"]
    # value unchanged
    assert second["value"] == first["value"]


@pytest.mark.asyncio
async def test_touch_missing_key_returns_false(control_plane_url: str, mint_test_token) -> None:
    from loom import Agent

    ns = f"mt-miss-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        result = await a.memory.touch("nonexistent", ttl_seconds=60)
    assert result["touched"] is False
    assert result["version"] is None


@pytest.mark.asyncio
async def test_touch_tombstoned_returns_false(control_plane_url: str, mint_test_token) -> None:
    """A deleted (tombstoned) key can't be touched."""
    from loom import Agent

    ns = f"mt-tomb-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
        await a.memory.delete("k")
        result = await a.memory.touch("k", ttl_seconds=60)
    assert result["touched"] is False


@pytest.mark.asyncio
async def test_touch_preserves_value_and_type(control_plane_url: str, mint_test_token) -> None:
    from loom import Agent

    ns = f"mt-pres-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set(
            "k",
            {"x": "complex", "y": [1, 2, 3]},
            memory_type="preference",
            confidence=0.7,
        )
        await a.memory.touch("k", ttl_seconds=120)
        result = await a.memory.get("k")
    assert result["value"] == {"x": "complex", "y": [1, 2, 3]}
    assert result["memory_type"] == "preference"
    assert abs(result["confidence"] - 0.7) < 1e-5  # Float32 round-trip
