"""namespace.tune's document statements, run on clickhouse-local against the real tables.

The sample picks live documents by a hash of their source_uri, the same pick on every
read, reads a text document's text only when it is short enough and never an ingested
one's (its artifact is read instead), and folds a document written twice under FINAL.
The writers count documents per agent. The per-format reads find the sources whose current
job failed format_unavailable (not one a later write replaced, not one whose newer job
published) with what their format is read from, and the published extractions per format
with what they lost. Another namespace's rows are not counted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.knowledge.store import inspect as inspect_store
from server.knowledge.store import tune as store
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

LOST = '{"complete": false, "extracted": 8, "failed": [], "lost": ["sql_structure"], ' + (
    '"total": 9, "unit": "statements"}'
)
WHOLE = '{"complete": true, "extracted": 4, "failed": [], "lost": [], "total": 4, "unit": "keys"}'


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _doc(uri: str, **kw: Any) -> str:
    cols = {
        "org": "'o'",
        "namespace": f"'{kw.pop('ns', 'ns')}'",
        "source_uri": f"'{uri}'",
        "content": f"'{kw.pop('content', 'text of ' + uri)}'",
        "version": "1",
        **{k: f"'{v}'" if isinstance(v, str) else str(v) for k, v in kw.items()},
    }
    return f"INSERT INTO loom.documents ({', '.join(cols)}) VALUES ({', '.join(cols.values())})"


def _job(uri: str, n: int, state: str, *, age_s: int, **kw: Any) -> str:
    cols = {
        "org": "'o'",
        "namespace": f"'{kw.pop('ns', 'ns')}'",
        "ingestion_id": f"'{_id(n)}'",
        "source_uri": f"'{uri}'",
        "upload_id": f"'{_id(900 + n)}'",
        "sha256": f"'{'a' * 64}'",
        "state": f"'{state}'",
        "created_at": f"now64(3) - toIntervalSecond({age_s})",
        "updated_at": f"now64(3) - toIntervalSecond({age_s - 1})",
        "version": "1",
        **{k: f"'{v}'" if isinstance(v, str) else str(v) for k, v in kw.items()},
    }
    return (
        f"INSERT INTO loom.knowledge_ingestions ({', '.join(cols)}) "
        f"VALUES ({', '.join(cols.values())})"
    )


SETUP = [
    ddl("documents"),
    ddl("knowledge_ingestions"),
    ddl("knowledge_ingest_cancels"),
    *[_doc(f"docs/{i}.md", agent="sync") for i in range(8)],
    # Written twice: one live document under FINAL, with its later text.
    _doc("docs/0.md", version=2, content="new text", agent="sync"),
    _doc("docs/long.md", content="x" * 50, agent="bot"),
    _doc(
        "files/q.sql",
        format="sql",
        ingestion_id=_id(1),
        artifact_sha256="b" * 64,
        coverage=LOST,
        blob_content_type="application/sql",
        agent="bot",
    ),
    _doc("files/c.toml", format="toml", ingestion_id=_id(2), coverage=WHOLE, agent="bot"),
    # Published by a job and stored with no artifact: cut from its text, as a text write is.
    _doc("files/plain.md", ingestion_id=_id(30), content="plain words", agent="bot"),
    _doc("docs/gone.md", superseded_at="2026-01-05 00:00:00"),
    _doc("docs/other.md", ns="other"),
    # No extractor takes these: by file name, by media type, by the declared format.
    _job("files/a.pdf", 3, "failed", age_s=60, filename="a.pdf", error_code="format_unavailable"),
    _job(
        "files/b",
        4,
        "failed",
        age_s=61,
        filename="b",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        error_code="format_unavailable",
    ),
    _job(
        "files/c",
        5,
        "failed",
        age_s=62,
        filename="c.txt",
        options='{"format": "pptx"}',
        error_code="format_unavailable",
    ),
    # Failed, then a newer job published: its current job is the newer one.
    _job(
        "files/c.toml", 6, "failed", age_s=600, filename="c.toml", error_code="format_unavailable"
    ),
    _job("files/c.toml", 2, "ready", age_s=30, filename="c.toml"),
    # Failed, then replaced by a text write after it ended.
    _job("docs/7.md", 7, "failed", age_s=3600, filename="7.pdf", error_code="format_unavailable"),
    # Failed on the file itself: not the extractor's routing.
    _job("files/bad.sql", 8, "failed", age_s=63, filename="bad.sql", error_code="encoding"),
    _job("files/z.pdf", 9, "failed", age_s=64, ns="other", error_code="format_unavailable"),
]


class _Local:
    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(SETUP, sql, params)


def _go(coro: Any) -> Any:
    return asyncio.run(coro)


def test_the_sample_is_a_stable_pick_of_live_documents_by_hash() -> None:
    rows = _go(
        store.sample_documents("o", "ns", limit=4, max_chars=20, total_chars=1000, ch=_Local())
    )
    again = _go(
        store.sample_documents("o", "ns", limit=4, max_chars=20, total_chars=1000, ch=_Local())
    )
    assert [r["source_uri"] for r in rows] == [r["source_uri"] for r in again]
    every = _go(
        store.sample_documents("o", "ns", limit=100, max_chars=20, total_chars=1000, ch=_Local())
    )
    uris = [r["source_uri"] for r in every]
    # Every live document of the namespace once; not the deleted one, not another
    # namespace's.
    assert sorted(uris) == sorted(
        [
            *(f"docs/{i}.md" for i in range(8)),
            "docs/long.md",
            "files/q.sql",
            "files/c.toml",
            "files/plain.md",
        ]
    )
    # The pick of four is the first four of the hash order.
    assert [r["source_uri"] for r in rows] == uris[:4]
    by = {r["source_uri"]: r for r in every}
    assert by["docs/0.md"]["text"] == "new text"
    # Too long to sample: its length, no text.
    assert by["docs/long.md"]["text"] == "" and int(by["docs/long.md"]["chars"]) == 50
    # An ingested document: its artifact and format, never its text.
    assert by["files/q.sql"]["text"] == "" and by["files/q.sql"]["artifact_sha256"] == "b" * 64
    assert by["files/q.sql"]["doc_format"] == "sql"
    # A document with no artifact is read for its text, whoever published it.
    assert by["files/plain.md"]["text"] == "plain words"


def test_the_writers_count_live_documents_per_agent() -> None:
    rows = _go(store.document_writers("o", "ns", limit=1, ch=_Local()))
    assert [(r["agent"], int(r["rows"]), int(r["total"])) for r in rows] == [("sync", 8, 2)]


def test_the_sources_failed_for_their_format_carry_what_it_is_read_from() -> None:
    rows = _go(inspect_store.format_unavailable_sources("o", "ns", lease_s=120.0, ch=_Local()))
    got = {
        r["source_uri"]: (r["ingestion_id"], r["filename"], r["content_type"], r["declared_format"])
        for r in rows
    }
    assert got == {
        "files/a.pdf": (_id(3), "a.pdf", "", ""),
        "files/b": (
            _id(4),
            "b",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "",
        ),
        "files/c": (_id(5), "c.txt", "", "pptx"),
    }


def test_the_extractions_are_counted_per_format_with_what_they_lost() -> None:
    rows = _go(inspect_store.extraction_by_format("o", "ns", ch=_Local()))
    got = {
        r["doc_format"]: (
            int(r["documents"]),
            int(r["incomplete"]),
            r["incomplete_uris"],
            r["lost"],
            r["content_types"],
        )
        for r in rows
    }
    assert got == {
        "sql": (1, 1, ["files/q.sql"], ["sql_structure"], ["application/sql"]),
        "toml": (1, 0, [], [], [""]),
    }
    # Most incomplete first.
    assert rows[0]["doc_format"] == "sql"


def test_the_sample_reads_text_only_up_to_its_total() -> None:
    every = _go(
        store.sample_documents("o", "ns", limit=100, max_chars=20, total_chars=40, ch=_Local())
    )
    texts = [r for r in every if r["text"]]
    # In sample order, the texts read add up to no more than the total, and a text past it
    # is left out even when a shorter one after it would fit.
    assert texts and sum(int(r["chars"]) for r in texts) <= 40
    cut = [r for r in every if not r["artifact_sha256"] and int(r["chars"]) <= 20]
    assert [r["source_uri"] for r in texts] == [r["source_uri"] for r in cut][: len(texts)]
