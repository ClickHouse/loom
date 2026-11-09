"""Unit tests for server/knowledge/store/documents.py — mocked CH client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.knowledge.store.documents import (
    DocumentRow,
    get_many_by_uris,
    get_one,
    insert_many,
    insert_one,
)


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


def _doc_row() -> dict[str, Any]:
    return {
        "org": "clickhouse",
        "namespace": "ns",
        "source_uri": "kb://policy.md",
        "doc_id": "11111111-2222-3333-4444-555555555555",
        "title": "Policy",
        "content": "...",
        "tags": ["policy"],
        "metadata": '{"k":"v"}',
        "version": 1,
        "superseded_at": None,
        "ingested_at": datetime(2026, 5, 1, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_get_one_filters_superseded_by_default() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_doc_row()])
    out = await get_one("clickhouse", "ns", "kb://policy.md", ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "superseded_at IS NULL" in sql
    assert "FROM loom.documents FINAL" in sql
    assert params["uris"] == ["kb://policy.md"]
    assert isinstance(out, DocumentRow)
    assert out.source_uri == "kb://policy.md"


@pytest.mark.asyncio
async def test_get_one_includes_superseded_when_requested() -> None:
    """audit/replay paths fetch the latest row regardless of supersede state."""
    ch = StubCH()
    await get_one(
        "clickhouse",
        "ns",
        "kb://policy.md",
        include_superseded=True,
        ch=ch,
    )
    sql, _ = ch.query_all_calls[0]
    assert "superseded_at IS NULL" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head_version", "ch_version", "served"),
    # A tie: ClickHouse's copy, which only a stamp made in place (an older build's
    # erasure tombstone) sets apart.
    [
        (5, 3, "head"),
        (3, 5, "clickhouse"),
        (5, 5, "clickhouse"),
        (None, 4, "clickhouse"),
        (4, None, "head"),
    ],
)
async def test_under_postgres_the_newer_of_the_head_and_clickhouse_answers(
    monkeypatch: pytest.MonkeyPatch,
    head_version: int | None,
    ch_version: int | None,
    served: str,
) -> None:
    """The head serves a commit ClickHouse does not have yet; ClickHouse a row a build
    that commits no heads wrote after the head."""
    from server.core import pg
    from server.knowledge.store import pg_documents

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, Any]:
        if head_version is None:
            return {}
        row = {**_doc_row(), "version": head_version, "title": "head"}
        # The head serves above ClickHouse only while it is owed (not yet published).
        return {"kb://policy.md": pg_documents.Head(row, owed=served == "head")}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "heads", _heads)
    ch = StubCH()
    if ch_version is not None:
        ch.query_all_returns.append([{**_doc_row(), "version": ch_version, "title": "clickhouse"}])
    out = await get_one("clickhouse", "ns", "kb://policy.md", ch=ch)
    assert out is not None and out.title == served
    sql, _ = ch.query_all_calls[0]
    assert "superseded_at IS NULL" not in sql  # a newer deleted row outranks a live one


@pytest.mark.asyncio
async def test_under_postgres_a_newer_deleted_row_hides_the_live_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg
    from server.knowledge.store import pg_documents

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, Any]:
        return {"kb://policy.md": pg_documents.Head({**_doc_row(), "version": 3}, owed=False)}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "heads", _heads)
    ch = StubCH()
    gone = {**_doc_row(), "version": 4, "superseded_at": datetime(2026, 5, 2, tzinfo=UTC)}
    ch.query_all_returns.append([gone])
    assert await get_one("clickhouse", "ns", "kb://policy.md", ch=ch) is None
    ch.query_all_returns.append([gone])
    out = await get_one("clickhouse", "ns", "kb://policy.md", include_superseded=True, ch=ch)
    assert out is not None and out.version == 4


@pytest.mark.asyncio
async def test_get_many_by_uris_emits_array_filter() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_doc_row(), _doc_row()])
    rows = await get_many_by_uris(
        "clickhouse",
        "ns",
        ["kb://a.md", "kb://b.md"],
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "source_uri IN {uris:Array(String)}" in sql
    assert params["uris"] == ["kb://a.md", "kb://b.md"]
    assert len(rows) == 2 and all(isinstance(r, DocumentRow) for r in rows)


@pytest.mark.asyncio
async def test_get_many_by_uris_empty_input_skips_ch() -> None:
    ch = StubCH()
    out = await get_many_by_uris("clickhouse", "ns", [], ch=ch)
    assert out == []
    assert ch.query_all_calls == []


@pytest.mark.asyncio
async def test_insert_one_forces_synchronous_visibility() -> None:
    ch = StubCH()
    await insert_one(_doc_row(), over=0, ch=ch)
    table, rows, settings = ch.insert_calls[0]
    assert table == "loom.documents"
    assert len(rows) == 1
    # search-visibility contract: next FINAL read sees this row
    assert settings.get("async_insert") == 0


@pytest.mark.asyncio
async def test_insert_many_returns_count_and_skips_on_empty() -> None:
    ch = StubCH()
    n = await insert_many([_doc_row(), _doc_row(), _doc_row()], over={}, ch=ch)
    assert n == 3
    n2 = await insert_many([], over={}, ch=ch)
    assert n2 == 0
    # Only one CH insert call (the empty one short-circuited)
    assert len(ch.insert_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owed", [True, False])
async def test_under_postgres_a_head_clickhouse_lost_serves_only_while_owed(
    monkeypatch: pytest.MonkeyPatch, owed: bool
) -> None:
    """ClickHouse holds no row of the source: a head still owed to it is a commit not yet
    published and serves; one owing nothing was erased by a build that keeps no heads,
    and is dropped."""
    from server.core import pg
    from server.knowledge.store import pg_documents

    dropped: list[tuple[str, int]] = []

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, Any]:
        return {"kb://policy.md": pg_documents.Head({**_doc_row(), "version": 3}, owed=owed)}

    async def _drop(org: str, namespace: str, uri: str, version: int) -> None:
        dropped.append((uri, version))

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "heads", _heads)
    monkeypatch.setattr(pg_documents, "drop_unbacked", _drop)
    out = await get_one("clickhouse", "ns", "kb://policy.md", ch=StubCH())
    assert (out is not None) is owed
    assert dropped == ([] if owed else [("kb://policy.md", 3)])


@pytest.mark.asyncio
async def test_under_postgres_a_replica_behind_the_head_never_drops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replica read misses a row just published: the sequentially consistent read
    finds it, so the head serves and stays."""
    from server.core import ch as ch_module
    from server.core import pg
    from server.knowledge.store import pg_documents

    dropped: list[str] = []
    sequential: list[bool] = []

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, Any]:
        return {"kb://policy.md": pg_documents.Head({**_doc_row(), "version": 3}, owed=False)}

    async def _drop(org: str, namespace: str, uri: str, version: int) -> None:
        dropped.append(uri)

    class _LaggingCH(StubCH):
        async def query_all(self, sql, params=None):
            sequential.append(ch_module.sequential_reads_active())
            return [{**_doc_row(), "version": 3}] if sequential[-1] else []

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "heads", _heads)
    monkeypatch.setattr(pg_documents, "drop_unbacked", _drop)
    out = await get_one("clickhouse", "ns", "kb://policy.md", ch=_LaggingCH())
    assert out is not None and out.version == 3
    assert sequential == [False, True] and dropped == []


