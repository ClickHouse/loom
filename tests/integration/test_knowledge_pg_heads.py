"""Knowledge document heads under the Postgres authority, against a server run with
LOOM_STORAGE_AUTHORITY=postgres.

Each test reads Postgres directly (LOOM_PG_DSN) beside the API: the head a read serves
and the deliveries still owed to ClickHouse's loom.documents. Skipped against a server
of the ClickHouse authority.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pg_conn(control_plane_url: str) -> AsyncIterator[Any]:
    """A connection to the Postgres authority the server under test runs on; skips
    otherwise."""
    import asyncpg

    from server.core import pg

    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        health = (await c.get("/v1/health")).json()
    if health.get("storage_authority") != "postgres":
        pytest.skip("the server under test is not of the Postgres authority")
    # Some tests run the authority's code in this process, which reads its own settings.
    assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"
    from server.core.ch import get_client

    conn = await asyncpg.connect(dsn)
    await pg.init_connection(conn)  # jsonb as Python values, as the server's pool reads it
    ch = get_client()
    await ch.connect()
    try:
        yield conn
    finally:
        await ch.disconnect()
        await pg.close_pool()  # the pool the authority's code opened in this process
        await conn.close()


@pytest.fixture
def org() -> str:
    return unique_org("pgdocs")


@pytest.fixture
def ns() -> str:
    return f"kb-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def call(control_plane_url: str, mint_test_token: Any, org: str, ns: str) -> Any:
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer", "admin", "org_admin"])

    async def _call(op: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as c:
            r = await c.post(
                f"/v1/{op}",
                json={"org": org, "namespace": ns, **body},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, (op, r.text[:400])
        return r.json()

    return _call


async def _head(pg_conn: Any, org: str, ns: str, uri: str) -> Any:
    return await pg_conn.fetchrow(
        "SELECT version, revision, deleted, row FROM loom.knowledge_heads "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3",
        org,
        ns,
        uri,
    )


async def _owed(pg_conn: Any, org: str, ns: str) -> int:
    return int(
        await pg_conn.fetchval(
            # Settled deliveries are published; they wait out the request they replaced.
            "SELECT count(*) FROM loom.knowledge_deliveries "
            "WHERE org = $1 AND namespace = $2 AND NOT settled",
            org,
            ns,
        )
    )


async def _ch_document(org: str, ns: str, uri: str) -> dict[str, Any] | None:
    from server.core.ch import get_client

    return await get_client().query_one(
        "SELECT version, revision, superseded_at, content FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String} "
        "SETTINGS select_sequential_consistency = 1",  # the replica that has the write
        params={"org": org, "ns": ns, "uri": uri},
    )


async def _ch_published(org: str, ns: str, uri: str, columns: list[str]) -> dict[str, Any]:
    from server.core.ch import get_client
    from server.knowledge.store.documents import DOCUMENT_COLUMNS

    assert set(columns) <= set(DOCUMENT_COLUMNS)
    row = await get_client().query_one(
        f"SELECT {', '.join(columns)} FROM loom.documents FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": ns, "uri": uri},
    )
    assert row is not None
    return {**row, "doc_id": str(row["doc_id"])}  # a UUID column; the row carries its text


async def test_each_upsert_is_the_head_and_its_clickhouse_row(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    uri = "docs/guide.md"
    await call("knowledge.upsert", {"source_uri": uri, "content": "# Intro\nOld text."})
    out = await call("knowledge.upsert", {"source_uri": uri, "content": "# Intro\nNew text."})
    assert out["chunks_superseded"] >= 1

    head = await _head(pg_conn, org, ns, uri)
    assert head is not None and head["revision"] == 2 and not head["deleted"]
    assert head["row"]["content"] == "# Intro\nNew text."
    from server.knowledge.store.pg_documents import decode_row

    stored = decode_row(head["row"])
    published = await _ch_published(org, ns, uri, list(stored))
    assert published == stored, "the head holds every column of the published row"
    assert stored["version"] == head["version"] and stored["revision"] == 2
    assert await _owed(pg_conn, org, ns) == 0  # each publication finished in its request

    got = await call("knowledge.get", {"source_uri": uri})
    assert got["found"] and got["content"] == "# Intro\nNew text."
    hits = await call("knowledge.search", {"query": "Old text", "top_k": 10})
    assert all("Old text" not in h["content"] for h in hits["results"])


async def test_a_delete_is_a_deleted_head_and_the_get_finds_nothing(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    uri = "docs/gone.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Soon gone."})
    out = await call("knowledge.delete", {"source_uri": uri})
    assert out["deleted"] and out["chunks_tombstoned"] >= 1

    head = await _head(pg_conn, org, ns, uri)
    assert head is not None and head["deleted"]
    ch_row = await _ch_document(org, ns, uri)
    assert ch_row is not None and ch_row["superseded_at"] is not None
    assert int(ch_row["version"]) == head["version"]
    assert not (await call("knowledge.get", {"source_uri": uri}))["found"]
    again = await call("knowledge.delete", {"source_uri": uri})
    assert not again["deleted"]


async def test_a_commit_over_a_version_its_writer_did_not_read_is_refused(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/raced.md"
    await call("knowledge.add", {"source_uri": uri, "content": "First."})
    head = await _head(pg_conn, org, ns, uri)
    live = await documents_repo.get_one(org, ns, uri)
    assert live is not None and live.version == head["version"]

    stale = documents_repo.build_document_row(live, version=head["version"] + 1, title="stale")
    with pytest.raises(pg_documents.HeadMoved):
        await documents_repo.insert_one(stale, over=head["version"] - 1)
    after = await _head(pg_conn, org, ns, uri)
    assert after["version"] == head["version"] and await _owed(pg_conn, org, ns) == 0


async def test_a_source_written_before_its_namespace_had_heads_is_adopted(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """A loom.documents row with no head (an earlier build's write) is read from
    ClickHouse, and the source's next write takes its first head over it."""
    from server.knowledge.store import documents as documents_repo

    uri = "docs/older.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Written before heads."})
    await pg_conn.execute(
        "DELETE FROM loom.knowledge_heads WHERE org = $1 AND namespace = $2", org, ns
    )
    got = await call("knowledge.get", {"source_uri": uri})
    assert got["found"] and got["content"] == "Written before heads."
    read = await documents_repo.get_one(org, ns, uri)
    assert read is not None

    await call("knowledge.upsert", {"source_uri": uri, "content": "Written after heads."})
    head = await _head(pg_conn, org, ns, uri)
    assert head is not None and head["version"] > read.version and head["revision"] == 2
    assert (await call("knowledge.get", {"source_uri": uri}))["content"] == "Written after heads."


