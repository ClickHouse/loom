"""The knowledge dataset catalog under the Postgres authority (`loom.knowledge_datasets`),
on the stores a server run with LOOM_STORAGE_AUTHORITY=postgres uses: a row lands only
over an older version; an approval is refused once the source's head moved past the
version it reviewed, an erasure removed the document or a write of the row followed the
one read; the deletes, and the erasure step. Skipped against a server of the ClickHouse
authority.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

_URI = "repo://sales.csv"


@pytest.fixture
async def stores(control_plane_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """The server and its Postgres authority, for the catalog store's code in this
    process; skips against a server of the ClickHouse authority."""
    from server.core import pg
    from server.core.ch import get_client

    os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        health = (await c.get("/v1/health")).json()
        if health.get("storage_authority") != "postgres":
            pytest.skip("the server under test is not of the Postgres authority")
        assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"
        ch = get_client()
        await ch.connect()
        try:
            yield c
        finally:
            await ch.disconnect()
            await pg.close_pool()


async def _head(c: httpx.AsyncClient, org: str, content: str) -> int:
    """Write the source's document and return its head's version."""
    from server.core import pg

    r = await c.post(
        "/v1/knowledge.add",
        json={"org": org, "namespace": "docs", "source_uri": _URI, "content": content},
    )
    assert r.status_code == 200, r.text[:300]
    version = await pg.fetchval(
        "SELECT version FROM loom.knowledge_heads"
        " WHERE org = $1 AND namespace = 'docs' AND source_uri = $2",
        org,
        _URI,
    )
    return int(version)


def _catalog(org: str, doc_version: int, **fields: Any) -> dict[str, Any]:
    from server.knowledge.store import datasets as repo

    return repo.dataset_row(
        None,
        **{
            "org": org,
            "namespace": "docs",
            "source_uri": _URI,
            "dataset_id": "ds_sales",
            "doc_version": doc_version,
            "snapshot_id": 1,
            "revision": 1,
            "table_id": "t1",
            "position": 0,
            "name": "sales",
            "sheet": "",
            "row_start": 1,
            "row_end": 3,
            "schema": "[]",
            "schema_state": "needs_clarification",
            "clarifications": "[]",
            "row_count": 2,
            "rejected_rows": 0,
            "rejected": "[]",
            "complete": True,
            "artifact_sha256": "a" * 64,
            "agent": "-",
            "user_id": "uploader",
            "session_id": "-",
            "actor": "user:uploader",
            "source_class": "imported",
            **fields,
        },
    )


async def test_a_catalog_row_lands_only_over_an_older_version(
    stores: httpx.AsyncClient,
) -> None:
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-upsert")
    first = _catalog(org, 100, name="sales\x00")  # text Postgres cannot store, kept
    assert await pg_datasets.upsert([first]) == [first]
    (read,) = await pg_datasets.at_version(org, "docs", _URI, 100)
    assert read == first
    later = {**first, "snapshot_id": 2, "version": int(first["version"]) + 5}
    assert await pg_datasets.upsert([later]) == [later]
    other = {**first, "dataset_id": "ds_other"}
    # One statement: the older version stays out, a new key lands.
    assert await pg_datasets.upsert([first, other]) == [other]
    (read,) = await pg_datasets.at_version(org, "docs", _URI, 100, dataset_id="ds_sales")
    assert read["snapshot_id"] == 2


async def test_an_approval_commits_only_over_the_version_it_read(
    stores: httpx.AsyncClient,
) -> None:
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-approve")
    v = await _head(stores, org, "region,amount\nEU,10\n")
    served = _catalog(org, v)
    await pg_datasets.upsert([served])
    read = int(served["version"])

    def approval(snapshot: int) -> dict[str, Any]:
        return {
            **served,
            "snapshot_id": snapshot,
            "schema_state": "approved",
            "version": read + snapshot,
        }

    # Another write of the row followed the one read: nothing commits.
    assert await pg_datasets.approve(approval(3), read=read - 1, doc_id="") == "moved"
    assert await pg_datasets.approve(approval(3), read=read, doc_id="") == "switched"
    (got,) = await pg_datasets.at_version(org, "docs", _URI, v)
    assert (got["snapshot_id"], got["schema_state"]) == (3, "approved")
    # An older build's approval newer in ClickHouse alone: the version read is past
    # Postgres's row, which the approval then commits over.
    assert await pg_datasets.approve(approval(9), read=read + 5, doc_id="") == "switched"
    # A later write of the source: its head no longer serves version v.
    await _head(stores, org, "region,amount\nUS,7\n")
    assert await pg_datasets.approve(approval(12), read=read + 9, doc_id="") == "moved"
    (got,) = await pg_datasets.at_version(org, "docs", _URI, v)
    assert got["snapshot_id"] == 9


async def test_an_approval_of_a_dataset_postgres_does_not_hold_inserts_it(
    stores: httpx.AsyncClient,
) -> None:
    """Published by a build that predates the table: Postgres holds no row of it."""
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-adopt")
    v = await _head(stores, org, "region,amount\nEU,10\n")
    new = _catalog(org, v, schema_state="approved")
    assert await pg_datasets.approve(new, read=1, doc_id="") == "switched"
    (got,) = await pg_datasets.at_version(org, "docs", _URI, v)
    assert got["schema_state"] == "approved"


async def test_a_source_with_no_head_or_an_older_one_is_approved(
    stores: httpx.AsyncClient,
) -> None:
    """A source written before its namespace had heads has none; one an older build wrote
    since has a head behind ClickHouse's row. The source's claim keeps a publish out."""
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-nohead")
    first = _catalog(org, 100, schema_state="approved")
    assert await pg_datasets.approve(first, read=1, doc_id="") == "switched"
    v = await _head(stores, org, "region,amount\nEU,10\n")
    behind = _catalog(org, v + 1000, schema_state="approved")  # past the head's version
    assert await pg_datasets.approve(behind, read=1, doc_id="") == "switched"


async def test_a_deleted_source_s_dataset_is_not_approved(stores: httpx.AsyncClient) -> None:
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-deleted")
    v = await _head(stores, org, "region,amount\nEU,10\n")
    served = _catalog(org, v)
    await pg_datasets.upsert([served])
    r = await stores.post(
        "/v1/knowledge.delete", json={"org": org, "namespace": "docs", "source_uri": _URI}
    )
    assert r.status_code == 200, r.text[:300]
    later = {**served, "snapshot_id": 2, "version": int(served["version"]) + 1}
    assert await pg_datasets.approve(later, read=int(served["version"]), doc_id="") == "moved"


async def test_the_deletes_name_the_datasets_and_keep_what_later_rows_serve(
    stores: httpx.AsyncClient,
) -> None:
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-forget")
    await pg_datasets.upsert(
        [_catalog(org, 100), _catalog(org, 200, snapshot_id=7), _catalog(org, 300, snapshot_id=8)]
    )
    assert await pg_datasets.forget(org, "docs", [_URI], before=200) == {"ds_sales"}
    assert await pg_datasets.at_version(org, "docs", _URI, 100) == []
    named, kept = await pg_datasets.forget_published(org, "docs", {_URI: 200})
    assert named == {"ds_sales"} and kept == {8}
    assert await pg_datasets.at_version(org, "docs", _URI, 200) == []
    assert len(await pg_datasets.at_version(org, "docs", _URI, 300)) == 1
    assert await pg_datasets.forget(org, "docs", [_URI], only=300) == {"ds_sales"}


async def test_an_erasure_deletes_the_scope_s_catalog_rows(stores: httpx.AsyncClient) -> None:
    from server.erasure.store import steps_pg
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-erase")
    await pg_datasets.upsert([_catalog(org, 100), _catalog(org, 100, dataset_id="ds_other")])
    assert await steps_pg.pg_holds_namespace(org, "docs")
    assert await steps_pg.delete_pg_knowledge_by_user(org, "uploader") == 2
    assert await pg_datasets.at_version(org, "docs", _URI, 100) == []
    residue = await steps_pg.pg_residue(org, "user", "uploader")
    assert residue["pg:loom.knowledge_datasets"] == 0


async def test_an_approval_after_an_erasure_writes_nothing_back(
    stores: httpx.AsyncClient,
) -> None:
    """The erasure deleted the head and the catalog rows: an approval that read before it
    finds no head, and the erasure's evidence of the document refuses it."""
    from server.core import pg
    from server.erasure.store import steps_pg
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-erased")
    v = await _head(stores, org, "region,amount\nEU,10\n")
    doc_id = await pg.fetchval(
        "SELECT row->>'doc_id' FROM loom.knowledge_heads WHERE org = $1 AND source_uri = $2",
        org,
        _URI,
    )
    served = _catalog(org, v)
    await pg_datasets.upsert([served])
    await steps_pg.delete_pg_knowledge_by_namespace(org, "docs")
    approval = {**served, "schema_state": "approved", "version": int(served["version"]) + 1}
    assert await pg_datasets.approve(approval, read=int(served["version"]), doc_id=doc_id) == (
        "moved"
    )
    assert await pg_datasets.at_version(org, "docs", _URI, v) == []


