"""knowledge.restore — a delete undone: the document row and its chunks are live
again, searches find it, and what an erasure reached stays retired."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from server.core.ch import get_client
from server.erasure.app import ledger_reads
from server.erasure.store import ledger
from server.knowledge.api.models_knowledge import KnowledgeRestoreInput
from server.knowledge.app.restore import handle_knowledge_restore
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
from server.knowledge.store.documents import DOCUMENT_COLUMNS

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("krestore")


# Every column of the document row but the two a delete and a restore change.
_DOC_FIELDS = tuple(c for c in DOCUMENT_COLUMNS if c not in ("version", "superseded_at"))


def _live_chunks(ch_client, ns: str, uri: str) -> int:
    rows = ch_client.query(
        "SELECT count() FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String}"
        " AND source_uri = {uri:String} AND " + LIVE_CHUNK_SQL,
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    return int(rows[0][0])


def _version(ch_client, ns: str, uri: str) -> int:
    """The version of the row `FINAL` keeps."""
    rows = ch_client.query(
        "SELECT version FROM loom.documents FINAL WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String}",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    return int(rows[0][0])


def _doc_row(ch_client, ns: str, uri: str) -> dict:
    cols = ", ".join((*_DOC_FIELDS, "superseded_at"))
    res = ch_client.query(
        f"SELECT {cols} FROM loom.documents FINAL WHERE org = {{org:String}}"
        " AND namespace = {ns:String} AND source_uri = {uri:String}",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    )
    (row,) = res.result_rows
    return dict(zip(res.column_names, row, strict=True))


@pytest.mark.asyncio
async def test_a_deleted_document_is_restored_with_its_chunks(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="Teal giraffes graze on the north ridge.",
            source_uri=uri,
            title="giraffes",
            tags=["fauna"],
        )
        before = _doc_row(ch_client, ns, uri)
        live = _live_chunks(ch_client, ns, uri)
        assert live >= 1
        await a.knowledge.delete(uri)
        assert _live_chunks(ch_client, ns, uri) == 0

        out = await a.knowledge.restore(uri)
        assert out["restored"] is True
        assert (out["chunks_restored"], out["original"]) == (live, "none")
        after = _doc_row(ch_client, ns, uri)
        assert after["superseded_at"] is None
        assert {f: after[f] for f in _DOC_FIELDS} == {f: before[f] for f in _DOC_FIELDS}
        assert _live_chunks(ch_client, ns, uri) == live
        hits = await a.knowledge.search("teal giraffes north ridge")
        assert any(h.source_uri == uri for h in hits)

        again = await a.knowledge.restore(uri)
        assert (again["restored"], again["reason"], again["doc_id"]) == (
            False,
            "live",
            str(before["doc_id"]),
        )


@pytest.mark.asyncio
async def test_restore_names_the_later_write_and_honours_the_revision(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.upsert(content="first version", source_uri=uri, title="v")
        rev = _doc_row(ch_client, ns, uri)["revision"]
        await a.knowledge.delete(uri)
        refused = await a.knowledge.restore(uri, if_revision=rev + 1)
        assert (refused["reason"], refused["revision"]) == ("revision_mismatch", rev)
        assert _live_chunks(ch_client, ns, uri) == 0

        await a.knowledge.upsert(content="second version", source_uri=uri, title="v")
        later = await a.knowledge.restore(uri, if_revision=rev)
        assert later["reason"] == "live" and later["revision"] > rev
        assert "second version" in _doc_row(ch_client, ns, uri)["content"]


@pytest.mark.asyncio
async def test_a_missing_document_is_not_found(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        out = await a.knowledge.restore("doc://never-written")
    assert (out["restored"], out["reason"]) == (False, "not_found")


def _erase(control_plane_url: str, scope_type: str, value: str) -> None:
    r = httpx.post(
        f"{control_plane_url}/v1/erasure.request",
        json={"org": ORG, "scope_type": scope_type, "scope_value": value, "requester": "t@x.io"},
        timeout=180,
    )
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text


def _forget_erasure(ch_client, value: str) -> None:
    """Remove the erasure's ledger rows, as their TTL would."""
    ch_client.command(
        "DELETE FROM loom.erasure_requests WHERE org = {org:String} AND scope_value = {v:String}",
        parameters={"org": ORG, "v": value},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_first", [True, False])
async def test_an_erased_document_is_not_restorable(
    control_plane_url: str, ch_client, deleted_first: bool
) -> None:
    """A namespace erasure that keeps rows: one document deleted before it ran (its
    retired chunks stamped, so it holds without the ledger row), and one live when it ran
    (its row superseded in place, its chunks stamped, no supersession rows)."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Ochre herons nest by the weir.", source_uri=uri, title="h")
        if deleted_first:
            await a.knowledge.delete(uri)
        _erase(control_plane_url, "namespace", ns)
        if deleted_first:
            _forget_erasure(ch_client, ns)
        out = await a.knowledge.restore(uri)
    assert (out["restored"], out["reason"]) == (False, "not_restorable")
    assert _live_chunks(ch_client, ns, uri) == 0
    assert _doc_row(ch_client, ns, uri)["superseded_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_erased", [True, False])
async def test_a_user_erasure_holds_only_that_user_s_deleted_document(
    control_plane_url: str, ch_client, mint_test_token, writer_erased: bool
) -> None:
    """The writer's erasure stamps the retired chunks, so it holds without its ledger row;
    another user's erasure leaves the document restorable."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    writer, other = f"u-{uuid.uuid4().hex[:6]}", f"u-{uuid.uuid4().hex[:6]}"
    erased = writer if writer_erased else other
    token = mint_test_token(org=ORG, namespaces=[ns], read_user_id=writer)
    agent = Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=token)
    async with agent as a:
        await a.knowledge.add(content="Slate owls roost.", source_uri=uri, title="o")
        await a.knowledge.delete(uri)
        _erase(control_plane_url, "user_id", erased)
        try:
            if writer_erased:
                _forget_erasure(ch_client, erased)
            out = await a.knowledge.restore(uri)
        finally:
            # A user scope spans the org: the other tests' restores must not meet it.
            _forget_erasure(ch_client, erased)
    if writer_erased:
        assert (out["restored"], out["reason"]) == (False, "not_restorable")
        assert _live_chunks(ch_client, ns, uri) == 0
    else:
        assert out["restored"] is True
        assert _live_chunks(ch_client, ns, uri) >= 1