@pytest.mark.asyncio
async def test_under_postgres_a_jobs_commit_not_yet_published_counts_as_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ingest run that stopped after its commit: the head (or the delivery still owed)
    names the job, so the next run finishes it instead of retiring its chunks."""
    from server.core import pg
    from server.knowledge.store import documents, pg_documents

    async def _committed(org: str, namespace: str, uri: str, ingestion_id: str) -> int | None:
        return 4 if ingestion_id == "job-1" else None

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "committed_revision", _committed)
    ch = StubCH()
    assert await documents.revision_by_ingestion("o", "n", "u", "job-1", ch=ch) == 4
    assert ch.query_one_calls == []
    assert await documents.revision_by_ingestion("o", "n", "u", "job-2", ch=ch) is None
    assert len(ch.query_one_calls) == 1  # ClickHouse, for a row published before heads


@pytest.mark.asyncio
async def test_under_postgres_a_published_head_an_older_build_purged_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older build's user purge deleted the head's version and left another writer's
    older row, stamped deleted: confirmed where every write is, the head is dropped and
    the document reads as deleted."""
    from server.core import pg
    from server.knowledge.store import pg_documents

    dropped: list[int] = []
    older = {**_doc_row(), "version": 1, "superseded_at": datetime(2026, 5, 2, tzinfo=UTC)}

    async def _heads(org: str, namespace: str, uris: list[str]) -> dict[str, Any]:
        return {"kb://policy.md": pg_documents.Head({**_doc_row(), "version": 3}, owed=False)}

    async def _drop(org: str, namespace: str, uri: str, version: int) -> None:
        dropped.append(version)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "heads", _heads)
    monkeypatch.setattr(pg_documents, "drop_unbacked", _drop)
    ch = StubCH()
    ch.query_all_returns += [[older], [older]]
    assert await get_one("clickhouse", "ns", "kb://policy.md", ch=ch) is None
    assert dropped == [3]
    ch.query_all_returns += [[older], [older]]
    out = await get_one("clickhouse", "ns", "kb://policy.md", include_superseded=True, ch=ch)
    assert out is not None and out.version == 1