async def test_a_publish_s_catalog_write_after_an_erasure_writes_nothing(
    stores: httpx.AsyncClient,
) -> None:
    """The publishing job is read in the write's transaction: once an erasure deleted it,
    the write raises and leaves no catalog row behind."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from server.erasure.store import steps_pg
    from server.knowledge.store import ingestions, pg_datasets, pg_jobs

    org = unique_org("pgds-pubrace")
    job = ingestions.ingestion_row(
        org=org,
        namespace="docs",
        ingestion_id=str(uuid.uuid4()),
        source_uri=_URI,
        upload_id=str(uuid.uuid4()),
        sha256="a" * 64,
        state="processing",
        next_attempt_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=6),
    )
    await pg_jobs.insert(job)
    key = (org, "docs", job["ingestion_id"])
    await pg_datasets.upsert([_catalog(org, 100)], job=key)  # the job is held: written
    assert len(await pg_datasets.at_version(org, "docs", _URI, 100)) == 1
    await steps_pg.delete_pg_knowledge_by_namespace(org, "docs")
    with pytest.raises(pg_datasets.JobErased):
        await pg_datasets.upsert([_catalog(org, 200)], job=key)
    assert await pg_datasets.at_version(org, "docs", _URI, 200) == []


async def test_a_user_erasure_locks_the_namespace_of_the_user_s_headless_catalog_row(
    stores: httpx.AsyncClient,
) -> None:
    """A headless source's catalog row, of the user, in a namespace where the user has no
    head: the user erasure's lock covers that namespace, so an approval of it waits for
    the erasure (and times out here, the lock held) rather than write the row back."""
    import asyncpg

    from server.core.pg import PostgresUnavailableError
    from server.erasure.store import steps_pg
    from server.knowledge.store import pg_datasets

    org = unique_org("pgds-user-lock")
    served = _catalog(org, 100)  # no head: written before the namespace had heads
    await pg_datasets.upsert([served])
    approval = {**served, "schema_state": "approved", "version": int(served["version"]) + 1}
    conn = await asyncpg.connect(os.environ["LOOM_PG_DSN"])
    try:
        tx = conn.transaction()
        await tx.start()
        await steps_pg._lock_user_knowledge(conn, org, "uploader")  # as the erasure does
        with pytest.raises(PostgresUnavailableError):
            await pg_datasets.approve(approval, read=int(served["version"]), doc_id="")
        await tx.rollback()
    finally:
        await conn.close()
    (got,) = await pg_datasets.at_version(org, "docs", _URI, 100)
    assert got["schema_state"] == "needs_clarification"
