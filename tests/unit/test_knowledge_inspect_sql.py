"""knowledge.inspect's statements, run on clickhouse-local against the real tables.

One namespace holds a document written twice (an older row of it collapses under
FINAL, its first version's chunks retired), a PDF with a query string in its URI, a
deleted document, a site URI whose host is not an extension, a document with no chunks,
one embedded under another model, one with two live versions and one unembedded chunk;
another namespace holds a document that must not be counted. Decision rows cite the
documents' sections, one through a longer URI that starts with a shorter one's.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.knowledge.store import inspect as store
from server.memory.store.document_decisions import decisions_by_document
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

MODEL = "embed-a"


def _doc(uri: str, version: int, **kw: Any) -> str:
    cols = {
        "org": "'o'",
        "namespace": f"'{kw.pop('ns', 'ns')}'",
        "source_uri": f"'{uri}'",
        "title": f"'{uri} title'",
        "content": "'text'",
        "version": str(version),
        "ingested_at": f"toDateTime('2026-01-0{version} 00:00:00')",
        **{k: f"'{v}'" if isinstance(v, str) else str(v) for k, v in kw.items()},
    }
    return f"INSERT INTO loom.documents ({', '.join(cols)}) VALUES ({', '.join(cols.values())})"


def _chunk(
    uri: str,
    version: int,
    idx: int,
    text: str,
    *,
    model: str = MODEL,
    dim: int = 3072,
    keys: str = "[]",
) -> str:
    return (
        "INSERT INTO loom.chunks (org, namespace, source_uri, chunk_idx, content, doc_version, "
        f"embedding_model, embedding_dim, embedding, keys) VALUES ('o', 'ns', '{uri}', {idx}, "
        f"'{text}', {version}, '{model}', {dim}, arrayWithConstant(3072, toBFloat16(0)), "
        f"{keys})"
    )


# A document knowledge.ingest published as revision 2 (chunk version 6, with keys). Its
# version 5 chunks are still live (an insert it could not confirm), and version 7 is an
# attempt that never published: retired, numbered by no revision.
INGEST = "files/report.pdf"


SETUP = [
    "SET enable_quantized_codec = 1",
    ddl("documents"),
    ddl("chunks"),
    ddl("chunk_supersessions"),
    _doc("docs/a.md", 1),
    _doc("docs/a.md", 2, revision=2),
    _doc(
        INGEST,
        6,
        revision=2,
        format="pdf",
        blob_content_type="application/pdf",
        ingestion_id="job-1",
        parser="ref/1",
        artifact_sha256="a" * 64,
    ),
    _doc(
        "https://x.example/r/report.pdf?dl=1",
        3,
        blob_content_type="application/pdf",
        original_filename="report.pdf",
    ),
    _doc("gdrive://abc", 1, superseded_at="2026-01-05 00:00:00"),
    _doc("https://example.com", 1),
    _doc("docs/empty.md", 1),
    _doc("docs/old.md", 1),
    _doc("docs/twice.md", 4),
    _doc("docs/elsewhere.md", 1, ns="other"),
    # Under the server's model, but with no vector: unembedded, not under another model.
    _doc("docs/flat.md", 1),
    # A deleted document that extends docs/a.md with '#'.
    _doc("docs/a.md#v2", 1, superseded_at="2026-01-05 00:00:00"),
    _chunk("docs/a.md", 1, 0, "first text"),
    _chunk("docs/a.md", 2, 0, "0123456789"),
    _chunk("docs/a.md", 2, 1, "x" * 30),
    _chunk("docs/a.md", 2, 2, "abc", dim=0),
    _chunk("https://x.example/r/report.pdf?dl=1", 3, 0, "pdf text"),
    _chunk("https://example.com", 1, 0, "site"),
    _chunk("docs/old.md", 1, 0, "old model", model="embed-old"),
    _chunk("docs/twice.md", 1, 0, "one"),
    _chunk("docs/twice.md", 4, 0, "two"),
    _chunk("docs/flat.md", 1, 0, "flat", dim=0),
    _chunk(INGEST, 5, 0, "Sheet1 v1", keys="['Sheet1']"),
    _chunk(INGEST, 6, 0, "Sheet1 v2", keys="['Sheet1']"),
    _chunk(INGEST, 7, 0, "Sheet1 v3", keys="['Sheet1']"),
    "INSERT INTO loom.chunk_supersessions (org, namespace, embedding_model, source_uri, "
    "doc_version, superseded_at) VALUES ('o', 'ns', 'embed-a', 'docs/a.md', 1, now()), "
    "('o', 'ns', 'embed-a', 'docs/a.md', 1, now()), ('o', 'ns', 'embed-a', 'gdrive://abc', 1, now()), "
    f"('o', 'ns', 'embed-a', '{INGEST}', 7, now())",
]


class _Local:
    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(SETUP, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        rows = run(SETUP, sql, params)
        return rows[0] if rows else None


def _go(coro: Any) -> Any:
    return asyncio.run(coro)


def test_documents_group_by_format_and_scheme_under_final() -> None:
    rows = _go(store.document_groups("o", "ns", ch=_Local()))
    got = {(r["doc_format"], r["source_scheme"]): (int(r["live"]), int(r["deleted"])) for r in rows}
    assert got == {
        (".md", "(none)"): (5, 1),  # docs/a.md once, despite its two rows
        ("pdf", "(none)"): (1, 0),  # the row's own format before its content type
        ("application/pdf", "https"): (1, 0),
        ("(none)", "gdrive"): (0, 1),
        ("(none)", "https"): (1, 0),  # the host's ".com" is not an extension
    }


def test_a_synced_document_counts_under_the_format_its_source_converted() -> None:
    # A source sync writes through knowledge.upsert, which sets no `format`; it records the
    # converted format in the row's metadata. A recording's URI has no extension.
    meet = "https://meet.googleapis.com/v2/conferenceRecords/abc"
    rows = run(
        [
            ddl("documents"),
            _doc(meet, 1, metadata='{"ingested_format":"transcript"}'),
            _doc("gdrive://doc/1", 1, metadata='{"ingested_format":"markdown"}'),
            _doc("files/own.pdf", 1, format="pdf", metadata='{"ingested_format":"markdown"}'),
            _doc(
                "gdrive://file/2",
                1,
                blob_content_type="application/pdf",
                metadata='{"ingested_format":"pdf"}',
            ),
        ],
        f"SELECT source_uri, {store.FORMAT_SQL} AS f FROM loom.documents FINAL ORDER BY source_uri",
    )
    assert {r["source_uri"]: r["f"] for r in rows} == {
        meet: "transcript",
        "gdrive://doc/1": "markdown",
        "files/own.pdf": "pdf",  # the row's own format first
        "gdrive://file/2": "pdf",  # a synced upload: the converted format before its type
    }


def test_revisions_come_from_the_rows() -> None:
    rows = _go(store.document_groups("o", "ns", ch=_Local()))
    # docs/a.md and the ingested document are each at revision 2; the rest are unnumbered.
    assert sum(int(r["earlier_revisions"]) for r in rows) == 2
    assert sum(int(r["unnumbered"]) for r in rows) == 8


def test_retired_versions_count_each_document_version_once() -> None:
    # docs/a.md's first version, the deleted gdrive document's, and the ingest attempt
    # that never published, which no revision numbers.
    assert _go(store.retired_versions("o", "ns", ch=_Local())) == 3
    assert _go(store.document_retired_versions("o", "ns", [INGEST], ch=_Local())) == {INGEST: 1}
    assert _go(store.document_retired_versions("o", "ns", ["docs/a.md", "x"], ch=_Local())) == {
        "docs/a.md": 1
    }


def test_chunk_models_count_live_and_embedded_chunks() -> None:
    rows = _go(store.chunk_models("o", "ns", ch=_Local()))
    assert {r["model"]: (int(r["live"]), int(r["embedded"])) for r in rows} == {
        MODEL: (10, 8),
        "embed-old": (1, 1),
    }


@pytest.mark.parametrize("nulls", [False, True])
def test_chunk_health_names_the_documents_search_cannot_serve(nulls: bool) -> None:
    class _Nulls(_Local):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
            return run(["SET join_use_nulls = 1", *SETUP], sql, params)

    h = _go(store.chunk_health("o", "ns", MODEL, ch=_Nulls() if nulls else _Local()))
    assert int(h["live_docs"]) == 8
    assert (int(h["without_chunks"]), h["without_chunks_uris"]) == (1, ["docs/empty.md"])
    assert int(h["unembedded"]) == 2
    assert sorted(h["unembedded_uris"]) == ["docs/a.md", "docs/flat.md"]
    # docs/flat.md has no searchable chunk, but its chunk is under the server's model.
    assert (int(h["other_model"]), h["other_model_uris"]) == (1, ["docs/old.md"])
    assert int(h["mixed_versions"]) == 2
    assert sorted(h["mixed_versions_uris"]) == ["docs/twice.md", INGEST]
    # The ingested document is named apart, for its knowledge.ingest remedy.
    assert (int(h["mixed_versions_ingested"]), h["mixed_versions_ingested_uris"]) == (1, [INGEST])
    assert int(h["without_chunks_ingested"]) == 0 and h["other_model_ingested_uris"] == []


def test_newest_documents_are_the_live_ones_newest_first() -> None:
    rows = _go(store.newest_documents("o", "ns", limit=3, ch=_Local()))
    assert [(r["source_uri"], int(r["version"])) for r in rows] == [
        (INGEST, 6),
        ("docs/twice.md", 4),
        ("https://x.example/r/report.pdf?dl=1", 3),
    ]
    ingested, _, pdf = rows
    assert (ingested["doc_format"], int(ingested["revision"]), ingested["ingestion_id"]) == (
        "pdf",
        2,
        "job-1",
    )
    assert pdf["doc_format"] == "application/pdf" and pdf["source_scheme"] == "https"
    assert (int(pdf["revision"]), pdf["ingestion_id"]) == (0, "")


def test_document_chunks_measure_the_live_chunks_of_the_listed_documents() -> None:
    rows = _go(
        store.document_chunks(
            "o",
            "ns",
            ["docs/a.md", "docs/old.md", "docs/empty.md", "docs/flat.md"],
            MODEL,
            ch=_Local(),
        )
    )
    by = {r["source_uri"]: r for r in rows}
    assert set(by) == {"docs/a.md", "docs/old.md", "docs/flat.md"}
    # The health read and the listing agree: an unembedded chunk under the server's model
    # is not under another model.
    flat = by["docs/flat.md"]
    assert (int(flat["unembedded"]), int(flat["other_model"]), int(flat["searchable"])) == (1, 0, 0)
    a = by["docs/a.md"]
    assert (int(a["live"]), int(a["searchable"]), int(a["unembedded"]), int(a["versions"])) == (
        3,
        2,
        1,
        1,
    )
    assert (int(a["min_chars"]), int(a["median_chars"]), int(a["max_chars"])) == (3, 10, 30)
    old = by["docs/old.md"]
    assert (int(old["other_model"]), int(old["searchable"]), old["models"]) == (1, 0, ["embed-old"])


def test_longer_documents_are_the_ones_extending_a_listed_uri_with_a_hash() -> None:
    got = _go(store.longer_document_uris("o", "ns", ["docs/a.md", "docs/old.md"], ch=_Local()))
    assert got == ["docs/a.md#v2"]  # deleted, still a document a citation can name
    assert _go(store.longer_document_uris("o", "ns", [], ch=_Local())) == []


def _decision(key: str, resource: str, **kw: Any) -> str:
    cols = {
        "org": "'o'",
        "namespace": f"'{kw.pop('ns', 'ns')}'",
        "agent": "'loom-decisions'",
        "user_id": f"'{kw.pop('user_id', '-')}'",
        "session_id": "'-'",
        "memory_key": f"'{key}'",
        "memory_type": "'decision'",
        "value": "'v'",
        "version": "1",
        "source_resource_id": f"'{resource}'",
        **{k: str(v) for k, v in kw.items()},
    }
    return f"INSERT INTO loom.memory ({', '.join(cols)}) VALUES ({', '.join(cols.values())})"


SHA = "a" * 64
DECISIONS = [
    ddl("memory"),
    _decision("d1", "knowledge:ns/docs/a.md#Intro", pinned=1),
    _decision(
        "d2",
        "knowledge:ns/docs/a.md#v2#Intro",
        content_sha256=f"'{SHA}'",
        verified_by="'human:x'",
        verified_sha256=f"'{SHA}'",
    ),
    _decision("d3", "knowledge:ns/docs/a.md#Plan", tags="['decision', 'source-changed']"),
    _decision("d4", "knowledge:ns/docs/other.md#S", tags="['decision:outcome-known']"),
    _decision("d5", "knowledge:ns/docs/a.md#Gone", tombstoned_at="now()"),
    _decision("d6", "knowledge:ns/docs/a.md#U2", user_id="u2"),
    _decision("d7", "knowledge:other/docs/a.md#Intro"),
    _decision("d8", "knowledge:ns/docs/a.md#Intro", ns="other"),
    _decision("n1", "knowledge:ns/docs/a.md#Intro", memory_type="'semantic'"),
    _decision("n2", "knowledge:ns/docs/a.md#Intro", memory_type="'semantic'", tags="['decision']"),
    # A proposal: pinned, and its document has since changed.
    _decision(
        "p1",
        "knowledge:ns/docs/a.md#Plan",
        pinned=1,
        tags="['decision', 'decision:proposed', 'source-changed']",
    ),
    _decision("p2", "knowledge:ns/docs/a.md#Plan", tags="['decision', 'decision:proposed']"),
]


class _Memory:
    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(DECISIONS, sql, params)


@pytest.mark.parametrize(
    ("scope", "a_decided"),
    [
        (WHOLE_NAMESPACE, 4),  # d1, d3, d6 and the tagged n2
        (ReadFilters(scope="own_user", user_id="u2", session_id=None), 1),
    ],
)
def test_decisions_go_to_the_longest_uri_their_citation_names_by_stage(
    scope: ReadFilters, a_decided: int
) -> None:
    rows = _go(
        decisions_by_document("o", "ns", ["docs/a.md", "docs/a.md#v2"], scope=scope, ch=_Memory())
    )
    by = {(r["source_uri"], int(r["proposed"])): r for r in rows}
    assert int(by["docs/a.md", 0]["total"]) == a_decided
    if scope is WHOLE_NAMESPACE:
        a, v2, rest = by["docs/a.md", 0], by["docs/a.md#v2", 0], by["", 0]
        assert (int(a["confirmed"]), int(a["source_changed"])) == (1, 1)  # d1 is pinned
        assert (int(v2["total"]), int(v2["confirmed"])) == (1, 1)  # attested in its content
        assert (int(rest["total"]), int(rest["outcome_known"])) == (1, 1)
        # The two proposals are their own row: one pinned, one source-changed.
        p = by["docs/a.md", 1]
        assert (int(p["total"]), int(p["confirmed"]), int(p["source_changed"])) == (2, 1, 1)
        assert set(by) == {("docs/a.md", 0), ("docs/a.md", 1), ("docs/a.md#v2", 0), ("", 0)}
    else:
        assert set(by) == {("docs/a.md", 0)}