@pytest.mark.asyncio
async def test_under_postgres_a_publish_whose_delivery_could_not_be_cleared_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is in and Postgres fails clearing the delivery: the write is committed and
    published, so it answers PUBLICATION_PENDING (the worker clears the delivery), never
    an error after which its writer would retire its chunks."""
    from server.core import pg
    from server.core.pg import PostgresUnavailableError
    from server.core.pg_lease import PublicationPending
    from server.knowledge.store import pg_documents

    async def _commit(rows: Any, **_: Any) -> float:
        return float("inf")

    async def _clear(rows: Any, **_: Any) -> None:
        raise PostgresUnavailableError("gone")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "commit", _commit)
    monkeypatch.setattr(pg_documents, "complete_own", _clear)
    ch = StubCH()
    row = {**_doc_row(), "version": 5}
    with pytest.raises(PublicationPending):
        await insert_one(row, over=0, ch=ch)  # type: ignore[arg-type]
    assert [t for t, _, _ in ch.insert_calls] == ["loom.documents"]


@pytest.mark.asyncio
async def test_a_commit_taken_back_waits_out_its_deliverys_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder of the dropped delivery may still land the row: the caller deletes it from
    ClickHouse only once that lease is waited out."""
    from server.core import pg
    from server.knowledge.store import documents, pg_documents

    waited: list[list[float]] = []

    async def _drop(org: str, namespace: str, uri: str, version: int) -> list[float]:
        return [3.0]

    async def _wait(leases: list[float]) -> None:
        waited.append(leases)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "drop_version", _drop)
    monkeypatch.setattr(pg, "wait_out", _wait)
    await documents.drop_committed("o", "n", "u", 5)
    assert waited == [[3.0]]


def test_a_head_keeps_its_datetimes_to_the_second_as_clickhouse_does() -> None:
    from server.knowledge.store import pg_documents

    stored = pg_documents.encode_row(
        {"superseded_at": datetime(2026, 5, 2, 10, 0, 0, 123456), "ingested_at": None}
    )
    assert stored["superseded_at"] == "2026-05-02T10:00:00"


