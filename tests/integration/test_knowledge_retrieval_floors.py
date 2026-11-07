"""Retrieval floors per slice, end to end on the server's ClickHouse: a corpus of
Markdown, SQL, TOML, CSV, HTML and PDF files is uploaded and ingested (the job and
the reference extractor run in this process, as a knowledge_ingest worker pass
runs one), then knowledge.search answers labelled queries in three slices:

- exact identifiers: an error code, a table, a key path or a form number;
- table questions: a row of a CSV table cut into several blocks, found by its
  values in its own block, and a row of tables beside others with the same columns;
- filtered recall: with `formats`, `block_types` or `tags` set, every hit is inside
  the filter, among other documents the filter keeps.

Every query's target must be the first hit.

The floors are the "Retrieval floors" table of docs/architecture/document-extraction.md,
read strictly (tests/fixtures/floor_tables.py). They are measured with the fake
n-gram embedder the integration tests run, in the server and in this process (which
embeds the chunks), so the ranking is the same on every run; with any other embedder
the test skips.
"""

from __future__ import annotations

import functools
import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fixtures.floor_tables import read_floors
from tests.fixtures.office_files import pdf
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")

DOC = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "document-extraction.md"
TOP_K = 5  # the hits each query reads: the first is scored, all are filter-checked

RUNBOOK = b"""# Runbook: replication errors

## E4031: replica lag over budget

The replica falls more than 30 seconds behind its leader.

Drain the replica, then restart it; it rejoins on its own.

## LT-2207: lease timeout

A writer lost its lease. Retry the write after five seconds.

## M-41: merge stalled

Merges stopped on one shard. Start them again on that shard.
"""

SCHEMA = b"""-- Invoices, one row per issued invoice.
CREATE TABLE billing_invoices (id UInt64, customer String, total Decimal(18, 2), issued Date)
ENGINE = MergeTree ORDER BY (customer, issued);

-- Totals per customer and month.
CREATE MATERIALIZED VIEW invoice_totals ENGINE = SummingMergeTree ORDER BY (customer, month)
AS SELECT customer, toStartOfMonth(issued) AS month, sum(total) AS total
FROM billing_invoices GROUP BY customer, month;
"""

CONFIG = b"""# Upload limits.
[ingest]
max_parallel_uploads = 8
chunk_bytes = 65536

[search.rerank]
model = "cross-small"
"""

DEPLOY = b"""# Runbook: deploys

## Restarting a service

Restart the API pods one at a time, waiting for each to report ready.

## Rolling back

Roll back by deploying the previous image tag.
"""

ORDERS = b"""-- Orders, one row per order; an invoice is issued for each.
CREATE TABLE sales_orders (id UInt64, customer String, total Decimal(18, 2), placed Date)
ENGINE = MergeTree ORDER BY (customer, placed);

-- Orders per customer and day.
CREATE MATERIALIZED VIEW order_counts ENGINE = SummingMergeTree ORDER BY (customer, day)
AS SELECT customer, toDate(placed) AS day, count() AS n FROM sales_orders GROUP BY customer, day;
"""

# 30 regions: the CSV extracts as two blocks of rows (1-18 and 19-30), so a row
# is found in its own block.
REGION_ROWS = [(f"zone-{n:02d}", f"team-{chr(96 + n % 26 + 1)}{n}", n * 8) for n in range(1, 31)]
REGIONS = (
    "region,owner,storage_limit_gb\n" + "".join(f"{r},{o},{g}\n" for r, o, g in REGION_ROWS)
).encode()
# The same columns, for other regions.
SITES = b"region,owner,storage_limit_gb\nwest-1,Ops,96\nwest-2,Data,320\neast-1,Platform,64\n"


def _plans(title: str, rows: list[tuple[str, int, int]]) -> bytes:
    cells = "".join(f"<tr><td>{p}</td><td>{s}</td><td>{m}</td></tr>" for p, s, m in rows)
    return (
        f"<html><body><h1>{title}</h1><table>"
        "<tr><th>Plan</th><th>Seats</th><th>Monthly price</th></tr>"
        f"{cells}</table></body></html>"
    ).encode()


PRICING = _plans("Plans", [("Starter", 5, 49), ("Scale", 50, 399)])
SUPPORT = _plans("Support plans", [("Standard", 10, 99), ("Priority", 100, 990)])

