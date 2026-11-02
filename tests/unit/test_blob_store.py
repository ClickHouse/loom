"""loom.document_blobs: an original is addressed by its sha256 per namespace, is
served without being buffered, and leaves only through the sweep's aged delete,
one namespace at a time."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import Any

from server.knowledge.store import blobs


class _CH:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], bytes] = {}
        self.inserts: list[dict[str, Any]] = []
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.uploads_deleted: list[str] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        assert table == "loom.document_blobs" and settings == {"async_insert": 0}
        for r in rows:
            self.inserts.append(r)
            self.rows[(r["org"], r["namespace"], r["sha256"])] = r["data"]

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.seen.append((sql, params))
        if "AS cutoff_ms" in sql:  # the ClickHouse clock, less the age
            return {"cutoff_ms": 1_000_000 - params["age"] * 1000}
        data = self.rows.get((params["org"], params["ns"], params["sha"]))
        return None if data is None else {"size": len(data)}

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.seen.append((sql, params))
        assert "DISTINCT" in sql and "FROM loom.document_blobs" in sql
        return [{"sha": s} for s in params["shas"] if (params["org"], params["ns"], s) in self.rows]

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.seen.append((sql, params))
        assert "IN {shas:" in sql
        if sql.startswith("DELETE FROM loom.blob_uploads"):
            self.uploads_deleted.extend(params["shas"])
            return
        assert sql.startswith("DELETE FROM loom.document_blobs")
        for sha in params["shas"]:
            self.rows.pop((params["org"], params["ns"], sha), None)

    async def raw_bytes(self, sql: str, params: dict[str, Any]) -> bytes:
        self.seen.append((sql, params))
        return self.rows.get((params["org"], params["ns"], params["sha"]), b"")

    @asynccontextmanager
    async def raw_stream(self, sql: str, params: dict[str, Any]) -> Any:
        self.seen.append((sql, params))
        data = self.rows.get((params["org"], params["ns"], params["sha"]), b"")

        async def chunks() -> Any:
            for i in range(0, len(data), 100):
                yield data[i : i + 100]

        yield chunks()


def test_put_stream_delete_round_trips_binary_bytes() -> None:
    ch = _CH()
    raw = bytes(range(256)) * 3

    async def go() -> tuple[str, int | None, bytes, int, int | None]:
        sha = await blobs.put("o", "ns", raw, ch=ch)
        size = await blobs.size("o", "ns", sha, ch=ch)
        async with blobs.stream("o", "ns", sha, ch=ch) as chunks:
            got = b"".join([c async for c in chunks])
        n = await blobs.delete("o", "ns", [sha, sha], ch=ch)
        return sha, size, got, n, await blobs.size("o", "ns", sha, ch=ch)

    sha, size, got, n, gone = asyncio.run(go())
    assert sha == hashlib.sha256(raw).hexdigest()
    assert size == len(raw) and got == raw and n == 1 and gone is None
    assert ch.uploads_deleted == [sha], "who uploaded it goes with it"
    assert ch.inserts[0]["size"] == len(raw)
    stream_sql = next(sql for sql, _ in ch.seen if "RawBLOB" in sql)
    assert stream_sql.endswith("LIMIT 1 FORMAT RawBLOB") and "base64" not in stream_sql


def test_read_returns_the_whole_original_or_nothing() -> None:
    ch = _CH()
    raw = bytes(range(256))
    sha = asyncio.run(blobs.put("o", "ns", raw, ch=ch))
    assert asyncio.run(blobs.read("o", "ns", sha, ch=ch)) == raw
    assert asyncio.run(blobs.read("o", "other", sha, ch=ch)) == b""
    assert ch.seen[-1][0].endswith("LIMIT 1 FORMAT RawBLOB")


def test_an_original_is_read_only_in_its_own_namespace() -> None:
    ch = _CH()
    sha = asyncio.run(blobs.put("o", "ns", b"x", ch=ch))
    assert asyncio.run(blobs.size("o", "other", sha, ch=ch)) is None


def test_a_delete_with_a_min_age_spares_a_fresh_row_and_counts_what_went() -> None:
    """The sweep races a write of the same bytes that has stored its original
    but not its document row yet: only a row older than the age goes, and the
    count is of the originals gone, not of those asked for."""
    ch = _CH()
    ch.rows[("o", "ns", "a")] = b"a"

    async def spare(sql: str, params: dict[str, Any]) -> None:
        ch.seen.append((sql, params))  # the restamped row survives the aged delete

    ch.execute = spare  # type: ignore[method-assign]
    assert asyncio.run(blobs.delete("o", "ns", ["a"], min_age_s=3600, ch=ch)) == 0
    (clock, _), (aged, p) = ch.seen[:2]
    assert "now64(3)" in clock and "now64" not in aged  # a replicated table refuses it
    assert "created_at < fromUnixTimestamp64Milli({cutoff_ms:Int64})" in aged
    assert p["cutoff_ms"] == 1_000_000 - 3_600_000
    assert not any("blob_uploads" in sql for sql, _ in ch.seen), "a spared one keeps its record"
    assert asyncio.run(blobs.delete("o", "ns", [], min_age_s=3600, ch=ch)) == 0


def test_unreferenced_is_one_namespace_after_a_cursor() -> None:
    """The candidates read is bounded by the namespace on both sides of the NOT
    IN, so no statement scans every tenant's documents."""
    seen: dict[str, Any] = {}

    class _Rec:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            seen["sql"], seen["params"] = sql, params
            return [{"sha": "a" * 64}]

    got = asyncio.run(blobs.unreferenced("o", "ns", 60, 10, after="0" * 64, ch=_Rec()))
    assert got == ["a" * 64]
    sql = seen["sql"]
    assert "HAVING max(created_at) < now64(3) - INTERVAL {age:UInt32} SECOND" in sql
    assert sql.count("org = {org:String} AND namespace = {ns:String}") == 2
    assert "sha256 > {after:String}" in sql and "FROM loom.documents FINAL" in sql
    from server.knowledge.store.documents import LIVE_ORIGINAL_SQL

    assert LIVE_ORIGINAL_SQL in sql
    assert seen["params"] == {"org": "o", "ns": "ns", "after": "0" * 64, "age": 60, "lim": 10}
