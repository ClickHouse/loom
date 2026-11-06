"""Document originals in an S3-compatible bucket, against live ClickHouse and a
local S3-compatible store (`LOOM_TEST_S3_ENDPOINT`, skipped without it).

- An original is an object named by its catalog row, and reads back byte for
  byte buffered and streamed.
- The sweep deletes the object of a deleted row and an object no row names,
  and keeps every object a row names.
- Namespace erasure deletes every object of the scope and its census counts
  none left; user erasure deletes the objects of the user's own originals and
  keeps another writer's.
- `loom knowledge migrate-originals` moves the originals held in ClickHouse that
  the namespace still needs to checked objects and switches their rows, refuses
  bytes that do not hash to their sha or fill their size, and keeps a row replaced
  or gone since the walk read it. It is fenced by the erasure ledger: it does not
  start under an unfinished or just finished erasure of the namespace or of any
  user of the org, reports an original an erasure kept while it was copied, takes
  back a move a namespace erasure overtook, and leaves one a user erasure overtook
  to that erasure's outcome, read from the moved object.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core import object_store
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_memory
from server.knowledge.app import originals
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


async def _document(ch: Any, org: str, ns: str, sha: str, uri: str = "", user: str = "-") -> None:
    """A live document of `user` naming `sha` as its original: the namespace needs it."""
    await ch.insert(
        "loom.documents",
        [
            {
                "org": org,
                "namespace": ns,
                "source_uri": uri or f"docs/{sha[:12]}.md",
                "content": "",
                "tags": [],
                "version": 1,
                "blob_path": sha,
                "user_id": user,
            }
        ],
        settings={"async_insert": 0},
    )


@pytest.mark.asyncio
async def test_an_extraction_artifact_and_an_unexpired_upload_count_as_needed(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-needed"), "docs"
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    original = await blobs.put(org, ns, b"the document's original", ch=ch)
    artifact = await blobs.put(org, ns, b"its extraction artifact", ch=ch)
    uploaded = await blobs.put(org, ns, b"uploaded, not yet ingested", ch=ch)
    # A live document names the first as its original and the second as its artifact.
    await ch.insert(
        "loom.documents",
        [
            {
                "org": org,
                "namespace": ns,
                "source_uri": "docs/with-artifact.md",
                "content": "",
                "tags": [],
                "version": 1,
                "blob_path": original,
                "artifact_sha256": artifact,
            }
        ],
        settings={"async_insert": 0},
    )
    # An upload that has not expired holds the third.
    await ch.insert(
        "loom.knowledge_uploads",
        [
            {
                "org": org,
                "namespace": ns,
                "upload_id": str(uuid.uuid4()),
                "sha256": uploaded,
                "size": 26,
                "expires_at": datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
            }
        ],
        settings={"async_insert": 0},
    )
    monkeypatch.setattr(settings, "document_object_store", "s3")
    result = await migrate_originals(org=org, namespace=ns)
    assert (result.moved, result.unneeded, result.errors) == (3, 0, [])
    for sha in (original, artifact, uploaded):
        stored = await blobs.locate(org, ns, sha, ch=ch)
        assert stored is not None and stored.location.startswith("s3://")


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
    for sha in (big, small, bad):
        await _document(ch, org, ns, sha)
    # One no document names: the sweep's, so it is not moved.
    stray = await blobs.put(org, ns, b"nobody names this", ch=ch)
    monkeypatch.setattr(settings, "document_object_store", "s3")

    counted = await migrate_originals(org=org, dry_run=True)
    assert (counted.pending, counted.bytes, counted.moved) == (3, len(RAW) + 5 + 11, 0)
    assert counted.unneeded == 1
    assert _keys(s3) == []

    result = await migrate_originals(org=org)
    assert (result.moved, result.bytes, result.skipped, result.unneeded) == (2, len(RAW) + 5, 0, 1)
    assert await blobs.locate(org, ns, stray, ch=ch) == blobs.Stored(17, "")
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
    page = await blobs.held_in_clickhouse(org, ns, "", 10, ch=ch)
    assert page.after == sha
    (held,) = page.held
    assert (held.org, held.namespace, held.sha, held.size) == (org, ns, sha, 5)
    assert held.version_ms > 0
    # Uploaded again on the bucket store after the walk read the row.
    await blobs.put(org, ns, b"raced", ch=ch)
    uploaded = await blobs.locate(org, ns, sha, ch=ch)
    assert await originals.move_to_bucket(held, ch=ch) is False
    assert await blobs.locate(org, ns, sha, ch=ch) == uploaded
    # An original no row holds any more has nothing to move.
    gone = blobs.Held(org, ns, "0" * 64, 1, 0)
    assert await originals.move_to_bucket(gone, ch=ch) is False


def objects_key(location: str, bucket: str) -> str:
    loc = object_store.Location.parse(location)
    assert loc.bucket == bucket
    return loc.key


async def _held(ch: Any, org: str, ns: str, data: bytes, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An original written on the ClickHouse store, as the walk lists it, with the
    bucket store switched on."""
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    sha = await blobs.put(org, ns, data, ch=ch)
    await _document(ch, org, ns, sha)
    monkeypatch.setattr(settings, "document_object_store", "s3")
    (held,) = (await blobs.held_in_clickhouse(org, ns, "", 10, ch=ch)).held
    assert held.sha == sha
    return held


