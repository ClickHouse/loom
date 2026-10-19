"""`loom export-okf` reads the rows the bundle needs: superseded rows stay in
the export so the supersedes chain has both
ends; the typed edges are paged through memory.export_evidence kind=edges."""

from __future__ import annotations

from typing import Any

import pytest

from server.cli import okf as cli_okf


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, body))
        if op == "memory.list":
            return {
                "entries": [
                    {
                        "memory_key": "new",
                        "agent": "a",
                        "user_id": "-",
                        "session_id": "-",
                        "status": "active",
                    },
                    {
                        "memory_key": "old",
                        "agent": "a",
                        "user_id": "-",
                        "session_id": "-",
                        "status": "superseded",
                    },
                ],
                "truncated": False,
                "total": 2,
                "namespace_version": "7",
            }
        if op == "memory.bulk_get":
            return {
                "results": {
                    "new": {"value": "v2", "memory_id": "m-new", "source_class": "asserted"},
                    "old": {
                        "value": "v1",
                        "memory_id": "m-old",
                        "status": "superseded",
                        "superseded_by": "m-new",
                        "source_class": "extracted",
                    },
                }
            }
        if op == "memory.export_evidence" and body.get("kind") == "edges":
            if not body.get("cursor"):
                return {
                    "edges": [
                        {
                            "from_key": "new",
                            "to_key": "old",
                            "relation": "supersedes",
                            "confidence": 1.0,
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "c1",
                }
            return {
                "edges": [
                    {"from_key": "old", "to_key": "new", "relation": "precedes", "confidence": 1.0}
                ],
                "has_more": False,
                "next_cursor": "",
            }
        raise AssertionError(op)


@pytest.mark.asyncio
async def test_export_keeps_superseded_rows_and_pages_edges() -> None:
    client = _FakeClient()
    rows, whole = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert whole
    list_call = next(b for op, b in client.calls if op == "memory.list")
    assert list_call["include_superseded"] is True
    assert sorted(r["memory_key"] for r in rows) == ["new", "old"]
    old = next(r for r in rows if r["memory_key"] == "old")
    assert old["superseded_by"] == "m-new" and old["source_class"] == "extracted"

    edges = await cli_okf._fetch_edges(client, "o", "ns")  # type: ignore[arg-type]
    assert [e["relation"] for e in edges] == ["supersedes", "precedes"]
    cursors = [b.get("cursor") for op, b in client.calls if op == "memory.export_evidence"]
    assert cursors == ["", "c1"]

    from datetime import UTC, datetime

    from server.memory.domain import okf_bundle as okf

    files, orphans, skipped = cli_okf._plan_export(
        rows,
        edges,
        [],
        [],
        namespace="ns",
        title=None,
        include_high=False,
        complete=True,
    )
    newer = okf.parse_concept("semantic/new.md", files["semantic/new.md"])
    assert newer.relates_to == [{"path": "semantic/old.md", "kind": "supersedes"}]
    assert orphans == [] and skipped == 0
    assert datetime.now(UTC).date().isoformat() in files["log.md"]


class _ShiftingClient:
    """Pages of one row. The first page's row leaves the namespace before the second
    page is read on the first `shifts` listings, so that listing skips a live row. With
    `restore`, a row restored below the offset keeps the total the same."""

    def __init__(self, shifts: int, *, restore: bool = False) -> None:
        self.shifts = shifts
        self.restore = restore
        self.listings = 0

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.bulk_get":
            return {"results": {k: {"value": k} for k in body["memory_keys"]}}
        assert op == "memory.list"
        if body["offset"] == 0:
            self.listings += 1
        keys, version = ["a", "b", "c"], "1"
        if body["offset"] > 0 and self.listings <= self.shifts:
            # "a" was deleted: "b" moved up to offset 0 and is skipped
            keys, version = ["b", "c"], "2"
            if self.restore:
                keys, version = ["b", "c", "r"], "3"  # "r" restored at its old place
        page = keys[body["offset"] : body["offset"] + 1]
        return {
            "entries": [
                {"memory_key": k, "agent": "x", "user_id": "-", "session_id": "-"} for k in page
            ],
            "truncated": body["offset"] + 1 < len(keys),
            "total": len(keys),
            "namespace_version": version,
        }


@pytest.mark.asyncio
async def test_a_listing_that_skipped_a_row_is_read_again() -> None:
    client = _ShiftingClient(shifts=1)
    rows, whole = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert whole and client.listings == 2
    assert [r["memory_key"] for r in rows] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_a_namespace_that_keeps_shifting_is_not_called_complete() -> None:
    # The bundle then carries no completeness marker, and --prune refuses it.
    client = _ShiftingClient(shifts=99)
    rows, whole = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert not whole and client.listings == cli_okf._LIST_ATTEMPTS
    assert [r["memory_key"] for r in rows] == ["a", "c"]


@pytest.mark.asyncio
async def test_a_delete_and_a_restore_mid_listing_are_not_called_complete() -> None:
    # The skipped row and the restored one cancel out in the count; the version does not.
    client = _ShiftingClient(shifts=99, restore=True)
    rows, whole = await cli_okf._fetch_namespace(client, "o", "ns", include_tombstoned=False)  # type: ignore[arg-type]
    assert [r["memory_key"] for r in rows] == ["a", "c", "r"]
    assert not whole
