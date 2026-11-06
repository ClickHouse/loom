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

    async def fence(org: str, ns: str) -> str:
        fences.append(1)
        return ""

    async def move(held: blobs.Held) -> bool:
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
    # One read for the namespace, one after the switch an erasure raced, and one after the
    # original that was not switched (an erasure may be what took it).
    assert len(fences) == 3
    (unconfirmed,) = result.left_to_erasure
    assert unconfirmed.startswith(f"acme/docs/{page[3].sha}: switched")


@pytest.mark.asyncio
async def test_an_erasure_holding_one_back_refuses_the_rest_of_the_namespace_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = [_held(i) for i in range(3)]
    reads: list[str] = []

    async def held_in_clickhouse(org: str, ns: str, after: str, limit: int) -> blobs.HeldPage:
        return blobs.HeldPage(page, "") if after == "" else blobs.HeldPage([], "")

    async def needed(org: str, ns: str, shas: list[str]) -> set[str]:
        return set(shas)

    async def fence(org: str, ns: str) -> str:
        reads.append("fence")
        # The second read sees the erasure that held the first original back.
        return "erasure e1 (namespace, in_progress) covers it" if len(reads) > 1 else ""

    async def move(held: blobs.Held) -> bool:
        if held is page[0]:
            raise blobs.ErasureOpen("erasure e1 (namespace, in_progress) overlapped its copy")
        return True

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    monkeypatch.setattr(blobs, "needed", needed)
    monkeypatch.setattr(blobs, "fence", fence)
    monkeypatch.setattr(blobs, "move_to_bucket", move)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    # The first is held back after its copy; the fence read again then refuses the rest
    # of the namespace in one line, before any more copies.
    assert result.moved == 0
    assert result.errors[0].startswith(f"acme/docs/{page[0].sha}: ")
    assert result.errors[1] == (
        "acme/docs: erasure e1 (namespace, in_progress) covers it; its originals are left in "
        "ClickHouse"
    )
    assert len(result.errors) == 2 and reads == ["fence", "fence"]


@pytest.mark.asyncio
async def test_a_namespace_the_fence_refuses_is_reported_once_and_not_walked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fence(org: str, ns: str) -> str:
        return "erasure e9 (user_id, failed) covers it; complete it first"

    async def held_in_clickhouse(*a: object) -> blobs.HeldPage:
        raise AssertionError("a refused namespace is not walked")

    monkeypatch.setattr(blobs, "fence", fence)
    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert result.errors == [
        "acme/docs: erasure e9 (user_id, failed) covers it; complete it first; its originals are "
        "left in ClickHouse"
    ]