ONBOARDING = pdf(
    [
        "Onboarding\nBadge requests use form FAC-7713 at the facilities desk.",
        "Laptops\nA laptop is issued on the first day and returned on the last.",
    ],
    [("Onboarding", 0, []), ("Laptops", 1, [])],
)

# (file name, bytes, tags)
CORPUS: list[tuple[str, bytes, list[str]]] = [
    ("runbook.md", RUNBOOK, ["runbook"]),
    ("deploy.md", DEPLOY, ["runbook"]),
    ("schema.sql", SCHEMA, ["billing"]),
    ("orders.sql", ORDERS, ["billing"]),
    ("config.toml", CONFIG, ["config"]),
    ("regions.csv", REGIONS, ["capacity"]),
    ("sites.csv", SITES, ["capacity"]),
    ("pricing.html", PRICING, ["billing"]),
    ("support.html", SUPPORT, ["support"]),
    ("onboarding.pdf", ONBOARDING, ["handbook"]),
]
# Text writes, with no format, beside the ingested files.
NOTES = [
    ("kb://invoice-retention", "Retention: invoices are kept for seven years, then deleted."),
    ("kb://ticket-retention", "Retention: support tickets are kept for two years, then deleted."),
    ("kb://log-retention", "Retention: audit logs are kept for one year, then deleted."),
]


@dataclass(frozen=True)
class Query:
    """A labelled query: the source it must find, text the hit must show (in its
    passage or its headings), and the filters it runs with."""

    slice: str
    text: str
    source: str
    holds: str
    filters: dict[str, Any] = field(default_factory=dict)


QUERIES = [
    Query("exact identifiers", "E4031", "repo://runbook.md", "E4031"),
    Query("exact identifiers", "LT-2207", "repo://runbook.md", "LT-2207"),
    Query("exact identifiers", "M-41", "repo://runbook.md", "M-41"),
    Query(
        "exact identifiers",
        "billing_invoices",
        "repo://schema.sql",
        "CREATE TABLE billing_invoices",
    ),
    Query("exact identifiers", "invoice_totals", "repo://schema.sql", "invoice_totals"),
    Query(
        "exact identifiers",
        "ingest.max_parallel_uploads",
        "repo://config.toml",
        "max_parallel_uploads",
    ),
    Query("exact identifiers", "FAC-7713", "repo://onboarding.pdf", "FAC-7713"),
    Query("table questions", "zone-25 storage limit", "repo://regions.csv", "region: zone-25;"),
    Query("table questions", "zone-03 owner", "repo://regions.csv", "region: zone-03;"),
    Query("table questions", "west-2 storage limit", "repo://sites.csv", "region: west-2;"),
    Query("table questions", "Scale plan monthly price", "repo://pricing.html", "Scale | 50 | 399"),
    Query(
        "table questions",
        "Priority support plan seats",
        "repo://support.html",
        "Priority | 100 | 990",
    ),
    Query(
        "filtered recall",
        "invoice totals per customer and month",
        "repo://schema.sql",
        "invoice_totals",
        {"formats": ["sql"]},
    ),
    Query(
        "filtered recall",
        "how long are invoices kept",
        NOTES[0][0],
        "seven years",
        {"formats": [""]},
    ),
    Query(
        "filtered recall",
        "restart a replica that fell behind",
        "repo://runbook.md",
        "restart",
        {"formats": ["markdown"]},
    ),
    Query(
        "filtered recall",
        "orders per customer and day",
        "repo://orders.sql",
        "order_counts",
        {"block_types": ["sql_statement"]},
    ),
    Query("filtered recall", "Scale plan", "repo://pricing.html", "Scale", {"tags": ["billing"]}),
]


@functools.cache
def _floors() -> dict[tuple[str, ...], float]:
    return read_floors(DOC.read_text(), "## Retrieval floors", 3)


