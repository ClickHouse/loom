"""knowledge.inspect — a namespace's documents, their chunks and what was extracted.

Each section is read through its store function (stubbed here; the statements run on
clickhouse-local in test_knowledge_inspect_sql.py): the totals fold the format and scheme
groups; the findings say which documents search cannot serve and carry the findings
contract; the listed documents carry their chunks, retired versions and decisions;
the audit is called in process and cited by code. A failed section is named in
`skipped` with its fields null or empty, a store outage fails the call, and a narrowed scope
runs no audit and reads decisions inside the scope.
"""

from __future__ import annotations

from datetime import datetime
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
    },
]
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
    assert out.coverage.rules_run == list(ki._CHECKS) and out.coverage.complete
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
    assert "knowledge.ingest" in other.remedy and "--source-uri" in other.remedy
    assert all("knowledge.ingest" in f.remedy for f in out.findings)
    assert other.share == pytest.approx(0.5)
    mixed = by_code["mixed_live_versions"]
    assert mixed.kind == "usage_heuristic" and mixed.source_op == "knowledge.inspect"
    # An ingest's publish window passes through this state too.
    assert "knowledge.ingest publish" in mixed.uncertainty
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
    assert "memory_findings.oversized_values: not reported by namespace.audit" in out.skipped


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
