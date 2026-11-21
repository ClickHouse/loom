"""Title projections use the current authority and return no bodies or diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.integration._resources import unique_org


@asynccontextmanager
async def document(**fields):
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.store import pg_documents, source_documents

    org, namespace = unique_org("title-projection"), new_mirror_name()
    content = "Body outside a title read. " * 1000
    row = {
        "org": org,
        "namespace": namespace,
        "source_uri": "https://docs.example.com/path/report.PDF?query=1#native#" + str(uuid4()),
        "doc_id": str(uuid4()),
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "title": "Inspected title",
        "metadata": '{"source_revision":"r1"}',
        "format": "",
        "blob_content_type": "",
        "original_filename": "",
        "version": 17,
        "revision": 0,
        "ingested_at": datetime.now(UTC).replace(microsecond=0),
        "superseded_at": None,
        **fields,
    }
    ch = get_client()
    set_query_tag("test.mirror.title_projection")
    try:
        await ch.connect()
        if pg.enabled():
            await pg_documents.commit([row], over={}, targets={row["source_uri"]: 1})
        else:
            await ch.insert("loom.documents", [row])
        yield source_documents, row
    finally:
        try:
            await ch.disconnect()
        finally:
            await pg.close_pool()


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"format": "html", "metadata": '{"ingested_format":"slides"}'}, "html"),
        ({"metadata": '{"ingested_format":"slides","source_revision":"r1"}'}, "slides"),
        (
            {"metadata": '{"ingested_format":7}', "blob_content_type": "application/pdf"},
            "application/pdf",
        ),
        ({"original_filename": "Uploaded.DOCX"}, ".docx"),
        ({}, ".pdf"),
        ({"source_uri": "gdrive:file#native#" + "1" * 36}, "(none)"),
        ({"metadata": "not json"}, ".pdf"),
        ({"metadata": '{"ingested_format":true,"source_revision":7}'}, ".pdf"),
        ({"metadata": '{"ingested_format":{"x":1},"source_revision":[1,"a"]}'}, ".pdf"),
        ({"metadata": '{"ingested_format":[1],"source_revision":false}'}, ".pdf"),
        ({"metadata": '{"ingested_format":null,"source_revision":null}'}, ".pdf"),
    ],
)
async def test_title_projection_preserves_scalar_format_rules_at_exact_identity(fields, expected):
    async with document(**fields) as (store, row):
        args = row["org"], row["namespace"], row["source_uri"], 17, 0
        got = await store.title_projection(*args)
        assert got is not None and set(got) == {
            "org",
            "namespace",
            "source_uri",
            "version",
            "revision",
            "title",
            "ingested_at",
            "superseded_at",
            "doc_format",
            "source_scheme",
            "source_revision",
        }
        assert got["doc_format"] == expected and got["title"] == row["title"]
        assert got["source_uri"] == row["source_uri"]
        assert got["source_scheme"] == (
            "https" if row["source_uri"].startswith("https://") else "(none)"
        )
        assert (got["version"], got["revision"]) == (17, 0)
        assert got["ingested_at"].replace(tzinfo=UTC) == row["ingested_at"]
        try:
            metadata = json.loads(row["metadata"])
        except ValueError:
            metadata = {}
        revision = metadata.get("source_revision", "")
        assert got["source_revision"] == (revision if isinstance(revision, str) else "")
        assert await store.title_projection(*args[:3], 18, 0) is None
        assert await store.title_projection(*args[:3], 17, 1) is None
        assert await store.title_projection(row["org"] + "-foreign", *args[1:]) is None


@pytest.mark.parametrize("unrelated", [r'"\u0000"', r'"\ud800"', "1e1000000"])
async def test_postgres_rejects_unsupported_metadata_without_losing_the_title(unrelated):
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg

    if not pg.enabled():
        pytest.skip("Postgres metadata conversion")
    metadata = '{"ingested_format":"slides","source_revision":"r1","unused":' + unrelated + "}"
    async with document(metadata=metadata) as (store, row):
        got = await store.title_projection(row["org"], row["namespace"], row["source_uri"], 17, 0)
        assert got is not None and got["title"] == row["title"]
        assert got["doc_format"] == ".pdf" and got["source_revision"] == ""
