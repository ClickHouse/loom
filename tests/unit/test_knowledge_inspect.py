"""knowledge.inspect — a namespace's documents, their chunks and what was extracted.

Each section is read through its store function (stubbed here; the statements run on
clickhouse-local in test_knowledge_inspect_sql.py): the totals fold the format and scheme
groups; the findings say which documents search cannot serve and carry the findings
contract; the listed documents carry their chunks, retired versions and decisions; the job
totals fold the states and carry the server's settings; each ingest state (a backlog, an
extractor unavailable, missing access, rejected content, a job failed on the server's side,
an extraction that lost structure) is its own finding with its kind and the job id of each
sample; a listed document carries its extraction's coverage and its jobs (the newest, and
the open one a caller passes to knowledge.ingest_status); the audit is called in process
and cited by code. A failed section is named in `skipped` with its fields null or empty, a
store outage fails the call, and a narrowed scope runs no audit and reads decisions inside
the scope.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import BackendUnavailableError
from server.identity.api.models_findings import ReportCoverage
from server.identity.api.models_namespace_catalog import (
    NamespaceAuditFinding,
    NamespaceAuditOutput,
    NamespaceAuditSummary,
)
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, honours_read_scope
from server.knowledge.api.models_inspect import KnowledgeInspectInput
from server.knowledge.app import inspect as ki
from server.knowledge.store import inspect as store
from server.operations.registry import OPERATIONS_BY_NAME
from tests.unit.ch_fake import RecordingCH

T0, T1 = datetime(2026, 1, 1), datetime(2026, 1, 2)
MODEL = "embed-a"


def _group(fmt: str, scheme: str, live: int, deleted: int, newest: datetime, **kw: int):
    return {
        "doc_format": fmt,
        "source_scheme": scheme,
        "live": live,
        "deleted": deleted,
        "newest": newest,
        "earlier_revisions": kw.get("earlier", 0),
        "unnumbered": kw.get("unnumbered", 0),
    }


GROUPS = [
    _group("application/pdf", "https", 2, 1, T0, earlier=3),
    _group(".md", "(none)", 3, 0, T1, earlier=1, unnumbered=1),
    _group("pdf", "(none)", 1, 0, T0, earlier=2),
    # Only deleted documents: its zero `newest` is not the newest live ingest.
    _group(".txt", "s3", 0, 2, datetime(1970, 1, 1)),
]


def _health(**hits: tuple[list[str], list[str]]) -> dict[str, Any]:
    """Per check, (matching URIs, the ones knowledge.ingest published)."""
    out: dict[str, Any] = {"live_docs": 6}
    for name in ("without_chunks", "unembedded", "other_model", "mixed_versions"):
        uris, ingested = hits.get(name, ([], []))
        out |= {
            name: len(uris),
            f"{name}_uris": uris,
            f"{name}_ingested": len(ingested),
            f"{name}_ingested_uris": ingested,
        }
    return out


HEALTH = _health(
    without_chunks=(["docs/empty.md"], []),
    other_model=(["a", "b", "c"], ["b", "c"]),
    mixed_versions=(["docs/b.md"], []),
)
_NO_COVERAGE = {
    "has_coverage": 0,
    "cov_unit": "",
    "cov_total": 0,
    "cov_extracted": 0,
    "cov_failed": 0,
    "cov_lost": [],
}
NEWEST = [
    {
        "source_uri": "docs/b.md",
        "title": "B",
        "version": 9,
        "revision": 3,
        "ingestion_id": "",
        "ingested_at": T1,
        "doc_format": ".md",
        "source_scheme": "(none)",
        **_NO_COVERAGE,
    },
    {
        "source_uri": "docs/b.md#v2",
        "title": "B2",
        "version": 4,
        "revision": 1,
        "ingestion_id": "job-1",
        "ingested_at": T0,
        "doc_format": "pdf",
        "source_scheme": "(none)",
        "has_coverage": 1,
        "cov_unit": "statements",
        "cov_total": 9,
        "cov_extracted": 8,
        "cov_failed": 1,
        "cov_lost": ["sql_structure"],
    },
]


def _job_fields(prefix: str, **over: Any) -> dict[str, Any]:
    job = {
        "ingestion_id": "",
        "state": "",
        "stage": "",
        "attempts": 0,
        "error_code": "",
        "failure_class": "",
        "queued_at": datetime(1970, 1, 1),
        "updated_at": datetime(1970, 1, 1),
        "next_attempt_at": datetime(1970, 1, 1),
        "stalled": 0,
    } | over
    return {f"{prefix}{k}": v for k, v in job.items()}


def _source(
    uri: str,
    jobs: int,
    latest: dict[str, Any],
    open_: dict[str, Any] | None = None,
    replaced: int = 0,
):
    """A source_jobs row: its newest job, and its oldest open one when `open_` is given."""
    return {
        "source_uri": uri,
        "jobs": jobs,
        "open_jobs": 1 if open_ else 0,
        "has_document": 0,
        "replaced": replaced,
        **_job_fields("latest_", **latest),
        **_job_fields("open_", **(open_ or {})),
    }


# docs/b.md#v2: published by job-1; job-2 stalled at embed past its lease, job-3 queued
# behind it.
SOURCE_JOBS = [
    _source(
        "docs/b.md#v2",
        3,
        {
            "ingestion_id": "job-3",
            "state": "pending",
            "queued_at": T1,
            "updated_at": T1,
            "next_attempt_at": T1,
        },
        {
            "ingestion_id": "job-2",
            "state": "processing",
            "stage": "embed",
            "attempts": 1,
            "queued_at": T0,
            "updated_at": T0,
            "stalled": 1,
        },
    )
]
WITHOUT_DOCUMENT = [
    _source(
        "files/bad.sql",
        1,
        {
            "ingestion_id": "job-9",
            "state": "failed",
            "error_code": "encoding",
            "failure_class": "bad_content",
            "queued_at": T0,
            "updated_at": T1,
            "attempts": 1,
        },
    )
]


def _ingest_health(**hits: list[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "sources": 8,
        "without_document": 3,
        "replaced": 2,
        "pending": 2,
        "processing": 1,
        "stalled": 1,
        "ready": 4,
        "partial": 1,
        "failed": 3,
        "cancelled": 0,
        "open_jobs": 3,
        "oldest_open_queued_at": T0,
    }
    for code in store.INGEST_CHECKS:
        samples = hits.get(code, [])
        out |= {
            code: len(samples),
            f"{code}_samples": samples,
            f"{code}_without_document": sum(1 for u, _ in samples if u.startswith("new/")),
        }
    return out


INGEST_HEALTH = _ingest_health(
    ingest_backlog=[("docs/b.md#v2", "job-2"), ("new/q.md", "job-5")],
    extractor_unavailable=[("new/x.pdf", "job-6")],
    ingest_missing_access=[("docs/c.md", "job-7")],
    ingest_content_rejected=[("files/bad.sql", "job-9"), ("new/y.sql", "job-8")],
    ingest_failed_on_server=[("new/z.md", "job-10")],
)
EXTRACTION = {
    "documents": 4,
    "incomplete": 2,
    "incomplete_uris": ["docs/b.md#v2", "files/c.toml"],
    "lost_names": ["sql_structure", "exact_keys"],
    "lost_counts": [2, 1],
}
CHUNKS = [
    {
        "source_uri": "docs/b.md",
        "live": 5,
        "searchable": 4,
        "unembedded": 1,
        "other_model": 0,
        "versions": 2,
        "min_chars": 10,
        "median_chars": 400,
        "max_chars": 900,
        "models": [MODEL],
    }
]


def _stage_row(uri: str, proposed: int, total: int, confirmed: int, changed: int, known: int):
    return {
        "source_uri": uri,
        "proposed": proposed,
        "total": total,
        "confirmed": confirmed,
        "source_changed": changed,
        "outcome_known": known,
    }


# docs/b.md: three decided rows and one proposed (pinned) row; two decided rows cite
# documents that are not listed.
DECISIONS = [
    _stage_row("docs/b.md", 0, 3, 1, 1, 0),
    _stage_row("docs/b.md", 1, 1, 1, 0, 0),
    _stage_row("", 0, 2, 2, 0, 1),
]


def _audit(**over: Any) -> NamespaceAuditOutput:
    def finding(code: str, count: int) -> NamespaceAuditFinding:
        return NamespaceAuditFinding(
            code=code,
            severity="warning" if count else "info",
            count=count,
            share=0.1 if count else 0.0,
            detail=f"{code} detail",
            remedy="knowledge.upsert",
            kind="usage_heuristic",
            rule_version=1,
            source_op="namespace.audit",
            sample_limit=0,
            samples_complete=not count,
        )

    fields: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "computed_at": T1,
        "window_seconds": 1,
        "summary": NamespaceAuditSummary(),
        "findings": [finding("oversized_values", 4), finding("episodic_blobs", 0)],
        "coverage": ReportCoverage(rules_run=["oversized_values"], complete=True),
        "healthy": True,
        "criticals": 0,
        "warnings": 1,
        "infos": 1,
    }
    return NamespaceAuditOutput(**{**fields, **over})


class Stubs:
    """Every read the handler makes, each replaceable, with the calls recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: dict[str, list[tuple[Any, ...]]] = {}
        self.answers: dict[str, Any] = {
            "document_groups": GROUPS,
            "retired_versions": 7,
            "chunk_models": [
                {"model": MODEL, "live": 10, "embedded": 9},
                {"model": "embed-old", "live": 4, "embedded": 4},
            ],
            "chunk_health": HEALTH,
            "newest_documents": NEWEST,
            "document_chunks": CHUNKS,
            "document_retired_versions": {"docs/b.md": 2},
            # docs/b.md#v3 extends a listed document and is not listed itself.
            "longer_document_uris": ["docs/b.md#v2", "docs/b.md#v3"],
            "ingestion_health": INGEST_HEALTH,
            "sources_without_document": WITHOUT_DOCUMENT,
            "source_jobs": SOURCE_JOBS,
            "extraction_health": EXTRACTION,
        }
        for name in self.answers:
            monkeypatch.setattr(store, name, self._stub(name))
        self.decisions: Any = DECISIONS
        self.audit: Any = _audit()
        self.scope = WHOLE_NAMESPACE
        self.model: Any = MODEL

        async def decisions(org: str, ns: str, uris: list[str]) -> Any:
            self.calls.setdefault("decisions", []).append((org, ns, uris))
            return self._answer(self.decisions)

        async def audit(inp: Any) -> Any:
            self.calls.setdefault("audit", []).append((inp,))
            return self._answer(self.audit)

        async def model() -> str:
            return self._answer(self.model)

        monkeypatch.setattr(ki, "document_decision_tally", decisions)
        monkeypatch.setattr(ki, "handle_namespace_audit", audit)
        monkeypatch.setattr(ki, "request_scope", lambda: self.scope)
        monkeypatch.setattr(ki, "_embedder_model", model)

    @staticmethod
    def _answer(value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    def _stub(self, name: str) -> Any:
        async def read(*args: Any, **kw: Any) -> Any:
            self.calls.setdefault(name, []).append((*args, kw))
            return self._answer(self.answers[name])

        return read


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Stubs:
    return Stubs(monkeypatch)


def _inp(**kw: Any) -> KnowledgeInspectInput:
    return KnowledgeInspectInput(**{"org": "o", "namespace": "ns", **kw})


def test_the_op_is_a_read_only_viewer_op_a_narrowed_token_may_call() -> None:
    op = OPERATIONS_BY_NAME["knowledge.inspect"]
    assert op.read_only and op.maturity == "experimental" and not op.audit
    assert honours_read_scope("knowledge.inspect")


@pytest.mark.asyncio
async def test_the_totals_fold_the_format_and_scheme_groups(stubs: Stubs) -> None:
    out = await ki.handle_knowledge_inspect(_inp())
    d = out.documents
    assert (d.live, d.deleted, d.retired_versions) == (6, 3, 7)
    # Revisions come from the rows; the retired chunk versions are counted apart.
    assert (d.earlier_revisions, d.unnumbered) == (6, 1)
    assert d.newest_ingested_at == T1
    assert [(c.name, c.live, c.deleted) for c in d.by_format] == [
        (".md", 3, 0),
        ("application/pdf", 2, 1),
        ("pdf", 1, 0),
        (".txt", 0, 2),
    ]
    assert [(c.name, c.live, c.deleted) for c in d.by_source_scheme] == [
        ("(none)", 4, 0),
        ("https", 2, 1),
        ("s3", 0, 2),
    ]
    assert out.skipped == []


@pytest.mark.asyncio
async def test_searchable_chunks_are_the_embedded_ones_under_the_servers_model(
    stubs: Stubs,
) -> None:
    c = (await ki.handle_knowledge_inspect(_inp())).chunks
    assert (c.embedder_model, c.live, c.searchable, c.unembedded) == (MODEL, 14, 9, 1)
    assert [m.model for m in c.by_model] == [MODEL, "embed-old"]


@pytest.mark.asyncio
async def test_each_check_is_a_finding_with_its_kind_and_clean_ones_are_info(
    stubs: Stubs,
) -> None:
    out = await ki.handle_knowledge_inspect(_inp(sample_limit=2))
    by_code = {f.code: f for f in out.findings}
    assert list(by_code) == list(ki._CHECKS)
    assert out.coverage.rules_run[: len(ki._CHECKS)] == list(ki._CHECKS)
    assert out.coverage.complete
    missing = by_code["documents_without_chunks"]
    assert (missing.kind, missing.severity, missing.count, missing.population) == (
        "system_state",
        "warning",
        1,
        6,
    )
    assert missing.samples == ["docs/empty.md"] and missing.samples_complete
    clean = by_code["unembedded_chunks"]
    assert (clean.severity, clean.count, clean.share) == ("info", 0, 0.0)
    # The samples are cut to the request's limit and say so.
    other = by_code["chunks_under_other_model"]
    assert other.samples == ["a", "b"] and not other.samples_complete
    # The two ingested documents are named apart, and the remedy says to ingest them.
    assert (other.ingested, other.ingested_samples) == (2, ["b", "c"])
    assert any("--source-uri" in r for r in other.refs)
    assert all("knowledge.ingest" in f.refs for f in out.findings)
    # The exact names ride `refs`; the detail and remedy say them in words (a name that is
    # a plain word, such as `ingested`, may be said as one).
    assert all(
        not any(
            r in f.detail + f.remedy + f.exceptions + f.uncertainty
            for r in f.refs
            if not r.isalpha()
        )
        for f in out.findings
    )
    assert other.share == pytest.approx(0.5)
    mixed = by_code["mixed_live_versions"]
    assert mixed.kind == "usage_heuristic" and mixed.source_op == "knowledge.inspect"
    # An ingest's publish window passes through this state too.
    assert "An ingestion passes through this state" in mixed.uncertainty
    assert all(f.rule_version == 1 and f.sample_limit == 2 for f in out.findings)


@pytest.mark.asyncio
async def test_the_listed_documents_carry_chunks_versions_and_decisions(stubs: Stubs) -> None:
    out = await ki.handle_knowledge_inspect(_inp(sample_limit=5))
    b, v2 = out.listed
    assert (b.source_uri, b.version, b.retired_versions) == ("docs/b.md", 9, 2)
    assert (b.revision, b.ingestion_id, b.format) == (3, "", ".md")
    assert (v2.revision, v2.ingestion_id, v2.format) == (1, "job-1", "pdf")
    assert b.chunks is not None
    assert (b.chunks.live, b.chunks.searchable, b.chunks.unembedded, b.chunks.versions) == (
        5,
        4,
        1,
        2,
    )
    assert (b.chunks.min_chars, b.chunks.median_chars, b.chunks.max_chars) == (10, 400, 900)
    assert b.decisions is not None
    decided, proposed = b.decisions.decided, b.decisions.proposed
    assert (decided.total, decided.confirmed, decided.unconfirmed) == (3, 1, 2)
    assert decided.source_changed == 1
    # A proposal is counted apart: its pin confirms the proposal, not a choice.
    assert (proposed.total, proposed.confirmed, proposed.unconfirmed) == (1, 1, 0)
    # A listed document with no chunk and no decision reads zero, not unknown.
    assert v2.chunks is not None and v2.chunks.live == 0 and v2.retired_versions == 0
    assert v2.decisions is not None
    assert v2.decisions.decided.total == 0 and v2.decisions.proposed.total == 0
    # The namespace total counts rows citing documents that are not listed too.
    total = out.decisions.all_documents
    assert total is not None
    d = total.decided
    assert (d.total, d.confirmed, d.outcome_known) == (5, 3, 1)
    assert (total.proposed.total, total.proposed.confirmed) == (1, 1)
    # The longer URI is matched first, so a section of `docs/b.md#v2` is its own.
    # The listed documents and every longer one go to the decision read (which sorts them
    # longest first), so a row citing `docs/b.md#v3#...` is not counted under `docs/b.md`.
    assert stubs.calls["decisions"] == [("o", "ns", ["docs/b.md", "docs/b.md#v2", "docs/b.md#v3"])]
    ((_, _, longer_of, _kw),) = stubs.calls["longer_document_uris"]
    assert longer_of == ["docs/b.md", "docs/b.md#v2"]
    ((_, _, uris, model, _kw),) = stubs.calls["document_chunks"]
    assert uris == ["docs/b.md", "docs/b.md#v2"] and model == MODEL
    assert out.listed_complete is False  # 2 listed of 6 live


@pytest.mark.asyncio
async def test_the_listing_is_complete_when_it_holds_every_live_document(stubs: Stubs) -> None:
    stubs.answers["document_groups"] = [_group(".md", "(none)", 2, 5, T1)]
    assert (await ki.handle_knowledge_inspect(_inp())).listed_complete


@pytest.mark.asyncio
async def test_no_sample_reads_no_document(stubs: Stubs) -> None:
    out = await ki.handle_knowledge_inspect(_inp(sample_limit=0))
    assert out.listed == [] and "newest_documents" not in stubs.calls
    # The namespace's decision total is still read.
    assert stubs.calls["decisions"] == [("o", "ns", ["docs/b.md#v2", "docs/b.md#v3"])]


@pytest.mark.asyncio
async def test_the_audit_is_cited_by_code_and_count(stubs: Stubs) -> None:
    out = await ki.handle_knowledge_inspect(_inp())
    assert [(m.code, m.count, m.source_op) for m in out.memory_findings] == [
        ("oversized_values", 4, "namespace.audit"),
        ("episodic_blobs", 0, "namespace.audit"),
    ]
    ((audit_input,),) = stubs.calls["audit"]
    assert (audit_input.org, audit_input.namespace, audit_input.sample_limit) == ("o", "ns", 0)


@pytest.mark.asyncio
async def test_an_audit_check_that_did_not_run_is_skipped(stubs: Stubs) -> None:
    stubs.audit = _audit(
        findings=[],
        skipped=["oversized_values: scan cap", "episodic_blobs: the value read failed"],
        coverage=ReportCoverage(
            rules_skipped=["oversized_values", "episodic_blobs"], complete=False
        ),
    )
    out = await ki.handle_knowledge_inspect(_inp())
    assert out.memory_findings == []
    # The audit's own reason for each rule it skipped reaches `skipped`.
    assert "memory_findings.oversized_values: scan cap" in out.skipped
    assert "memory_findings.episodic_blobs: the value read failed" in out.skipped


@pytest.mark.asyncio
async def test_a_skipped_audit_rule_keeps_no_finding_the_audit_still_listed(
    stubs: Stubs,
) -> None:
    stubs.audit = _audit(
        skipped=["episodic_blobs: scan cap"],
        coverage=ReportCoverage(rules_skipped=["episodic_blobs"], complete=False),
    )
    out = await ki.handle_knowledge_inspect(_inp())
    assert [m.code for m in out.memory_findings] == ["oversized_values"]
    assert "memory_findings.episodic_blobs: scan cap" in out.skipped


@pytest.mark.asyncio
async def test_a_rule_the_audit_did_not_report_says_so(stubs: Stubs) -> None:
    stubs.audit = _audit(findings=[])
    out = await ki.handle_knowledge_inspect(_inp())
    assert "memory_findings.oversized_values: not reported by the namespace audit" in out.skipped


@pytest.mark.asyncio
async def test_a_narrowed_scope_runs_no_audit(stubs: Stubs) -> None:
    stubs.scope = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    out = await ki.handle_knowledge_inspect(_inp())
    assert "audit" not in stubs.calls and out.memory_findings == []
    assert any(s.startswith("memory_findings: ") for s in out.skipped)
    # The documents are still the namespace's.
    assert out.documents.live == 6


@pytest.mark.parametrize(
    ("read", "section"),
    [
        ("document_groups", "documents"),
        ("retired_versions", "documents.retired_versions"),
        ("chunk_models", "chunks"),
        ("chunk_health", "findings"),
        ("newest_documents", "listed"),
        ("document_chunks", "listed.chunks"),
        ("document_retired_versions", "listed.retired_versions"),
        ("longer_document_uris", "decisions.longer_documents"),
        ("ingestion_health", "ingestions"),
        ("sources_without_document", "ingestions.without_document"),
        ("source_jobs", "listed.jobs"),
        ("extraction_health", "extraction"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_store_read_is_skipped_and_its_fields_are_null(
    stubs: Stubs, read: str, section: str
) -> None:
    stubs.answers[read] = RuntimeError("boom")
    out = await ki.handle_knowledge_inspect(_inp())
    assert f"{section}: RuntimeError" in out.skipped
    if read == "document_groups":
        assert out.documents.live is None and out.documents.by_format == []
        assert not out.listed_complete
    elif read == "retired_versions":
        assert out.documents.retired_versions is None
    elif read == "chunk_models":
        assert out.chunks.live is None and out.chunks.searchable is None
    elif read == "chunk_health":
        assert out.findings == [] and out.coverage.rules_skipped == list(ki._CHECKS)
        assert not out.coverage.complete
    elif read == "newest_documents":
        assert out.listed == []
    elif read == "longer_document_uris":
        assert out.decisions.all_documents is None and "decisions" not in stubs.calls
    elif read == "document_chunks":
        assert all(d.chunks is None for d in out.listed)
    elif read == "ingestion_health":
        i = out.ingestions
        assert i.sources is None and i.pending is None and i.without_document is None
        assert {f.code for f in out.ingest_findings} == {"extraction_incomplete"}
        assert set(out.coverage.rules_skipped) == set(store.INGEST_CHECKS)
        # Without the count, nothing says a source has no document: none is listed.
        assert i.without_document_listed == [] and "sources_without_document" not in stubs.calls
    elif read == "sources_without_document":
        assert out.ingestions.without_document_listed == []
        assert out.ingestions.without_document == 3
    elif read == "source_jobs":
        assert all(d.jobs is None for d in out.listed)
    elif read == "extraction_health":
        assert out.extraction.documents is None and out.extraction.lost == []
        assert "extraction_incomplete" in out.coverage.rules_skipped
        assert "extraction_incomplete" not in {f.code for f in out.ingest_findings}
        # The listed documents' own coverage comes from their rows.
        assert out.listed[1].extraction is not None
    else:
        assert all(d.retired_versions is None for d in out.listed)


@pytest.mark.asyncio
async def test_failed_decision_and_audit_reads_are_skipped(stubs: Stubs) -> None:
    stubs.decisions = RuntimeError("boom")
    stubs.audit = ValueError("boom")
    out = await ki.handle_knowledge_inspect(_inp())
    assert "decisions: RuntimeError" in out.skipped and "memory_findings: ValueError" in out.skipped
    assert out.decisions.all_documents is None and all(d.decisions is None for d in out.listed)
    assert out.decisions.definition and out.memory_findings == []
    # The definition says it in words; the exact names ride the refs.
    assert out.decisions.refs == ki._DECISIONS_REFS
    assert not any(r in out.decisions.definition for r in out.decisions.refs)


@pytest.mark.asyncio
async def test_without_the_embedder_no_chunk_is_judged(stubs: Stubs) -> None:
    stubs.model = RuntimeError("no embedder")
    out = await ki.handle_knowledge_inspect(_inp())
    assert "embedder: RuntimeError" in out.skipped
    assert out.chunks.live is None and out.findings == []
    assert out.coverage.rules_skipped == list(ki._CHECKS)
    assert out.listed and all(d.chunks is None for d in out.listed)
    assert "chunk_models" not in stubs.calls and "document_chunks" not in stubs.calls


@pytest.mark.asyncio
async def test_too_many_groups_leave_the_totals_unknown(stubs: Stubs) -> None:
    stubs.answers["document_groups"] = GROUPS * (store.MAX_GROUPS // len(GROUPS) + 1)
    out = await ki.handle_knowledge_inspect(_inp())
    assert out.documents.live is None and not out.listed_complete
    assert any(s.startswith("documents: more than") for s in out.skipped)


@pytest.mark.asyncio
async def test_exactly_the_cap_of_groups_is_a_full_read(stubs: Stubs) -> None:
    rows = [{**GROUPS[0], "doc_format": f"f{n}"} for n in range(store.MAX_GROUPS)]
    stubs.answers["document_groups"] = rows
    out = await ki.handle_knowledge_inspect(_inp())
    assert out.documents.live is not None
    assert not any(s.startswith("documents: more than") for s in out.skipped)


@pytest.mark.asyncio
async def test_a_store_outage_fails_the_call(stubs: Stubs) -> None:
    stubs.answers["chunk_models"] = BackendUnavailableError("down")
    with pytest.raises(BackendUnavailableError):
        await ki.handle_knowledge_inspect(_inp())


@pytest.mark.parametrize(
    ("scope", "bound"),
    [
        (WHOLE_NAMESPACE, False),
        (ReadFilters(scope="own_user", user_id="u9", session_id=None), True),
    ],
)
@pytest.mark.asyncio
async def test_the_decision_read_takes_the_scope(
    monkeypatch: pytest.MonkeyPatch, scope: ReadFilters, bound: bool
) -> None:
    from server.memory.store import document_decisions

    ch = RecordingCH().on("FROM loom.memory FINAL", [])
    await document_decisions.decisions_by_document("o", "ns", ["d"], scope=scope, ch=ch)
    (s,) = ch.statements("FROM loom.memory FINAL")
    assert ("user_id = {scope_uid:String}" in s.sql) is bound
    assert s.params["prefix"] == "knowledge:ns/" and s.params["uris"] == ["d"]


@pytest.mark.asyncio
async def test_the_decision_read_sorts_the_uris_longest_first() -> None:
    from server.memory.store import document_decisions

    ch = RecordingCH().on("FROM loom.memory FINAL", [])
    await document_decisions.decisions_by_document(
        "o", "ns", ["a", "a#b", "b", "a#bc"], scope=WHOLE_NAMESPACE, ch=ch
    )
    (s,) = ch.statements("FROM loom.memory FINAL")
    assert s.params["uris"] == ["a#bc", "a#b", "a", "b"]


@pytest.mark.asyncio
async def test_too_many_longer_documents_say_the_attribution_may_be_off(stubs: Stubs) -> None:
    stubs.answers["longer_document_uris"] = [f"docs/b.md#{n}" for n in range(store.MAX_LONGER + 1)]
    out = await ki.handle_knowledge_inspect(_inp())
    assert any(s.startswith("decisions: more than") for s in out.skipped)
    assert out.decisions.all_documents is not None
    # The extra row only says the read was cut; the tally reads the capped list.
    assert (
        len(stubs.calls["decisions"][0][2])
        <= len(stubs.answers["newest_documents"]) + store.MAX_LONGER
    )


@pytest.mark.asyncio
async def test_exactly_the_cap_of_longer_documents_is_a_full_read(stubs: Stubs) -> None:
    stubs.answers["longer_document_uris"] = [f"docs/b.md#{n}" for n in range(store.MAX_LONGER)]
    out = await ki.handle_knowledge_inspect(_inp())
    assert not any(s.startswith("decisions: more than") for s in out.skipped)


@pytest.mark.asyncio
async def test_the_jobs_are_counted_by_state_with_the_servers_settings(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ki.settings, "knowledge_ingest_enabled", False)
    monkeypatch.setattr(ki.settings, "document_extractor_url", "")
    monkeypatch.setattr(ki.settings, "document_extractors_json", '{"sql": "http://x:1"}')
    i = (await ki.handle_knowledge_inspect(_inp())).ingestions
    assert (i.sources, i.without_document, i.replaced) == (8, 3, 2)
    assert (i.pending, i.processing, i.stalled, i.ready, i.partial, i.failed, i.cancelled) == (
        2,
        1,
        1,
        4,
        1,
        3,
        0,
    )
    assert i.oldest_open_queued_at == T0
    assert (i.worker_enabled, i.extractor_configured) == (False, True)
    (bad,) = i.without_document_listed
    assert (bad.source_uri, bad.open_ingestion_id, bad.current.ingestion_id) == (
        "files/bad.sql",
        "",
        "job-9",
    )
    assert bad.current.failure_class == "bad_content" and bad.current.ended_at == T1
    assert bad.current.next_attempt_at is None
    monkeypatch.setattr(ki.settings, "document_extractors_json", "{}")
    i = (await ki.handle_knowledge_inspect(_inp())).ingestions
    assert i.extractor_configured is False


@pytest.mark.asyncio
async def test_no_open_job_leaves_the_oldest_queued_at_unknown(stubs: Stubs) -> None:
    stubs.answers["ingestion_health"] = INGEST_HEALTH | {"open_jobs": 0}
    i = (await ki.handle_knowledge_inspect(_inp())).ingestions
    assert i.oldest_open_queued_at is None


@pytest.mark.asyncio
async def test_each_ingest_state_is_its_own_finding_with_its_kind(stubs: Stubs) -> None:
    out = await ki.handle_knowledge_inspect(_inp(sample_limit=1))
    by = {f.code: f for f in out.ingest_findings}
    assert list(by) == [*store.INGEST_CHECKS, "extraction_incomplete"]
    assert out.coverage.rules_run[-len(by) :] == list(by) and out.coverage.complete
    kinds = {code: f.kind for code, f in by.items()}
    assert kinds == {
        "ingest_backlog": "system_state",
        "extractor_unavailable": "system_state",
        "ingest_missing_access": "system_state",
        "ingest_content_rejected": "contract_violation",
        "ingest_failed_on_server": "system_state",
        "extraction_incomplete": "usage_heuristic",
    }
    backlog = by["ingest_backlog"]
    assert (backlog.count, backlog.population, backlog.without_document) == (2, 8, 1)
    # Cut to the limit, the job ids follow their samples.
    assert (backlog.samples, backlog.ingestion_ids) == (["docs/b.md#v2"], ["job-2"])
    assert not backlog.samples_complete and backlog.share == pytest.approx(0.25)
    assert backlog.uncertainty and backlog.severity == "warning"
    access = by["ingest_missing_access"]
    assert (access.samples, access.ingestion_ids, access.samples_complete) == (
        ["docs/c.md"],
        ["job-7"],
        True,
    )
    incomplete = by["extraction_incomplete"]
    assert (incomplete.count, incomplete.population, incomplete.ingestion_ids) == (2, 4, [])
    # Each remedy names the real op or setting it takes.
    assert "knowledge.ingest_cancel" in backlog.refs
    assert "LOOM_KNOWLEDGE_INGEST_ENABLED" in backlog.refs
    assert "LOOM_DOCUMENT_EXTRACTORS_JSON" in by["extractor_unavailable"].refs
    assert "LOOM_DOCUMENT_EXTRACTOR_TOKEN" in access.refs
    assert "allow an incomplete extraction to publish" in by["ingest_content_rejected"].remedy
    server = by["ingest_failed_on_server"]
    assert (server.samples, server.ingestion_ids, server.without_document) == (
        ["new/z.md"],
        ["job-10"],
        1,
    )
    assert "attempt limit" in server.detail and "server defect" in server.detail
    assert "LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS" in server.refs
    # Every remedy's exact names ride `refs`; the remedy itself says them in words (a code
    # that is a plain word, such as `encoding`, may be said as one).
    assert all(
        f.refs
        and not any(
            r in f.detail + f.remedy + f.exceptions + f.uncertainty
            for r in f.refs
            if not r.isalpha()
        )
        for f in by.values()
    )
    assert all(f.source_op == "knowledge.inspect" for f in by.values())


@pytest.mark.asyncio
async def test_a_clean_namespace_has_info_ingest_findings(stubs: Stubs) -> None:
    stubs.answers["ingestion_health"] = _ingest_health() | {"sources": 0, "without_document": 0}
    stubs.answers["extraction_health"] = EXTRACTION | {"incomplete": 0, "incomplete_uris": []}
    out = await ki.handle_knowledge_inspect(_inp())
    assert all(f.severity == "info" and f.count == 0 for f in out.ingest_findings)
    assert all(f.share == 0.0 and f.samples_complete for f in out.ingest_findings)
    # No source lacks a document: none is read.
    assert "sources_without_document" not in stubs.calls


@pytest.mark.asyncio
async def test_the_extraction_totals_count_lost_capabilities(stubs: Stubs) -> None:
    e = (await ki.handle_knowledge_inspect(_inp())).extraction
    assert (e.documents, e.incomplete) == (4, 2)
    assert [(c.name, c.live) for c in e.lost] == [("sql_structure", 2), ("exact_keys", 1)]


@pytest.mark.asyncio
async def test_too_many_lost_capabilities_are_cut_and_say_so(stubs: Stubs) -> None:
    n = store.MAX_GROUPS + 1
    stubs.answers["extraction_health"] = EXTRACTION | {
        "lost_names": [f"c{k}" for k in range(n)],
        "lost_counts": [1] * n,
    }
    out = await ki.handle_knowledge_inspect(_inp())
    assert len(out.extraction.lost) == store.MAX_GROUPS
    assert any(s.startswith("extraction: more than") for s in out.skipped)


@pytest.mark.asyncio
async def test_a_listed_document_carries_its_extraction_and_jobs(stubs: Stubs) -> None:
    b, v2 = (await ki.handle_knowledge_inspect(_inp())).listed
    # A text write has no extraction and no job.
    assert b.extraction is None and b.jobs is None
    e = v2.extraction
    assert e is not None
    assert (e.unit, e.total, e.extracted, e.failed, e.lost, e.complete) == (
        "statements",
        9,
        8,
        1,
        ["sql_structure"],
        False,
    )
    j = v2.jobs
    assert j is not None and (j.jobs, j.open_jobs, j.open_ingestion_id) == (3, 1, "job-2")
    # The source waits on its oldest open job; the newest is queued behind it.
    assert (j.current.ingestion_id, j.current.stage, j.current.stalled) == ("job-2", "embed", True)
    assert j.current.ended_at is None and j.current.next_attempt_at is None
    assert (j.latest.ingestion_id, j.latest.state, j.latest.next_attempt_at) == (
        "job-3",
        "pending",
        T1,
    )
    ((_, _, uris, kw),) = stubs.calls["source_jobs"]
    assert uris == ["docs/b.md", "docs/b.md#v2"] and kw["lease_s"] == ki.JOB_LEASE_S


def test_the_extractor_client_messages_fall_in_their_failure_class() -> None:
    """Every ExtractorUnavailable the extractor client raises starts as the job
    classifier expects, and only the refused-credential answers read as access."""
    import re
    from pathlib import Path

    from server.knowledge.app import extractors

    source = Path(extractors.__file__).read_text()
    raised = re.findall(r'(?:ExtractorUnavailable|FormatUnavailable)\(\s*f?"([^"]*)"', source)
    assert len(raised) >= 8
    for message in raised:
        assert re.match(store.EXTRACTOR_MESSAGE_RE, message), message
    access = re.compile(store.REFUSED_CREDENTIAL_RE)
    for status in (401, 403, 407):
        assert access.match(f"the extractor answered {status}")
        assert access.match(f"the extractor's capabilities answered {status}")
    assert not access.match("the extractor answered 503")
    assert not access.match("the extractor answered 4031")


@pytest.mark.asyncio
async def test_a_job_whose_retries_ran_out_reads_its_own_class(stubs: Stubs) -> None:
    """A failed job whose outage was not the extractor's is `retries_exhausted`, not
    `retrying`, which only an open job carries."""
    stubs.answers["sources_without_document"] = [
        _source(
            "files/exhausted.md",
            1,
            {
                "ingestion_id": "job-14",
                "state": "failed",
                "error_code": "retries_exhausted",
                "failure_class": "retries_exhausted",
                "queued_at": T0,
                "updated_at": T1,
                "attempts": 5,
            },
        )
    ]
    (row,) = (await ki.handle_knowledge_inspect(_inp())).ingestions.without_document_listed
    assert (row.current.failure_class, row.current.state, row.current.ended_at) == (
        "retries_exhausted",
        "failed",
        T1,
    )
    assert row.open_ingestion_id == ""


@pytest.mark.asyncio
async def test_malformed_extractor_routes_skip_only_that_field(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ki.settings, "document_extractor_url", "")
    monkeypatch.setattr(ki.settings, "document_extractors_json", "[1, 2]")
    out = await ki.handle_knowledge_inspect(_inp())
    assert out.ingestions.extractor_configured is None
    assert "ingestions.extractor_configured: ValueError" in out.skipped
    # The jobs are still read.
    assert out.ingestions.sources == 8 and out.ingest_findings


@pytest.mark.asyncio
async def test_a_source_a_later_write_replaced_says_so(stubs: Stubs) -> None:
    stubs.answers["source_jobs"] = [
        {**SOURCE_JOBS[0], "open_jobs": 0, "replaced": 1},
    ]
    (_, v2) = (await ki.handle_knowledge_inspect(_inp())).listed
    assert v2.jobs is not None and v2.jobs.replaced
    assert v2.jobs.open_ingestion_id == "" and v2.jobs.current.ingestion_id == "job-3"


@pytest.mark.asyncio
async def test_decisions_and_memory_findings_are_read_at_one_projection_wait(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision rows and the audit's findings are memory rows: both are read inside one
    wait for ClickHouse to publish what the namespace committed, the audit run in
    process waits no second time, and a wait that runs out still reads them and names
    the projection in `skipped`."""
    from server.core.ch import sequential_reads_active
    from server.memory.app.pg_admission import ProjectionBehindError
    from server.memory.app.search import pg_validation

    waits: list[str] = []

    async def behind(org: str, namespaces: Any) -> bool:
        waits.append(org)
        raise ProjectionBehindError()

    audited: list[bool] = []

    async def audit(inp: Any) -> Any:
        async with pg_validation.published_reads(inp.org, [inp.namespace]):
            audited.append(sequential_reads_active())
            return _audit()

    monkeypatch.setattr(pg_validation, "admit", behind)
    monkeypatch.setattr(ki, "handle_namespace_audit", audit)
    out = await ki.handle_knowledge_inspect(_inp())
    assert waits == ["o"] and audited == [True]
    assert out.decisions.all_documents is not None and stubs.calls["decisions"]
    assert out.memory_findings
    assert [s.split(": ")[:2] for s in out.skipped] == [["projection", "PROJECTION_BEHIND"]]


# Each check's `refs` list as written: a change to one is made here too, on purpose.
_PINNED_REFS = {
    "documents_without_chunks": [
        "knowledge.search",
        "knowledge.upsert",
        "loom rechunk --org <org> --namespace <namespace> --source-uri <uri> --apply",
        "ingested",
        "GET /v1/knowledge.download",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
    ],
    "unembedded_chunks": [
        "knowledge.search",
        "loom rechunk --org <org> --namespace <namespace> --source-uri <uri> --apply",
        "ingested",
        "GET /v1/knowledge.download",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
    ],
    "chunks_under_other_model": [
        "knowledge.search",
        "documents_without_chunks",
        "loom rechunk --org <org> --namespace <namespace> --source-uri <uri> --apply",
        "ingested",
        "GET /v1/knowledge.download",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
    ],
    "mixed_live_versions": [
        "knowledge.search",
        "knowledge.add",
        "source_uri",
        "knowledge.upsert",
        "knowledge.ingest",
        "knowledge.ingest_status",
        "ingested",
        "POST /v1/knowledge.upload",
    ],
    "ingest_backlog": [
        "knowledge.search",
        "knowledge.ingest",
        "LOOM_KNOWLEDGE_INGEST_ENABLED",
        "LOOM_KNOWLEDGE_INGEST_INTERVAL_SECONDS",
        "LOOM_KNOWLEDGE_INGEST_IDLE_INTERVAL_SECONDS",
        "LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS",
        "pending",
        "processing",
        "ingestions.worker_enabled",
        "ingestions.stalled",
        "failure_class",
        "retrying",
        "knowledge.ingest_status",
        "ingestion_ids",
        "jobs",
        "error_code",
        "knowledge.ingest_cancel",
        "extractor_unavailable",
        "ingest_missing_access",
    ],
    "extractor_unavailable": [
        "LOOM_DOCUMENT_EXTRACTOR_URL",
        "LOOM_DOCUMENT_EXTRACTORS_JSON",
        "LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S",
        "ingestions.extractor_configured",
        "pending",
        "format_unavailable",
        "invalid_artifact",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
        "knowledge.ingest_status",
        "ingestion_ids",
        "jobs",
        "error_code",
    ],
    "ingest_missing_access": [
        "LOOM_DOCUMENT_EXTRACTOR_TOKEN",
        "LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON",
        "original_gone",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
        "knowledge.ingest_status",
        "ingestion_ids",
        "jobs",
        "error_code",
    ],
    "ingest_content_rejected": [
        "knowledge.search",
        "knowledge.ingest_status",
        "error_code",
        "format",
        "format_mismatch",
        "encoding",
        "corrupt",
        "unsupported_format",
        "too_large",
        "incomplete_extraction",
        "no_text",
        "resource_limit",
        "sql_dialect",
        "allow_partial",
        "extraction_incomplete",
        "LOOM_EXTRACTOR_MEMORY_MB",
        "LOOM_EXTRACTOR_MAX_ARTIFACT_BYTES",
        "LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES",
        "settings.set knowledge_ingest_max_chunks",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
    ],
    "ingest_failed_on_server": [
        "LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS",
        "LOOM_KNOWLEDGE_INGEST_MAX_PASS_SECONDS",
        "LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S",
        "retries_exhausted",
        "internal",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
        "knowledge.ingest_status",
        "ingestion_ids",
        "jobs",
        "error_code",
        "extractor_unavailable",
        "ingest_missing_access",
    ],
    "extraction_incomplete": [
        "knowledge.search",
        "extraction",
        "extraction.lost",
        "sql_structure",
        "exact_keys",
        "section_path",
        "LOOM_DOCUMENT_EXTRACTORS_JSON",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
    ],
}


def test_each_checks_refs_list_is_pinned() -> None:
    by = {code: t[-1] for code, t in {**ki._CHECKS, **ki._INGEST_CHECKS}.items()}
    assert by == _PINNED_REFS


def test_every_ref_list_names_registered_ops_and_settings_as_they_are_changed() -> None:
    from server.identity.app import namespace_tune
    from server.knowledge.app import tune as knowledge_tune
    from server.memory.app import inspect as memory_inspect
    from server.operations.registry import ALL_OPERATIONS
    from server.settings import Settings
    from server.settings._live import is_live

    lists = [t[-1] for t in {**ki._CHECKS, **ki._INGEST_CHECKS}.values()]
    lists += [ki._DECISIONS_REFS, knowledge_tune._CHUNKS_REFS, knowledge_tune._ROUTES_REFS]
    lists += [
        namespace_tune._EXTRACTION_REFS,
        namespace_tune._CATEGORY_REFS,
        namespace_tune._GUIDE_REFS,
    ]
    lists += [memory_inspect.InspectSettings().extraction_instructions_refs]
    lists += list(memory_inspect._DEFINITION_REFS.values())
    lists += [list(namespace_tune._FINDING_RULE_REFS.values())]
    refs = {r for refs in lists for r in refs}
    ops = {op.name for op in ALL_OPERATIONS}
    fields = Settings.model_fields
    planes = ("knowledge.", "memory.", "namespace.", "context.", "working_memory.", "decision.")
    for r in refs:
        if r.startswith("settings.set "):
            # A setting `settings.set` changes live.
            assert is_live(fields[r.removeprefix("settings.set ")]), r
        elif r.startswith("LOOM_") and not r.startswith("LOOM_EXTRACTOR_"):
            # A setting only the environment sets (the extractor service's own are not ours).
            name = r.removeprefix("LOOM_").lower()
            assert name in fields and not is_live(fields[name]), r
        elif r.startswith(planes) and " " not in r:
            assert r in ops, r


def test_an_exception_names_another_finding_by_its_label_on_the_page() -> None:
    import re
    from pathlib import Path

    page = Path(__file__).parents[2] / "frontend/src/views/assistant/DocumentInspection.tsx"
    body = page.read_text(encoding="utf-8").split("const FINDING_LABEL", 1)[1].split("};", 1)[0]
    labels = dict(re.findall(r'(\w+): "([^"]+)"', body))
    assert f"counted under {labels['documents_without_chunks']}." in (
        ki._CHECKS["chunks_under_other_model"][0].exceptions
    )
    assert f"counted under {labels['extraction_incomplete']}." in (
        ki._INGEST_CHECKS["ingest_content_rejected"][0].exceptions
    )


class _Authority:
    """The Postgres authority as knowledge.inspect reads it: its unpublished document
    commits and what `knowledge.get` serves for each listed source."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from server.core import pg
        from server.knowledge.store import documents, pg_documents
        from server.memory.app.search import pg_validation

        self.unpublished: Any = (0, 0)
        self.served: Any = {"docs/b.md": (9, False), "docs/b.md#v2": (4, False)}

        async def unpublished(org: str, ns: str) -> tuple[int, int]:
            return Stubs._answer(self.unpublished)

        async def get_many(org: str, ns: str, uris: list[str], **kw: Any) -> Any:
            assert kw == {"include_superseded": True}
            served = Stubs._answer(self.served)
            return [
                SimpleNamespace(source_uri=u, version=v, superseded_at=T1 if deleted else None)
                for u, (v, deleted) in served.items()
                if u in uris
            ]

        async def nothing_behind(*_: object) -> bool:
            return False  # the memory rows are all in ClickHouse

        monkeypatch.setattr(pg, "enabled", lambda: True)
        monkeypatch.setattr(pg_documents, "unpublished", unpublished)
        monkeypatch.setattr(documents, "get_many_by_uris", get_many)
        monkeypatch.setattr(pg_validation, "admit", nothing_behind)


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch) -> _Authority:
    return _Authority(monkeypatch)


def _whole(stubs: Stubs) -> None:
    """A namespace whose two listed documents are all of its live ones."""
    stubs.answers["document_groups"] = [_group(".md", "(none)", 2, 5, T1)]


@pytest.mark.asyncio
async def test_under_postgres_a_published_namespace_reads_as_on_clickhouse(
    stubs: Stubs, authority: _Authority
) -> None:
    _whole(stubs)
    out = await ki.handle_knowledge_inspect(_inp())
    assert [d.source_uri for d in out.listed] == ["docs/b.md", "docs/b.md#v2"]
    assert out.listed_complete and not [s for s in out.skipped if "projection" in s]


@pytest.mark.asyncio
async def test_under_postgres_unpublished_document_commits_are_counted_and_said(
    stubs: Stubs, authority: _Authority
) -> None:
    """Commits ClickHouse does not have yet, owed or parked, are named in `skipped`, and
    the listing is not called complete: the document sections read ClickHouse."""
    _whole(stubs)
    authority.unpublished = (2, 1)
    out = await ki.handle_knowledge_inspect(_inp())
    lines = [s for s in out.skipped if s.startswith("projection: ")]
    assert lines == [
        "projection: 3 document commit(s) whose publication to ClickHouse has not "
        "finished, 1 of them parked after LOOM_PG_DELIVERY_MAX_ATTEMPTS refusals; the "
        "document sections may miss them or still count older versions"
    ]
    assert len(out.listed) == 2 and not out.listed_complete


@pytest.mark.asyncio
async def test_under_postgres_an_unanswering_authority_reads_on_and_says_so(
    stubs: Stubs, authority: _Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Postgres down: the unpublished count and the listing's check go unread, the report
    still answers from ClickHouse, and says why it may be short."""
    from server.core import pg
    from server.memory.app.search import pg_validation

    async def down(*_: object) -> bool:
        raise AssertionError("the memory sections' wait is not tried against a dead pool")

    _whole(stubs)
    authority.unpublished = pg.PostgresUnavailableError()
    authority.served = pg.PostgresUnavailableError()
    monkeypatch.setattr(pg_validation, "admit", down)
    out = await ki.handle_knowledge_inspect(_inp())
    assert [d.source_uri for d in out.listed] == ["docs/b.md", "docs/b.md#v2"]
    outages = [s for s in out.skipped if s.startswith("projection: POSTGRES_UNAVAILABLE: ")]
    assert len(outages) == 1  # said once, waited out once
    assert stubs.calls["decisions"] and out.memory_findings  # the memory sections still read
    # The listing's check is not tried again against a Postgres already found down.
    assert not [s for s in out.skipped if s.startswith("listed.heads")]
    assert not out.listed_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", [(10, False), (9, True), None])
async def test_under_postgres_a_listed_document_the_authority_moved_past_is_not_listed(
    stubs: Stubs, authority: _Authority, moved: Any
) -> None:
    """A listed ClickHouse row is kept only while knowledge.get serves it at that version,
    live: rewritten, deleted or gone in the authority, it leaves the listing, its chunks
    are not read, and `skipped` counts it."""
    _whole(stubs)
    authority.served = {"docs/b.md#v2": (4, False)}
    if moved is not None:
        authority.served["docs/b.md"] = moved
    out = await ki.handle_knowledge_inspect(_inp())
    assert [d.source_uri for d in out.listed] == ["docs/b.md#v2"]
    ((_, _, uris, _, _),) = stubs.calls["document_chunks"]
    assert uris == ["docs/b.md#v2"]
    assert (
        "listed.heads: 1 document(s) rewritten or deleted in the Postgres authority since "
        "ClickHouse's copy; not listed"
    ) in out.skipped
    assert not out.listed_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["down", "error"])
async def test_under_postgres_a_failed_listing_check_lists_clickhouses_rows(
    stubs: Stubs, authority: _Authority, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The count answered but the listing's check did not: the rows are listed as
    ClickHouse holds them, the failure is named once, and the listing is not complete.
    Postgres down there is said as an outage, and the memory sections do not wait on it
    again."""
    from server.core import pg
    from server.memory.app.search import pg_validation

    async def no_wait(*_: object) -> bool:
        raise AssertionError("the memory sections' wait is not tried against a dead pool")

    _whole(stubs)
    if failure == "down":
        authority.served = pg.PostgresUnavailableError()
        monkeypatch.setattr(pg_validation, "admit", no_wait)
    else:
        authority.served = ValueError("bad row")
    out = await ki.handle_knowledge_inspect(_inp())
    assert [d.source_uri for d in out.listed] == ["docs/b.md", "docs/b.md#v2"]
    expected = (
        [s for s in out.skipped if s.startswith("projection: POSTGRES_UNAVAILABLE: ")]
        if failure == "down"
        else [s for s in out.skipped if s == "listed.heads: ValueError"]
    )
    assert len(expected) == 1
    assert not out.listed_complete
