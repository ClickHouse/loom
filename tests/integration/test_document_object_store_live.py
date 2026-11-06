"""Document originals in an S3-compatible bucket, against live ClickHouse and a
local S3-compatible store (`LOOM_TEST_S3_ENDPOINT`, skipped without it).

- An original is an object named by its catalog row, and reads back byte for
  byte buffered and streamed.
- The sweep deletes the object of a deleted row and an object no row names,
  and keeps every object a row names.
- Namespace erasure deletes every object of the scope and its census counts
  none left; user erasure deletes the objects of the user's own originals and
  keeps another writer's.
- `loom knowledge migrate-originals` moves the originals held in ClickHouse to
  checked objects and switches their rows, refuses bytes that do not hash to
  their sha, and keeps a row replaced since the walk read it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.core import object_store
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_memory
from server.knowledge.app.originals import migrate_originals
from server.knowledge.store import blobs
from server.settings import settings
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

RAW = bytes(range(256)) * 9000  # about 2.3 MB: several stream chunks


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Any:
    endpoint = os.environ.get("LOOM_TEST_S3_ENDPOINT") or pytest.skip("needs LOOM_TEST_S3_ENDPOINT")
    import boto3

    for name, value in {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    bucket = f"loom-it-{uuid.uuid4().hex[:12]}"
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    for field, value in {
        "document_object_store": "s3",
        "document_s3_bucket": bucket,
        "document_s3_endpoint_url": endpoint,
        "document_s3_region": "us-east-1",
        "document_s3_prefix": "it/originals",
    }.items():
        monkeypatch.setattr(settings, field, value)
    object_store._client_for.cache_clear()
    yield client, bucket
    object_store._client_for.cache_clear()
    left = client.list_objects_v2(Bucket=bucket).get("Contents", [])
    if left:
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in left]})
    client.delete_bucket(Bucket=bucket)


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


def _keys(s3: Any, prefix: str = "") -> list[str]:
    client, bucket = s3
    out = client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    return sorted(o["Key"] for o in out)


async def _read(ch: Any, org: str, ns: str, sha: str) -> bytes | None:
    stored = await blobs.locate(org, ns, sha, ch=ch)
    return None if stored is None else await blobs.read(org, ns, sha, stored=stored, ch=ch)


async def _rows(ch: Any, org: str, ns: str) -> list[dict[str, Any]]:
    return await ch.query_all(
        "SELECT toString(sha256) AS sha, location, size, length(data) AS data_len"
        " FROM loom.document_blobs WHERE org = {org:String} AND namespace = {ns:String}"
        " ORDER BY created_at",
        params={"org": org, "ns": ns},
    )


@pytest.mark.asyncio
async def test_an_original_is_a_checked_object_that_reads_back_whole(s3: Any, ch: Any) -> None:
    org, ns = unique_org("objstore"), "docs"
    sha = await blobs.put(org, ns, RAW, uploader="alice", ch=ch)
    assert sha == hashlib.sha256(RAW).hexdigest()
    (key,) = _keys(s3)
    assert key.startswith(f"it/originals/{org}/{ns}/{sha}/")
    (row,) = await _rows(ch, org, ns)
    assert row == {"sha": sha, "location": f"s3://{s3[1]}/{key}", "size": len(RAW), "data_len": 0}
    # Every other column of the catalog row: its scope, and its version (created_at).
    whole = await ch.query_one(
        "SELECT org, namespace, dateDiff('second', created_at, now64(3)) AS age"
        " FROM loom.document_blobs WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": ns},
    )
    assert (whole["org"], whole["namespace"]) == (org, ns) and 0 <= int(whole["age"]) < 60
    assert await _read(ch, org, ns, sha) == RAW
    stored = await blobs.locate(org, ns, sha, ch=ch)
    assert stored is not None
    async with blobs.stream(org, ns, sha, stored=stored, ch=ch) as chunks:
        assert b"".join([c async for c in chunks]) == RAW
    assert await blobs.locate(org, ns, sha, ch=ch) == blobs.Stored(len(RAW), row["location"])


@pytest.mark.asyncio
async def test_a_deleted_rows_object_goes_with_it_and_the_walk_takes_an_orphan(
    s3: Any, ch: Any
) -> None:
    org, ns = unique_org("objstore-gc"), "docs"
    kept = await blobs.put(org, ns, b"kept", ch=ch)
    dropped = await blobs.put(org, ns, b"dropped", ch=ch)
    client, bucket = s3
    orphan = f"{object_store.namespace_prefix(org, ns)}{'1' * 64}/never-cataloged"
    client.put_object(Bucket=bucket, Key=orphan, Body=b"orphan")
    assert await blobs.delete(org, ns, [dropped], ch=ch) == blobs.Deleted(1)
    assert not any(f"/{dropped}/" in k for k in _keys(s3, object_store.namespace_prefix(org, ns)))
    # The store's `Date` is whole seconds and the walk reads an age by it: let the
    # orphan be a second old.
    await asyncio.sleep(1.1)
    gone, after = 0, ""
    while True:  # the bucket is this test's alone
        page = await blobs.unnamed_objects(after, 1000, 0, ch=ch)
        stale, after = page.keys, page.after
        await object_store.delete(bucket, stale)
        gone += len(stale)
        if not after:
            break
    assert gone == 1, "the deleted row's object went with it; the walk took the orphan"
    left = _keys(s3, object_store.namespace_prefix(org, ns))
    assert len(left) == 1 and f"/{kept}/" in left[0]
    assert await _read(ch, org, ns, kept) == b"kept"


@pytest.mark.asyncio
async def test_namespace_erasure_leaves_no_object_and_the_census_proves_it(
    s3: Any, ch: Any
) -> None:
    org = unique_org("objstore-erase-ns")
    for ns in ("a", "b", "keep"):
        await blobs.put(org, ns, f"original of {ns}".encode(), ch=ch)
    assert await erasure_census.object_residue(org, ["a", "b"]) == 2
    await steps_memory.delete_document_blobs_by_namespace(org, ["a", "b"], ch=ch)
    assert await erasure_census.object_residue(org, ["a", "b"]) == 0
    assert [k.split("/")[3] for k in _keys(s3, f"it/originals/{org}/")] == ["keep"]
    census, bucket_error = await erasure_census.evidence_census(org, "namespace", ["a", "b"], ch=ch)
    assert bucket_error is None
    assert census[erasure_census.OBJECT_RESIDUE] == 0 and census["loom.document_blobs"] == 0


@pytest.mark.asyncio
async def test_user_erasure_deletes_their_objects_and_keeps_another_writers(
    s3: Any, ch: Any
) -> None:
    org, ns = unique_org("objstore-erase-user"), "docs"
    mine = await blobs.put(org, ns, b"alice's original", uploader="alice", ch=ch)
    await blobs.put(org, ns, b"alice's original", uploader="alice", ch=ch)  # a second write
    theirs = await blobs.put(org, ns, b"bob's original", uploader="bob", ch=ch)
    assert len(_keys(s3, f"it/originals/{org}/{ns}/{mine}/")) == 2
    await steps_memory.erase_blobs_by_user(org, "alice", ch=ch)
    assert _keys(s3, f"it/originals/{org}/{ns}/{mine}/") == []
    assert len(_keys(s3, f"it/originals/{org}/{ns}/{theirs}/")) == 1
    assert await _read(ch, org, ns, theirs) == b"bob's original"
    rows = {r["sha"] for r in await _rows(ch, org, ns)}
    assert mine not in rows and theirs in rows, "alice's rows went, bob's stayed"


@pytest.mark.asyncio
async def test_erasure_refuses_a_row_laid_out_under_a_former_nested_prefix(
    s3: Any, ch: Any
) -> None:
    """A row written under `it/originals/old/...` (the prefix since moved up to
    `it/originals`) starts with the root but is not laid out under it."""
    org, ns = unique_org("objstore-old-prefix"), "docs"
    sha = hashlib.sha256(b"old").hexdigest()
    client, bucket = s3
    key = f"it/originals/old/{org}/{ns}/{sha}/w"
    client.put_object(Bucket=bucket, Key=key, Body=b"old")
    await ch.insert(
        "loom.document_blobs",
        [
            {
                "org": org,
                "namespace": ns,
                "sha256": sha,
                "data": b"",
                "size": 3,
                "location": f"s3://{bucket}/{key}",
            }
        ],
        settings={"async_insert": 0},
    )
    with pytest.raises(RuntimeError, match="outside LOOM_DOCUMENT_S3_BUCKET"):
        await steps_memory.delete_document_blobs_by_namespace(org, ns, ch=ch)
    await blobs.put(org, "fresh", b"current layout", ch=ch)
    assert await steps_memory.delete_document_blobs_by_namespace(org, "fresh", ch=ch) == 1
    assert _keys(s3, object_store.namespace_prefix(org, "fresh")) == []


@pytest.mark.asyncio
async def test_the_upload_record_cleanup_statement_runs_on_clickhouse(s3: Any, ch: Any) -> None:
    """The record of an original no row holds goes; one whose row is there stays."""
    org, ns = unique_org("objstore-forget"), "docs"
    gone = await blobs.put(org, ns, b"forgotten", ch=ch, uploader="alice")
    kept = await blobs.put(org, ns, b"still named", ch=ch, uploader="alice")
    await ch.execute(
        "DELETE FROM loom.document_blobs WHERE org = {org:String} AND namespace = {ns:String}"
        " AND sha256 = {sha:String}",
        params={"org": org, "ns": ns, "sha": gone},
    )
    await blobs.forget_uploads([(org, ns, gone), (org, ns, kept)], 0, ch=ch)
    rows = await ch.query_all(
        "SELECT toString(sha256) AS sha FROM loom.blob_uploads WHERE org = {org:String}",
        params={"org": org},
    )
    assert [str(r["sha"]) for r in rows] == [kept]


@pytest.mark.asyncio
async def test_originals_in_clickhouse_move_to_checked_objects(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-move"), "docs"
    client, bucket = s3
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    big = await blobs.put(org, ns, RAW, uploader="alice", ch=ch)
    small = await blobs.put(org, ns, b"small", ch=ch)
    # A row whose bytes do not hash to its sha: refused, and left where it is.
    bad = hashlib.sha256(b"what it should hold").hexdigest()
    await ch.insert(
        "loom.document_blobs",
        [{"org": org, "namespace": ns, "sha256": bad, "data": b"other bytes", "size": 11}],
        settings={"async_insert": 0},
    )
    monkeypatch.setattr(settings, "document_object_store", "s3")

    counted = await migrate_originals(org=org, dry_run=True)
    assert (counted.pending, counted.bytes_moved, counted.moved) == (3, len(RAW) + 5 + 11, 0)
    assert _keys(s3) == []

    result = await migrate_originals(org=org)
    assert (result.moved, result.bytes_moved, result.skipped) == (2, len(RAW) + 5, 0)
    assert len(result.errors) == 1 and bad in result.errors[0]
    for sha, raw in ((big, RAW), (small, b"small")):
        stored = await blobs.locate(org, ns, sha, ch=ch)
        assert stored is not None and stored.size == len(raw)
        key = objects_key(stored.location, bucket)
        assert key.startswith(object_store.original_prefix(org, ns, sha))
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert hashlib.sha256(body).hexdigest() == sha
        assert await _read(ch, org, ns, sha) == raw
    assert (await blobs.locate(org, ns, bad, ch=ch)) == blobs.Stored(11, "")
    # The uploader's record stays, so their erasure still finds the moved original.
    uploads = await ch.query_all(
        "SELECT toString(sha256) AS sha, user_id FROM loom.blob_uploads"
        " WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": ns},
    )
    assert uploads == [{"sha": big, "user_id": "alice"}]

    # Run again: nothing more moves, and the refused row is reported again.
    again = await migrate_originals(org=org, namespace=ns)
    assert (again.moved, again.skipped, len(again.errors)) == (0, 0, 1)
    assert len(_keys(s3, object_store.namespace_prefix(org, ns))) == 2


@pytest.mark.asyncio
async def test_a_row_replaced_since_the_walk_is_kept(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-race"), "docs"
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    sha = await blobs.put(org, ns, b"raced", ch=ch)
    monkeypatch.setattr(settings, "document_object_store", "s3")
    (held,) = await blobs.held_in_clickhouse(("", "", ""), 10, org=org, ch=ch)
    assert held == blobs.Held(org, ns, sha, 5)
    # Uploaded again on the bucket store after the walk read the row.
    await blobs.put(org, ns, b"raced", ch=ch)
    uploaded = await blobs.locate(org, ns, sha, ch=ch)
    assert await blobs.move_to_bucket(held, ch=ch) is False
    assert await blobs.locate(org, ns, sha, ch=ch) == uploaded
    # An original no row holds any more has nothing to move.
    gone = blobs.Held(org, ns, "0" * 64, 1)
    assert await blobs.move_to_bucket(gone, ch=ch) is False


def objects_key(location: str, bucket: str) -> str:
    loc = object_store.Location.parse(location)
    assert loc.bucket == bucket
    return loc.key