async def test_a_delivery_its_request_left_is_published_by_the_worker(
    pg_conn: Any, org: str, ns: str
) -> None:
    """A commit whose request stopped before ClickHouse: the delivery worker publishes
    the row once its lease runs out, its retirement bit included (the source has no older
    chunks here; the unit tests pin the retirement)."""
    from server.knowledge.app import pg_delivery
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/stranded.md"
    row = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Only Postgres has it.",
        version=time.time_ns(),
        revision=1,
    )
    await pg_documents.commit(
        [row],
        over={},
        targets={uri: pg_documents.DOCUMENT_ROW | pg_documents.RETIRE_OLDER},
    )
    assert await _ch_document(org, ns, uri) is None
    assert (await documents_repo.get_one(org, ns, uri)) is not None  # the head serves it

    await pg_conn.execute(
        "UPDATE loom.knowledge_deliveries SET lease_until = now() - interval '1 second' "
        "WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    # This process's pass, or the server's delivery worker when it claims first.
    deadline = time.monotonic() + 30
    while (ch_row := await _ch_document(org, ns, uri)) is None and time.monotonic() < deadline:
        await pg_delivery.publish_due(100)
        await asyncio.sleep(0.2)
    assert ch_row is not None and int(ch_row["version"]) == int(row["version"])
    for _ in range(150):  # the publisher settles its delivery after the insert
        if not await _owed(pg_conn, org, ns):
            break
        await asyncio.sleep(0.2)
    assert await _owed(pg_conn, org, ns) == 0
    settled = await pg_conn.fetchval(  # kept until the request it replaced is waited out
        "SELECT count(*) FROM loom.knowledge_deliveries "
        "WHERE org = $1 AND namespace = $2 AND settled",
        org,
        ns,
    )
    assert settled == 1


async def test_a_namespace_erasure_leaves_no_knowledge_heads(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    for i in range(3):
        await call("knowledge.add", {"source_uri": f"docs/{i}.md", "content": f"Page {i}."})
    out = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert out["status"] == "completed", out
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["pg_knowledge.delete"]["rows_affected"] >= 3
    for table in ("loom.knowledge_heads", "loom.knowledge_deliveries"):
        assert (
            await pg_conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE org = $1 AND namespace = $2", org, ns
            )
            == 0
        ), table


async def test_a_newer_row_a_build_without_heads_wrote_is_served_and_adopted(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """A rolling upgrade's older replica writes loom.documents alone: the newer row
    serves over the head, and the source's next commit replaces the head over it."""
    from server.knowledge.store import documents as documents_repo

    uri = "docs/mixed.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Written with a head."})
    head = await _head(pg_conn, org, ns, uri)
    live = await documents_repo.get_one(org, ns, uri)
    assert live is not None
    older_build = documents_repo.build_document_row(
        live, version=head["version"] + 1, content="Written without one.", revision=2
    )
    await documents_repo.publish([older_build])
    assert (await call("knowledge.get", {"source_uri": uri}))["content"] == "Written without one."

    await call("knowledge.upsert", {"source_uri": uri, "content": "Written with one again."})
    after = await _head(pg_conn, org, ns, uri)
    assert after["version"] > older_build["version"] and after["revision"] == 3
    got = await call("knowledge.get", {"source_uri": uri})
    assert got["content"] == "Written with one again."


