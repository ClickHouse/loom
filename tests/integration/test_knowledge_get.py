"""knowledge.get — fetch a single document by source_uri."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kget")


@pytest.mark.asyncio
async def test_get_returns_document(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kg-{uuid.uuid4().hex[:6]}"
    src = f"doc://kg-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="Loom integrates memory and knowledge.",
            source_uri=src,
            title="intro",
            tags=["doc"],
            metadata={"author": "loom"},
        )
        result = await a.knowledge.get(src)
    assert result is not None
    assert result["source_uri"] == src
    assert result["title"] == "intro"
    assert result["content"] == "Loom integrates memory and knowledge."
    assert "doc" in result["tags"]
    assert result["metadata"]["author"] == "loom"
    assert result["superseded"] is False


@pytest.mark.asyncio
async def test_get_missing_returns_none(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kg-miss-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        assert await a.knowledge.get(f"doc://ghost-{uuid.uuid4().hex[:6]}") is None


@pytest.mark.asyncio
async def test_get_excludes_deleted_by_default(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kg-del-{uuid.uuid4().hex[:6]}"
    src = f"doc://del-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="x", source_uri=src, title="t")
        await a.knowledge.delete(src)
        # Default: superseded hidden
        assert await a.knowledge.get(src) is None
        # include_superseded=True surfaces the tombstoned row
        row = await a.knowledge.get(src, include_superseded=True)
    assert row is not None
    assert row["superseded"] is True


@pytest.mark.asyncio
async def test_get_returns_latest_after_upsert(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kg-up-{uuid.uuid4().hex[:6]}"
    src = f"doc://up-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="v1 content", source_uri=src, title="v1")
        await a.knowledge.upsert(content="v2 content", source_uri=src, title="v2")
        result = await a.knowledge.get(src)
    assert result is not None
    assert result["title"] == "v2"
    assert result["content"] == "v2 content"


@pytest.mark.asyncio
async def test_get_cross_namespace_isolation(control_plane_url: str) -> None:
    from loom import Agent

    ns_a = f"kg-a-{uuid.uuid4().hex[:6]}"
    ns_b = f"kg-b-{uuid.uuid4().hex[:6]}"
    src = "doc://shared-uri"  # same uri, different namespaces

    async with Agent(org=ORG, namespace=ns_a, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="A", source_uri=src, title="A")
    async with Agent(org=ORG, namespace=ns_b, agent="w", base_url=control_plane_url) as a:
        # ns_b doesn't see ns_a's doc
        assert await a.knowledge.get(src) is None
        await a.knowledge.add(content="B", source_uri=src, title="B")
        assert (await a.knowledge.get(src))["title"] == "B"
