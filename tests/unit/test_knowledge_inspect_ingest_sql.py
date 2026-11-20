"""knowledge.inspect's job and extraction statements, run on clickhouse-local against the
real tables.

One namespace's sources each sit in one state: a first ingest queued, an ingested document
with an older published job, a stalled run and a later job queued behind it, jobs failed or
retrying for the extractor, for access and on the file itself, a revision conflict, and a
store outage retried, an outage that used up its attempts, a defect, a retried job running
again, a pending job with a cancel, two failures a later write replaced (a text write, a
delete), one a metadata edit did not, one a reindex did and one a write in the job's last
second did not, and an artifact that broke the contract. A job row written twice collapses
under FINAL. Another namespace's job and cancel are not counted. Two ingested documents
carry an extraction's coverage, one that lost structure, and a text document carries none.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.knowledge.store import inspect as store
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

LEASE_S = 120.0
# One second for both rows of the same-second case, so the two statements cannot straddle a
# second boundary; recent, because the jobs table expires old rows.
SAME_SECOND = (datetime.now(UTC) - timedelta(seconds=100)).strftime("%Y-%m-%d %H:%M:%S")


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _job(uri: str, n: int, state: str, *, age_s: int, updated_s: int = 0, **kw: Any) -> str:
    """A job row queued `age_s` seconds ago and last changed `updated_s` seconds ago."""
    cols = {
        "org": "'o'",
        "namespace": f"'{kw.pop('ns', 'ns')}'",
        "ingestion_id": f"'{_id(n)}'",
        "source_uri": f"'{uri}'",
        "upload_id": f"'{_id(900 + n)}'",
        "sha256": f"'{'a' * 64}'",
        "state": f"'{state}'",
        "created_at": f"now64(3) - toIntervalSecond({age_s})",
        "updated_at": f"now64(3) - toIntervalSecond({updated_s})",
        "next_attempt_at": f"now64(3) - toIntervalSecond({updated_s})",
        "version": str(kw.pop("version", 1)),
        **{k: f"'{v}'" if isinstance(v, str) else str(v) for k, v in kw.items()},
    }
    return (
        f"INSERT INTO loom.knowledge_ingestions ({', '.join(cols)}) "
        f"VALUES ({', '.join(cols.values())})"
    )


def _doc(uri: str, **kw: Any) -> str:
    cols = {
        "org": "'o'",
        "namespace": "'ns'",
        "source_uri": f"'{uri}'",
        "content": "'text'",
        "version": "1",
        **{k: f"'{v}'" if isinstance(v, str) else str(v) for k, v in kw.items()},
    }
    return f"INSERT INTO loom.documents ({', '.join(cols)}) VALUES ({', '.join(cols.values())})"


REPORT = "files/report.sql"
LOST = (
    '{"complete": false, "extracted": 8, "failed": [{"reason": "parse", "unit": "s9"}], '
    '"lost": ["sql_structure", "sql_structure"], "total": 9, "unit": "statements"}'
)
WHOLE = '{"complete": true, "extracted": 4, "failed": [], "lost": [], "total": 4, "unit": "keys"}'

SETUP = [
    ddl("documents"),
    ddl("knowledge_ingestions"),
    ddl("knowledge_ingest_cancels"),
    _doc(REPORT, format="sql", ingestion_id=_id(2), coverage=LOST),
    # Published by job 20: a publish writes the row before the job's final save.
    _doc(
        "files/conf.toml",
        format="toml",
        ingestion_id=_id(20),
        coverage=WHOLE,
        ingested_at="2026-01-01 00:00:00",
    ),
    _doc("docs/a.md"),
    _doc("files/deleted.md", superseded_at="2026-01-05 00:00:00"),
    # A first ingest queued: written pending, no document yet.
    _job("docs/queued.md", 1, "pending", age_s=5),
    # The report: published by job 2; job 3 stalled past its lease (written pending, then
    # processing), and job 4 queued behind it.
    _job(REPORT, 2, "ready", age_s=9000, updated_s=8900),
    _job(REPORT, 3, "pending", age_s=4000, updated_s=4000),
    _job(REPORT, 3, "processing", age_s=4000, updated_s=3600, stage="embed", attempts=1, version=2),
    _job(REPORT, 4, "pending", age_s=100, updated_s=100),
    _job("files/conf.toml", 20, "ready", age_s=50, updated_s=40),
    # The extractor: none takes the format; one unreachable, retried.
    _job("files/down.sql", 5, "failed", age_s=60, error_code="format_unavailable"),
    _job(
        "files/retry.toml",
        6,
        "pending",
        age_s=61,
        attempts=2,
        error_code="retrying",
        error_message="the extractor is unreachable: ConnectError",
    ),
    # Access: the extractor refused the server's token; the original is gone.
    _job(
        "files/token.md",
        7,
        "failed",
        age_s=62,
        attempts=5,
        error_code="retries_exhausted",
        error_message="the extractor answered 403",
    ),
    _job("files/gone.md", 8, "failed", age_s=63, error_code="original_gone"),
    # The file: the extractor refused its encoding.
    _job("files/bad.sql", 9, "failed", age_s=64, error_code="encoding", error_message="x"),
    # A later write landed first: no finding of its own.
    _job("docs/a.md", 10, "failed", age_s=60, error_code="revision_conflict"),
    # A store outage, retried: the backlog's.
    _job(
        "files/store.md",
        11,
        "pending",
        age_s=65,
        attempts=1,
        error_code="retrying",
        error_message="BackendUnavailableError",
    ),
    # The deleted document's last ingest was cancelled.
    _job("files/deleted.md", 12, "cancelled", age_s=70, error_code="cancelled"),
    _job("docs/queued.md", 13, "failed", age_s=10, ns="other", error_code="encoding"),
    # Failed on the server's side: runs cut off before they finished, and a defect.
    _job(
        "files/exhausted.md",
        14,
        "failed",
        age_s=66,
        attempts=5,
        error_code="retries_exhausted",
        error_message="earlier runs stopped before they finished",
    ),
    _job("files/defect.md", 15, "failed", age_s=67, error_code="internal"),
    # Retried after the extractor was down and taken again: running, it still carries
    # `retrying` and the extractor's message, and is the backlog's.
    _job(
        "files/rerun.toml",
        16,
        "processing",
        age_s=68,
        attempts=2,
        stage="parse",
        error_code="retrying",
        error_message="the extractor is unreachable: ConnectError",
    ),
    # Cancelled while pending: only the cancel row says so until a worker takes it.
    _job("files/cancelled.md", 17, "pending", age_s=69, updated_s=50),
    # Failed, then replaced by a later write: a text write of the source, and a delete.
    # Neither writes a job row; only the document row says so.
    _job(
        "files/fixed.toml", 18, "failed", age_s=300, updated_s=200, error_code="format_unavailable"
    ),
    _doc("files/fixed.toml"),
    _job("files/removed.md", 19, "failed", age_s=300, updated_s=200, error_code="encoding"),
    "INSERT INTO loom.documents (org, namespace, source_uri, content, version, superseded_at) "
    "VALUES ('o', 'ns', 'files/removed.md', 'text', 1, now())",
    # Failed, then a metadata edit: it raises the row's version and keeps its ingested_at,
    # so it replaces nothing and the failure stands.
    _job("files/meta.md", 22, "failed", age_s=300, updated_s=200, error_code="encoding"),
    _doc("files/meta.md", version=2, ingested_at="2026-01-01 00:00:00"),
    # The extractor answered an artifact that breaks the contract: the extractor's fault.
    _job("files/invalid.sql", 21, "failed", age_s=70, error_code="invalid_artifact"),
    # A text document, a file ingest over it that failed, then a metadata edit with reindex:
    # it rewrites the document (ingested_at now), a write that replaces the job.
    _doc("files/reindexed.md", ingested_at="2026-01-01 00:00:00"),
    _job("files/reindexed.md", 23, "failed", age_s=300, updated_s=200, error_code="encoding"),
    _doc("files/reindexed.md", version=2),
    # A job that failed at S.400 and a document row written in the same second S (stored as
    # whole seconds): the write may have come after the job, and it does not replace it.
    _doc("files/same.md", ingested_at=SAME_SECOND),
    "INSERT INTO loom.knowledge_ingestions (org, namespace, ingestion_id, source_uri, "
    "upload_id, sha256, state, error_code, created_at, updated_at, next_attempt_at, version) "
    f"VALUES ('o', 'ns', '{_id(24)}', 'files/same.md', '{_id(924)}', '{'a' * 64}', 'failed', "
    f"'encoding', now64(3) - 200, toDateTime64('{SAME_SECOND}', 3) + "
    "toIntervalMillisecond(400), now64(3), 1)",
    "INSERT INTO loom.knowledge_ingest_cancels (org, namespace, ingestion_id, requested_at) "
    f"VALUES ('o', 'ns', '{_id(17)}', now64(3) - toIntervalSecond(30)), "
    # Another namespace's cancel of the same id is not this job's.
    f"('o', 'other', '{_id(1)}', now64(3))",
]


class _Local:
    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(SETUP, sql, params)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        rows = run(SETUP, sql, params)
        return rows[0] if rows else None


def _go(coro: Any) -> Any:
    return asyncio.run(coro)


def _samples(row: dict[str, Any], code: str) -> dict[str, str]:
    return {uri: job for uri, job in row[f"{code}_samples"]}


def test_the_namespace_jobs_are_counted_by_state_under_final() -> None:
    h = _go(store.ingestion_health("o", "ns", lease_s=LEASE_S, ch=_Local()))
    assert int(h["sources"]) == 21
    # fixed, removed and reindexed: each document row was written or deleted after the job.
    assert int(h["replaced"]) == 3
    # queued, down, retry, token, gone, bad, store, exhausted, defect, rerun, cancelled, and
    # the deleted document's (its job ended after the delete). The removed source, deleted
    # after its job failed, is not counted.
    assert int(h["without_document"]) == 13
    got = {k: int(h[k]) for k in ("pending", "processing", "ready", "failed", "cancelled")}
    # Job 3's pending row is folded away by its processing one; the pending job with a
    # cancel is cancelled.
    assert got == {"pending": 4, "processing": 2, "ready": 2, "failed": 13, "cancelled": 2}
    assert int(h["stalled"]) == 1 and int(h["partial"]) == 0
    assert int(h["open_jobs"]) == 6


def test_each_state_is_its_own_finding() -> None:
    h = _go(store.ingestion_health("o", "ns", lease_s=LEASE_S, ch=_Local()))
    # The open job is the oldest one: the stalled run, not the job queued behind it.
    # A running job that last failed on the extractor is the backlog's; a cancelled pending
    # one is not.
    assert _samples(h, "ingest_backlog") == {
        "docs/queued.md": _id(1),
        REPORT: _id(3),
        "files/store.md": _id(11),
        "files/rerun.toml": _id(16),
    }
    assert int(h["ingest_backlog_without_document"]) == 3
    # A pending job retrying for the extractor is the extractor's, not the backlog's.
    assert _samples(h, "extractor_unavailable") == {
        "files/down.sql": _id(5),
        "files/retry.toml": _id(6),
        "files/invalid.sql": _id(21),
    }
    assert _samples(h, "ingest_missing_access") == {
        "files/token.md": _id(7),
        "files/gone.md": _id(8),
    }
    # The metadata edit after the failure replaced nothing.
    assert _samples(h, "ingest_content_rejected") == {
        "files/bad.sql": _id(9),
        "files/meta.md": _id(22),
        "files/same.md": _id(24),
    }
    assert _samples(h, "ingest_failed_on_server") == {
        "files/exhausted.md": _id(14),
        "files/defect.md": _id(15),
    }
    for code in store.INGEST_CHECKS:
        assert int(h[code]) == len(h[f"{code}_samples"])


def test_an_empty_namespace_reads_zero() -> None:
    h = _go(store.ingestion_health("o", "none", lease_s=LEASE_S, ch=_Local()))
    assert int(h["sources"]) == 0 and int(h["open_jobs"]) == 0
    assert all(int(h[c]) == 0 for c in store.INGEST_CHECKS)


def test_a_listed_source_carries_its_newest_and_oldest_open_job() -> None:
    rows = _go(
        store.source_jobs("o", "ns", [REPORT, "docs/a.md", "nope"], lease_s=LEASE_S, ch=_Local())
    )
    by = {r["source_uri"]: r for r in rows}
    assert set(by) == {REPORT, "docs/a.md"}
    r = by[REPORT]
    assert (int(r["jobs"]), int(r["open_jobs"])) == (3, 2)
    assert (r["latest_ingestion_id"], r["latest_state"]) == (_id(4), "pending")
    assert (r["open_ingestion_id"], r["open_state"], r["open_stage"]) == (
        _id(3),
        "processing",
        "embed",
    )
    assert int(r["open_stalled"]) == 1 and int(r["latest_stalled"]) == 0
    assert int(r["open_attempts"]) == 1
    a = by["docs/a.md"]
    assert (a["latest_error_code"], a["latest_failure_class"]) == (
        "revision_conflict",
        "revision_conflict",
    )
    assert int(a["open_jobs"]) == 0


def test_the_failure_classes_read_the_code_and_the_message_class() -> None:
    rows = _go(
        store.source_jobs(
            "o",
            "ns",
            [
                "files/retry.toml",
                "files/token.md",
                "files/store.md",
                "files/bad.sql",
                "files/exhausted.md",
                "files/defect.md",
                "files/rerun.toml",
                "files/invalid.sql",
            ],
            lease_s=LEASE_S,
            ch=_Local(),
        )
    )
    got = {r["source_uri"]: r["latest_failure_class"] for r in rows}
    assert got == {
        "files/retry.toml": "extractor_unavailable",
        "files/token.md": "missing_access",
        "files/store.md": "retrying",
        "files/bad.sql": "bad_content",
        # An outage that used up its attempts and is not the extractor's has its own class;
        # `retrying` is only an open job's.
        "files/exhausted.md": "retries_exhausted",
        "files/defect.md": "internal",
        "files/rerun.toml": "retrying",
        "files/invalid.sql": "extractor_unavailable",
    }


def test_a_pending_job_with_a_cancel_reads_cancelled() -> None:
    (r,) = _go(store.source_jobs("o", "ns", ["files/cancelled.md"], lease_s=LEASE_S, ch=_Local()))
    assert (r["latest_state"], r["latest_error_code"], r["latest_failure_class"]) == (
        "cancelled",
        "cancelled",
        "cancelled",
    )
    assert int(r["open_jobs"]) == 0 and r["open_ingestion_id"] == ""
    # Its other fields are its row's, as knowledge.ingest_status serves them: changed
    # before the cancel came.
    (cancel,) = run(
        SETUP,
        f"SELECT toString(requested_at) AS t FROM loom.knowledge_ingest_cancels "
        f"WHERE namespace = 'ns' AND ingestion_id = '{_id(17)}'",
    )
    assert r["latest_updated_at"] < cancel["t"]
    # Another namespace's cancel row of an id leaves this namespace's job open.
    (q,) = _go(store.source_jobs("o", "ns", ["docs/queued.md"], lease_s=LEASE_S, ch=_Local()))
    assert (q["latest_state"], int(q["open_jobs"])) == ("pending", 1)


def test_sources_without_a_document_are_listed_newest_first() -> None:
    rows = _go(store.sources_without_document("o", "ns", limit=3, lease_s=LEASE_S, ch=_Local()))
    assert [r["source_uri"] for r in rows] == [
        "docs/queued.md",
        "files/down.sql",
        "files/retry.toml",
    ]
    rows = _go(store.sources_without_document("o", "ns", limit=50, lease_s=LEASE_S, ch=_Local()))
    uris = {r["source_uri"] for r in rows}
    assert REPORT not in uris and "files/removed.md" not in uris and len(rows) == 13


def test_the_extraction_coverage_counts_lost_capabilities_once_per_document() -> None:
    h = _go(store.extraction_health("o", "ns", ch=_Local()))
    assert (int(h["documents"]), int(h["incomplete"])) == (2, 1)
    assert h["incomplete_uris"] == [REPORT]
    assert h["lost_names"] == ["sql_structure"] and [int(c) for c in h["lost_counts"]] == [1]


def test_a_coverage_without_complete_is_judged_from_its_counts() -> None:
    setup = [
        ddl("documents"),
        _doc("x.toml", coverage='{"extracted": 3, "total": 4, "unit": "keys"}'),
        _doc("y.toml", coverage='{"extracted": 4, "total": 4, "unit": "keys", "lost": ["a", "b"]}'),
        _doc("z.toml", coverage='{"extracted": 4, "total": 4, "unit": "keys", "lost": ["b"]}'),
    ]

    class Local(_Local):
        async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
            return run(setup, sql, params)[0]

    h = _go(store.extraction_health("o", "ns", ch=Local()))
    assert int(h["incomplete"]) == 3
    # Most documents first, then by name.
    assert h["lost_names"] == ["b", "a"] and [int(c) for c in h["lost_counts"]] == [2, 1]


def test_the_listed_documents_carry_their_coverage_without_failed_unit_names() -> None:
    rows = _go(store.newest_documents("o", "ns", limit=10, ch=_Local()))
    by = {r["source_uri"]: r for r in rows}
    r = by[REPORT]
    assert int(r["has_coverage"]) == 1 and r["cov_unit"] == "statements"
    assert (int(r["cov_total"]), int(r["cov_extracted"]), int(r["cov_failed"])) == (9, 8, 1)
    assert r["cov_lost"] == ["sql_structure"]
    assert int(by["docs/a.md"]["has_coverage"]) == 0
    assert "s9" not in str(rows)


def test_a_failure_a_later_write_replaced_is_no_finding() -> None:
    h = _go(store.ingestion_health("o", "ns", lease_s=LEASE_S, ch=_Local()))
    for code in store.INGEST_CHECKS:
        replaced = {"files/fixed.toml", "files/removed.md", "files/reindexed.md"}
        assert not replaced & set(_samples(h, code)), code
    rows = _go(
        store.source_jobs(
            "o",
            "ns",
            [
                "files/fixed.toml",
                "files/removed.md",
                "docs/a.md",
                "files/meta.md",
                "files/reindexed.md",
                "files/conf.toml",
                "files/same.md",
            ],
            lease_s=LEASE_S,
            ch=_Local(),
        )
    )
    by = {r["source_uri"]: r for r in rows}
    assert (
        int(by["files/fixed.toml"]["replaced"]),
        int(by["files/fixed.toml"]["has_document"]),
    ) == (1, 1)
    assert (
        int(by["files/removed.md"]["replaced"]),
        int(by["files/removed.md"]["has_document"]),
    ) == (1, 0)
    # A document row written before its job ended does not replace it.
    assert int(by["docs/a.md"]["replaced"]) == 0
    # Nor one written in the second the job ended: ingested_at counts whole seconds, so a
    # write in that second is not told after the job, and its failure stands.
    assert int(by["files/same.md"]["replaced"]) == 0
    # A reindex rewrote the document after the failure: a write that replaced it.
    assert int(by["files/reindexed.md"]["replaced"]) == 1
    # The job that published a document ended after the row it wrote.
    assert int(by["files/conf.toml"]["replaced"]) == 0
    # Nor one followed by a metadata edit, which keeps the row's ingested_at.
    meta = by["files/meta.md"]
    assert (int(meta["replaced"]), int(meta["has_document"])) == (0, 1)


def test_the_job_reads_leave_out_a_row_past_the_tables_ttl() -> None:
    """A job no run has saved for 30 days, which a merge has not dropped yet, is in no
    count, as the queue offers it to no worker. clickhouse-local applies the TTL itself,
    so the statements are checked for the window instead."""
    from server.knowledge.store.ingestions import UNEXPIRED

    sent: list[str] = []

    class _Recording(_Local):
        async def query_all(self, sql: str, params: Any = None, **kw: Any) -> Any:
            sent.append(sql)
            return await super().query_all(sql, params, **kw)

        async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
            sent.append(sql)
            return await super().query_one(sql, params, **kw)

    ch = _Recording()
    _go(store.ingestion_health("o", "ns", lease_s=LEASE_S, ch=ch))
    _go(store.source_jobs("o", "ns", ["repo://queued.sql"], lease_s=LEASE_S, ch=ch))
    _go(store.sources_without_document("o", "ns", limit=20, lease_s=LEASE_S, ch=ch))
    _go(store.format_unavailable_sources("o", "ns", lease_s=LEASE_S, ch=ch))
    reads = [sql for sql in sent if "knowledge_ingestions FINAL" in sql]
    assert len(reads) == 4 and all(UNEXPIRED in sql for sql in reads)
