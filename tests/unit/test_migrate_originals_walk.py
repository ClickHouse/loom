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

    async def move(held: blobs.Held) -> bool:
        out = outcomes[held.sha]
        if isinstance(out, Exception):
            raise out
        return bool(out)

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    monkeypatch.setattr(blobs, "needed", needed)
    monkeypatch.setattr(blobs, "move_to_bucket", move)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert (result.unneeded, result.moved, result.skipped) == (1, 2, 1)
    assert result.bytes == 20
    (error,) = result.errors
    assert error.startswith(f"acme/docs/{page[2].sha}: ") and "too long" in error
    (unconfirmed,) = result.left_to_erasure
    assert unconfirmed.startswith(f"acme/docs/{page[3].sha}: switched")
