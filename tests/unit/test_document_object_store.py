"""Document originals in an S3-compatible bucket: each write its own object,
written before the catalog row naming it; reads follow the row; the sweep
deletes only objects no row names; erasure deletes the scope's objects, counts
what is left, and fails closed when it cannot reach them.

A fake S3 client stands in for boto3's, with the store's SHA-256 check, paging
and the error shape botocore raises.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core import object_store
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_memory
from server.knowledge.store import blobs
from server.knowledge.workers import blob_gc
from server.settings import settings

BUCKET = "originals"


def _client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code}}, "S3")


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos, self.closed = data, 0, False

    def read(self, n: int = -1) -> bytes:
        end = len(self.data) if n < 0 else self.pos + n
        out, self.pos = self.data[self.pos : end], min(end, len(self.data))
        return out

    def close(self) -> None:
        self.closed = True


class FakeS3:
    """The calls `object_store` makes, over one dict per bucket. Lists page by
    at most two keys so every listing crosses a page."""

    PAGE = 2

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, datetime]] = {}
        self.fail: str = ""  # an operation that raises a server error

    def _check(self, op: str) -> None:
        if self.fail == op:
            raise _client_error("InternalError")

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ChecksumSHA256: str) -> dict:
        self._check("put")
        if base64.b64encode(hashlib.sha256(Body).digest()).decode() != ChecksumSHA256:
            raise _client_error("BadDigest")
        self.objects[(Bucket, Key)] = (Body, datetime.now(UTC))
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self._check("get")
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey")
        return {"Body": _Body(self.objects[(Bucket, Key)][0])}

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str, MaxKeys: int = 1000, StartAfter: str = ""
    ) -> dict:
        self._check("list")
        keys = sorted(
            k for b, k in self.objects if b == Bucket and k.startswith(Prefix) and k > StartAfter
        )
        page = keys[: min(MaxKeys, self.PAGE)]
        return {
            "IsTruncated": len(keys) > len(page),
            "Contents": [{"Key": k, "LastModified": self.objects[(Bucket, k)][1]} for k in page],
        }

    def delete_objects(self, *, Bucket: str, Delete: dict) -> dict:
        self._check("delete")
        for o in Delete["Objects"]:
            self.objects.pop((Bucket, o["Key"]), None)
        return {}

    def keys(self) -> list[str]:
        return sorted(k for _, k in self.objects)

    def age_all(self, seconds: int) -> None:
        for k, (data, when) in list(self.objects.items()):
            self.objects[k] = (data, when - timedelta(seconds=seconds))


class CatalogCH:
    """loom.document_blobs as rows (non-merged: every insert kept), for the
    statements `blobs` sends."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.uploads_deleted: list[str] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table == "loom.document_blobs":
            self.rows.extend(dict(r) for r in rows)

    def _latest(self, p: dict[str, Any]) -> dict[str, Any] | None:
        hits = [
            r
            for r in self.rows
            if (r["org"], r["namespace"], r["sha256"]) == (p["org"], p["ns"], p["sha"])
        ]
        return hits[-1] if hits else None

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if "AS cutoff_ms" in sql:  # the ClickHouse clock, less the age
            return {"cutoff_ms": 0}
        row = self._latest(params)
        if row is None:
            return None
        assert sql.startswith("SELECT size, location")
        return {"size": row["size"], "location": row["location"]}

    def _of(self, p: dict[str, Any], sql: str = "") -> list[dict[str, Any]]:
        """The rows of `blobs.delete`'s scope: (org, ns) and the shas, and under
        its age bound only the rows a test marked `old`."""
        aged = "created_at <" in sql
        return [
            r
            for r in self.rows
            if (r["org"], r["namespace"]) == (p["org"], p["ns"])
            and r["sha256"] in p["shas"]
            and (r.get("old") or not aged)
        ]

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "keys" in params:  # the bucket walk's catalog read
            assert sql.startswith("SELECT DISTINCT location, org, namespace")
            return [
                {
                    "location": r["location"],
                    "org": r["org"],
                    "namespace": r["namespace"],
                    "sha": r["sha256"],
                }
                for r in self.rows
                if (r["org"], r["namespace"], r["sha256"]) in set(params["keys"])
            ]
        if sql.startswith("SELECT DISTINCT location"):  # before blobs.delete
            return [{"location": r["location"]} for r in self._of(params, sql) if r["location"]]
        assert sql.startswith("SELECT DISTINCT toString(sha256) AS sha, location")
        return [{"sha": r["sha256"], "location": r["location"]} for r in self._of(params)]

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        if sql.startswith("DELETE FROM loom.blob_uploads") and "keys" in params:
            # The walk's: the keys no row holds (the fake has no record ages).
            self.uploads_deleted.extend(k[2] for k in params["keys"])
        elif sql.startswith("DELETE FROM loom.blob_uploads"):
            self.uploads_deleted.extend(params["shas"])
        if sql.startswith("DELETE FROM loom.document_blobs"):
            mine = params.get("mine")
            gone = [
                r
                for r in self._of(params, sql)
                if not r["location"] or (mine and r["location"].startswith(mine))
            ]
            self.rows = [r for r in self.rows if r not in gone]

    async def raw_bytes(self, sql: str, params: dict[str, Any]) -> bytes:
        row = self._latest(params)
        return row["data"] if row else b""

    @asynccontextmanager
    async def raw_stream(self, sql: str, params: dict[str, Any]) -> Any:
        row = self._latest(params)

        async def chunks() -> Any:
            if row:
                yield row["data"]

        yield chunks()


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch, settings_override: Any) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(object_store, "_client", lambda: fake)
    settings_override(
        document_object_store="s3", document_s3_bucket=BUCKET, document_s3_prefix="loom/originals"
    )
    return fake


def _stream(ch: CatalogCH, org: str, ns: str, sha: str) -> bytes:
    async def go() -> bytes:
        stored = await blobs.locate(org, ns, sha, ch=ch)
        if stored is None:
            return b""
        async with blobs.stream(org, ns, sha, stored=stored, ch=ch) as chunks:
            return b"".join([c async for c in chunks])

    return asyncio.run(go())


def _read(ch: CatalogCH, org: str, ns: str, sha: str) -> bytes | None:
    async def go() -> bytes | None:
        stored = await blobs.locate(org, ns, sha, ch=ch)
        return None if stored is None else await blobs.read(org, ns, sha, stored=stored, ch=ch)

    return asyncio.run(go())


def test_an_original_is_an_object_named_by_its_row_and_reads_back_whole(s3: FakeS3) -> None:
    ch = CatalogCH()
    raw = bytes(range(256)) * 5000  # crosses a stream chunk
    sha = asyncio.run(blobs.put("o", "ns", raw, ch=ch))
    assert sha == hashlib.sha256(raw).hexdigest()
    (key,) = s3.keys()
    assert key.startswith(f"loom/originals/o/ns/{sha}/") and len(key.rsplit("/", 1)[1]) == 32
    row = ch.rows[0]
    assert (
        row["data"] == b"" and row["size"] == len(raw) and row["location"] == f"s3://{BUCKET}/{key}"
    )
    assert _read(ch, "o", "ns", sha) == raw
    assert _stream(ch, "o", "ns", sha) == raw
    assert asyncio.run(blobs.locate("o", "ns", sha, ch=ch)) == blobs.Stored(
        len(raw), row["location"]
    )


def test_a_second_write_of_the_same_bytes_is_its_own_object(s3: FakeS3) -> None:
    ch = CatalogCH()
    asyncio.run(blobs.put("o", "ns", b"same", ch=ch))
    asyncio.run(blobs.put("o", "ns", b"same", ch=ch))
    assert len(s3.keys()) == 2 and len({r["location"] for r in ch.rows}) == 2


def test_a_failed_object_write_leaves_no_row(s3: FakeS3) -> None:
    ch = CatalogCH()
    s3.fail = "put"
    with pytest.raises(object_store.ObjectStoreError) as e:
        asyncio.run(blobs.put("o", "ns", b"x", ch=ch))
    assert ch.rows == [] and e.value.status_code == 503
    assert BUCKET not in str(e.value) and "InternalError" in str(e.value)


async def _put(loc: object_store.Location, data: bytes, sha: str) -> None:
    async with object_store.transfer() as lease:
        await object_store.put(loc, data, sha, lease)