@pytest.fixture
def reference_extractor(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reference extractor in this process, as the job's only service."""
    from loom_extractor import service
    from server.knowledge.app import extractors
    from server.settings import settings

    token = "t" * 24
    app = service.create_app(service.Config(token=token))
    monkeypatch.setattr(extractors, "transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(settings, "document_extractor_url", "http://extractor:8790")
    monkeypatch.setattr(settings, "document_extractors_json", "{}")
    monkeypatch.setattr(settings, "document_extractor_token", token)
    monkeypatch.setattr(settings, "document_extractor_tokens_json", "{}")
    extractors._declared.clear()
    yield
    extractors._declared.clear()


async def _op(c: httpx.AsyncClient, op: str, **body: Any) -> dict[str, Any]:
    r = await c.post(f"/v1/{op}", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _ingest(
    c: httpx.AsyncClient, org: str, ns: str, name: str, data: bytes, tags: list[str]
) -> None:
    from server.core.ch import get_client
    from server.knowledge.app import ingest_run
    from server.knowledge.store import ingestions

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
        tags=tags,
    )
    ch = get_client()
    await ch.connect()
    try:
        row = await ingestions.get_ingestion(org, ns, job["ingestion_id"])
        assert row is not None
        assert await ingest_run.take_and_run(row) == "ready", name
    finally:
        await ch.disconnect()


def _inside(hit: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Whether a hit is inside every filter the query set."""
    return (
        ("formats" not in filters or hit["format"] in filters["formats"])
        and ("block_types" not in filters or hit["block_type"] in filters["block_types"])
        and ("tags" not in filters or bool(set(hit["tags"]) & set(filters["tags"])))
    )


def _text(hit: dict[str, Any]) -> str:
    """What a hit shows: its passage and the headings it sits under (an error code
    is often its section's heading, not the passage's text)."""
    return "\n".join([*hit["section_path"], hit["content"]])


def _measure(
    results: list[tuple[Query, list[dict[str, Any]]]],
) -> dict[tuple[str, str], tuple[float, list[str]]]:
    """Per (slice, metric): the share of its queries that pass, and the ones that
    do not. `found first`: the first hit is from the source and shows the expected
    text; `kept in filter`: every hit inside the query's filters."""
    tally: dict[tuple[str, str], list[tuple[bool, str]]] = {}
    for q, hits in results:
        found = bool(hits) and hits[0]["source_uri"] == q.source and q.holds in _text(hits[0])
        tally.setdefault((q.slice, "found first"), []).append((found, q.text))
        if q.filters:
            kept = all(_inside(h, q.filters) for h in hits)
            tally.setdefault((q.slice, "kept in filter"), []).append((kept, q.text))
    return {
        key: (sum(ok for ok, _ in v) / len(v), [t for ok, t in v if not ok])
        for key, v in tally.items()
    }


@pytest.mark.usefixtures("reference_extractor")
@pytest.mark.asyncio
async def test_each_retrieval_slice_meets_its_floor(control_plane_url: str) -> None:
    from server.embedding.embedder import embedder_is_fake
    from server.settings import settings

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        health = (await c.get("/v1/health")).json()
    # The server embeds the queries and this process the chunks: the same fake model.
    same = (health.get("embedding_model"), health.get("embedding_dim_configured")) == (
        settings.embedding_model,
        settings.embedding_dim,
    )
    if not (health.get("fake_embedding") and embedder_is_fake() and same):
        pytest.skip("the floors are measured with one fake n-gram embedder, here and in the server")
    org, ns = unique_org("retrieval-floors"), f"docs-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        for name, data, tags in CORPUS:
            await _ingest(c, org, ns, name, data, tags)
        for uri, text in NOTES:
            await _op(c, "knowledge.add", org=org, namespace=ns, source_uri=uri, content=text)
        results = []
        for q in QUERIES:
            out = await _op(
                c,
                "knowledge.search",
                org=org,
                namespace=ns,
                query=q.text,
                namespace_scope="this",
                collapse_sections=False,
                top_k=TOP_K,
                **q.filters,
            )
            results.append((q, out["results"]))
    measured = _measure(results)
    floors = _floors()
    assert set(floors) == set(measured), (
        f"rows no query measures: {sorted(set(floors) - set(measured))};"
        f" measured with no row: {sorted(set(measured) - set(floors))}"
    )
    under = [
        f"{' / '.join(key)}: {measured[key][0]:.3f} under {floor}, missed {measured[key][1]}"
        for key, floor in floors.items()
        if measured[key][0] < floor
    ]
    assert not under, "\n".join(under)