async def _erasure(ch: Any, org: str, scope_type: str, scope_value: str, status: str) -> str:
    """A ledger row for an erasure of the scope; its id."""
    erasure_id = str(uuid.uuid4())
    await ch.insert(
        "loom.erasure_requests",
        [
            {
                "id": erasure_id,
                "org": org,
                "scope_type": scope_type,
                "scope_value": scope_value,
                "requester": "it",
                "status": status,
                "version": 1,
            }
        ],
        settings={"async_insert": 0},
    )
    return erasure_id


@pytest.mark.asyncio
async def test_an_unfinished_erasure_of_its_namespace_refuses_the_move(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-fence"), "docs"
    await _held(ch, org, ns, b"fenced", monkeypatch)
    # Another namespace's erasure, or a list of others, does not cover it: it moves.
    await _erasure(ch, org, "namespace", "other", "in_progress")
    await _erasure(ch, org, "namespace_list", '{"namespaces": ["other"]}', "in_progress")
    moved = await migrate_originals(org=org)
    assert (moved.moved, moved.refused) == (1, [])
    # This namespace's does: a second original stays, refused by that erasure by name.
    second = await _held_another(ch, org, ns, b"fenced too", monkeypatch)
    covering = await _erasure(ch, org, "namespace", ns, "in_progress")
    result = await migrate_originals(org=org)
    assert (result.moved, result.errors, len(result.refused)) == (0, [], 1)
    assert f"erasure {covering} (namespace, in_progress)" in result.refused[0]
    assert await blobs.locate(org, ns, second.sha, ch=ch) == blobs.Stored(10, "")
    assert len(_keys(s3)) == 1  # the first original's object alone


async def _held_another(
    ch: Any, org: str, ns: str, data: bytes, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """One more original of the namespace written on the ClickHouse store, as `_held`."""
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    sha = await blobs.put(org, ns, data, ch=ch)
    await _document(ch, org, ns, sha, uri=f"docs/{sha[:12]}-b.md")
    monkeypatch.setattr(settings, "document_object_store", "s3")
    page = await blobs.held_in_clickhouse(org, ns, "", 10, ch=ch)
    (held,) = (h for h in page.held if h.sha == sha)
    return held


@pytest.mark.asyncio
async def test_an_erasure_during_the_copy_ends_the_move_before_its_row(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-undo"), "docs"
    held = await _held(ch, org, ns, b"erased while moving", monkeypatch)
    put = object_store.put

    async def put_then_erasure(*a: Any, **kw: Any) -> None:
        await put(*a, **kw)
        await _erasure(ch, org, "namespace", ns, "in_progress")
        await steps_memory.delete_document_blobs_by_namespace(org, ns, ch=ch)

    monkeypatch.setattr(object_store, "put", put_then_erasure)
    assert await originals.move_to_bucket(held, ch=ch) is False
    # No row named the object, so the move deleted it; the original is gone.
    assert await blobs.locate(org, ns, held.sha, ch=ch) is None
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_an_original_a_user_erasure_started_on_during_the_copy_is_reported(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-kept"), "docs"
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    sha = await blobs.put(org, ns, b"kept for another writer", uploader="alice", ch=ch)
    monkeypatch.setattr(settings, "document_object_store", "s3")
    (held,) = (await blobs.held_in_clickhouse(org, ns, "", 10, ch=ch)).held
    put = object_store.put

    async def put_then_erasure(*a: Any, **kw: Any) -> None:
        await put(*a, **kw)
        # Alice's erasure registers during the copy and has not reached the original yet.
        await _erasure(ch, org, "user_id", "alice", "in_progress")

    monkeypatch.setattr(object_store, "put", put_then_erasure)
    with pytest.raises(originals.ErasureOpen, match="stays in ClickHouse"):
        await originals.move_to_bucket(held, ch=ch)
    assert await blobs.locate(org, ns, sha, ch=ch) == blobs.Stored(held.size, "")
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_any_user_erasure_of_the_org_fences_the_move(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-author"), "docs"
    held = await _held(ch, org, ns, b"whose is it", monkeypatch)
    # Carol never uploaded it; a document of hers could name it at any moment.
    await _erasure(ch, org, "user_id", "carol", "in_progress")
    result = await migrate_originals(org=org)
    assert (result.moved, result.errors, len(result.refused)) == (0, [], 1)
    assert "user_id, in_progress" in result.refused[0]
    assert await blobs.locate(org, ns, held.sha, ch=ch) == blobs.Stored(held.size, "")


@pytest.mark.asyncio
async def test_a_failed_erasure_fences_the_move_until_it_is_completed(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-failed"), "docs"
    held = await _held(ch, org, ns, b"behind a failed erasure", monkeypatch)
    await _erasure(ch, org, "namespace", ns, "failed")
    # The advice names the worker that retries it and the command that completes it.
    result = await migrate_originals(org=org, namespace=ns)
    (error,) = result.refused
    assert "recovery worker" in error and "loom erasure-re-verify --resume" in error
    assert await blobs.locate(org, ns, held.sha, ch=ch) == blobs.Stored(held.size, "")
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_an_erasure_that_just_finished_fences_the_move(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-recent"), "docs"
    held = await _held(ch, org, ns, b"just erased around", monkeypatch)
    await ch.insert(
        "loom.erasure_requests",
        [
            {
                "id": str(uuid.uuid4()),
                "org": org,
                "scope_type": "user_id",
                "scope_value": "alice",
                "requester": "it",
                "status": "completed",
                "version": 1,
                "completed_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
        settings={"async_insert": 0},
    )
    result = await migrate_originals(org=org, namespace=ns)
    (error,) = result.refused
    assert "which finished just now, covers it; run again in a minute" in error
    assert await blobs.locate(org, ns, held.sha, ch=ch) == blobs.Stored(held.size, "")
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_a_failed_check_after_the_switch_is_reported_as_switched(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-unchecked"), "docs"
    held = await _held(ch, org, ns, b"checked after", monkeypatch)
    erasures = originals._erasures_since
    insert = ch.insert
    switched: list[str] = []

    async def insert_then_mark(table: str, rows: Any, **kw: Any) -> Any:
        out = await insert(table, rows, **kw)
        if table == blobs.TABLE and rows[0]["location"]:
            switched.append(rows[0]["location"])
        return out

    async def fails_after_the_switch(*a: Any, **kw: Any) -> Any:
        if switched:
            raise RuntimeError("ClickHouse went away")
        return await erasures(*a, **kw)

    monkeypatch.setattr(ch, "insert", insert_then_mark)
    monkeypatch.setattr(originals, "_erasures_since", fails_after_the_switch)
    with pytest.raises(originals.SwitchUnconfirmed, match="ClickHouse went away"):
        await originals.move_to_bucket(held, ch=ch)
    stored = await blobs.locate(org, ns, held.sha, ch=ch)
    assert stored is not None and stored.location == switched[0]


async def _raced(
    ch: Any,
    s3: Any,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    scopes: list[tuple[str, str]],
    step: Any = None,
) -> tuple[str, str, Any, list[str]]:
    """An original of alice's whose move meets erasures of `scopes` as its row lands: they
    register after the move's last ledger read and before the switching insert, and
    `step` (the erasure's own blob step, when given) runs there too, so the switched row
    lands after the erasure deleted the original's rows."""
    org, ns = unique_org(label), "docs"
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    sha = await blobs.put(org, ns, b"erased as it lands", uploader="alice", ch=ch)
    await _document(ch, org, ns, sha, user="alice")
    monkeypatch.setattr(settings, "document_object_store", "s3")
    (held,) = (await blobs.held_in_clickhouse(org, ns, "", 10, ch=ch)).held
    monkeypatch.setattr(originals, "_ERASURE_POLL_S", 0.05)
    insert = ch.insert
    landed: list[str] = []

    async def erasures_then_insert(table: str, rows: Any, **kw: Any) -> Any:
        if table == blobs.TABLE and rows[0]["location"] and not landed:
            landed.append(rows[0]["location"])
            for scope_type, scope_value in scopes:
                await _erasure(ch, org, scope_type, scope_value, "in_progress")
            if step is not None:
                await step(org, ns)
        return await insert(table, rows, **kw)

    monkeypatch.setattr(ch, "insert", erasures_then_insert)
    return org, ns, held, landed


async def _finish_erasures(ch: Any, org: str, after_s: float, then: Any = None) -> None:
    """Mark the org's erasures completed after `after_s`, running `then` first: their
    steps."""
    await asyncio.sleep(after_s)
    try:
        if then is not None:
            await then()
    finally:
        await _complete_erasures(ch, org)


async def _complete_erasures(ch: Any, org: str) -> None:
    await ch.execute(
        "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, requester,"
        " status, version, completed_at) SELECT id, org, scope_type, scope_value, requester,"
        " 'completed', 2, now() FROM loom.erasure_requests FINAL WHERE org = {org:String}",
        params={"org": org},
    )


@pytest.mark.asyncio
async def test_a_namespace_erasure_as_the_row_lands_takes_the_move_back(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A namespace's erasure, alone or with a user's: the move deletes its row and object
    # at once, without waiting.
    async def namespace_step(org: str, ns: str) -> None:
        await steps_memory.delete_document_blobs_by_namespace(org, ns, ch=ch)

    for label, scopes in (
        ("objstore-race-ns", [("namespace", "docs")]),
        ("objstore-race-both", [("user_id", "alice"), ("namespace", "docs")]),
    ):
        # The namespace erasure's own blob step runs as the row lands.
        org, ns, held, landed = await _raced(ch, s3, monkeypatch, label, scopes, namespace_step)
        assert await originals.move_to_bucket(held, ch=ch) is False
        assert await blobs.locate(org, ns, held.sha, ch=ch) is None
        assert not _keys(s3, object_store.namespace_prefix(org, ns))


@pytest.mark.asyncio
async def test_a_take_back_after_a_merge_keeps_a_re_upload_that_raced_the_switch(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The namespace's erasure deletes the original, the same bytes are uploaded again, and
    # then the switched row lands. The switch is versioned after the row the walk read, so
    # the re-upload wins the merge that runs before the take-back, and the take-back
    # deletes nothing of it.
    async def erased_then_uploaded(org: str, ns: str) -> None:
        await steps_memory.delete_document_blobs_by_namespace(org, ns, ch=ch)
        await blobs.put(org, ns, b"erased as it lands", uploader="bob", ch=ch)

    org, ns, held, landed = await _raced(
        ch, s3, monkeypatch, "objstore-race-merged", [("namespace", "docs")], erased_then_uploaded
    )
    unwrite = blobs.unwrite

    async def merged_then_unwrite(*a: Any, **kw: Any) -> None:
        await ch.execute(f"OPTIMIZE TABLE {blobs.TABLE} FINAL")
        await unwrite(*a, **kw)

    monkeypatch.setattr(blobs, "unwrite", merged_then_unwrite)
    assert await originals.move_to_bucket(held, ch=ch) is False
    stored = await blobs.locate(org, ns, held.sha, ch=ch)
    assert stored is not None and stored.location and stored.location != landed[0]
    assert await blobs.read(org, ns, held.sha, stored=stored, ch=ch) == b"erased as it lands"
    assert _keys(s3, object_store.namespace_prefix(org, ns)) == [
        objects_key(stored.location, s3[1])
    ]


@pytest.mark.asyncio
async def test_a_user_erasure_as_the_row_lands_decides_by_the_moved_object(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, bucket = s3
    # Each erasure here finishes within a second; a hung one fails the test, not the run.
    monkeypatch.setattr(originals, "_ERASURE_WAIT_S", 30)

    # The erasure took the original before the switched row landed: its step deleted every
    # object under the original's prefix (the move's included) and the original's rows. The
    # move's row lands alone, and the move deletes it.
    async def alices_step(org: str, ns: str) -> None:
        # Alice's erasure's own blob step: the original is hers alone.
        await steps_memory.erase_blobs_by_user(org, "alice", ch=ch)

    org, ns, held, landed = await _raced(
        ch, s3, monkeypatch, "objstore-race-took", [("user_id", "alice")], alices_step
    )
    finishing = asyncio.create_task(_finish_erasures(ch, org, 0.3))
    assert await originals.move_to_bucket(held, ch=ch) is False
    await finishing
    assert await blobs.locate(org, ns, held.sha, ch=ch) is None
    assert not _keys(s3, object_store.namespace_prefix(org, ns))

    # The erasure kept it (another writer needs it): the moved object is still there, and
    # the switch stands.
    org, ns, held, landed = await _raced(
        ch, s3, monkeypatch, "objstore-race-kept", [("user_id", "alice")], alices_step
    )
    # Bob's live document names the same bytes, so alice's erasure keeps them.
    await ch.insert(
        "loom.documents",
        [
            {
                "org": org,
                "namespace": ns,
                "source_uri": "docs/bobs.md",
                "content": "",
                "tags": [],
                "version": 1,
                "user_id": "bob",
                "blob_path": held.sha,
            }
        ],
        settings={"async_insert": 0},
    )

    finishing = asyncio.create_task(_finish_erasures(ch, org, 0.3))
    assert await originals.move_to_bucket(held, ch=ch) is True
    await finishing
    stored = await blobs.locate(org, ns, held.sha, ch=ch)
    assert stored is not None and stored.location == landed[0]
    assert _keys(s3, object_store.original_prefix(org, ns, held.sha))

    # It took the row but not the object, as a replica without the bucket set does when
    # it read the scope's rows before the switched row landed: the object goes too.
    org, ns, held, landed = await _raced(
        ch, s3, monkeypatch, "objstore-race-rowonly", [("user_id", "alice")]
    )

    async def row_only() -> None:
        await ch.execute(
            "DELETE FROM loom.document_blobs WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": ns},
        )

    finishing = asyncio.create_task(_finish_erasures(ch, org, 0.3, row_only))
    assert await originals.move_to_bucket(held, ch=ch) is False
    await finishing
    assert not _keys(s3, object_store.namespace_prefix(org, ns))

    # One that does not finish in time leaves the switched row to it, named.
    org, ns, held, landed = await _raced(
        ch, s3, monkeypatch, "objstore-race-slow", [("user_id", "alice")]
    )
    monkeypatch.setattr(originals, "_ERASURE_WAIT_S", 0.2)
    with pytest.raises(originals.SwitchUnconfirmed, match="user_id, in_progress"):
        await originals.move_to_bucket(held, ch=ch)
    stored = await blobs.locate(org, ns, held.sha, ch=ch)
    assert stored is not None and stored.location == landed[0]


@pytest.mark.asyncio
async def test_a_move_whose_original_stopped_being_needed_is_left_to_the_sweep(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-unneeded"), "docs"
    held = await _held(ch, org, ns, b"its document goes", monkeypatch)
    insert = ch.insert

    async def insert_then_document_goes(table: str, rows: Any, **kw: Any) -> Any:
        out = await insert(table, rows, **kw)
        if table == blobs.TABLE and rows[0]["location"]:
            await ch.execute(
                "DELETE FROM loom.documents WHERE org = {org:String} AND namespace = {ns:String}",
                params={"org": org, "ns": ns},
            )
        return out

    monkeypatch.setattr(ch, "insert", insert_then_document_goes)
    # The switch stands: a delete unbounded by age could take a racing writer's only row.
    assert await originals.move_to_bucket(held, ch=ch) is True
    stored = await blobs.locate(org, ns, held.sha, ch=ch)
    assert stored is not None and stored.location.startswith("s3://")
    # The sweep takes it once it is old enough, as any original nothing names.
    assert await blobs.unreferenced(org, ns, 0, 10, ch=ch) == [held.sha]


@pytest.mark.asyncio
async def test_a_failed_erasure_as_the_row_lands_is_waited_for_like_any(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns, held, landed = await _raced(ch, s3, monkeypatch, "objstore-race-failed", [])
    insert = ch.insert

    async def insert_then_failed(table: str, rows: Any, **kw: Any) -> Any:
        out = await insert(table, rows, **kw)
        if table == blobs.TABLE and rows[0]["location"]:
            await _erasure(ch, org, "user_id", "alice", "failed")
        return out

    monkeypatch.setattr(ch, "insert", insert_then_failed)
    # The recovery worker retries a failed erasure, so it is waited for: past the wait, the
    # switched row is left to it, named.
    monkeypatch.setattr(originals, "_ERASURE_WAIT_S", 0.3)
    with pytest.raises(originals.SwitchUnconfirmed, match="user_id, failed"):
        await originals.move_to_bucket(held, ch=ch)
    stored = await blobs.locate(org, ns, held.sha, ch=ch)
    assert stored is not None and stored.location == landed[0]


@pytest.mark.asyncio
async def test_a_copy_past_the_upload_budget_is_not_switched(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-slow"), "docs"
    held = await _held(ch, org, ns, b"slow copy", monkeypatch)
    monkeypatch.setattr(originals, "DOCUMENT_UPLOAD_BUDGET_S", -1)
    with pytest.raises(object_store.ObjectStoreError, match="before its row"):
        await originals.move_to_bucket(held, ch=ch)
    assert await blobs.locate(org, ns, held.sha, ch=ch) == blobs.Stored(held.size, "")
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_a_row_gone_before_the_switch_is_not_switched(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-gone"), "docs"
    held = await _held(ch, org, ns, b"gone mid move", monkeypatch)
    put = object_store.put

    async def put_then_delete(*a: Any, **kw: Any) -> None:
        await put(*a, **kw)
        await blobs.delete(org, ns, [held.sha], ch=ch)

    monkeypatch.setattr(object_store, "put", put_then_delete)
    assert await originals.move_to_bucket(held, ch=ch) is False
    assert await blobs.locate(org, ns, held.sha, ch=ch) is None
    # No row names the object, nor will: the move deleted it.
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_an_object_gone_before_its_read_back_is_not_switched(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-vanish"), "docs"
    held = await _held(ch, org, ns, b"vanished", monkeypatch)
    client, bucket = s3
    put = object_store.put

    async def put_then_vanish(loc: Any, *a: Any, **kw: Any) -> None:
        await put(loc, *a, **kw)
        client.delete_object(Bucket=bucket, Key=loc.key)

    monkeypatch.setattr(object_store, "put", put_then_vanish)
    # Not an erasure's (none covers it): an error, and the original stays.
    with pytest.raises(object_store.ObjectStoreError, match="gone before its read-back"):
        await originals.move_to_bucket(held, ch=ch)
    assert await blobs.locate(org, ns, held.sha, ch=ch) == blobs.Stored(held.size, "")


@pytest.mark.asyncio
async def test_bytes_that_do_not_fill_their_size_are_refused(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-short"), "docs"
    sha = hashlib.sha256(b"twelve bytes").hexdigest()
    await ch.insert(
        "loom.document_blobs",
        [{"org": org, "namespace": ns, "sha256": sha, "data": b"eleven byte", "size": 12}],
        settings={"async_insert": 0},
    )
    await _document(ch, org, ns, sha)
    result = await migrate_originals(org=org, namespace=ns)
    assert (result.moved, result.skipped, len(result.errors)) == (0, 0, 1)
    assert "size" in result.errors[0]
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_the_walk_pages_a_namespace_by_key(
    s3: Any, ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, ns = unique_org("objstore-pages"), "docs"
    monkeypatch.setattr(settings, "document_object_store", "clickhouse")
    shas = sorted([await blobs.put(org, ns, f"page {i}".encode(), ch=ch) for i in range(5)])
    for sha in shas:
        await _document(ch, org, ns, sha)
    monkeypatch.setattr(settings, "document_object_store", "s3")
    first = await blobs.held_in_clickhouse(org, ns, "", 2, ch=ch)
    assert ([h.sha for h in first.held], first.after) == (shas[:2], shas[1])
    # A moved key is no longer held. Until the table merges, its two versions are two of
    # a page's rows; either way a walk moves on with every page and finds the rest.
    assert await originals.move_to_bucket(first.held[0], ch=ch) is True
    found, after, pages = [], "", 0
    while True:
        page = await blobs.held_in_clickhouse(org, ns, after, 2, ch=ch)
        if not page.after:
            break
        assert page.after > after
        found += [h.sha for h in page.held]
        after, pages = page.after, pages + 1
    assert found == shas[1:] and pages >= 3
    last = await blobs.held_in_clickhouse(org, ns, shas[3], 2, ch=ch)
    assert ([h.sha for h in last.held], last.after) == (shas[4:], shas[4])
    # Merged, the moved key is one row, and the walk still finds the rest.
    await ch.execute("OPTIMIZE TABLE loom.document_blobs FINAL")
    merged, after = [], ""
    while True:
        page = await blobs.held_in_clickhouse(org, ns, after, 2, ch=ch)
        if not page.after:
            break
        merged += [h.sha for h in page.held]
        after = page.after
    assert merged == shas[1:]
    assert await blobs.held_in_clickhouse(org, ns, shas[4], 2, ch=ch) == blobs.HeldPage([], "")