async def test_a_source_a_build_without_heads_erased_is_not_served_from_its_head(
    call: Any, pg_conn: Any, org: str, ns: str, ch_client: Any
) -> None:
    """An older replica's erasure removes the source's ClickHouse rows and leaves its
    head: the head owes nothing, so the read drops it, and the source can be written
    again."""
    uri = "docs/erased.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Erased by an older build."})
    ch_client.command(
        "DELETE FROM loom.documents WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    )
    assert not (await call("knowledge.get", {"source_uri": uri}))["found"]
    assert await _head(pg_conn, org, ns, uri) is None

    await call("knowledge.add", {"source_uri": uri, "content": "Written again."})
    assert (await call("knowledge.get", {"source_uri": uri}))["content"] == "Written again."


async def test_of_two_first_commits_of_a_source_one_is_refused(
    pg_conn: Any, org: str, ns: str
) -> None:
    """Neither writer read a head: a lock taken on a row that does not exist holds
    nothing, so the head's insert itself decides."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/first.md"
    rows = [
        documents_repo.build_document_row(
            org=org, namespace=ns, source_uri=uri, content=f"Writer {i}.", version=time.time_ns()
        )
        for i in range(2)
    ]
    got = await asyncio.gather(
        *(
            pg_documents.commit([r], over={}, targets={uri: pg_documents.DOCUMENT_ROW})
            for r in rows
        ),
        return_exceptions=True,
    )
    assert sorted(type(g).__name__ for g in got) == ["HeadMoved", "float"]
    won = rows[[i for i, g in enumerate(got) if isinstance(g, float)][0]]
    head = await _head(pg_conn, org, ns, uri)
    assert head["version"] == won["version"] and await _owed(pg_conn, org, ns) == 1


async def test_an_erasure_over_a_parked_delivery_completes(
    call: Any, pg_conn: Any, org: str, ns: str, control_plane_url: str
) -> None:
    """A parked delivery's lease is endless: the erasure waits only as long as its last
    send could still land (`fenced_until`), here not at all."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/parked.md"
    row = documents_repo.build_document_row(
        org=org, namespace=ns, source_uri=uri, content="Never published.", version=time.time_ns()
    )
    await pg_documents.commit([row], over={}, targets={uri: pg_documents.DOCUMENT_ROW})
    await pg_conn.execute(  # parked an hour ago: its last send cannot land any more
        "UPDATE loom.knowledge_deliveries SET lease_until = 'infinity', attempts = 10, "
        "fenced_until = now() - interval '1 hour' WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    assert await pg_documents.parked_count() >= 1
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        health = (await c.get("/v1/health?include=postgres")).json()
    assert health["postgres"]["parked_knowledge_deliveries"] >= 1
    out = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert out["status"] == "completed", out
    assert await _owed(pg_conn, org, ns) == 0 and await _head(pg_conn, org, ns, uri) is None


async def test_a_write_built_from_a_document_read_before_its_erasure_is_refused(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """A delete or a metadata edit copies the row it read: once an erasure removed that
    document, its commit is refused, and no version of it is committed or published."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/secret.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Erase me."})
    read = await documents_repo.get_one(org, ns, uri)
    assert read is not None
    out = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert out["status"] == "completed", out
    evidence = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.erased_documents WHERE org = $1 AND namespace = $2 "
        "AND doc_id = $3",
        org,
        ns,
        read.doc_id,
    )
    assert evidence == 1

    late = documents_repo.build_document_row(read, version=time.time_ns(), title="edited")
    with pytest.raises(pg_documents.DocumentErased):
        await documents_repo.insert_one(late, over=read.version)
    assert await _head(pg_conn, org, ns, uri) is None and await _owed(pg_conn, org, ns) == 0
    ch_row = await _ch_document(org, ns, uri)  # the erasure's own tombstone, at most
    assert ch_row is None or (int(ch_row["version"]), ch_row["superseded_at"] is not None) == (
        read.version,
        True,
    )
    assert not (await call("knowledge.get", {"source_uri": uri}))["found"]


async def test_an_upsert_built_from_an_erased_document_deletes_its_own_chunks(
    call: Any,
    pg_conn: Any,
    org: str,
    ns: str,
    control_plane_url: str,
    mint_test_token: Any,
) -> None:
    """An upsert carries the live document's author (`built_from`): refused once an
    erasure recorded that document's id, it deletes the chunk rows it inserted rather
    than retiring them, so the erased author does not stay in loom.chunks."""
    from server.core.ch import get_client
    from server.knowledge.store import documents as documents_repo

    uri = "docs/erased-author.md"
    await call("knowledge.upsert", {"source_uri": uri, "content": "First text."})
    live = await documents_repo.get_one(org, ns, uri)
    assert live is not None
    await pg_conn.execute(  # the evidence an erasure of that document leaves
        "INSERT INTO loom.erased_documents (org, namespace, doc_id) VALUES ($1, $2, $3)",
        org,
        ns,
        live.doc_id,
    )
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as c:
        r = await c.post(
            "/v1/knowledge.upsert",
            json={"org": org, "namespace": ns, "source_uri": uri, "content": "Second text."},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 404, r.text[:400]
    detail = r.json()["detail"]  # DocumentErased, not a missing document
    assert (detail["code"], detail["message"]) == (
        "RESOURCE_NOT_FOUND",
        "the document was erased while this write ran",
    ), detail
    rows = await get_client().query_all(
        "SELECT DISTINCT doc_version FROM loom.chunks "
        "WHERE org = {org:String} AND namespace = {ns:String} AND source_uri = {uri:String} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": ns, "uri": uri},
    )
    assert [int(r["doc_version"]) for r in rows] == [live.version], "only the first write's"
    head = await _head(pg_conn, org, ns, uri)
    assert head is not None and head["version"] == live.version


async def test_a_tombstone_a_build_without_heads_stamped_in_place_hides_the_head(
    call: Any, pg_conn: Any, org: str, ns: str, ch_client: Any
) -> None:
    """An older replica's default erasure stamps `superseded_at` on the rows it finds,
    at their version: at a tie ClickHouse's copy answers, so the document reads as gone."""
    uri = "docs/stamped.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Tombstoned by an older build."})
    ch_client.command(
        "ALTER TABLE loom.documents UPDATE superseded_at = now() "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
        settings={"mutations_sync": 2},
    )
    head = await _head(pg_conn, org, ns, uri)
    assert head is not None and not head["deleted"]  # the older build left it live
    assert not (await call("knowledge.get", {"source_uri": uri}))["found"]


async def test_a_user_erasure_removes_that_users_documents_only(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    from server.knowledge.store import documents as documents_repo

    other_ns = f"{ns}-b"
    for namespace, uri, user in (
        (ns, "a.md", "alice"),
        (other_ns, "b.md", "alice"),
        (ns, "c.md", "bob"),
    ):
        row = documents_repo.build_document_row(
            org=org,
            namespace=namespace,
            source_uri=uri,
            content=f"{user}'s page.",
            user_id=user,
            version=time.time_ns(),
        )
        await documents_repo.insert_one(row, over=0)  # committed and published, as a request does
    out = await call(
        "erasure.request", {"scope_type": "user_id", "scope_value": "alice", "requester": "t"}
    )
    assert out["status"] == "completed", out
    left = await pg_conn.fetch(
        "SELECT namespace, source_uri FROM loom.knowledge_heads WHERE org = $1 ORDER BY 1, 2", org
    )
    assert [(r["namespace"], r["source_uri"]) for r in left] == [(ns, "c.md")]
    owed = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.knowledge_deliveries WHERE org = $1 AND user_id = 'alice'",
        org,
    )
    assert owed == 0
    evidence = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.erased_documents WHERE org = $1", org
    )
    assert evidence == 2  # each version's doc_id: one per document here