def _queue_erasure(
    ch_client, scope_type: str, value: str, *, purge: bool = False, completed_at=None
) -> None:
    """A ledger row: queued (the saga has not run, so only the ledger says it covers
    anything), or completed at `completed_at`."""
    status = "queued" if completed_at is None else "completed"
    ch_client.insert(
        "loom.erasure_requests",
        [[uuid.uuid4(), ORG, scope_type, value, "t@x.io", status, 1, purge, completed_at]],
        column_names=[
            "id",
            "org",
            "scope_type",
            "scope_value",
            "requester",
            "status",
            "version",
            "purge",
            "completed_at",
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["namespace", "user_id"])
async def test_a_pending_erasure_of_the_namespace_or_the_writer_keeps_the_document(
    control_plane_url: str, ch_client, mint_test_token, scope_type: str
) -> None:
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    writer = f"u-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], read_user_id=writer)
    agent = Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=token)
    value = ns if scope_type == "namespace" else writer
    async with agent as a:
        await a.knowledge.add(content="Umber newts hide.", source_uri=uri, title="n")
        await a.knowledge.delete(uri)
        _queue_erasure(ch_client, scope_type, value)
        try:
            out = await a.knowledge.restore(uri)
        finally:
            _forget_erasure(ch_client, value)
    assert (out["restored"], out["reason"]) == (False, "not_restorable")
    assert _live_chunks(ch_client, ns, uri) == 0


@pytest.mark.asyncio
async def test_the_ledger_read_returns_the_rows_that_may_reach_the_namespace(ch_client) -> None:
    ns, writer, other = (f"kr-{uuid.uuid4().hex[:6]}" for _ in range(3))
    listed = json.dumps({"namespaces": [ns], "purge": False})
    since = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    queued = [
        ("namespace", ns),
        ("user_id", writer),
        ("user_id", other),
        ("namespace_list", listed),
    ]
    for scope_type, value in queued:
        _queue_erasure(ch_client, scope_type, value, purge=value == writer)
    _queue_erasure(ch_client, "namespace", f"{ns}-other")
    # The time branch: one finished long before `since`, one after it.
    _queue_erasure(ch_client, "namespace", f"{ns}-old", completed_at=since - timedelta(days=1))
    _queue_erasure(ch_client, "namespace", ns, completed_at=since + timedelta(seconds=5))
    names = [value for _, value in queued] + [f"{ns}-other", f"{ns}-old"]
    client = get_client()
    await client.connect()
    try:
        epoch = int(since.replace(tzinfo=UTC).timestamp())
        rows = await ledger.unfinished_reaching(ORG, ns, writer, epoch, 60, ch=client)
        everyone = await ledger.unfinished_reaching(ORG, ns, None, epoch, 60, ch=client)
    finally:
        await client.disconnect()
        for value in names:
            _forget_erasure(ch_client, value)
    mine = {ns, writer, other, listed}
    got = sorted(
        (r["scope_type"], r["scope_value"], bool(r["purge"]))
        for r in rows
        if r["scope_value"] in mine
    )
    want = [
        ("namespace", ns, False),
        ("namespace", ns, False),
        ("namespace_list", listed, False),
        ("user_id", writer, True),
    ]
    assert got == sorted(want)
    assert ("user_id", other) in {(r["scope_type"], r["scope_value"]) for r in everyone}


