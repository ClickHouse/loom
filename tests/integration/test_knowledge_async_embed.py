"""knowledge.add embeds its chunks inline: search finds them at once, with
no worker pass. (An embed failure fails the add; no chunk stub is written.)"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kasync")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.fixture(autouse=True)
async def _ch_connected():
    """Function-scoped — see test_auto_promote + test_embedding_worker.
    Also resets the embedder singleton so the per-test event loop
    doesn't inherit a closed httpx.AsyncClient."""
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.mark.asyncio
async def test_inline_path_still_default(control_plane_url, ch_client) -> None:
    """knowledge.add writes embedded chunks inline; search returns them
    immediately without waiting for the worker."""
    from loom import Agent

    ns = f"kasync-inline-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="inline embed content unique here",
            source_uri=f"doc://inline-{uuid.uuid4().hex[:6]}",
            title="t",
        )
        # No worker run — should still find via search
        hits = await a.knowledge.search("inline embed content", top_k=5)
    assert hits, "inline path must return search hits without worker"