def test_the_store_refuses_bytes_that_do_not_match_their_sha(s3: FakeS3) -> None:
    loc = object_store.new_location("o", "ns", "0" * 64)
    with pytest.raises(object_store.ObjectStoreError, match="BadDigest"):
        asyncio.run(_put(loc, b"not zeros", "0" * 64))


def test_a_row_is_read_from_where_it_says_whatever_the_store_is_now(
    s3: FakeS3, settings_override: Any
) -> None:
    ch = CatalogCH()
    settings_override(document_object_store="clickhouse")
    sha_ch = asyncio.run(blobs.put("o", "ns", b"in clickhouse", ch=ch))
    settings_override(document_object_store="s3")
    sha_s3 = asyncio.run(blobs.put("o", "ns", b"in the bucket", ch=ch))
    assert ch.rows[0]["location"] == "" and ch.rows[0]["data"] == b"in clickhouse"
    assert _read(ch, "o", "ns", sha_ch) == b"in clickhouse"
    assert _read(ch, "o", "ns", sha_s3) == b"in the bucket"
    assert _stream(ch, "o", "ns", sha_ch) == b"in clickhouse"


def test_an_object_gone_from_the_bucket_reads_as_nothing(s3: FakeS3) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "ns", b"x", ch=ch))
    s3.objects.clear()
    assert _read(ch, "o", "ns", sha) is None
    assert _stream(ch, "o", "ns", sha) == b""


def test_a_read_the_bucket_fails_is_an_error_not_an_empty_file(s3: FakeS3) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "ns", b"x", ch=ch))
    s3.fail = "get"
    with pytest.raises(object_store.ObjectStoreError):
        _read(ch, "o", "ns", sha)


def _walk(ch: CatalogCH, min_age_s: int, limit: int = 1000) -> int:
    """Every page of the sweep from the start; how many objects went."""
    gone, after = 0, ""
    while True:
        page = asyncio.run(blobs.unnamed_objects(after, limit, min_age_s, ch=ch))
        stale, after = page.keys, page.after
        asyncio.run(object_store.delete(BUCKET, stale))
        gone += len(stale)
        if not after:
            return gone


def test_the_sweep_deletes_only_old_objects_no_row_names(s3: FakeS3) -> None:
    ch = CatalogCH()
    asyncio.run(blobs.put("o", "a", b"named", ch=ch))
    s3.objects[(BUCKET, "loom/originals/o/a/" + "1" * 64 + "/w1")] = (b"orphan", datetime.now(UTC))
    s3.objects[(BUCKET, "loom/originals/o/b/" + "2" * 64 + "/w2")] = (b"orphan", datetime.now(UTC))
    s3.objects[(BUCKET, "loom/originals/not-an-original")] = (b"other", datetime.now(UTC))
    s3.objects[(BUCKET, "loom/originals/o/a/" + "z" * 70 + "/w")] = (b"other", datetime.now(UTC))
    assert _walk(ch, 3600) == 0, "a fresh object may be a write in flight"
    s3.age_all(7200)
    assert _walk(ch, 3600) == 2
    left = s3.keys()
    assert len(left) == 3 and "loom/originals/not-an-original" in left
    assert object_store.scope_of("loom/originals/o/a/" + "z" * 70 + "/w") is None
    assert _read(ch, "o", "a", hashlib.sha256(b"named").hexdigest()) == b"named"


def test_the_sweep_pages_from_a_cursor_and_the_pass_stops_at_its_page_budget(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    for i in range(7):
        s3.objects[(BUCKET, f"loom/originals/o/ns{i}/{'3' * 64}/w")] = (b"x", datetime.now(UTC))
    s3.age_all(7200)
    ch = CatalogCH()
    page = asyncio.run(blobs.unnamed_objects("", 3, 3600, ch=ch))
    stale, after = page.keys, page.after
    assert len(stale) == 2 and after == f"loom/originals/o/ns1/{'3' * 64}/w"  # pages by two
    page = asyncio.run(blobs.unnamed_objects(after, 3, 3600, ch=ch))
    stale, after = page.keys, page.after
    assert len(stale) == 2 and after.startswith("loom/originals/o/ns3/")

    async def no_catalog(after: Any, limit: int) -> list[Any]:
        return []

    monkeypatch.setattr(blobs, "namespaces", no_catalog)
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    monkeypatch.setattr(blob_gc.settings, "document_blob_gc_object_pages", 1)
    cursors: list[str] = []
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", cursors.append)
    settings_override(document_blob_min_age_seconds=3600)
    result = asyncio.run(blob_gc.run_pass())
    assert result.objects == 2 and cursors and cursors[-1].startswith("loom/originals/o/ns1/")


def test_a_delete_the_store_refuses_is_recorded_and_the_sweep_walks_on(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(4):
        s3.objects[(BUCKET, f"loom/originals/o/ns{i}/{'3' * 64}/w")] = (b"x", datetime.now(UTC))
    s3.age_all(7200)
    ch = CatalogCH()
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    failures: list[str] = []
    monkeypatch.setattr(
        blob_gc.pass_context, "note_failure", lambda e, what="": failures.append(what)
    )
    cursors: list[str] = []
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", cursors.append)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "")
    s3.fail = "delete"
    assert asyncio.run(blob_gc._sweep_bucket(3600)) == 0
    assert failures and cursors[-1] == object_store.root_prefix(), "the walk reached the end"
    s3.fail = ""
    assert asyncio.run(blob_gc._sweep_bucket(3600)) == 4


def _sweep(ch: CatalogCH, ns: str, shas: list[str], min_age_s: int = 0) -> int:
    """The sweep's delete: the rows, then the objects they named."""
    result = asyncio.run(blobs.delete("o", ns, shas, min_age_s=min_age_s, ch=ch))
    assert result.error is None and result.kept == 0
    return result.gone


def test_the_sweep_deletes_a_deleted_rows_object_with_it_and_keeps_others(s3: FakeS3) -> None:
    ch = CatalogCH()
    gone = asyncio.run(blobs.put("o", "a", b"gone", ch=ch))
    kept = asyncio.run(blobs.put("o", "a", b"kept", ch=ch))
    assert _sweep(ch, "a", [gone]) == 1
    (key,) = s3.keys()
    assert f"/{kept}/" in key, "the deleted row's object went with it, before any walk"


def test_a_re_upload_during_the_sweep_keeps_its_object(s3: FakeS3) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "a", b"same", ch=ch))
    ch.rows[-1]["old"] = True
    asyncio.run(blobs.put("o", "a", b"same", ch=ch))  # a new write, its own key, too young
    old_key, new_key = (object_store.Location.parse(r["location"]).key for r in ch.rows)
    assert _sweep(ch, "a", [sha], min_age_s=3600) == 0, "the sha has a live row"
    assert s3.keys() == [new_key] and old_key != new_key
    assert _read(ch, "o", "a", sha) == b"same"


def test_an_object_delete_the_store_refuses_is_recorded_and_the_rows_count(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "a", b"x", ch=ch))
    s3.fail = "delete"
    result = asyncio.run(blobs.delete("o", "a", [sha], ch=ch))
    assert result.gone == 1 and result.error is not None and len(s3.keys()) == 1
    assert ch.uploads_deleted == [], "the upload record stays, so a user erasure finds the bytes"


def test_a_row_naming_an_object_the_sweep_cannot_reach_is_kept(
    s3: FakeS3, settings_override: Any
) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "a", b"elsewhere", ch=ch))
    ch.rows[-1]["location"] = ch.rows[-1]["location"].replace(f"s3://{BUCKET}/", "s3://former/")
    result = asyncio.run(blobs.delete("o", "a", [sha], ch=ch))
    assert (result.gone, result.kept) == (0, 1) and len(ch.rows) == 1 and not ch.uploads_deleted
    settings_override(document_object_store="clickhouse", document_s3_bucket="")
    sha2 = asyncio.run(blobs.put("o", "a", b"no bucket now", ch=ch))
    assert asyncio.run(blobs.delete("o", "a", [sha2], ch=ch)).gone == 1, "a ClickHouse row goes"
    assert asyncio.run(blobs.delete("o", "a", [sha], ch=ch)).kept == 1, "no bucket: kept"