def test_a_head_keeps_text_postgres_cannot_store_exactly() -> None:
    """A NUL or a lone surrogate in a document's text (which ClickHouse stores) is kept in
    the head's row in base64 and comes back as it was, so such a document can be
    deleted, edited and restored; only a key column refuses one."""
    import json

    from server.core import pg
    from server.core.errors import ValidationError
    from server.knowledge.store import pg_documents

    row = {"content": "a\x00b", "title": "\ud800x", "tags": ["t\x00", "ok"], "revision": 2}
    stored = pg_documents.encode_row(row)
    assert not pg.unstorable(stored)  # nothing left that Postgres text cannot hold
    json.dumps(stored)  # plain JSON, as the jsonb codec writes it
    assert pg_documents.decode_row(stored) == row
    with pytest.raises(ValidationError, match="source_uri"):
        pg_documents.encode_row({"source_uri": "a\x00"})


@pytest.mark.asyncio
async def test_a_tombstone_retires_its_chunks_before_its_row_and_a_version_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a delete is visible its chunks are retired already, so a restore that reads it
    brings them back and no retirement lands after it; a new version's older chunks
    serve until its row does."""
    from server.knowledge.store import chunks as chunks_repo
    from server.knowledge.store import documents

    order: list[str] = []

    class _CH(StubCH):
        async def insert(self, table, rows, settings=None):
            order.append("row")

    clients: list[Any] = []

    async def retire(org: str, ns: str, before: dict[str, int], **kw: Any) -> None:
        order.append("retire")
        clients.append(kw.get("ch"))

    monkeypatch.setattr(chunks_repo, "mark_docs_superseded", retire)
    gone = {**_doc_row(), "version": 5, "superseded_at": datetime(2026, 5, 2, tzinfo=UTC)}
    ch = _CH()
    await documents.publish([gone], retire_older={"kb://policy.md"}, ch=ch)
    assert order == ["retire", "row"]
    order.clear()
    await documents.publish([{**_doc_row(), "version": 6}], retire_older={"kb://policy.md"}, ch=ch)
    assert order == ["row", "retire"]
    assert clients == [ch, ch]  # the retirements go through the client the rows do


@pytest.mark.asyncio
async def test_under_postgres_a_source_uri_postgres_cannot_store_is_read_from_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No head holds such a key, so the heads' read never sends it (Postgres would refuse
    the statement): the document ClickHouse holds under it reads, deletes and edits as any
    source without a head."""
    from server.core import pg

    sent: list[list[str]] = []

    async def _fetch(sql: str, *args: Any) -> list[Any]:
        sent.append(list(args[2]))
        return []

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "fetch", _fetch)
    ch = StubCH()
    ch.query_all_returns.append([{**_doc_row(), "source_uri": "kb://a\x00.md"}])
    rows = await get_many_by_uris("clickhouse", "ns", ["kb://a\x00.md", "kb://b.md"], ch=ch)
    assert [r.source_uri for r in rows] == ["kb://a\x00.md"]
    assert sent == [["kb://b.md"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["postgres", "clickhouse"])
async def test_a_write_cancelled_after_its_commit_is_marked_as_may_have_committed(
    monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    """Cancelled while it publishes (after its Postgres commit, or with its row's insert
    under way): the row is committed or may land, so the cancel carries the mark that
    keeps its writer from retiring its chunks (`pg.may_have_committed`)."""
    import asyncio

    from server.core import pg
    from server.knowledge.store import pg_documents

    async def _commit(rows: Any, **_: Any) -> float:
        return float("inf")

    class _CH(StubCH):
        async def insert(self, table, rows, settings=None):
            raise asyncio.CancelledError()

    monkeypatch.setattr(pg, "enabled", lambda: authority == "postgres")
    monkeypatch.setattr(pg_documents, "commit", _commit)
    with pytest.raises(asyncio.CancelledError) as raised:
        await insert_one({**_doc_row(), "version": 5}, over=0, ch=_CH())  # type: ignore[arg-type]
    assert pg.may_have_committed(raised.value)