async def test_an_upsert_built_from_a_live_read_before_its_erasure_is_refused(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """A new row of its own id that would carry the erased document's original and author:
    it names the document it read, which the evidence refuses."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/carried.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Carried forward."})
    read = await documents_repo.get_one(org, ns, uri)
    assert read is not None
    out = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert out["status"] == "completed", out
    fresh = documents_repo.build_document_row(
        org=org, namespace=ns, source_uri=uri, content="New text.", version=time.time_ns()
    )
    with pytest.raises(pg_documents.DocumentErased):
        await documents_repo.insert_one(fresh, over=read.version, built_from=read.doc_id)
    assert await _head(pg_conn, org, ns, uri) is None


async def test_a_head_an_older_builds_user_purge_deleted_is_not_served_over_an_older_row(
    call: Any, pg_conn: Any, org: str, ns: str, ch_client: Any
) -> None:
    """An older build's user purge deletes the user's rows and stamps another writer's
    older version deleted: the head, at the purged version and owing nothing, is
    dropped, and the document reads as deleted."""
    from server.knowledge.store import documents as documents_repo

    uri = "docs/two-writers.md"
    first = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Bob's text.",
        user_id="bob",
        version=time.time_ns(),
    )
    await documents_repo.insert_one(first, over=0)
    second = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Alice's text.",
        user_id="alice",
        version=documents_repo.version_past(first["version"]),
    )
    await documents_repo.insert_one(second, over=first["version"])
    head = await _head(pg_conn, org, ns, uri)
    assert head["version"] == second["version"] and await _owed(pg_conn, org, ns) == 0

    for sql in (  # what the older build's purge of alice does to loom.documents
        "ALTER TABLE loom.documents UPDATE superseded_at = now() WHERE org = {org:String} "
        "AND namespace = {ns:String} AND user_id != 'alice'",
        "ALTER TABLE loom.documents DELETE WHERE org = {org:String} "
        "AND namespace = {ns:String} AND user_id = 'alice'",
    ):
        ch_client.command(sql, parameters={"org": org, "ns": ns}, settings={"mutations_sync": 2})
    assert not (await call("knowledge.get", {"source_uri": uri}))["found"]
    assert await _head(pg_conn, org, ns, uri) is None
    # A merge may have folded Bob's older version into Alice's before the purge: then no
    # row is left, as after a purge of the only writer. Either way the head serves nothing.
    gone = await documents_repo.get_one(org, ns, uri, include_superseded=True)
    assert gone is None or gone.version == first["version"]


async def test_an_erasure_waits_for_the_send_that_parked_a_delivery(
    pg_conn: Any, org: str, ns: str
) -> None:
    """Parked by a timed-out insert that may still land: the erasure's wait covers it from
    the moment it parked, as a lease would."""
    from server.erasure.store import steps_pg
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/parked-now.md"
    row = documents_repo.build_document_row(
        org=org, namespace=ns, source_uri=uri, content="Parked just now.", version=time.time_ns()
    )
    await pg_documents.commit([row], over={}, targets={uri: pg_documents.DOCUMENT_ROW})
    # Parked under the commit's own token while its request lease still runs: no
    # delivery worker beside the test can claim it first.
    delivery = pg_documents.Delivery(
        org,
        ns,
        uri,
        int(row["version"]),
        dict(row),
        pg_documents.DOCUMENT_ROW,
        pg_documents.COMMIT_TOKEN,
        9,
    )
    await pg_documents.failed(delivery, "BackendTimeoutError", park=True)
    removed, leases = await steps_pg._erase_knowledge_once(org, [ns], None)
    assert removed == 2 and len(leases) == 1 and -5 < leases[0] <= 0


async def test_an_erasure_waits_for_a_writer_whose_delivery_the_worker_finished(
    pg_conn: Any, org: str, ns: str
) -> None:
    """The worker took a delivery over and published it: the request that held it first
    may still have an insert in flight, so the delivery is kept settled, its lease ending
    then, and the erasure waits it out. A delivery its own request finished is gone."""
    from server.erasure.store import steps_pg

    await _publish_by(pg_conn, org, ns, "docs/own.md", "-", taken_over=False)
    await _publish_by(pg_conn, org, ns, "docs/taken-over.md", "-", taken_over=True)
    removed, leases = await steps_pg._erase_knowledge_once(org, [ns], None)
    assert removed == 3 and len(leases) == 1 and -5 < leases[0] <= 0  # the settled one


async def test_a_user_erasure_waits_for_that_users_publication_another_write_replaced(
    pg_conn: Any, org: str, ns: str
) -> None:
    """Alice's publication was taken over and finished, then Bob's write took the head:
    her settled delivery still names her, so erasing her waits for her request."""
    from server.erasure.store import steps_pg
    from server.knowledge.store import documents as documents_repo

    uri = "docs/shared.md"
    first = await _publish_by(pg_conn, org, ns, uri, "alice", taken_over=True)
    bobs = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Bob's.",
        user_id="bob",
        version=documents_repo.version_past(first),
    )
    await documents_repo.insert_one(bobs, over=first)
    removed, leases = await steps_pg._erase_knowledge_once(org, None, "alice")
    assert removed == 1 and len(leases) == 1 and -5 < leases[0] <= 0
    assert (await _head(pg_conn, org, ns, uri))["version"] == bobs["version"]


async def _publish_by(
    pg_conn: Any, org: str, ns: str, uri: str, user: str, *, taken_over: bool
) -> int:
    """Commit a row of `user`, and publish it as its request (token 1) or as the worker
    after a takeover (token 2). Returns its version."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    row = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content=f"{user}'s.",
        user_id=user,
        version=time.time_ns(),
    )
    await pg_documents.commit([row], over={}, targets={uri: pg_documents.DOCUMENT_ROW})
    token = 2 if taken_over else pg_documents.COMMIT_TOKEN
    await pg_conn.execute(  # the worker's claim, under the next token
        "UPDATE loom.knowledge_deliveries SET lease_token = $4 "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3",
        org,
        ns,
        uri,
        token,
    )
    await documents_repo.publish([row])
    if taken_over:
        await pg_documents.complete(org, ns, uri, int(row["version"]), token=token)
    else:
        await pg_documents.complete_own([row])
    return int(row["version"])