def test_the_walk_drops_the_upload_records_of_an_original_whose_last_object_it_deleted(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = CatalogCH()
    gone = asyncio.run(blobs.put("o", "a", b"refused once", ch=ch))
    kept = asyncio.run(blobs.put("o", "a", b"still named", ch=ch))
    s3.fail = "delete"
    assert asyncio.run(blobs.delete("o", "a", [gone], ch=ch)).error is not None
    assert ch.uploads_deleted == [], "kept while the object is"
    s3.fail = ""
    s3.age_all(7200)
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", lambda c: None)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "")
    assert asyncio.run(blob_gc._sweep_bucket(3600)) == 1
    assert ch.uploads_deleted == [gone] and kept not in ch.uploads_deleted


def test_a_missing_object_reads_as_none_and_a_0_byte_original_as_empty(s3: FakeS3) -> None:
    ch = CatalogCH()
    gone = asyncio.run(blobs.put("o", "a", b"bytes", ch=ch))
    empty = asyncio.run(blobs.put("o", "a", b"", ch=ch))
    s3.objects.pop((BUCKET, object_store.Location.parse(ch.rows[0]["location"]).key))
    assert _read(ch, "o", "a", gone) is None
    assert _read(ch, "o", "a", empty) == b""


def test_a_slash_in_a_name_cannot_reach_another_namespace(s3: FakeS3) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "team/x", b"nested", ch=ch))
    (key,) = s3.keys()
    assert key.startswith("loom/originals/o/team%2Fx/") and object_store.scope_of(key) == (
        "o",
        "team/x",
        sha,
    )
    assert object_store.namespace_prefix("o", "team") == "loom/originals/o/team/"
    assert not key.startswith(object_store.namespace_prefix("o", "team"))
    s3.age_all(7200)
    assert _walk(ch, 3600) == 0, "the row in team/x names it"
    asyncio.run(steps_memory.delete_document_blobs_by_namespace("o", "team", ch=EraseCH(1, [])))
    assert s3.keys() == [key]
    assert _read(ch, "o", "team/x", sha) == b"nested"


class EraseCH:
    """The erasure statements: the scope's rows naming objects count `stored`,
    those outside the configured bucket and prefix `elsewhere`; the user's own
    (namespace, sha) pairs are `own`."""

    def __init__(self, stored: int, own: list[dict[str, Any]], elsewhere: int = 0) -> None:
        self.stored, self.own, self.elsewhere = stored, own, elsewhere
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.counted: list[tuple[str, dict[str, Any]]] = []
        self.own_reads = 0

    async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
        self.counted.append((sql, dict(params or {})))
        if "location != ''" in sql:
            return {"stored": self.stored, "elsewhere": self.elsewhere}
        return {"n": 0}

    async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
        if sql.startswith("SELECT DISTINCT namespace, sha FROM"):  # the user's own originals
            self.own_reads += 1
            return self.own
        if sql.startswith("SELECT namespace, sha FROM ("):  # read again before the delete
            pairs = set(params["pairs"])
            return [r for r in self.own if (r["namespace"], r["sha"]) in pairs]
        return []

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
        self.executed.append((sql, dict(params or {})))


def _blob_pairs(ch: EraseCH) -> set[tuple[tuple[str, str], ...]]:
    """The pairs a user erasure's bucket check and row count were bound to."""
    return {tuple(p["pairs"]) for sql, p in ch.counted if "FROM loom.document_blobs" in sql}


def test_namespace_erasure_deletes_every_object_of_the_scope_and_the_census_counts_them(
    s3: FakeS3,
) -> None:
    for ns in ("a", "b", "keep"):
        s3.objects[(BUCKET, f"loom/originals/o/{ns}/{'4' * 64}/w")] = (b"x", datetime.now(UTC))
    s3.objects[(BUCKET, f"loom/originals/other/a/{'4' * 64}/w")] = (b"x", datetime.now(UTC))
    left = asyncio.run(erasure_census.object_residue("o", ["a", "b"]))
    assert left == 2
    asyncio.run(steps_memory.delete_document_blobs_by_namespace("o", ["a", "b"], ch=EraseCH(1, [])))
    assert [k.split("/")[2:4] for k in s3.keys()] == [["o", "keep"], ["other", "a"]]
    assert asyncio.run(erasure_census.object_residue("o", ["a", "b"])) == 0


def test_user_erasure_deletes_every_write_of_their_originals_and_no_one_elses(s3: FakeS3) -> None:
    mine, theirs = "5" * 64, "6" * 64
    for sha, writes in ((mine, ("w1", "w2")), (theirs, ("w3",))):
        for w in writes:
            s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/{w}")] = (b"x", datetime.now(UTC))
    s3.age_all(60)
    # w2 has no catalog row (its insert failed): the pair comes from the upload record.
    own = [{"namespace": "ns", "sha": mine}]
    ch = EraseCH(0, own)
    asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    assert s3.keys() == [f"loom/originals/o/ns/{theirs}/w3"]
    # The bucket check and the count are bound to the pairs whose objects went.
    assert ch.own_reads == 1
    assert _blob_pairs(ch) == {(("ns", mine),)}


def test_user_erasure_goes_in_batches_each_with_its_own_mutation_tag(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.erasure.store import mutations

    shas = [f"{i:x}" * 64 for i in range(5)]
    for sha in shas:
        s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/w")] = (b"x", datetime.now(UTC))
    monkeypatch.setattr(  # two originals per statement
        steps_memory, "chunks", lambda v: (list(v[i : i + 2]) for i in range(0, len(v), 2))
    )

    class _RowsToDelete(EraseCH):
        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            out = await super().query_one(sql, params, **kw)
            return {"n": 1} if out == {"n": 0} and "pairs" in (params or {}) else out

    async def no_wait(*args: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(mutations, "wait_for_mutation", no_wait)
    ch = _RowsToDelete(0, [{"namespace": "ns", "sha": sha} for sha in shas])
    with mutations.saga_scope("saga-1"):
        asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    assert s3.keys() == []
    sent = [
        (len(p["pairs"]), p["erasure_mutation_tag"])
        for sql, p in ch.executed
        if "ALTER TABLE loom.document_blobs" in sql
    ]
    assert [n for n, _ in sent] == [2, 2, 1]
    assert len({tag for _, tag in sent}) == 3, "each batch's statement carries its own tag"


def test_a_write_of_the_users_bytes_during_the_step_leaves_no_object(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user uploads the same bytes again after the step listed their prefixes
    and before it deleted the rows: the mutation takes that write's row too, so its
    object goes in the listing after the rows."""
    sha = "a" * 64
    s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/first")] = (b"x", datetime.now(UTC))
    real = steps_memory.count_then_mutate

    async def write_lands_first(table: str, **kw: Any) -> int:
        if table == "loom.document_blobs" and "pairs" in kw["params"]:
            s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/again")] = (b"x", datetime.now(UTC))
        return await real(table, **kw)

    monkeypatch.setattr(steps_memory, "count_then_mutate", write_lands_first)
    ch = EraseCH(0, [{"namespace": "ns", "sha": sha}])
    asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    assert s3.keys() == []


def test_a_users_upload_whose_row_survives_the_mutation_keeps_its_object(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that lands after the mutation is not deleted by it: its object stays, so
    the row never names a deleted object (the walk or a later erasure takes both)."""
    sha = "b" * 64
    s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/first")] = (b"x", datetime.now(UTC))
    late = f"loom/originals/o/ns/{sha}/late"
    real = steps_memory.count_then_mutate

    async def write_lands_after(table: str, **kw: Any) -> int:
        n = await real(table, **kw)
        if table == "loom.document_blobs" and "pairs" in kw["params"]:
            s3.objects[(BUCKET, late)] = (b"x", datetime.now(UTC))
        return n

    class _LateRow(EraseCH):
        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
            if sql.startswith("SELECT DISTINCT location FROM loom.document_blobs"):
                return [{"location": f"s3://{BUCKET}/{late}"}]
            return await super().query_all(sql, params, **kw)

    monkeypatch.setattr(steps_memory, "count_then_mutate", write_lands_after)
    ch = _LateRow(0, [{"namespace": "ns", "sha": sha}])
    asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    assert s3.keys() == [late], "the first write's object goes; the surviving row's stays"


def test_with_a_bucket_an_original_another_writer_named_since_the_first_read_keeps_its_object(
    s3: FakeS3,
) -> None:
    mine, shared = "a" * 64, "b" * 64
    for sha in (mine, shared):
        s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/w")] = (b"x", datetime.now(UTC))

    class _Shared(EraseCH):
        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
            if sql.startswith("SELECT namespace, sha FROM ("):  # another writer named `shared`
                return [r for r in self.own if r["sha"] == mine]
            return await super().query_all(sql, params, **kw)

    own = [{"namespace": "ns", "sha": mine}, {"namespace": "ns", "sha": shared}]
    ch = _Shared(0, own)
    asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    assert s3.keys() == [f"loom/originals/o/ns/{shared}/w"]
    # The bucket check and the count are bound to the original still the user's alone.
    assert _blob_pairs(ch) == {(("ns", mine),)}


def test_user_erasure_fails_closed_when_the_store_keeps_an_object(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "7" * 64
    s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/w")] = (
        b"x",
        datetime.now(UTC) - timedelta(minutes=1),
    )
    monkeypatch.setattr(s3, "delete_objects", lambda **kw: {})  # acknowledges, deletes nothing
    own = [{"namespace": "ns", "sha": sha}]
    with pytest.raises(object_store.ObjectStoreError, match="still holds 1"):
        asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=EraseCH(0, own)))


def test_a_delete_the_store_refuses_fails_the_erasure(s3: FakeS3) -> None:
    s3.objects[(BUCKET, f"loom/originals/o/a/{'8' * 64}/w")] = (b"x", datetime.now(UTC))
    s3.fail = "delete"
    with pytest.raises(object_store.ObjectStoreError):
        asyncio.run(steps_memory.delete_document_blobs_by_namespace("o", "a", ch=EraseCH(1, [])))


def test_rows_naming_objects_with_no_bucket_configured_fail_the_erasure(
    settings_override: Any,
) -> None:
    settings_override(document_s3_bucket="")
    with pytest.raises(RuntimeError, match="LOOM_DOCUMENT_S3_BUCKET is unset"):
        asyncio.run(steps_memory.delete_document_blobs_by_namespace("o", "a", ch=EraseCH(1, [])))
    own = [{"namespace": "a", "sha": "9" * 64}]
    with pytest.raises(RuntimeError):
        asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=EraseCH(1, own)))
    # Nothing in an object store: nothing to reach, nothing refused.
    asyncio.run(steps_memory.delete_document_blobs_by_namespace("o", "a", ch=EraseCH(0, [])))