@pytest.mark.asyncio
async def test_a_pending_erasure_listing_the_namespace_keeps_the_document(
    control_plane_url: str, ch_client
) -> None:
    """A `namespace_list` request in its object form, not yet run."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Russet voles burrow early.", source_uri=uri, title="v")
        await a.knowledge.delete(uri)
        scope = json.dumps({"namespaces": [f" {ns} "], "purge": False})
        _queue_erasure(ch_client, "namespace_list", scope)
        try:
            out = await a.knowledge.restore(uri)
        finally:
            _forget_erasure(ch_client, scope)
    assert (out["restored"], out["reason"]) == (False, "not_restorable")
    assert _live_chunks(ch_client, ns, uri) == 0


@pytest.mark.asyncio
async def test_a_bulk_deleted_document_is_restored(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uris = [f"doc://r-{uuid.uuid4().hex[:6]}" for _ in range(2)]
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i, uri in enumerate(uris):
            await a.knowledge.add(content=f"Bulk document {i} about slate kites.", source_uri=uri)
        await a.call("knowledge.bulk_delete", {"source_uris": uris})
        out = await a.knowledge.restore(uris[0])
    assert out["restored"] is True and out["chunks_restored"] >= 1
    assert _live_chunks(ch_client, ns, uris[0]) == out["chunks_restored"]
    assert _live_chunks(ch_client, ns, uris[1]) == 0


@pytest.mark.asyncio
async def test_a_restore_that_failed_after_its_chunks_is_finished(
    control_plane_url: str, ch_client
) -> None:
    """The supersession rows gone and the row still retired, as a restore that failed
    between its two writes leaves it: the next call writes the row."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Amber moths gather at dusk.", source_uri=uri, title="m")
        live = _live_chunks(ch_client, ns, uri)
        await a.knowledge.delete(uri)
        ch_client.command(
            "DELETE FROM loom.chunk_supersessions WHERE org = {org:String}"
            " AND namespace = {ns:String} AND source_uri = {uri:String}",
            parameters={"org": ORG, "ns": ns, "uri": uri},
        )
        assert _live_chunks(ch_client, ns, uri) == live
        out = await a.knowledge.restore(uri)
    assert (out["restored"], out["chunks_restored"]) == (True, live)
    assert _doc_row(ch_client, ns, uri)["superseded_at"] is None


@pytest.mark.asyncio
async def test_a_document_added_twice_comes_back_at_its_last_revision(
    control_plane_url: str, ch_client
) -> None:
    """`knowledge.add` keeps an earlier version live; a delete retires both, and restore
    brings back the revision the row names."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="First text about grey wrens.", source_uri=uri, title="w")
        await a.knowledge.add(content="Second text about grey wrens nesting.", source_uri=uri)
        rev = _doc_row(ch_client, ns, uri)["revision"]
        await a.knowledge.delete(uri)
        out = await a.knowledge.restore(uri)
    assert (out["restored"], out["revision"]) == (True, rev)
    rows = ch_client.query(
        "SELECT DISTINCT JSONExtractUInt(chunk_metadata, 'revision') FROM loom.chunks"
        " WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String}"
        " AND " + LIVE_CHUNK_SQL,
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    assert [r[0] for r in rows] == [rev]


@pytest.mark.asyncio
async def test_chunks_written_before_they_recorded_a_revision_come_back(
    control_plane_url: str, ch_client
) -> None:
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Slate herons fish the shallows.", source_uri=uri, title="s")
        live = _live_chunks(ch_client, ns, uri)
        # As a chunk row from before chunks recorded their revision.
        ch_client.command(
            "ALTER TABLE loom.chunks UPDATE chunk_metadata = '{}' WHERE org = {org:String}"
            " AND namespace = {ns:String} AND source_uri = {uri:String}"
            " SETTINGS mutations_sync = 2",
            parameters={"org": ORG, "ns": ns, "uri": uri},
        )
        await a.knowledge.delete(uri)
        out = await a.knowledge.restore(uri)
    assert (out["restored"], out["chunks_restored"]) == (True, live)


@pytest.mark.asyncio
async def test_a_delete_right_after_a_restore_retires_the_chunks_again(
    control_plane_url: str, ch_client
) -> None:
    """Delete, restore and delete in the same second: the second delete's supersession
    rows equal the first's, and land (each insert carries its own deduplication token)."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Jade finches sing at noon.", source_uri=uri, title="f")
        await a.knowledge.delete(uri)
        assert (await a.knowledge.restore(uri))["restored"] is True
        await a.knowledge.delete(uri)
    assert _live_chunks(ch_client, ns, uri) == 0


