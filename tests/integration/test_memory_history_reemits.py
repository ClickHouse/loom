"""memory.history leaves out the entity tagger's re-emits and keeps every change
a caller made: tags, attestations, values — also past a run of re-emits longer
than the limit."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("history-reemit")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def _tag_entities(ns: str, key: str, entities: list[str]) -> None:
    from server.memory.store.entities import apply_entities_batch

    await apply_entities_batch(
        [
            {
                "org": ORG,
                "namespace": ns,
                "agent": "t",
                "user_id": "-",
                "session_id": "-",
                "memory_key": key,
                "entities": entities,
            }
        ]
    )


async def _history(a: Any, key: str, n: int, *, limit: int = 50) -> list[dict[str, Any]]:
    async def _n_rows() -> list[dict[str, Any]] | None:
        rows = await a.memory.history(key, limit=limit)
        return rows if len(rows) >= n else None

    return await eventually(_n_rows, description=f"{n} history rows of {key}")


@pytest.mark.asyncio
async def test_reemits_are_left_out_and_edits_stay(control_plane_url, mint_test_token) -> None:
    from loom import Agent

    ns = f"hist-reemit-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # Value edit, then twelve re-emits: more than the inspector's eight-row window.
        await a.memory.set("v", "First meaning")
        await a.memory.set("v", "Second meaning")
        await _history(a, "v", 2)
        await _tag_entities(ns, "v", ["meaning"])
        for i in range(11):
            await _tag_entities(ns, "v", ["meaning", f"e{i}"])
        rows = await _history(a, "v", 2, limit=8)
        assert [r["value"] for r in rows] == ["Second meaning", "First meaning"], rows

        # A tag change is a caller's edit.
        await a.memory.set("t", "owned", tags=["owner:old"])
        await a.memory.set("t", "owned", tags=["owner:new"])
        assert len(await _history(a, "t", 2)) == 2

        # Attest, then revoke: three versions.
        await a.memory.set("a", "attested")
        await _history(a, "a", 1)
        assert (await a.memory.verify("a")).get("changed")
        assert (await a.memory.verify("a", revoke=True)).get("changed")
        assert len(await _history(a, "a", 3)) == 3