def test_object_residue_reopens_the_documents_step() -> None:
    assert erasure_census.steps_to_reopen({erasure_census.OBJECT_RESIDUE: 1}, "namespace") == {
        "documents.tombstone"
    }


def test_the_s3_store_needs_a_bucket_and_a_clean_prefix() -> None:
    from server.settings.catalog import StorageSettings

    with pytest.raises(ValueError, match="needs LOOM_DOCUMENT_S3_BUCKET"):
        StorageSettings(document_object_store="s3")
    with pytest.raises(ValueError, match="DOCUMENT_S3_PREFIX"):
        StorageSettings(document_s3_prefix="/loom/")
    with pytest.raises(ValueError, match="whitespace"):
        StorageSettings(document_object_store="s3", document_s3_bucket=" b")
    assert (
        StorageSettings(document_object_store="s3", document_s3_bucket="b").document_s3_prefix
        == "loom/originals"
    )
    assert StorageSettings().document_object_store == "clickhouse"
    from server.settings import Settings

    # The whole Settings, whose validators hold the min-age floor: without a bucket
    # it does not apply, so a deployment keeps whatever N-1 was configured with.
    assert Settings(document_blob_min_age_seconds=60).document_blob_min_age_seconds == 60


def test_a_row_naming_an_object_outside_the_bucket_and_prefix_fails_the_erasure(s3: FakeS3) -> None:
    with pytest.raises(RuntimeError, match="outside LOOM_DOCUMENT_S3_BUCKET"):
        asyncio.run(
            steps_memory.delete_document_blobs_by_namespace(
                "o", "a", ch=EraseCH(2, [], elsewhere=1)
            )
        )
    own = [{"namespace": "a", "sha": "b" * 64}]
    with pytest.raises(RuntimeError, match="outside"):
        asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=EraseCH(2, own, elsewhere=1)))


def test_a_listing_that_fails_partway_fails_the_user_erasure(s3: FakeS3) -> None:
    sha = "c" * 64
    for w in ("w1", "w2", "w3"):  # more than one page of the fake
        s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/{w}")] = (
            b"x",
            datetime.now(UTC) - timedelta(minutes=1),
        )
    calls = {"n": 0}
    real = s3.list_objects_v2

    def second_page_fails(**kw: Any) -> dict:
        calls["n"] += 1
        if calls["n"] == 2:  # the second page of the first prefix, before any delete
            raise _client_error("SlowDown")
        return real(**kw)

    s3.list_objects_v2 = second_page_fails  # type: ignore[method-assign]
    with pytest.raises(object_store.ObjectStoreError, match="SlowDown"):
        asyncio.run(
            steps_memory.erase_blobs_by_user(
                "o", "alice", ch=EraseCH(0, [{"namespace": "ns", "sha": sha}])
            )
        )


def _download(monkeypatch: pytest.MonkeyPatch, ch: CatalogCH, sha: str) -> Any:
    from fastapi.testclient import TestClient

    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store.documents import DocumentRow
    from server.main import app

    row = DocumentRow(
        org="o",
        namespace="ns",
        source_uri="doc://a",
        doc_id="00000000-0000-0000-0000-000000000001",
        content="text",
        version=1,
        ingested_at=datetime(2026, 1, 1),
        blob_path=sha,
        blob_size=3,
        original_filename="a.bin",
    )

    async def get_one(*a: Any, **k: Any) -> DocumentRow:
        return row

    monkeypatch.setattr(documents_repo, "get_one", get_one)
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(settings, "auth_required", False)
    # Auth off answers only a local Host.
    client = TestClient(app, base_url="http://localhost")
    return client.get("/v1/knowledge.download?org=o&namespace=ns&source_uri=doc://a")


def test_a_download_whose_object_is_gone_is_410_and_a_failing_bucket_503(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "ns", b"abc", ch=ch))
    assert _download(monkeypatch, ch, sha).content == b"abc"
    s3.fail = "get"
    failing = _download(monkeypatch, ch, sha)
    assert failing.status_code == 503 and failing.json()["detail"]["code"] == "BACKEND_UNAVAILABLE"
    s3.fail = ""
    s3.objects.clear()
    gone = _download(monkeypatch, ch, sha)
    assert gone.status_code == 410 and gone.json()["detail"]["code"] == "BLOB_GONE"


def test_a_bucket_failing_on_the_first_read_of_a_streamed_download_is_503(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.app import download_routes

    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "ns", b"abc" * 10, ch=ch))
    monkeypatch.setattr(download_routes, "_BUFFER_MAX_BYTES", 1)

    class _Failing(_Body):
        def read(self, n: int = -1) -> bytes:
            raise _client_error("InternalError")

    monkeypatch.setattr(s3, "get_object", lambda **kw: {"Body": _Failing(b"")})
    r = _download(monkeypatch, ch, sha)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "BACKEND_UNAVAILABLE"


def test_a_key_too_long_for_the_sweep_cursor_is_refused_at_write(s3: FakeS3) -> None:
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="too long"):
        asyncio.run(blobs.put("o", "n" * 600, b"x", ch=CatalogCH()))
    assert s3.keys() == []


def test_every_key_written_fits_the_sweep_cursor() -> None:
    from server.workers import pass_context

    assert object_store.MAX_KEY <= pass_context.MAX_CURSOR


def test_a_row_under_a_former_prefix_nested_in_the_current_one_fails_the_erasure(
    s3: FakeS3, settings_override: Any
) -> None:
    """Rows written under `loom/originals`, the prefix since set to `loom`: they
    start with the root, but the step would list `loom/<org>/<ns>/` and miss them."""
    from server.erasure.store.steps_memory import _erasure_bucket

    settings_override(document_s3_prefix="loom")
    seen: dict[str, Any] = {}

    class _Ch:
        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            seen["sql"], seen["root"] = sql, params["root"]
            return {"stored": 1, "elsewhere": 1}

    with pytest.raises(RuntimeError, match="outside"):
        asyncio.run(_erasure_bucket("1", {}, _Ch()))
    assert seen["root"] == f"s3://{BUCKET}/loom/"  # the live test runs the layout check itself