def _partition(ch_client, ns: str, uri: str) -> str:
    """The document's partition, to merge it alone: the stack is shared with other tests."""
    rows = ch_client.query(
        "SELECT _partition_id FROM loom.documents WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String} LIMIT 1",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    return str(rows[0][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [False, True])
async def test_a_purge_landing_between_the_ledger_reads_leaves_no_copy(
    control_plane_url: str, ch_client, monkeypatch: pytest.MonkeyPatch, merged: bool
) -> None:
    """The undo's own statements on a real ClickHouse: the handler runs in this process,
    and the second ledger read finds a purge that registered meanwhile. The undo deletes
    its own row; merged first (the delete's row folded into it), that leaves none, as the
    purge would."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Coral wasps build in clay.", source_uri=uri, title="w")
        await a.knowledge.delete(uri)
    deleted_version = _version(ch_client, ns, uri)
    reads: list[int] = []

    async def purge_on_second_read(*_a: object) -> list[dict[str, object]]:
        reads.append(1)
        if len(reads) < 2:
            return []
        if merged:
            partition = _partition(ch_client, ns, uri)
            ch_client.command(f"OPTIMIZE TABLE loom.documents PARTITION ID '{partition}' FINAL")
        listed = json.dumps({"namespaces": [ns], "purge": True})
        return [{"scope_type": "namespace_list", "scope_value": listed, "purge": True}]

    monkeypatch.setattr(ledger_reads, "unfinished_reaching", purge_on_second_read)
    client = get_client()
    await client.connect()
    try:
        out = await handle_knowledge_restore(
            KnowledgeRestoreInput(org=ORG, namespace=ns, source_uri=uri)
        )
    finally:
        await client.disconnect()
    assert (out.restored, out.reason) == (False, "not_restorable")
    assert _live_chunks(ch_client, ns, uri) == 0
    rows = ch_client.query(
        "SELECT version, superseded_at FROM loom.documents WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String} ORDER BY version DESC",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    if merged:
        assert rows == []
    else:
        # Unless a background merge folded them in first, the delete's row stays as the
        # newest, retired (the version filter itself: test_knowledge_restore_local_ch).
        assert rows == [] or (rows[0][0] == deleted_version and rows[0][1] is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [False, True])
async def test_an_unreadable_ledger_in_the_undo_leaves_the_delete_standing(
    control_plane_url: str, ch_client, monkeypatch: pytest.MonkeyPatch, merged: bool
) -> None:
    """The second ledger read and the undo's both fail: the row is written back retired,
    never deleted, so the document reads deleted whether or not a merge folded the delete's
    row (and the add's) into the restored one first."""
    from loom import Agent

    ns = f"kr-{uuid.uuid4().hex[:6]}"
    uri = f"doc://r-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Coral wasps build in clay.", source_uri=uri, title="w")
        await a.knowledge.delete(uri)
    reads: list[int] = []

    async def failing_after_the_first(*_a: object) -> list[dict[str, object]]:
        reads.append(1)
        if len(reads) == 1:
            return []
        if merged and len(reads) == 2:
            partition = _partition(ch_client, ns, uri)
            ch_client.command(f"OPTIMIZE TABLE loom.documents PARTITION ID '{partition}' FINAL")
        raise RuntimeError("ledger down")

    monkeypatch.setattr(ledger_reads, "unfinished_reaching", failing_after_the_first)
    client = get_client()
    await client.connect()
    try:
        with pytest.raises(RuntimeError):
            await handle_knowledge_restore(
                KnowledgeRestoreInput(org=ORG, namespace=ns, source_uri=uri)
            )
    finally:
        await client.disconnect()
    assert _live_chunks(ch_client, ns, uri) == 0
    assert _doc_row(ch_client, ns, uri)["superseded_at"] is not None