async def test_a_settled_delivery_does_not_keep_a_head_whose_row_an_older_build_purged(
    call: Any, pg_conn: Any, org: str, ns: str, ch_client: Any
) -> None:
    """A publication the worker took over stays settled for the insert bound, but owes
    nothing: an older build's purge of the row drops the head at once."""
    uri = "docs/settled-purged.md"
    await _publish_by(pg_conn, org, ns, uri, "-", taken_over=True)
    ch_client.command(
        "DELETE FROM loom.documents WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    )
    assert not (await call("knowledge.get", {"source_uri": uri}))["found"]
    assert await _head(pg_conn, org, ns, uri) is None


async def test_text_postgres_cannot_store_is_kept_exactly_and_the_document_deletes(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """A NUL in a document's text, which ClickHouse stores: the head keeps it (in base64
    inside its row), a read serves it as written, and the document can be deleted."""
    uri = "docs/nul.md"
    await call("knowledge.add", {"source_uri": uri, "title": "a\u0000b", "content": "x\u0000y"})
    got = await call("knowledge.get", {"source_uri": uri})
    assert (got["title"], got["content"]) == ("a\u0000b", "x\u0000y")
    from server.knowledge.store import pg_documents

    stored = (await _head(pg_conn, org, ns, uri))["row"]  # what a read before publish serves
    assert "\u0000" not in str(stored["content"])
    decoded = pg_documents.decode_row(stored)
    assert (decoded["title"], decoded["content"]) == ("a\u0000b", "x\u0000y")
    out = await call("knowledge.delete", {"source_uri": uri})
    assert out["deleted"]
    head = await _head(pg_conn, org, ns, uri)
    assert head is not None and head["deleted"]


async def test_a_write_built_from_a_read_older_than_the_erased_head_is_refused(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """A delete read version 1, an upsert then committed version 2, and an erasure removed
    that head: the delete's tombstone, built from version 1, keeps version 1's doc_id,
    which the erasure recorded from ClickHouse, and is refused. Version 1 lives in an
    earlier month's partition, so no merge folds it into version 2 first. A write that
    reads after the erasure commits."""
    from datetime import datetime

    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/raced-erasure.md"
    earlier = datetime(2025, 1, 15)  # another partition than version 2's
    v1 = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="First.",
        version=time.time_ns(),
        ingested_at=earlier,
    )
    await documents_repo.insert_one(v1, over=0)
    first = await documents_repo.get_one(org, ns, uri)
    assert first is not None and first.version == v1["version"]
    await call("knowledge.upsert", {"source_uri": uri, "content": "Second."})
    out = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert out["status"] == "completed", out
    stale = documents_repo.build_document_row(
        first, version=documents_repo.version_past(first.version), superseded_at=first.ingested_at
    )
    with pytest.raises(pg_documents.DocumentErased):
        await documents_repo.insert_one(stale, over=first.version)
    assert await _head(pg_conn, org, ns, uri) is None

    await call("knowledge.upsert", {"source_uri": uri, "content": "After the erasure."})
    got = await call("knowledge.get", {"source_uri": uri})
    assert got["content"] == "After the erasure."


async def test_a_write_after_an_erasure_of_a_head_never_published_commits(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """The erased head was still owed to ClickHouse, which keeps the version before it
    (stamped deleted by the erasure): a write that reads after the erasure reads that one,
    carries no recorded id, and commits."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/owed-then-erased.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Published."})
    first = await documents_repo.get_one(org, ns, uri)
    assert first is not None
    owed = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Never published.",
        version=documents_repo.version_past(first.version),
    )
    await pg_documents.commit(
        [owed], over={uri: first.version}, targets={uri: pg_documents.DOCUMENT_ROW}
    )
    await pg_conn.execute(  # parked long ago, its last send past landing: nothing to wait
        "UPDATE loom.knowledge_deliveries SET lease_until = 'infinity', attempts = 10, "
        "fenced_until = now() - interval '1 hour' WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    out = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert out["status"] == "completed", out
    await call("knowledge.upsert", {"source_uri": uri, "content": "After the erasure."})
    got = await call("knowledge.get", {"source_uri": uri})
    assert got["content"] == "After the erasure."


async def test_a_restore_waits_while_a_taken_over_delete_may_still_land(
    call: Any, pg_conn: Any, org: str, ns: str
) -> None:
    """The worker took a delete over and settled it: the request it replaced may still
    land its retirement of the chunks until the insert bound has passed, so the source
    counts as owed and a restore waits, rather than have its chunks retired again."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/settled-delete.md"
    await call("knowledge.add", {"source_uri": uri, "content": "Deleted, then restored."})
    live = await documents_repo.get_one(org, ns, uri)
    assert live is not None
    tomb = documents_repo.build_document_row(
        live, version=documents_repo.version_past(live.version), superseded_at=live.ingested_at
    )
    await pg_documents.commit(
        [tomb],
        over={uri: live.version},
        targets={uri: pg_documents.DOCUMENT_ROW | pg_documents.RETIRE_OLDER},
    )
    await pg_conn.execute(  # the worker's claim, under the next token
        "UPDATE loom.knowledge_deliveries SET lease_token = 2 "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3",
        org,
        ns,
        uri,
    )
    await documents_repo.publish([tomb], retire_older={uri})
    await pg_documents.complete(org, ns, uri, int(tomb["version"]), token=2)
    # Settled, nothing owed, yet within the insert bound: restore must wait (it answers 503
    # on this, `test_knowledge_restore.py`).
    assert await _owed(pg_conn, org, ns) == 0
    assert await documents_repo.publication_may_land(org, ns, uri)
    await pg_conn.execute(  # past the bound: the replaced request can land nothing more
        "UPDATE loom.knowledge_deliveries SET lease_until = now() - interval '1 hour' "
        "WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    assert not await documents_repo.publication_may_land(org, ns, uri)


async def test_an_unpublished_commit_keeps_its_original_from_the_sweep(
    pg_conn: Any, org: str, ns: str
) -> None:
    """A commit whose document row has not landed names its original only in Postgres:
    the originals sweep spares it, and another user's erasure keeps it, until the
    delivery is published. A pending delete's tombstone needs no original."""
    from server.erasure.store import steps_memory
    from server.knowledge.store import blobs, pg_documents
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.workers import blob_gc

    uri = "docs/owed-original.md"
    sha = await blobs.put(org, ns, f"owed {uuid.uuid4()}".encode())
    row = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Its row is still owed.",
        version=time.time_ns(),
        blob_path=sha,
        user_id="bob",
    )
    await pg_documents.commit([row], over={}, targets={uri: pg_documents.DOCUMENT_ROW})
    await pg_conn.execute(  # owed for the whole test: no worker publishes it meanwhile
        "UPDATE loom.knowledge_deliveries SET lease_until = now() + interval '1 hour' "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3",
        org,
        ns,
        uri,
    )
    assert await pg_documents.owed_originals(org, ns) == {sha}
    assert await steps_memory._others_owed_originals(org, "alice", [(ns, sha)]) == {(ns, sha)}
    assert await steps_memory._others_owed_originals(org, "bob", [(ns, sha)]) == set()
    assert sha in await blobs.unreferenced(org, ns, 0, 10)  # ClickHouse names it nowhere
    await blob_gc.sweep_namespace(org, ns, 0)
    assert sha in await blobs.unreferenced(org, ns, 0, 10)  # spared: still stored
    await pg_documents.complete_own([row])
    # A pending delete of it names the original, and needs none.
    tomb = {
        **row,
        "version": documents_repo.version_past(int(row["version"])),
        "superseded_at": row["ingested_at"],
    }
    await pg_documents.commit(
        [tomb], over={uri: int(row["version"])}, targets={uri: pg_documents.DOCUMENT_ROW}
    )
    await pg_conn.execute(  # the tombstone stays owed through the sweep below
        "UPDATE loom.knowledge_deliveries SET lease_until = now() + interval '1 hour' "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3",
        org,
        ns,
        uri,
    )
    assert await pg_documents.owed_originals(org, ns) == set()
    assert await steps_memory._others_owed_originals(org, "alice", [(ns, sha)]) == set()
    await blob_gc.sweep_namespace(org, ns, 0)
    assert sha not in await blobs.unreferenced(org, ns, 0, 10)  # swept


async def test_a_settled_delivery_and_an_artifact_alone_are_read_as_the_fragment_says(
    pg_conn: Any, org: str, ns: str
) -> None:
    """A delivery the worker settled has published its row: it holds no original. A live
    row with an extraction artifact and no original still keeps the artifact."""
    from server.knowledge.store import documents as documents_repo
    from server.knowledge.store import pg_documents

    uri = "docs/artifact-only.md"
    artifact = "e" * 64
    row = documents_repo.build_document_row(
        org=org,
        namespace=ns,
        source_uri=uri,
        content="Restored without its original.",
        version=time.time_ns(),
        artifact_sha256=artifact,
    )
    await pg_documents.commit([row], over={}, targets={uri: pg_documents.DOCUMENT_ROW})
    assert await pg_documents.owed_originals(org, ns) == {artifact}
    await pg_conn.execute(  # the worker's claim, under the next token
        "UPDATE loom.knowledge_deliveries SET lease_token = 2 "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3",
        org,
        ns,
        uri,
    )
    await pg_documents.complete(org, ns, uri, int(row["version"]), token=2)
    assert await pg_documents.owed_originals(org, ns) == set()  # settled: published
