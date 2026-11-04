"""Structural knowledge search end to end, against a running server and its ClickHouse:
files are uploaded and ingested (the worker pass runs in this process, the extractor
at LOOM_DOCUMENT_EXTRACTOR_URL), then knowledge.search reads them back.

- An exact identifier (a table a SQL statement creates, a TOML key path) finds the
  chunk that defines it through the exact-key leg, with its keys, locator, block type,
  format and revision.
- `formats` and `block_types` keep only chunks of those documents and blocks.
- A prose question whose words are SQL column names (`status`, `name`) still finds the
  document that answers it first, with no keys matched.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

SQL = (
    b"-- Raw events are kept for 30 days.\n"
    b"CREATE TABLE events (id UInt64, ts DateTime) ENGINE = MergeTree ORDER BY id\n"
    b"TTL ts + INTERVAL 30 DAY;\n\n"
    b"CREATE TABLE sessions (id UInt64) ENGINE = MergeTree ORDER BY id;\n\n"
    b"SELECT count() FROM events;\n"
)
TOML = b"# Service limits.\n[server]\nport = 7777\nworkers = 4\n\n[retention]\ndays = 30\n"


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    r = await c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _ingest(c: httpx.AsyncClient, org: str, ns: str, data: bytes, name: str) -> None:
    from server.core.ch import get_client
    from server.knowledge.workers import ingest

    up = await c.post(
        "/v1/knowledge.upload",
        params={
            "org": org,
            "namespace": ns,
            "filename": name,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert up.status_code == 201, up.text
    job = await _op(
        c,
        "knowledge.ingest",
        org=org,
        namespace=ns,
        upload_id=up.json()["upload_id"],
        source_uri=f"repo://{name}",
    )
    ch = get_client()
    await ch.connect()
    try:
        for _ in range(20):  # one job per pass; other tests' jobs may be due too
            await ingest.run_pass()
            status = await _op(
                c,
                "knowledge.ingest_status",
                org=org,
                namespace=ns,
                ingestion_id=job["ingestion_id"],
            )
            if status["state"] not in ("pending", "processing"):
                break
    finally:
        await ch.disconnect()
    assert status["state"] == "ready", status


@pytest.mark.asyncio
async def test_an_exact_identifier_finds_the_chunk_that_defines_it(control_plane_url: str) -> None:
    org, ns = unique_org("structural"), f"docs-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _ingest(c, org, ns, SQL, "retention.sql")
        await _ingest(c, org, ns, TOML, "config.toml")

        found = await _op(
            c,
            "knowledge.search",
            org=org,
            namespace=ns,
            query="sessions",
            namespace_scope="this",
            collapse_sections=False,
        )
        top = found["results"][0]
        assert (top["source_uri"], top["format"], top["revision"]) == (
            "repo://retention.sql",
            "sql",
            1,
        )
        assert "CREATE TABLE sessions" in top["content"] and "sessions" in top["keys_matched"]
        assert top["block_type"] == "sql_statement" and top["locator"]["line_start"] >= 1

        port = await _op(
            c,
            "knowledge.search",
            org=org,
            namespace=ns,
            query="server.port",
            namespace_scope="this",
            collapse_sections=False,
        )
        top = port["results"][0]
        assert top["source_uri"] == "repo://config.toml" and "server.port" in top["keys_matched"]


@pytest.mark.asyncio
async def test_format_and_block_type_filters_keep_only_those_chunks(
    control_plane_url: str,
) -> None:
    org, ns = unique_org("structural-filter"), f"docs-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _ingest(c, org, ns, SQL, "retention.sql")
        await _ingest(c, org, ns, TOML, "config.toml")
        await _op(
            c,
            "knowledge.add",
            org=org,
            namespace=ns,
            source_uri="kb://notes",
            content="Retention notes: raw events and sessions are kept for 30 days.",
        )

        async def uris(**filters: Any) -> set[tuple[str, str, str]]:
            out = await _op(
                c,
                "knowledge.search",
                org=org,
                namespace=ns,
                query="how long are events kept",
                namespace_scope="this",
                collapse_sections=False,
                top_k=20,
                **filters,
            )
            return {(r["source_uri"], r["format"], r["block_type"]) for r in out["results"]}

        everything = await uris()
        assert {u for u, _, _ in everything} == {
            "repo://retention.sql",
            "repo://config.toml",
            "kb://notes",
        }
        assert {u for u, _, _ in await uris(formats=["sql"])} == {"repo://retention.sql"}
        assert {u for u, _, _ in await uris(formats=[""])} == {"kb://notes"}
        # A SQL statement is a chunk of its own; a TOML table packs with its keys (`mixed`).
        statements = await uris(block_types=["sql_statement"])
        assert statements and {(u, b) for u, _, b in statements} == {
            ("repo://retention.sql", "sql_statement")
        }


@pytest.mark.asyncio
async def test_a_prose_question_is_not_pulled_to_columns_that_share_its_words(
    control_plane_url: str,
) -> None:
    """SQL chunks' keys include their columns, which are often common words (`status`,
    `name`, `value`): a prose question using those words still finds the document
    that answers it first, since only identifier-shaped terms are keys."""
    sql = (
        b"CREATE TABLE orders (id UInt64, status String, name String, value Float64)"
        b" ENGINE = MergeTree ORDER BY id;\n\n"
        b"SELECT status, count() FROM orders GROUP BY status;\n"
    )
    org, ns = unique_org("structural-prose"), f"docs-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _ingest(c, org, ns, sql, "orders.sql")
        await _op(
            c,
            "knowledge.add",
            org=org,
            namespace=ns,
            source_uri="kb://refunds",
            title="Refund policy",
            content=(
                "The status of a refund request shows whether finance approved it; the"
                " customer name and the order value are copied onto the refund."
            ),
        )
        found = await _op(
            c,
            "knowledge.search",
            org=org,
            namespace=ns,
            query="what does the status of a refund request show",
            namespace_scope="this",
        )
        assert found["results"][0]["source_uri"] == "kb://refunds"
        assert all(not r["keys_matched"] for r in found["results"])
