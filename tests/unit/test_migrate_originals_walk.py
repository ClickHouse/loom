"""How `migrate_originals` counts each original of a page: one nothing needs is left
to the sweep, a refused one (an object key too long included) is an error and the walk
goes on, and one switched but unconfirmed is listed apart. No ClickHouse: the store
calls are stubbed."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.errors import ValidationError
from server.knowledge.app import originals
from server.knowledge.store import blobs


def _held(n: int) -> blobs.Held:
    return blobs.Held("acme", "docs", f"{n:064x}", 10)


@pytest.mark.asyncio
async def test_each_original_lands_in_its_own_count(monkeypatch: pytest.MonkeyPatch) -> None:
    page = [_held(i) for i in range(5)]

    async def held_in_clickhouse(org: str, ns: str, after: str, limit: int) -> blobs.HeldPage:
        return blobs.HeldPage(page, "") if after == "" else blobs.HeldPage([], "")

    async def needed(org: str, ns: str, shas: list[str]) -> set[str]:
        return set(shas[1:])  # the first: nothing needs it

    outcomes: dict[str, Any] = {
        page[1].sha: True,
        page[2].sha: ValidationError("the org and namespace names are too long for an object key"),
        page[3].sha: blobs.SwitchedUnderErasure("switched to s3://b/k, but the check failed"),
        page[4].sha: False,
    }

    fences: list[int] = []

    async def fence(org: str, ns: str) -> blobs.Fence:
        fences.append(1)
        return blobs.Fence(0)

    async def move(held: blobs.Held, fenced: blobs.Fence | None = None) -> bool:
        out = outcomes[held.sha]
        if isinstance(out, Exception):
            raise out
        return bool(out)

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    monkeypatch.setattr(blobs, "needed", needed)
    monkeypatch.setattr(blobs, "move_to_bucket", move)
    monkeypatch.setattr(blobs, "fence", fence)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert (result.unneeded, result.moved, result.skipped) == (1, 2, 1)
    assert result.bytes == 20
    (error,) = result.errors
    assert error.startswith(f"acme/docs/{page[2].sha}: ") and "too long" in error
    # One read for the page: no erasure held an original back.
    assert len(fences) == 1
    (unconfirmed,) = result.left_to_erasure
    assert unconfirmed.startswith(f"acme/docs/{page[3].sha}: switched")


@pytest.mark.asyncio
async def test_an_erasure_holding_one_back_rereads_the_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = [_held(i) for i in range(3)]
    reads: list[str] = []

    async def held_in_clickhouse(org: str, ns: str, after: str, limit: int) -> blobs.HeldPage:
        return blobs.HeldPage(page, "") if after == "" else blobs.HeldPage([], "")

    async def needed(org: str, ns: str, shas: list[str]) -> set[str]:
        return set(shas)

    async def fence(org: str, ns: str) -> blobs.Fence:
        reads.append("fence")
        # The second read sees the erasure that held the first original back.
        return blobs.Fence(
            0, "erasure e1 (namespace, in_progress) covers it" if len(reads) > 1 else ""
        )

    async def move(held: blobs.Held, fenced: blobs.Fence | None = None) -> bool:
        if held is page[0] or (fenced and fenced.refusal):
            raise blobs.ErasureOpen(
                "erasure e1 (namespace, in_progress) started while it was copied"
            )
        return True

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    monkeypatch.setattr(blobs, "needed", needed)
    monkeypatch.setattr(blobs, "fence", fence)
    monkeypatch.setattr(blobs, "move_to_bucket", move)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert result.moved == 0 and len(result.errors) == 3
    # The page's read, then one after each original an erasure held back.
    assert reads == ["fence"] * 4
