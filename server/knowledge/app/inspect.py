"""knowledge.inspect — what a namespace's documents are, which of their versions search
serves, and what was extracted from them, read-only.

Every number comes from what is stored: the document rows (one per document, its
current version), the live chunks and the retired ingestions, the decision rows whose
citation names a document section, and namespace.audit's findings on memory rows shaped
like documents, called in process and cited by code and count. Nothing parses a
document, and nothing is written.

Documents are the namespace's under any read scope, as every `knowledge.*` read serves
them; the decision rows are memory rows and are read inside the request's scope, and
the audit, which reads every user's rows, is not run under a scope narrowed to one user
or session. A section that cannot be read is named in `skipped` and its fields stay
null or empty; a store that is down fails the call, as any read does.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.errors import BACKEND_NOT_NOW
from server.embedding import get_embedder
from server.identity.api.models_namespace_catalog import NamespaceAuditInput
from server.identity.app.namespace_audit import handle_namespace_audit
from server.identity.app.report_rules import RuleContract, RuleLog
from server.identity.app.request_scope import request_scope
from server.knowledge.api.models_inspect import (
    ChunkModelCount,
    ChunkTotals,
    DecisionStage,
    DecisionTotals,
    DocumentChunks,
    DocumentCount,
    DocumentDecisions,
    DocumentFinding,
    DocumentTotals,
    InspectDocument,
    KnowledgeInspectInput,
    KnowledgeInspectOutput,
    MemoryFindingRef,
)
from server.knowledge.store import inspect as store
from server.memory.app.query import document_decision_tally

log = logging.getLogger(__name__)

SOURCE_OP = "knowledge.inspect"
# namespace.audit's checks on memory rows that may belong in documents.
MEMORY_FINDING_CODES = ("oversized_values", "episodic_blobs")

DECISIONS_DEFINITION = (
    "Live decision rows in this namespace whose source_resource_id cites a section of one "
    "of its documents (knowledge:<namespace>/<source_uri>#<section>), as loom decisions "
    "extract and decision.record write it, inside the caller's read scope, by stage: "
    "decided, or proposed (tagged decision:proposed). Rows written into another "
    "namespace (a repository's) are not counted."
)

_RECENT = (
    "A document written in the last few seconds may not have every chunk yet; "
    "re-run before acting on a count of one."
)
# A document knowledge.ingest published was cut from its extracted artifact: a text write
# (knowledge.upsert, loom rechunk) re-cuts its rendered text and clears its format,
# parser, coverage and ingestion, as knowledge.update_metadata refuses to.
_INGESTED = (
    " For a document knowledge.ingest published (`ingested`), upload its original again "
    "(GET /v1/knowledge.download serves it) and knowledge.ingest it: a text write would "
    "turn it into a text document."
)


# code: (contract, health column, detail, remedy); a hit is a warning, a clean check info.
_CHECKS: dict[str, tuple[RuleContract, str, str, str]] = {
    "documents_without_chunks": (
        RuleContract("system_state", 1, uncertainty=_RECENT),
        "without_chunks",
        "Live documents with no live chunk: search cannot return them. A write that stored "
        "the document and not its chunks leaves one.",
        "For a text document, write it again with knowledge.upsert, or loom rechunk --org "
        "<org> --namespace <namespace> --source-uri <uri> --apply." + _INGESTED,
    ),
    "unembedded_chunks": (
        # No wire op writes a chunk without its vector, so a hit is never a write in flight.
        RuleContract("system_state", 1),
        "unembedded",
        "Live documents with a live chunk that carries no vector: knowledge.search does "
        "not return that chunk.",
        "For a text document, loom rechunk --org <org> --namespace <namespace> "
        "--source-uri <uri> --apply re-embeds it." + _INGESTED,
    ),
    "chunks_under_other_model": (
        RuleContract(
            "system_state",
            1,
            exceptions="A document with no live chunk at all is documents_without_chunks.",
            uncertainty="A rolling change of the embedding model passes through this state.",
        ),
        "other_model",
        "Live documents whose live chunks were all written under another embedding model "
        "than this server's: knowledge.search reads only the server's model, so it does not "
        "return them.",
        "For a text document, loom rechunk --org <org> --namespace <namespace> "
        "--source-uri <uri> --apply re-chunks it under the current model." + _INGESTED,
    ),
    "mixed_live_versions": (
        RuleContract(
            "usage_heuristic",
            1,
            uncertainty="Two versions kept on purpose read the same. A knowledge.ingest "
            "publish passes through this state: it inserts the new version's chunks before "
            "it writes the row and retires the old ones, and an insert whose outcome it "
            "could not confirm leaves both live until the job's next run.",
        ),
        "mixed_versions",
        "Live documents with live chunks of more than one version under the server's "
        "model: search serves both texts. knowledge.add on an existing source_uri adds a "
        "version beside the earlier one; knowledge.upsert retires it.",
        "Write a changed text document with knowledge.upsert. For an ingested one, a "
        "knowledge.ingest job of the source still processing at its publish stage "
        "(knowledge.ingest_status, with the id that ingest returned) settles this state on "
        "its next run; only when no ingest of the source is open, upload its original "
        "again and knowledge.ingest it.",
    ),
}


def _counts(groups: list[dict[str, Any]], key: str) -> list[DocumentCount]:
    totals: dict[str, list[int]] = {}
    for g in groups:
        t = totals.setdefault(str(g[key]), [0, 0])
        t[0] += int(g["live"])
        t[1] += int(g["deleted"])
    return [
        DocumentCount(name=name, live=live, deleted=deleted)
        for name, (live, deleted) in sorted(
            totals.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0])
        )
    ]


_STAGE_COUNTS = ("total", "confirmed", "source_changed", "outcome_known")


def _stage(rows: list[dict[str, Any]]) -> DecisionStage:
    total, confirmed, changed, known = (sum(int(r[k]) for r in rows) for k in _STAGE_COUNTS)
    return DecisionStage(
        total=total,
        confirmed=confirmed,
        unconfirmed=total - confirmed,
        source_changed=changed,
        outcome_known=known,
    )


def _decisions(rows: list[dict[str, Any]]) -> DocumentDecisions:
    """The rows (one per stage, or none) folded into decided and proposed counts."""
    return DocumentDecisions(
        decided=_stage([r for r in rows if not int(r["proposed"])]),
        proposed=_stage([r for r in rows if int(r["proposed"])]),
    )


class _Inspect:
    def __init__(self, inp: KnowledgeInspectInput) -> None:
        self.inp = inp
        self.skipped: list[str] = []
        self.rules = RuleLog()

    async def guarded[T](self, section: str, read: Callable[[], Awaitable[T]], default: T) -> T:
        """One section's read; a failure lands in `skipped` and the default stands. A
        store that is down or overloaded fails the call."""
        try:
            return await read()
        except BACKEND_NOT_NOW:
            raise
        except Exception as exc:  # noqa: BLE001 — a read-only report; one section must not sink the rest
            log.warning("knowledge.inspect %s skipped: %s", section, exc)
            self.skipped.append(f"{section}: {type(exc).__name__}")
            return default

    async def documents(self) -> DocumentTotals:
        i = self.inp
        out = DocumentTotals()
        groups = await self.guarded(
            "documents", lambda: store.document_groups(i.org, i.namespace), None
        )
        if groups is not None:
            if len(groups) > store.MAX_GROUPS:
                groups = groups[: store.MAX_GROUPS]
                self.skipped.append(
                    f"documents: more than {store.MAX_GROUPS} format and scheme pairs; "
                    "the counts cover the first"
                )
            else:
                out.live = sum(int(g["live"]) for g in groups)
                out.deleted = sum(int(g["deleted"]) for g in groups)
                out.earlier_revisions = sum(int(g["earlier_revisions"]) for g in groups)
                out.unnumbered = sum(int(g["unnumbered"]) for g in groups)
            out.by_format = _counts(groups, "doc_format")
            out.by_source_scheme = _counts(groups, "source_scheme")
            newest = [g["newest"] for g in groups if int(g["live"]) > 0]
            out.newest_ingested_at = max(newest) if newest else None
        out.retired_versions = await self.guarded(
            "documents.retired_versions",
            lambda: store.retired_versions(i.org, i.namespace),
            None,
        )
        return out

    async def chunks(self, model: str) -> ChunkTotals:
        i = self.inp
        out = ChunkTotals(embedder_model=model)
        rows = await self.guarded("chunks", lambda: store.chunk_models(i.org, i.namespace), None)
        if rows is not None:
            out.by_model = [
                ChunkModelCount(
                    model=str(r["model"]), live=int(r["live"]), embedded=int(r["embedded"])
                )
                for r in rows
            ]
            out.live = sum(m.live for m in out.by_model)
            out.unembedded = sum(m.live - m.embedded for m in out.by_model)
            out.searchable = sum(m.embedded for m in out.by_model if m.model == model)
        return out

    async def findings(self, model: str) -> list[DocumentFinding]:
        i = self.inp
        health = await self.guarded(
            "findings", lambda: store.chunk_health(i.org, i.namespace, model), None
        )
        if health is None:
            for code in _CHECKS:
                self.rules.skip(code, "the chunk read failed")
            return []
        population = int(health["live_docs"])
        out: list[DocumentFinding] = []
        for code, (contract, column, detail, remedy) in _CHECKS.items():
            count = int(health[column])
            uris = [str(u) for u in health[f"{column}_uris"]]
            samples = uris[: i.sample_limit]
            ingested = int(health[f"{column}_ingested"])
            out.append(
                DocumentFinding(
                    code=code,
                    kind=contract.kind,
                    rule_version=contract.version,
                    source_op=SOURCE_OP,
                    severity="warning" if count else "info",
                    count=count,
                    population=population,
                    share=count / population if population else 0.0,
                    sample_limit=i.sample_limit,
                    samples=samples,
                    samples_complete=len(samples) >= count,
                    ingested=ingested,
                    ingested_samples=[
                        str(u) for u in health[f"{column}_ingested_uris"][: i.sample_limit]
                    ],
                    detail=detail,
                    remedy=remedy,
                    exceptions=contract.exceptions,
                    uncertainty=contract.uncertainty,
                )
            )
            self.rules.ran(code)
        return out

    async def listed(self, model: str) -> list[InspectDocument]:
        i = self.inp
        if not i.sample_limit:
            return []
        rows = await self.guarded(
            "listed",
            lambda: store.newest_documents(i.org, i.namespace, limit=i.sample_limit),
            None,
        )
        if not rows:
            return []
        uris = [str(r["source_uri"]) for r in rows]
        # Without the server's model, nothing says which chunks search serves.
        chunks = (
            await self.guarded(
                "listed.chunks",
                lambda: store.document_chunks(i.org, i.namespace, uris, model),
                None,
            )
            if model
            else None
        )
        by_uri = {str(c["source_uri"]): c for c in chunks or []}
        retired = await self.guarded(
            "listed.retired_versions",
            lambda: store.document_retired_versions(i.org, i.namespace, uris),
            None,
        )
        out = []
        for r in rows:
            uri = str(r["source_uri"])
            c = by_uri.get(uri)
            doc = InspectDocument(
                source_uri=uri,
                title=str(r["title"]),
                format=str(r["doc_format"]),
                source_scheme=str(r["source_scheme"]),
                version=int(r["version"]),
                revision=int(r["revision"]),
                ingestion_id=str(r["ingestion_id"]),
                ingested_at=r["ingested_at"],
                retired_versions=None if retired is None else retired.get(uri, 0),
            )
            if chunks is not None:
                doc.chunks = _document_chunks(c)
            out.append(doc)
        return out

    async def decisions(self, listed: list[InspectDocument]) -> DecisionTotals:
        i = self.inp
        out = DecisionTotals(definition=DECISIONS_DEFINITION)
        listed_uris = [d.source_uri for d in listed]
        # A citation of `doc#v2#Intro` is `doc#v2`'s, listed or not: the longer documents
        # are passed too, so their rows are not counted under `doc`.
        longer = await self.guarded(
            "decisions.longer_documents",
            lambda: store.longer_document_uris(i.org, i.namespace, listed_uris),
            None,
        )
        if longer is None:
            return out
        if len(longer) > store.MAX_LONGER:
            longer = longer[: store.MAX_LONGER]
            self.skipped.append(
                f"decisions: more than {store.MAX_LONGER} documents extend the listed ones "
                "with '#'; a listed document may count some of their decisions"
            )
        uris = listed_uris + [u for u in longer if u not in listed_uris]
        rows = await self.guarded(
            "decisions", lambda: document_decision_tally(i.org, i.namespace, uris), None
        )
        if rows is None:
            return out
        out.all_documents = _decisions(rows)
        for d in listed:
            d.decisions = _decisions([r for r in rows if str(r["source_uri"]) == d.source_uri])
        return out

    async def memory_findings(self) -> list[MemoryFindingRef]:
        i = self.inp
        if request_scope().narrowed:
            self.skipped.append(
                "memory_findings: namespace.audit reads every user's rows; not run under a "
                "read scope bound to one user or session"
            )
            return []
        audit = await self.guarded(
            "memory_findings",
            lambda: handle_namespace_audit(
                NamespaceAuditInput(org=i.org, namespace=i.namespace, sample_limit=0)
            ),
            None,
        )
        if audit is None:
            return []
        # The audit's own "name: reason" lines, by name.
        skipped = dict(s.split(": ", 1) for s in audit.skipped if ": " in s)
        out = []
        for code in MEMORY_FINDING_CODES:
            f = next((f for f in audit.findings if f.code == code), None)
            if f is None or code in audit.coverage.rules_skipped:
                reason = skipped.get(code, "not reported by namespace.audit")
                self.skipped.append(f"memory_findings.{code}: {reason}")
                continue
            out.append(
                MemoryFindingRef(
                    code=f.code,
                    kind=f.kind,
                    severity=f.severity,
                    rule_version=f.rule_version,
                    source_op=f.source_op,
                    count=f.count,
                    share=f.share,
                    detail=f.detail,
                    remedy=f.remedy,
                )
            )
        return out


def _document_chunks(c: dict[str, Any] | None) -> DocumentChunks:
    if c is None:
        return DocumentChunks(
            live=0,
            searchable=0,
            unembedded=0,
            other_model=0,
            versions=0,
            min_chars=0,
            median_chars=0,
            max_chars=0,
        )
    return DocumentChunks(
        live=int(c["live"]),
        searchable=int(c["searchable"]),
        unembedded=int(c["unembedded"]),
        other_model=int(c["other_model"]),
        versions=int(c["versions"]),
        min_chars=int(c["min_chars"]),
        median_chars=int(c["median_chars"]),
        max_chars=int(c["max_chars"]),
        models=[str(m) for m in c["models"]],
    )


async def handle_knowledge_inspect(input: KnowledgeInspectInput) -> KnowledgeInspectOutput:
    run = _Inspect(input)
    computed_at = naive_utc_now()
    model = await run.guarded("embedder", _embedder_model, "")
    documents = await run.documents()
    if model:
        chunks = await run.chunks(model)
        findings = await run.findings(model)
    else:
        # Without the server's model, nothing says which chunks search serves.
        chunks, findings = ChunkTotals(), []
        for code in _CHECKS:
            run.rules.skip(code, "the embedder could not be built")
    listed = await run.listed(model)
    decisions = await run.decisions(listed)
    memory_findings = await run.memory_findings()
    return KnowledgeInspectOutput(
        org=input.org,
        namespace=input.namespace,
        computed_at=computed_at,
        documents=documents,
        chunks=chunks,
        decisions=decisions,
        listed=listed,
        listed_complete=documents.live is not None and len(listed) >= documents.live,
        findings=findings,
        coverage=run.rules.coverage,
        memory_findings=memory_findings,
        skipped=run.skipped,
    )


async def _embedder_model() -> str:
    return str(get_embedder().model_name)