def test_bucket_streams_beyond_the_slots_wait_then_answer_overloaded(
    s3: FakeS3, settings_override: Any
) -> None:
    from server.core.errors import OverloadedError

    settings_override(ch_pool_wait_ms=50)
    loc = object_store.new_location("o", "ns", "f" * 64)
    s3.objects[(BUCKET, loc.key)] = (b"x" * 10, datetime.now(UTC))

    async def go() -> None:
        held = [object_store.stream(loc) for _ in range(object_store.STREAM_SLOTS)]
        for g in held:
            await g.__anext__()  # each holds a slot while its body is open
        with pytest.raises(OverloadedError):
            await object_store.stream(loc).__anext__()
        for g in held:
            await g.aclose()
        assert [c async for c in object_store.stream(loc)] == [b"x" * 10]

    asyncio.run(go())


def test_the_walk_starts_over_at_the_end_of_the_bucket_through_the_supervisors_cursor_rule(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor keeps only a cursor that is not empty: the walk leaves the
    root prefix at the end, which lists again from the first key."""
    for i in range(5):
        s3.objects[(BUCKET, f"loom/originals/o/ns{i}/{'c' * 64}/w")] = (b"x", datetime.now(UTC))

    async def no_catalog(after: Any, limit: int) -> list[Any]:
        return []

    monkeypatch.setattr(blobs, "namespaces", no_catalog)
    monkeypatch.setattr(blob_gc.settings, "document_blob_gc_object_pages", 1)
    stored = {"cursor": ""}

    def keep_if_not_empty(value: str) -> None:
        if value:  # what server/workers/supervisor.py keeps
            stored["cursor"] = value

    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", keep_if_not_empty)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: stored["cursor"])
    seen: list[str] = []
    for _ in range(4):  # three pages of two, then the walk begins again
        asyncio.run(blob_gc.run_pass())
        seen.append(stored["cursor"])
    assert seen[2] == "loom/originals/" and seen[3].startswith("loom/originals/o/ns1/")


def test_a_bucket_that_cannot_be_listed_stops_only_the_walk(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures: list[str] = []
    monkeypatch.setattr(
        blob_gc.pass_context, "note_failure", lambda e, what="": failures.append(what)
    )
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "loom/originals/o/")
    cursors: list[str] = []
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", cursors.append)
    s3.fail = "list"
    assert asyncio.run(blob_gc._sweep_bucket(3600)) == 0
    assert failures == ["document_blob_gc: bucket walk"] and cursors == []


def test_user_erasure_reads_the_users_originals_with_sequential_consistency(
    s3: FakeS3,
) -> None:
    from server.core.ch import sequential_reads_active

    sha = "e" * 64
    s3.objects[(BUCKET, f"loom/originals/o/ns/{sha}/w")] = (b"x", datetime.now(UTC))

    class LaggingCH(EraseCH):
        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
            if (
                sql.startswith("SELECT DISTINCT namespace, sha FROM")
                and not sequential_reads_active()
            ):
                return []  # a replica that has not seen the upload yet
            return await super().query_all(sql, params, **kw)

    asyncio.run(
        steps_memory.erase_blobs_by_user(
            "o", "alice", ch=LaggingCH(0, [{"namespace": "ns", "sha": sha}])
        )
    )
    assert s3.keys() == [], "the fresh upload's bytes went"


def test_the_walk_reads_object_ages_against_the_stores_clock(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from email.utils import format_datetime

    key = f"loom/originals/o/a/{'f' * 64}/w"
    s3.objects[(BUCKET, key)] = (b"x", datetime.now(UTC) - timedelta(minutes=5))
    real = s3.list_objects_v2

    def store_ahead(**kw: Any) -> dict:
        out = real(**kw)
        # The store's clock runs two hours ahead of this host's.
        ahead = datetime.now(UTC) + timedelta(hours=2)
        out["ResponseMetadata"] = {"HTTPHeaders": {"date": format_datetime(ahead, usegmt=True)}}
        return out

    monkeypatch.setattr(s3, "list_objects_v2", store_ahead)
    page = asyncio.run(blobs.unnamed_objects("", 1000, 3600, ch=CatalogCH()))
    assert page.keys == [key], "five minutes old here, two hours old by the clock that stamped it"
    s3.objects[(BUCKET, key)] = (
        b"x",
        datetime.now(UTC) + timedelta(hours=2) - timedelta(minutes=5),
    )
    assert asyncio.run(blobs.unnamed_objects("", 1000, 3600, ch=CatalogCH())).keys == []


def test_user_erasure_counts_the_users_rows_with_sequential_consistency(s3: FakeS3) -> None:
    from server.core.ch import sequential_reads_active

    sha = "d" * 64

    class LaggingCH(EraseCH):
        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            self.counted.append((sql, dict(params or {})))
            if "system.mutations" in sql:  # the delete's mutation, done
                return {
                    "mutation_id": "m",
                    "is_done": 1,
                    "parts_to_do": 0,
                    "latest_fail_reason": "",
                }
            if "FROM loom.document_blobs" in sql and not sequential_reads_active():
                return {"n": 0, "stored": 0, "elsewhere": 0}  # behind the user's upload
            if "location != ''" in sql:
                return {"stored": 0, "elsewhere": 0}
            return {"n": 1}

        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
            if "system.mutations" in sql:  # the delete's mutation, done
                return [
                    {"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}
                ]
            return await super().query_all(sql, params, **kw)

    ch = LaggingCH(0, [{"namespace": "ns", "sha": sha}])
    asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    deletes = [sql for sql, _ in ch.executed if "loom.document_blobs" in sql]
    assert deletes, "the row's delete was sent: its count saw the row"


def test_a_bucket_needs_a_min_age_that_outlasts_a_writes_whole_retry_ladder() -> None:
    from server.settings import Settings

    with pytest.raises(
        ValueError, match="LOOM_DOCUMENT_BLOB_MIN_AGE_SECONDS must be at least 2346"
    ):
        Settings(
            document_object_store="s3",
            document_s3_bucket="b",
            ch_timeout_write_s=300,
            document_blob_min_age_seconds=600,
        )
    with pytest.raises(ValueError, match="bounded LOOM_CH_TIMEOUT_WRITE_S"):
        Settings(document_object_store="s3", document_s3_bucket="b", ch_timeout_write_s=0)
    for field, env in (("ch_timeout_read_s", "READ_S"), ("ch_pool_wait_ms", "POOL_WAIT_MS")):
        with pytest.raises(ValueError, match=f"bounded LOOM_CH_.*{env}"):
            Settings(document_object_store="s3", document_s3_bucket="b", **{field: 0})
    Settings(document_object_store="s3", document_s3_bucket="b")  # the defaults hold
    # The floor the docs give for the defaults.
    with pytest.raises(ValueError, match="must be at least 906 "):
        Settings(document_s3_bucket="b", document_blob_min_age_seconds=905)
    Settings(document_s3_bucket="b", document_blob_min_age_seconds=906)
    Settings(ch_timeout_write_s=0)  # no bucket: nothing to outwait


def test_the_walk_keeps_the_upload_record_while_an_object_of_the_original_is_left(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = CatalogCH()
    sha = "1" * 64
    s3.objects[(BUCKET, f"loom/originals/o/a/{sha}/old")] = (
        b"x",
        datetime.now(UTC) - timedelta(hours=2),
    )
    s3.objects[(BUCKET, f"loom/originals/o/a/{sha}/young")] = (b"x", datetime.now(UTC))
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", lambda c: None)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "")
    assert asyncio.run(blob_gc._sweep_bucket(3600)) == 1
    assert ch.uploads_deleted == [], "the young write's object is still there"


def test_the_walk_reads_the_catalog_with_sequential_consistency(s3: FakeS3) -> None:
    from server.core.ch import sequential_reads_active

    ch = CatalogCH()
    asyncio.run(blobs.put("o", "a", b"just written", ch=ch))
    s3.age_all(7200)
    real = ch.query_all

    async def lagging(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "keys" in params and not sequential_reads_active():
            return []  # a replica behind the row
        return await real(sql, params)

    ch.query_all = lagging  # type: ignore[method-assign]
    assert asyncio.run(blobs.unnamed_objects("", 1000, 3600, ch=ch)).keys == []


def test_namespace_erasure_checks_the_bucket_with_sequential_consistency(s3: FakeS3) -> None:
    from server.core.ch import sequential_reads_active

    class Lagging(EraseCH):
        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            if "location != ''" in sql and not sequential_reads_active():
                return {"stored": 0, "elsewhere": 0}  # a replica behind the stray row
            return await super().query_one(sql, params, **kw)

    with pytest.raises(RuntimeError, match="outside LOOM_DOCUMENT_S3_BUCKET"):
        asyncio.run(
            steps_memory.delete_document_blobs_by_namespace(
                "o", "a", ch=Lagging(2, [], elsewhere=1)
            )
        )


def test_a_listing_with_no_date_reads_ages_on_this_hosts_clock_less_the_signature_skew(
    s3: FakeS3,
) -> None:
    key = f"loom/originals/o/a/{'2' * 64}/w"
    s3.objects[(BUCKET, key)] = (b"x", datetime.now(UTC) - timedelta(minutes=70))
    # 70 minutes old, an hour's age, less 15 minutes for a clock the store did not send.
    assert asyncio.run(blobs.unnamed_objects("", 1000, 3600, ch=CatalogCH())).keys == []
    s3.objects[(BUCKET, key)] = (b"x", datetime.now(UTC) - timedelta(minutes=80))
    assert asyncio.run(blobs.unnamed_objects("", 1000, 3600, ch=CatalogCH())).keys == [key]


def test_a_recount_the_store_fails_keeps_the_records_and_the_pass_goes_on(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = CatalogCH()
    shas = ["3" * 64, "4" * 64]
    for sha in shas:
        s3.objects[(BUCKET, f"loom/originals/o/a/{sha}/w")] = (
            b"x",
            datetime.now(UTC) - timedelta(hours=2),
        )
    # The fake's page of two keys puts the second original last on its page: an edge.
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", lambda c: None)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "")
    failures: list[str] = []
    monkeypatch.setattr(
        blob_gc.pass_context, "note_failure", lambda e, what="": failures.append(what)
    )
    s3.objects[(BUCKET, f"loom/originals/o/b/{'5' * 64}/w")] = (b"x", datetime.now(UTC))
    real = s3.list_objects_v2

    def recount_fails(**kw: Any) -> dict:
        if kw.get("Prefix", "").endswith(f"{shas[1]}/"):
            raise _client_error("SlowDown")
        return real(**kw)

    s3.list_objects_v2 = recount_fails  # type: ignore[method-assign]
    assert asyncio.run(blob_gc._sweep_bucket(3600)) == 2
    assert failures == ["document_blob_gc: bucket recount"]
    assert shas[1] not in ch.uploads_deleted, "kept while its prefix could not be listed"


def test_an_original_whose_keys_began_on_a_refused_page_keeps_its_records(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = CatalogCH()
    sha = "6" * 64
    old = datetime.now(UTC) - timedelta(hours=2)
    s3.objects[(BUCKET, f"loom/originals/o/a/{'5' * 64}/w")] = (b"x", old)
    s3.objects[(BUCKET, f"loom/originals/o/a/{sha}/k1")] = (b"x", old)  # last of page one
    s3.objects[(BUCKET, f"loom/originals/o/a/{sha}/k2")] = (b"x", old)  # first of page two
    monkeypatch.setattr(blob_gc, "_OBJECT_PAGE", 2)
    monkeypatch.setattr(s3, "PAGE", 2)
    monkeypatch.setattr(blobs, "_default_client", lambda: ch)
    monkeypatch.setattr(blob_gc.pass_context, "set_cursor", lambda c: None)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "")
    monkeypatch.setattr(blob_gc.pass_context, "note_failure", lambda e, what="": None)
    real = s3.delete_objects
    calls = {"n": 0}

    def first_refused(**kw: Any) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _client_error("InternalError")
        return real(**kw)

    s3.delete_objects = first_refused  # type: ignore[method-assign]
    asyncio.run(blob_gc._sweep_bucket(3600))
    assert f"loom/originals/o/a/{sha}/k1" in s3.keys()
    assert sha not in ch.uploads_deleted, "k1 is left, so the record stays"


def test_a_key_the_store_refuses_within_a_delete_fails_the_namespace_erasure(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = f"loom/originals/o/a/{'7' * 64}/w"
    s3.objects[(BUCKET, key)] = (b"x", datetime.now(UTC))

    def refuse_some(*, Bucket: str, Delete: dict) -> dict:
        return {"Errors": [{"Key": o["Key"], "Code": "AccessDenied"} for o in Delete["Objects"]]}

    monkeypatch.setattr(s3, "delete_objects", refuse_some)
    with pytest.raises(object_store.ObjectStoreError, match="AccessDenied"):
        asyncio.run(steps_memory.delete_document_blobs_by_namespace("o", "a", ch=EraseCH(1, [])))


def test_a_key_already_gone_within_a_delete_is_no_error(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    def already_gone(*, Bucket: str, Delete: dict) -> dict:
        return {"Errors": [{"Key": o["Key"], "Code": "NoSuchKey"} for o in Delete["Objects"]]}

    monkeypatch.setattr(s3, "delete_objects", already_gone)
    asyncio.run(object_store.delete(BUCKET, ["loom/originals/o/a/x/w"]))


def test_a_buffered_read_waits_for_a_slot_and_answers_overloaded_when_none_frees(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    from server.core.errors import OverloadedError

    ch = CatalogCH()
    sha = asyncio.run(blobs.put("o", "a", b"x", ch=ch))
    settings_override(ch_pool_wait_ms=10)
    monkeypatch.setattr(object_store, "_TRANSFER_SLOTS", 0)  # every read slot busy
    with pytest.raises(OverloadedError):
        _read(ch, "o", "a", sha)


def test_a_catalog_failure_in_the_bucket_walk_lets_the_namespace_walk_run(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    s3.objects[(BUCKET, f"loom/originals/o/a/{'8' * 64}/w")] = (
        b"x",
        datetime.now(UTC) - timedelta(hours=2),
    )
    walked: list[tuple[str, str]] = []

    async def failing(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("ClickHouse is unavailable")

    async def namespaces(after: Any, limit: int) -> list[tuple[str, str]]:
        return [] if after != ("", "") else [("o", "a")]

    async def sweep_namespace(org: str, ns: str, min_age_s: int) -> int:
        walked.append((org, ns))
        return 0

    failures: list[str] = []
    monkeypatch.setattr(blobs, "_named", failing)
    monkeypatch.setattr(blobs, "namespaces", namespaces)
    monkeypatch.setattr(blob_gc, "sweep_namespace", sweep_namespace)
    monkeypatch.setattr(blob_gc.pass_context, "resume_cursor", lambda: "")
    monkeypatch.setattr(
        blob_gc.pass_context, "note_failure", lambda e, what="": failures.append(what)
    )
    monkeypatch.setattr(blobs, "_default_client", lambda: CatalogCH())
    asyncio.run(blob_gc.run_pass())
    assert failures == ["document_blob_gc: bucket walk"] and walked == [("o", "a")]


def test_the_walks_catalog_read_binds_what_one_parameter_carries_a_statement(
    s3: FakeS3,
) -> None:
    from server.core.ch_params import chunks

    ch = CatalogCH()
    for i in range(1000):
        s3.objects[(BUCKET, f"loom/originals/o/a/{i:064x}/w")] = (
            b"x",
            datetime.now(UTC) - timedelta(hours=2),
        )
    seen: list[int] = []
    real = ch.query_all

    async def record(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "keys" in params:
            seen.append(len(params["keys"]))
        return await real(sql, params)

    ch.query_all = record  # type: ignore[method-assign]
    s3.PAGE = 1000
    page = asyncio.run(blobs.unnamed_objects("", 1000, 3600, ch=ch))
    keys = [("o", "a", f"{i:064x}") for i in range(1000)]
    assert len(page.keys) == 1000 and len(seen) > 1
    assert seen == [len(c) for c in chunks(keys)]


def test_an_upload_the_bucket_is_too_busy_for_is_refused_with_no_row_or_object(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    from server.core.errors import OverloadedError

    ch = CatalogCH()
    inserted: list[str] = []
    real = ch.insert

    async def record(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        inserted.append(table)
        await real(table, rows, settings)

    ch.insert = record  # type: ignore[method-assign]
    settings_override(ch_pool_wait_ms=10)
    monkeypatch.setattr(object_store, "_TRANSFER_SLOTS", 0)
    with pytest.raises(OverloadedError):
        asyncio.run(blobs.put("o", "a", b"x", uploader="alice", ch=ch))
    # Only the record of who uploaded it, which a user erasure removes.
    assert inserted == ["loom.blob_uploads"] and ch.rows == [] and s3.keys() == []


def test_a_server_with_a_bucket_and_no_boto3_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "boto3" else real(name, *a)
    )
    settings_override(document_object_store="s3", document_s3_bucket="b")
    with pytest.raises(RuntimeError, match="needs boto3"):
        object_store.require_client()
    settings_override(document_object_store="clickhouse", document_s3_bucket="")
    object_store.require_client()  # no bucket: the CLI and the ClickHouse store need none


def test_a_server_with_a_bucket_and_an_old_boto3_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    import botocore.config

    real = botocore.config.Config

    def old_config(**kw: Any) -> Any:
        if "request_checksum_calculation" in kw:
            raise TypeError("Got unexpected keyword argument 'request_checksum_calculation'")
        return real(**kw)

    monkeypatch.setattr(botocore.config, "Config", old_config)
    settings_override(document_object_store="s3", document_s3_bucket="b")
    with pytest.raises(RuntimeError, match="needs boto3 1.36"):
        object_store.require_client()


def test_the_client_takes_its_timeouts_from_the_settings(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    # Credentials from the environment and a fixed region, so building the client looks up neither.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    settings_override(
        document_s3_region="us-east-1",
        document_s3_connect_timeout_s=2.5,
        document_s3_read_timeout_s=45.0,
    )
    object_store._client_for.cache_clear()
    try:
        config = object_store._client().meta.config
    finally:
        object_store._client_for.cache_clear()
    assert (config.connect_timeout, config.read_timeout) == (2.5, 45.0)


async def test_listings_and_deletes_share_one_bound_per_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every listing and delete, from any caller, waits on the same LISTING_SLOTS
    threads, so background work never takes a thread a transfer was promised."""
    running = peak = 0
    gate, full = asyncio.Event(), asyncio.Event()

    async def fake_s3(method: str, **kwargs: object) -> dict[str, object]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == object_store.LISTING_SLOTS:
            full.set()
        await gate.wait()
        running -= 1
        return {}

    monkeypatch.setattr(object_store, "_s3", fake_s3)
    calls = [object_store.count_under("b", f"p{i}/") for i in range(5)] + [
        object_store.delete("b", [f"k{i}"]) for i in range(5)
    ]
    tasks = [asyncio.create_task(c) for c in calls]
    await asyncio.wait_for(full.wait(), 5)
    await asyncio.sleep(0)  # a fifth call, were it let through, would start here
    assert peak == object_store.LISTING_SLOTS
    gate.set()
    await asyncio.gather(*tasks)


class _StalledBucket:
    """A client whose calls block their bucket thread until `gate` opens."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.running = 0
        self.lock = threading.Lock()

    def _call(self, **kwargs: object) -> dict[str, object]:
        with self.lock:
            self.running += 1
        self.gate.wait(10)
        with self.lock:
            self.running -= 1
        return {}

    list_objects_v2 = get_object = put_object = _call


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_a_cancelled_listing_keeps_its_slot_until_its_thread_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a caller cannot stop its bucket thread, so the slot stays
    taken until the thread is done: with every slot's caller cancelled, a new
    listing starts only once the stalled threads return."""
    bucket = _StalledBucket()
    monkeypatch.setattr(object_store, "_client", lambda: bucket)
    first = [asyncio.create_task(object_store.count_under("b", f"p{i}/")) for i in range(4)]
    await _until(lambda: bucket.running == object_store.LISTING_SLOTS)
    for t in first:
        t.cancel()
    await asyncio.gather(*first, return_exceptions=True)
    later = asyncio.create_task(object_store.count_under("b", "q/"))
    await asyncio.sleep(0.2)
    assert bucket.running == object_store.LISTING_SLOTS and not later.done(), (
        "it waits for a thread"
    )
    bucket.gate.set()
    assert await asyncio.wait_for(later, 5) == 0


async def test_a_cancelled_transfer_keeps_its_slot_until_its_thread_is_done(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    from server.core.errors import OverloadedError

    settings_override(ch_pool_wait_ms=100)
    bucket = _StalledBucket()
    monkeypatch.setattr(object_store, "_client", lambda: bucket)
    loc = object_store.new_location("o", "ns", "0" * 64)
    n = object_store._TRANSFER_SLOTS
    reads = [asyncio.create_task(object_store.read(loc)) for _ in range(n)]
    await _until(lambda: bucket.running == n)
    for t in reads:
        t.cancel()
    await asyncio.gather(*reads, return_exceptions=True)
    with pytest.raises(OverloadedError):
        await object_store.read(loc)  # every transfer thread is still taken: 503
    bucket.gate.set()


async def test_a_stream_stopped_mid_read_closes_its_body_after_the_read_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    gate, reading = threading.Event(), threading.Event()

    class _Body:
        def read(self, n: int = -1) -> bytes:
            events.append("read")
            reading.set()
            gate.wait(10)
            events.append("read done")
            return b"x"

        def close(self) -> None:
            events.append("close")

    class _Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            return {"Body": _Body()}

    monkeypatch.setattr(object_store, "_client", lambda: _Client())
    chunks = object_store.stream(object_store.new_location("o", "ns", "0" * 64))
    pending = asyncio.ensure_future(chunks.__anext__())
    await _until(reading.is_set)
    pending.cancel()  # the consumer goes; the generator's cleanup runs
    await asyncio.sleep(0.1)
    assert events == ["read"] and not pending.done(), "no close while the read runs"
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)
    assert events == ["read", "read done", "close"]


async def test_a_stream_cancelled_twice_still_closes_its_body_and_frees_its_slot(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    """A server cancels a gone client's response until it leaves: the second
    cancel lands in the stream's cleanup, which must still close and release."""
    settings_override(ch_pool_wait_ms=200)
    closed: list[bool] = []
    gate, reading = threading.Event(), threading.Event()
    stall = True

    class _Body:
        def __init__(self) -> None:
            self.left = [b"x"]

        def read(self, n: int = -1) -> bytes:
            if stall:
                reading.set()
                gate.wait(10)
            return self.left.pop() if self.left else b""

        def close(self) -> None:
            closed.append(True)

    class _Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            return {"Body": _Body()}

    monkeypatch.setattr(object_store, "_client", lambda: _Client())
    loc = object_store.new_location("o", "ns", "0" * 64)
    chunks = object_store.stream(loc)
    pending = asyncio.ensure_future(chunks.__anext__())
    await _until(reading.is_set)
    pending.cancel()
    await asyncio.sleep(0.05)
    pending.cancel()  # lands in the cleanup's wait
    with pytest.raises(asyncio.CancelledError):
        await pending
    stall = False
    gate.set()
    await _until(lambda: closed == [True])

    async def drain() -> bytes:
        return b"".join([c async for c in object_store.stream(loc)])

    # Every stream slot is free again: as many streams as there are slots all start.
    assert (
        await asyncio.gather(*(drain() for _ in range(object_store.STREAM_SLOTS)))
        == [b"x"] * object_store.STREAM_SLOTS
    )


def test_a_write_slower_than_its_upload_budget_is_refused_and_names_no_object(
    s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store may date an object from the start of its upload: a write past
    the budget the min-age floor allows leaves no row, and its object, which no row
    names, to the bucket walk (nothing in the request waits on a listing slot)."""
    ch = CatalogCH()
    monkeypatch.setattr(blobs, "DOCUMENT_UPLOAD_BUDGET_S", -1)  # every upload is too slow
    with pytest.raises(object_store.ObjectStoreError, match="took longer than") as e:
        asyncio.run(blobs.put("o", "ns", b"slow", ch=ch))
    assert e.value.status_code == 503 and ch.rows == [] and len(s3.keys()) == 1


def test_both_stores_write_every_column_of_the_catalog_row(
    s3: FakeS3, settings_override: Any
) -> None:
    """Each write of a sha re-inserts its key with its own location, and the
    newest row (`ORDER BY created_at DESC LIMIT 1`) is read whole: a column a
    write left out would read as its default, and a bucket row would turn into
    an empty ClickHouse one."""
    from tests.unit.schema_ddl import column_names

    ch = CatalogCH()
    settings_override(document_object_store="clickhouse")
    asyncio.run(blobs.put("o", "ns", b"in clickhouse", ch=ch))
    settings_override(document_object_store="s3")
    asyncio.run(blobs.put("o", "ns", b"in the bucket", ch=ch))
    defaulted = {"created_at"}  # the row's version: the server's clock at insert
    for row in ch.rows:
        assert set(row) | defaulted == set(column_names("document_blobs")), row


async def test_a_stream_call_that_fails_after_its_consumer_went_logs_no_lost_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gate, reading = threading.Event(), threading.Event()

    class _Body:
        def read(self, n: int = -1) -> bytes:
            reading.set()
            gate.wait(10)
            raise _client_error("RequestTimeout")

        def close(self) -> None:
            pass

    class _Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            return {"Body": _Body()}

    monkeypatch.setattr(object_store, "_client", lambda: _Client())
    chunks = object_store.stream(object_store.new_location("o", "ns", "0" * 64))
    pending = asyncio.ensure_future(chunks.__anext__())
    await _until(reading.is_set)
    pending.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 5)
    import gc

    gc.collect()
    await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text


def test_the_erasure_cli_refuses_a_bucket_without_boto3(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import importlib.util

    from server.cli import hygiene

    settings_override(document_s3_bucket="b")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert hygiene._cmd_erasure_re_verify(argparse.Namespace(resume=True, erase=False)) == 2
    assert hygiene._cmd_namespaces_gc(argparse.Namespace(erase=True)) == 2
    assert "needs boto3" in capsys.readouterr().err
    # A read-only re-verify still runs: its census lists the bucket apart.
    monkeypatch.setattr(hygiene, "run_connected", lambda run: 0)
    assert hygiene._cmd_erasure_re_verify(argparse.Namespace(resume=False, erase=False)) == 0


def test_a_failed_saga_keeps_its_table_counts_when_the_bucket_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga

    async def tables(*args: Any, **kw: Any) -> dict[str, int]:
        return {"loom.chunks": 2}

    async def bucket_down(*args: Any, **kw: Any) -> int:
        raise object_store.ObjectStoreError("object store list failed (SlowDown)", operation="list")

    monkeypatch.setattr(saga.erasure_census, "residue_census", tables)
    monkeypatch.setattr(saga.erasure_census, "object_residue", bucket_down)
    verify: dict[str, Any] = {}
    asyncio.run(saga._census_tail("o", "namespace", "a", verify, purge=False))
    assert verify["per_table"] == {"loom.chunks": 2} and verify["count"] == 2


def test_an_evidence_census_keeps_the_table_counts_when_the_bucket_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the census tail and `loom erasure-re-verify` print for a failed saga."""

    async def tables(*args: Any, **kw: Any) -> dict[str, int]:
        return {"loom.chunks": 2}

    down = object_store.ObjectStoreError("object store list failed (SlowDown)", operation="list")

    async def bucket_down(*args: Any, **kw: Any) -> int:
        raise down

    monkeypatch.setattr(erasure_census, "residue_census", tables)
    monkeypatch.setattr(erasure_census, "object_residue", bucket_down)
    assert asyncio.run(erasure_census.evidence_census("o", "namespace", "a")) == (
        {"loom.chunks": 2},
        down,
    )
    assert asyncio.run(erasure_census.evidence_census("o", "user_id", "u")) == (
        {"loom.chunks": 2},
        None,
    ), "a user scope lists no bucket"


def test_a_namespace_verify_counts_the_bucket_objects_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga_steps

    async def tables(*args: Any, **kw: Any) -> dict[str, int]:
        return {"loom.chunks": 0}

    async def left(*args: Any, **kw: Any) -> int:
        return 3

    monkeypatch.setattr(saga_steps.erasure_census, "residue_census", tables)
    monkeypatch.setattr(saga_steps.erasure_census, "object_residue", left)
    holder: dict[str, Any] = {}
    with pytest.raises(saga_steps.ErasureResidueError):
        asyncio.run(saga_steps._step_verify_residue("o", "namespace", "a", holder))
    assert holder["per_table"] == {"loom.chunks": 0, erasure_census.OBJECT_RESIDUE: 3}
    holder = {}
    assert asyncio.run(saga_steps._step_verify_residue("o", "user_id", "u", holder)) == 0
    assert holder["per_table"] == {"loom.chunks": 0}, "a user scope lists no bucket"


def test_user_erasure_on_the_clickhouse_store_deletes_by_the_pairs_it_read(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    """No bucket: the bytes are the rows, deleted by the pairs read, a batch per
    statement; no mutation carries a subquery (a replicated table refuses one)."""
    from server.erasure.store import mutations

    settings_override(document_object_store="clickhouse", document_s3_bucket="")

    class _Rows(EraseCH):
        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            out = await super().query_one(sql, params, **kw)
            return {"n": 3} if out == {"n": 0} and "loom.document_blobs" in sql else out

    async def no_wait(*args: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(mutations, "wait_for_mutation", no_wait)
    ch = _Rows(0, [{"namespace": "ns", "sha": f"{i:x}" * 64} for i in range(5)])
    assert asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch)) == 3
    deletes = [sql for sql, _ in ch.executed if "ALTER TABLE loom.document_blobs" in sql]
    assert len(deletes) == 1 and "{pairs:Array(Tuple(String, String))}" in deletes[0]
    assert "SELECT" not in deletes[0]
    assert ch.own_reads == 1


def test_an_original_another_writer_named_since_the_first_read_stays(
    monkeypatch: pytest.MonkeyPatch, settings_override: Any
) -> None:
    from server.erasure.store import mutations

    settings_override(document_object_store="clickhouse", document_s3_bucket="")

    class _Shared(EraseCH):
        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
            if sql.startswith("SELECT namespace, sha FROM ("):  # another writer named "b"
                return [r for r in self.own if r["sha"] == "a" * 64]
            return await super().query_all(sql, params, **kw)

        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            out = await super().query_one(sql, params, **kw)
            return {"n": 1} if out == {"n": 0} and "loom.document_blobs" in sql else out

    async def no_wait(*args: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(mutations, "wait_for_mutation", no_wait)
    ch = _Shared(0, [{"namespace": "ns", "sha": "a" * 64}, {"namespace": "ns", "sha": "b" * 64}])
    asyncio.run(steps_memory.erase_blobs_by_user("o", "alice", ch=ch))
    deleted = [p["pairs"] for sql, p in ch.executed if "ALTER TABLE loom.document_blobs" in sql]
    assert deleted == [[("ns", "a" * 64)]]


def test_a_namespace_erasure_after_a_prefix_moves_up_leaves_another_orgs_object(
    s3: FakeS3, settings_override: Any
) -> None:
    """With the prefix moved from `loom/originals` to `loom`, org `acme`'s object
    sits under org `originals`' namespace `acme`: erasing that namespace must not
    take it, nor count it as its residue."""
    sha = "c" * 64
    acme = f"loom/originals/acme/docs/{sha}/w"
    s3.objects[(BUCKET, acme)] = (b"x", datetime.now(UTC))
    settings_override(document_s3_prefix="loom")
    asyncio.run(
        steps_memory.delete_document_blobs_by_namespace("originals", "acme", ch=EraseCH(0, []))
    )
    assert s3.keys() == [acme]
    assert asyncio.run(erasure_census.object_residue("originals", "acme")) == 0


def test_object_residue_finds_the_scopes_keys_behind_a_page_of_another_scopes(
    s3: FakeS3, settings_override: Any
) -> None:
    """A full page of another org's keys under the prefix must not hide the erased
    scope's own residue."""
    settings_override(document_s3_prefix="loom")
    for i in range(3):  # the fake's page holds two keys
        s3.objects[(BUCKET, f"loom/originals/acme/docs/{i:064x}/w")] = (b"x", datetime.now(UTC))
    own = f"loom/originals/acme/{'f' * 64}/w"  # org `originals`, namespace `acme`
    s3.objects[(BUCKET, own)] = (b"x", datetime.now(UTC))
    assert asyncio.run(erasure_census.object_residue("originals", "acme")) >= 1
