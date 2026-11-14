"""knowledge.move: a document goes to another namespace whole (chunks, original,
metadata), is deleted where it was, and comes back there with knowledge.restore."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kmove")


def _live(ch_client: Any, ns: str, uri: str) -> list[tuple[int, str, str]]:
    """The live chunks of a source: (chunk_idx, content, user_id)."""
    rows = ch_client.query(
        "SELECT chunk_idx, content, user_id FROM loom.chunks WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String} AND "
        + LIVE_CHUNK_SQL
        + " ORDER BY chunk_idx",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _places() -> tuple[str, str, str]:
    tag = uuid.uuid4().hex[:6]
    return f"km-a-{tag}", f"km-b-{tag}", f"doc://move-{tag}.md"


async def _move(a: Any, src: str, dst: str, uris: list[str]) -> dict[str, Any]:
    return await a.transport.post(
        "/v1/knowledge.move",
        {"org": ORG, "namespace": src, "to_namespace": dst, "source_uris": uris},
    )


@pytest.mark.asyncio
async def test_a_moved_document_keeps_its_chunks_original_and_metadata(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    text = "Drain a stalled ingest lane.\n\n" + "Replay the dead-letter queue. " * 80
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.transport.post(
            "/v1/knowledge.add",
            {
                "org": ORG,
                "namespace": src,
                "source_uri": uri,
                "content": text,
                "title": "Stalled lane",
                "description": "Runbook",
                "tags": ["runbook"],
                "original_bytes_b64": base64.b64encode(b"%PDF-original").decode(),
                "original_filename": "lane.pdf",
                "blob_content_type": "application/pdf",
            },
        )
        before = _live(ch_client, src, uri)
        assert len(before) > 1

        out = await _move(a, src, dst, [uri, uri])
        assert out["found"] is True
        assert out["to_namespace"] == dst
        assert out["results"] == [{"source_uri": uri, "moved": True, "reason": ""}]

        # The same chunks, by the same author, now live in the new place only.
        assert _live(ch_client, dst, uri) == before
        assert _live(ch_client, src, uri) == []
        there = await a.transport.post(
            "/v1/knowledge.list", {"org": ORG, "namespace": dst, "limit": 10}
        )
        [row] = there["rows"]
        assert (row["source_uri"], row["title"], row["description"], row["tags"]) == (
            uri,
            "Stalled lane",
            "Runbook",
            ["runbook"],
        )
        assert (row["has_blob"], row["original_filename"]) == (True, "lane.pdf")
        # The original is stored again where it went, its bytes copied whole.
        stored = ch_client.query(
            "SELECT hex(data) FROM loom.document_blobs WHERE org = {org:String}"
            " AND namespace = {ns:String}",
            parameters={"org": ORG, "ns": dst},
        ).result_rows
        assert stored == [(b"%PDF-original".hex().upper(),)]
        listed = await a.transport.post(
            "/v1/knowledge.list", {"org": ORG, "namespace": src, "limit": 10}
        )
        assert [r["source_uri"] for r in listed["rows"]] == []

        # Searchable where it went, by its own text.
        hits = await a.transport.post(
            "/v1/knowledge.search",
            {
                "org": ORG,
                "namespace": dst,
                "query": "stalled ingest lane",
                "namespace_scope": "this",
            },
        )
        assert uri in {h["source_uri"] for h in hits["results"]}

        # Deleted where it was, so a restore there brings it back.
        restored = await a.knowledge.restore(uri)
        assert restored["restored"] is True
        assert _live(ch_client, src, uri) == before


@pytest.mark.asyncio
async def test_a_retry_finds_nothing_left_to_move(control_plane_url: str) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="once", source_uri=uri, title="t")
        assert (await _move(a, src, dst, [uri]))["results"][0]["moved"] is True
        again = await _move(a, src, dst, [uri])
    assert again["results"] == [{"source_uri": uri, "moved": False, "reason": "not_found"}]


@pytest.mark.asyncio
async def test_a_live_document_where_it_goes_is_not_overwritten(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="mine here", source_uri=uri, title="here")
    async with Agent(org=ORG, namespace=dst, agent="w", base_url=control_plane_url) as b:
        await b.knowledge.add(content="theirs there", source_uri=uri, title="there")
        out = await _move(b, src, dst, [uri])
    assert out["results"] == [{"source_uri": uri, "moved": False, "reason": "exists"}]
    assert [c[1] for c in _live(ch_client, dst, uri)] == ["theirs there"]
    assert [c[1] for c in _live(ch_client, src, uri)] == ["mine here"]


@pytest.mark.asyncio
async def test_a_move_over_a_deleted_document_is_its_next_revision(
    control_plane_url: str,
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=dst, agent="w", base_url=control_plane_url) as b:
        await b.knowledge.add(content="old text", source_uri=uri, title="old")
        await b.knowledge.delete(uri)
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="new text", source_uri=uri, title="new")
        out = await _move(a, src, dst, [uri])
        assert out["results"][0]["moved"] is True
        got = await a.transport.post(
            "/v1/knowledge.get", {"org": ORG, "namespace": dst, "source_uri": uri}
        )
    assert (got["title"], got["revision"]) == ("new", 2)


@pytest.mark.asyncio
async def test_a_move_to_the_same_namespace_is_refused(control_plane_url: str) -> None:
    from loom import Agent
    from loom.errors import LoomError

    src, _, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        with pytest.raises(LoomError) as e:
            await _move(a, src, src, [uri])
    assert "VALIDATION" in str(e.value).upper()


@pytest.mark.asyncio
async def test_a_moved_document_brings_its_tables(control_plane_url: str, ch_client: Any) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="region,total\nnorth,3\nsouth,4", source_uri=uri, title="sales"
        )
        _write_table(ch_client, src, uri)
        assert (await _move(a, src, dst, [uri]))["results"][0]["moved"] is True
        got = await a.transport.post(
            "/v1/knowledge.dataset_get",
            {"org": ORG, "namespace": dst, "source_uri": uri, "sample_rows": 5},
        )
    assert got["found"] is True
    [table] = got["datasets"]
    assert table["name"] == "sales"
    rows = ch_client.query(
        "SELECT s1, toFloat64(n1) FROM loom.dataset_rows WHERE org = {org:String}"
        " AND namespace = {ns:String} AND dataset_id = 'ds_sales' ORDER BY row_id",
        parameters={"org": ORG, "ns": dst},
    ).result_rows
    assert [tuple(r) for r in rows] == [("north", 3.0), ("south", 4.0)]


def _write_table(ch_client: Any, ns: str, uri: str) -> None:
    """A two-row table published with the source's live document version."""
    from server.knowledge.store import datasets as repo

    [[version]] = ch_client.query(
        "SELECT version FROM loom.documents FINAL WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String}",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    catalog = repo.dataset_row(
        None,
        org=ORG,
        namespace=ns,
        source_uri=uri,
        dataset_id="ds_sales",
        doc_version=int(version),
        snapshot_id=7,
        revision=1,
        table_id="t1",
        position=0,
        name="sales",
        sheet="",
        row_start=1,
        row_end=3,
        schema="[]",
        schema_state="needs_clarification",
        clarifications="[]",
        row_count=2,
        rejected_rows=0,
        rejected="[]",
        complete=True,
        artifact_sha256="",
        agent="w",
        user_id="-",
        session_id="-",
        actor="",
        source_class="asserted",
    )
    rows = [
        repo.slot_row(catalog, {"row_id": i, "s1": region, "n1": n}, catalog["created_at"])
        for i, (region, n) in enumerate([("north", 3), ("south", 4)], start=1)
    ]
    for table, written in (("loom.datasets", [catalog]), ("loom.dataset_rows", rows)):
        ch_client.insert(table, [list(r.values()) for r in written], column_names=list(written[0]))


def _live_rows(ch_client: Any, ns: str, uri: str) -> int:
    """The live document rows of a source."""
    [[n]] = ch_client.query(
        "SELECT count() FROM loom.documents FINAL WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String} AND superseded_at IS NULL",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    return int(n)


@pytest.mark.asyncio
async def test_a_document_whose_metadata_was_edited_moves_with_its_chunks(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    text = "Rotate the signing keys.\n\n" + "Revoke the old key after the grace period. " * 60
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content=text, source_uri=uri, title="Keys")
        before = _live(ch_client, src, uri)
        # A new row version; the chunks stay at theirs.
        await a.transport.post(
            "/v1/knowledge.update_metadata",
            {"org": ORG, "namespace": src, "source_uri": uri, "title": "Key rotation"},
        )
        out = await _move(a, src, dst, [uri])
        assert out["results"] == [{"source_uri": uri, "moved": True, "reason": ""}]
        assert _live(ch_client, dst, uri) == before
        got = await a.transport.post(
            "/v1/knowledge.get", {"org": ORG, "namespace": dst, "source_uri": uri}
        )
        assert got["title"] == "Key rotation"
        hits = await a.transport.post(
            "/v1/knowledge.search",
            {
                "org": ORG,
                "namespace": dst,
                "query": "rotate signing keys",
                "namespace_scope": "this",
            },
        )
        assert uri in {h["source_uri"] for h in hits["results"]}
    # Each chunk names the revision its new place's row has.
    revisions = ch_client.query(
        "SELECT DISTINCT JSONExtractUInt(chunk_metadata, 'revision') FROM loom.chunks"
        " WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String}",
        parameters={"org": ORG, "ns": dst, "uri": uri},
    ).result_rows
    assert revisions == [(got["revision"],)]


@pytest.fixture
async def in_process(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The move run in this process against the test ClickHouse, the caller allowed on
    every namespace."""
    from server.core.ch import get_client
    from server.knowledge.app import move

    async def allowed(org: str, ns: str, role: Any) -> bool:
        return True

    monkeypatch.setattr("server.auth.authz.row_in_scope", allowed)
    client = get_client()
    await client.connect()
    yield move
    await client.disconnect()


async def _move_here(move: Any, src: str, dst: str, uri: str) -> list[dict[str, Any]]:
    from server.knowledge.api.models_knowledge import KnowledgeMoveInput

    out = await move.handle_knowledge_move(
        KnowledgeMoveInput(org=ORG, namespace=src, to_namespace=dst, source_uris=[uri])
    )
    return [r.model_dump() for r in out.results]


async def _written(control_plane_url: str, src: str, uri: str) -> None:
    from loom import Agent

    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="Page the on-call. " * 80, source_uri=uri, title="t")


@pytest.mark.asyncio
async def test_chunks_that_did_not_copy_whole_leave_nothing_where_it_goes(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    real = move.chunks_repo.copy_revision

    async def short(*args: Any, **kwargs: Any) -> bool:
        await real(*args, **kwargs)
        return False

    monkeypatch.setattr(move.chunks_repo, "copy_revision", short)
    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": False, "reason": "busy"}
    ]
    assert _live(ch_client, dst, uri) == [] and _live_rows(ch_client, dst, uri) == 0
    assert _live(ch_client, src, uri) != [] and _live_rows(ch_client, src, uri) == 1


@pytest.mark.asyncio
async def test_a_row_that_did_not_land_leaves_nothing_where_it_goes(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    real = move.documents_repo.insert_one

    async def refused(row: Any, **kwargs: Any) -> None:
        if row["namespace"] == dst:
            raise RuntimeError("the insert failed")
        await real(row, **kwargs)

    monkeypatch.setattr(move.documents_repo, "insert_one", refused)
    with pytest.raises(RuntimeError):
        await _move_here(move, src, dst, uri)
    assert _live(ch_client, dst, uri) == [] and _live_rows(ch_client, dst, uri) == 0
    assert _live_rows(ch_client, src, uri) == 1


@pytest.mark.asyncio
async def test_an_erasure_registered_while_it_copied_takes_the_copy_back(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    real = move.chunks_repo.copy_revision

    async def then_erased(*args: Any, **kwargs: Any) -> bool:
        landed = await real(*args, **kwargs)
        ch_client.insert(
            "loom.erasure_requests",
            [[uuid.uuid4(), ORG, "namespace", dst, "itest", "queued", 1]],
            column_names=[
                "id",
                "org",
                "scope_type",
                "scope_value",
                "requester",
                "status",
                "version",
            ],
        )
        return bool(landed)

    monkeypatch.setattr(move.chunks_repo, "copy_revision", then_erased)
    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": False, "reason": "erased"}
    ]
    assert _live(ch_client, dst, uri) == [] and _live_rows(ch_client, dst, uri) == 0
    # Taken back before the delete: it stays where it was.
    assert _live_rows(ch_client, src, uri) == 1


def _row(ch_client: Any, ns: str, uri: str) -> dict[str, Any]:
    """The source's latest document row, every column."""
    res = ch_client.query(
        "SELECT * FROM loom.documents FINAL WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String}",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    )
    return dict(zip(res.column_names, res.result_rows[0], strict=True))


def _vectors(ch_client: Any, ns: str, uri: str, version: int) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in ch_client.query(
            "SELECT chunk_idx, embedding_model, embed_input_sha256, embedding FROM loom.chunks"
            " WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String}"
            " AND doc_version = {v:UInt64} ORDER BY chunk_idx, embedding_model",
            parameters={"org": ORG, "ns": ns, "uri": uri, "v": version},
        ).result_rows
    ]


@pytest.mark.asyncio
async def test_a_moved_row_is_the_documents_but_for_its_place_and_its_mover(
    control_plane_url: str, ch_client: Any, in_process: Any
) -> None:
    from server.memory.domain.provenance import pipeline_actor_var

    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    before = _row(ch_client, src, uri)
    vectors = _vectors(ch_client, src, uri, int(before["version"]))
    await asyncio.sleep(1.1)  # the copy's chunks are stamped a second after the source's
    mover = pipeline_actor_var.set("service:mover")
    try:
        assert await _move_here(move, src, dst, uri) == [
            {"source_uri": uri, "moved": True, "reason": ""}
        ]
    finally:
        pipeline_actor_var.reset(mover)
    after = _row(ch_client, dst, uri)
    changed = {"namespace", "doc_id", "version", "revision", "actor"}
    assert {c: after[c] for c in after if c not in changed} == {
        c: before[c] for c in before if c not in changed
    }
    assert (after["namespace"], after["actor"], after["revision"]) == (dst, "service:mover", 1)
    assert after["doc_id"] != before["doc_id"] and after["version"] > before["version"]
    # The vectors are the ones stored: nothing is embedded again.
    assert _vectors(ch_client, dst, uri, int(after["version"])) == vectors
    # Each chunk is stamped when it was written there, as a later move reads it.
    [[copied_at]] = ch_client.query(
        "SELECT min(created_at) FROM loom.chunks WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String}",
        parameters={"org": ORG, "ns": dst, "uri": uri},
    ).result_rows
    assert copied_at > before["ingested_at"]


@pytest.mark.asyncio
async def test_a_copy_that_lands_after_its_take_back_does_not_serve(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    real_take_back = move._take_back
    late: list[Any] = []

    async def take_back_then_land(copy: Any) -> None:
        await real_take_back(copy)
        # A cancelled INSERT … SELECT that ClickHouse finishes after the take-back.
        doc = copy.doc
        late.append(
            await move.chunks_repo.copy_revision(
                doc.org,
                doc.namespace,
                doc.source_uri,
                revision=doc.revision,
                upto_version=doc.version,
                to_namespace=copy.dst,
                to_version=copy.version,
                to_revision=1,
            )
        )

    async def refused(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(move, "_take_back", take_back_then_land)
    monkeypatch.setattr(move, "_head_is", refused)  # a write of the source meanwhile
    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": False, "reason": "busy"}
    ]
    assert late == [True]  # the late rows are there, retired
    assert _live(ch_client, dst, uri) == [] and _live_rows(ch_client, dst, uri) == 0
    assert _live_rows(ch_client, src, uri) == 1


@pytest.mark.asyncio
async def test_a_table_that_did_not_copy_whole_leaves_nothing_where_it_goes(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    from loom import Agent

    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="region,total\nnorth,3", source_uri=uri, title="sales")
    _write_table(ch_client, src, uri)
    real = move.datasets_repo.copy_snapshot

    async def short(*args: Any, **kwargs: Any) -> bool:
        await real(*args, **kwargs)
        return False

    monkeypatch.setattr(move.datasets_repo, "copy_snapshot", short)
    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": False, "reason": "busy"}
    ]
    assert _live(ch_client, dst, uri) == [] and _live_rows(ch_client, dst, uri) == 0
    for table in ("loom.datasets", "loom.dataset_rows"):
        [[n]] = ch_client.query(
            f"SELECT count() FROM {table} WHERE org = {{org:String}} AND namespace = {{ns:String}}",
            parameters={"org": ORG, "ns": dst},
        ).result_rows
        assert n == 0, table
    assert _live_rows(ch_client, src, uri) == 1


@pytest.mark.asyncio
async def test_a_delete_whose_table_cleanup_fails_after_its_tombstone_is_a_move(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    before = _live(ch_client, src, uri)

    async def failed(org: str, ns: str, through: Any) -> None:
        raise RuntimeError("the datasets delete failed")

    monkeypatch.setattr(move.datasets_repo, "forget_published", failed)
    # Deleted where it was, so the copy is the document now: moved, and it stays.
    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": True, "reason": ""}
    ]
    assert _live_rows(ch_client, src, uri) == 0
    assert _live_rows(ch_client, dst, uri) == 1 and _live(ch_client, dst, uri) == before


@pytest.mark.asyncio
async def test_a_retry_onto_a_copy_with_its_chunks_gone_copies_it_again(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    move = in_process
    src, dst, uri = _places()
    await _written(control_plane_url, src, uri)
    before = _live(ch_client, src, uri)

    async def cut_off(doc: Any) -> None:
        raise RuntimeError("cut off before the delete")

    with monkeypatch.context() as m:
        m.setattr(move, "_delete_source", cut_off)
        with pytest.raises(RuntimeError):
            await _move_here(move, src, dst, uri)
    # What a row insert that landed after its take-back leaves: the row, no chunks.
    ch_client.command(
        "DELETE FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String}"
        " SETTINGS lightweight_deletes_sync = 2",
        parameters={"org": ORG, "ns": dst},
    )
    assert _live_rows(ch_client, dst, uri) == 1 and _live(ch_client, dst, uri) == []

    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": True, "reason": ""}
    ]
    assert _live(ch_client, dst, uri) == before and _live_rows(ch_client, src, uri) == 0


@pytest.mark.asyncio
async def test_a_retry_onto_a_copy_with_its_chunks_and_tables_gone_copies_it_again(
    control_plane_url: str, ch_client: Any, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loom import Agent

    move = in_process
    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="region,total\nnorth,3", source_uri=uri, title="sales")
    _write_table(ch_client, src, uri)
    before = _live(ch_client, src, uri)

    async def cut_off(doc: Any) -> None:
        raise RuntimeError("cut off before the delete")

    with monkeypatch.context() as m:
        m.setattr(move, "_delete_source", cut_off)
        with pytest.raises(RuntimeError):
            await _move_here(move, src, dst, uri)
    # What a row insert that landed after its take-back leaves: the row, no chunks, no
    # catalog, no table rows.
    for table in ("loom.chunks", "loom.datasets", "loom.dataset_rows"):
        ch_client.command(
            f"DELETE FROM {table} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
            " SETTINGS lightweight_deletes_sync = 2",
            parameters={"org": ORG, "ns": dst},
        )
    assert _live_rows(ch_client, dst, uri) == 1

    assert await _move_here(move, src, dst, uri) == [
        {"source_uri": uri, "moved": True, "reason": ""}
    ]
    assert _live(ch_client, dst, uri) == before and _live_rows(ch_client, src, uri) == 0
    rows = ch_client.query(
        "SELECT s1 FROM loom.dataset_rows WHERE org = {org:String} AND namespace = {ns:String}"
        " ORDER BY row_id",
        parameters={"org": ORG, "ns": dst},
    ).result_rows
    assert [r[0] for r in rows] == ["north", "south"]


def test_a_place_the_token_cannot_write_answers_found_false_and_moves_nothing(
    control_plane_url: str, ch_client: Any, mint_test_token: Any
) -> None:
    import httpx

    src, dst, uri = _places()
    writer = mint_test_token(org=ORG, namespaces=[src, dst], roles=["writer"], subject="w")
    # Writer where the documents are, and no grant on the place they would go.
    src_only = mint_test_token(org=ORG, namespaces=[src], roles=["writer"], subject="mover")
    headers = {"Authorization": f"Bearer {writer}"}
    with httpx.Client(base_url=control_plane_url, timeout=30.0) as c:
        r = c.post(
            "/v1/knowledge.add",
            json={"org": ORG, "namespace": src, "source_uri": uri, "content": "stays here"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        before = _live(ch_client, src, uri)
        r = c.post(
            "/v1/knowledge.move",
            json={"org": ORG, "namespace": src, "to_namespace": dst, "source_uris": [uri]},
            headers={"Authorization": f"Bearer {src_only}"},
        )
        # Not a 403: the answer does not say whether the place exists.
        assert r.status_code == 200, r.text
        assert r.json() == {"found": False, "to_namespace": "", "results": []}
        assert _live(ch_client, src, uri) == before and _live_rows(ch_client, src, uri) == 1
        assert _live(ch_client, dst, uri) == [] and _live_rows(ch_client, dst, uri) == 0
        # The same call by a token that writes both moves it: the refusal was the grant's.
        allowed = c.post(
            "/v1/knowledge.move",
            json={"org": ORG, "namespace": src, "to_namespace": dst, "source_uris": [uri]},
            headers=headers,
        )
    assert allowed.json()["results"] == [{"source_uri": uri, "moved": True, "reason": ""}]


async def _moves_audited(a: Any, ns: str) -> list[dict[str, Any]]:
    out = await a.transport.post(
        "/v1/audit.list", {"org": ORG, "namespace": ns, "event_type": "knowledge.move"}
    )
    return list(out["rows"])


@pytest.mark.asyncio
async def test_a_move_is_audited_where_it_was_and_where_it_went(control_plane_url: str) -> None:
    from loom import Agent

    from tests.integration._eventually import eventually

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="audited", source_uri=uri, title="t")
        assert (await _move(a, src, dst, [uri]))["results"][0]["moved"] is True
        # The transport's row under the source, the handler's under the target.
        assert await eventually(lambda: _moves_audited(a, src), description="source row")
        [there] = await eventually(lambda: _moves_audited(a, dst), description="target row")
    assert json.loads(there["subject"]) == {"from_namespace": src, "source_uris": [uri]}


@pytest.mark.asyncio
async def test_a_failed_call_audits_its_finished_moves_where_they_went(
    control_plane_url: str, in_process: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loom import Agent

    from server.knowledge.api.models_knowledge import KnowledgeMoveInput
    from tests.integration._eventually import eventually

    move = in_process
    src, dst, uri = _places()
    failing = uri.replace(".md", "-2.md")
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        for u in (uri, failing):
            await a.knowledge.add(content=f"text of {u}", source_uri=u, title="t")
        real = move._move

        async def one_fails(doc: Any, *args: Any) -> Any:
            if doc is not None and doc.source_uri == failing:
                raise RuntimeError("the store failed")
            return await real(doc, *args)

        monkeypatch.setattr(move, "_move", one_fails)
        with pytest.raises(RuntimeError):
            await move.handle_knowledge_move(
                KnowledgeMoveInput(
                    org=ORG, namespace=src, to_namespace=dst, source_uris=[uri, failing]
                )
            )
        [there] = await eventually(lambda: _moves_audited(a, dst), description="target row")
    assert json.loads(there["subject"]) == {"from_namespace": src, "source_uris": [uri]}
