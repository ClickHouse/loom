"""How `migrate_originals` counts each original of a page and each namespace: one nothing
needs is left to the sweep, a refused original is an error and the walk goes on, one
switched but unconfirmed is listed apart, and a namespace an erasure holds, or whose
object keys would be too long, is refused once (in a dry run too). No ClickHouse: the
store calls are stubbed."""

from __future__ import annotations

from typing import Any

import pytest

from server.knowledge.app import originals
from server.knowledge.store import blobs


def _held(n: int) -> blobs.Held:
    return blobs.Held("acme", "docs", f"{n:064x}", 10, 0)


@pytest.mark.asyncio
async def test_each_original_lands_in_its_own_count(monkeypatch: pytest.MonkeyPatch) -> None:
    page = [_held(i) for i in range(5)]

    async def held_in_clickhouse(org: str, ns: str, after: str, limit: int) -> blobs.HeldPage:
        return blobs.HeldPage(page, "") if after == "" else blobs.HeldPage([], "")

    async def needed(org: str, ns: str, shas: list[str]) -> set[str]:
        return set(shas[1:])  # the first: nothing needs it

    outcomes: dict[str, Any] = {
        page[1].sha: True,
        page[2].sha: originals.CorruptOriginal("bytes in ClickHouse do not hash to its sha256"),
        page[3].sha: originals.SwitchUnconfirmed("switched to s3://b/k, but the check failed"),
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
    monkeypatch.setattr(originals, "move_to_bucket", move)
    monkeypatch.setattr(originals, "fence", fence)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert (result.unneeded, result.moved, result.skipped) == (1, 2, 1)
    assert result.bytes == 20
    (error,) = result.errors
    assert error.startswith(f"acme/docs/{page[2].sha}: ") and "do not hash" in error
    # One read for the namespace, one after the switch that could not be confirmed, and
    # one after the original that was not switched (an erasure may be what took it).
    assert len(fences) == 3
    (unconfirmed,) = result.unconfirmed
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
            raise originals.ErasureOpen("erasure e1 (namespace, in_progress) overlapped its copy")
        return True

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    monkeypatch.setattr(blobs, "needed", needed)
    monkeypatch.setattr(originals, "fence", fence)
    monkeypatch.setattr(originals, "move_to_bucket", move)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    # The first is held back after its copy; the fence read again then refuses the rest
    # of the namespace in one line, before any more copies.
    assert result.moved == 0
    assert result.errors[0].startswith(f"acme/docs/{page[0].sha}: ")
    assert result.refused[0] == (
        "acme/docs: erasure e1 (namespace, in_progress) covers it; the rest of its originals are left in "
        "ClickHouse"
    )
    assert len(result.errors) == 1 and len(result.refused) == 1 and reads == ["fence", "fence"]


@pytest.mark.asyncio
async def test_a_namespace_the_fence_refuses_is_reported_once_and_not_walked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fence(org: str, ns: str) -> str:
        return "erasure e9 (user_id, failed) covers it; complete it first"

    async def held_in_clickhouse(*a: object) -> blobs.HeldPage:
        raise AssertionError("a refused namespace is not walked")

    monkeypatch.setattr(originals, "fence", fence)
    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert result.errors == []
    assert result.refused == [
        "acme/docs: erasure e9 (user_id, failed) covers it; complete it first; the rest of its originals "
        "are left in ClickHouse"
    ]


@pytest.mark.asyncio
async def test_an_erased_original_whose_take_back_failed_is_not_counted_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = [_held(0)]

    async def held_in_clickhouse(org: str, ns: str, after: str, limit: int) -> blobs.HeldPage:
        return blobs.HeldPage(page, "") if after == "" else blobs.HeldPage([], "")

    async def needed(org: str, ns: str, shas: list[str]) -> set[str]:
        return set(shas)

    async def fence(org: str, ns: str) -> str:
        return ""

    async def move(held: blobs.Held) -> bool:
        raise originals.SwitchUnconfirmed("switched as an erasure took it, but ...", moved=False)

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    monkeypatch.setattr(blobs, "needed", needed)
    monkeypatch.setattr(originals, "fence", fence)
    monkeypatch.setattr(originals, "move_to_bucket", move)
    result = await originals.migrate_originals(org="acme", namespace="docs")
    assert (result.moved, result.bytes, result.skipped) == (0, 0, 0)
    assert len(result.unconfirmed) == 1


@pytest.mark.asyncio
async def test_a_namespace_whose_keys_would_be_too_long_is_refused_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fence(org: str, ns: str) -> str:
        return ""

    async def held_in_clickhouse(*a: object) -> blobs.HeldPage:
        raise AssertionError("no original of it is read")

    monkeypatch.setattr(originals, "fence", fence)
    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    result = await originals.migrate_originals(org="acme", namespace="n" * 600)
    (line,) = result.refused
    assert line.startswith(f"acme/{'n' * 600}: ") and "too long" in line


@pytest.mark.asyncio
async def test_a_refused_cleanup_delete_leaves_the_object_to_the_bucket_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    from contextlib import asynccontextmanager

    from server.core import object_store as objects

    data = b"replaced meanwhile"
    held = blobs.Held("acme", "docs", hashlib.sha256(data).hexdigest(), len(data), 0)

    async def ch_now_s(*, ch: Any = None) -> int:
        return 0

    async def read(*a: Any, **kw: Any) -> bytes:
        return data

    @asynccontextmanager
    async def transfer() -> Any:
        yield None

    async def put(*a: Any) -> None:
        return None

    async def read_back(loc: Any) -> bytes:
        return data

    async def still_held(*a: Any, **kw: Any) -> bool:
        return False  # replaced since the walk

    async def refused(*a: Any) -> None:
        raise objects.ObjectStoreError("object store delete failed (503)", operation="delete")

    monkeypatch.setattr(blobs, "ch_now_s", ch_now_s)
    monkeypatch.setattr(blobs, "read", read)
    monkeypatch.setattr(blobs, "still_held", still_held)
    monkeypatch.setattr(objects, "transfer", transfer)
    monkeypatch.setattr(objects, "put", put)
    monkeypatch.setattr(objects, "read", read_back)
    monkeypatch.setattr(objects, "delete", refused)
    monkeypatch.setattr(objects, "new_location", lambda *a: objects.Location("bucket", "k"))
    # The row was replaced: the move does not stand, whatever its cleanup met.
    assert await originals.move_to_bucket(held) is False


@pytest.mark.asyncio
async def test_a_dry_run_refuses_a_namespace_whose_keys_would_be_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fence(org: str, ns: str) -> str:
        return ""

    monkeypatch.setattr(originals, "fence", fence)

    async def held_in_clickhouse(*a: object) -> blobs.HeldPage:
        raise AssertionError("no original of it is counted")

    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    result = await originals.migrate_originals(org="acme", namespace="n" * 600, dry_run=True)
    assert result.pending == 0
    (line,) = result.refused
    assert "too long" in line


@pytest.mark.asyncio
async def test_a_dry_run_reports_a_namespace_an_erasure_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fence(org: str, ns: str) -> str:
        return "erasure e1 (namespace, in_progress) covers it; run again once it has finished"

    async def held_in_clickhouse(*a: object) -> blobs.HeldPage:
        raise AssertionError("a refused namespace is not counted")

    monkeypatch.setattr(originals, "fence", fence)
    monkeypatch.setattr(blobs, "held_in_clickhouse", held_in_clickhouse)
    result = await originals.migrate_originals(org="acme", namespace="docs", dry_run=True)
    assert result.pending == 0
    (line,) = result.refused
    assert "erasure e1" in line


@pytest.mark.asyncio
async def test_a_switch_a_later_write_outranked_does_not_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import object_store as objects

    loc = objects.Location("bucket", "k")

    async def no_erasures(*a: Any) -> list[Any]:
        return []

    async def outranked(*a: Any, **kw: Any) -> bool:
        return False

    monkeypatch.setattr(originals, "_erasures_since", no_erasures)
    monkeypatch.setattr(blobs, "latest_names", outranked)
    assert await originals._settle(_held(0), 0, loc, None) is False

    async def unread(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("ClickHouse went away")

    # An unread latest row is reported with what to check, not as an erasure's.
    monkeypatch.setattr(blobs, "latest_names", unread)
    with pytest.raises(originals.SwitchUnconfirmed, match="later write outranked it") as e:
        await originals._settle(_held(0), 0, loc, None)
    assert "erasure" not in str(e.value)
