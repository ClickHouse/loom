"""knowledge.inspect — what a namespace's documents are, which of their versions search
serves, and what was extracted from them, read-only.

Every number comes from what is stored: the document rows (one per document, its
current version, with the coverage its extraction reported), the live chunks and the
retired ingestions, the knowledge.ingest job rows, the decision rows whose citation
names a document section, and namespace.audit's findings on memory rows shaped like
documents, called in process and cited by code and count. Nothing parses a document,
nothing calls an extractor, and nothing is written but what `knowledge.get`'s read
repairs under the Postgres authority (below).

Documents are the namespace's under any read scope, as every `knowledge.*` read serves
them, and so are the job rows, which knowledge.ingest_status serves to any of the
namespace's viewers by id; the decision rows are memory rows and are read inside the
request's scope, and the audit, which reads every user's rows, is not run under a scope
narrowed to one user or session. A section that cannot be read is named in `skipped`
and its fields stay null or empty; a store that is down fails the call, as any read does,
but a Postgres authority that does not answer is named in `skipped` once and not asked
again in the call.

Under the Postgres authority the document rows ClickHouse holds can trail the heads
Postgres commits: `skipped` counts the namespace's commits ClickHouse does not have yet
(owed and parked), and a listed document is kept only while `knowledge.get` serves it at
the version read (that read drops a head an older build purged, as `knowledge.get`
does). Either makes `listed_complete` false.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from server.core import pg
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
    ExtractionCoverage,
    ExtractionTotals,
    FailureClass,
    IngestFinding,
    IngestionTotals,
    IngestJob,
    InspectDocument,
    KnowledgeInspectInput,
    KnowledgeInspectOutput,
    MemoryFindingRef,
    SourceJobs,
)
from server.knowledge.app.ingest_run import JOB_LEASE_S
from server.knowledge.domain.ingest_states import OPEN_STATES
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import inspect as store
from server.knowledge.store import pg_documents
from server.memory.app.query import document_decision_tally
from server.memory.app.search import pg_validation
from server.settings import settings

log = logging.getLogger(__name__)

# The `skipped` names, under the Postgres authority, of a document section that may miss
# what the authority committed, and of the listed rows it dropped (`<n> document(s) …`):
# the dashboard keys on these prefixes (DocumentInspection.tsx `documentsMayBeShort`).
DOCUMENTS_PROJECTION = "documents.projection"
LISTED_HEADS = "listed.heads"

SOURCE_OP = "knowledge.inspect"
# namespace.audit's checks on memory rows that may belong in documents.
MEMORY_FINDING_CODES = ("oversized_values", "episodic_blobs")

DECISIONS_DEFINITION = (
    "Live decisions in this namespace that cite a section of one of its documents, as "
    "decision extraction and decision recording write them, within what your token reads, "
    "by stage: decided, or proposed. Decisions written into another namespace (a "
    "repository's) are not counted."
)
# The exact names DECISIONS_DEFINITION says in words, for `refs`.
_DECISIONS_REFS = [
    "source_resource_id",
    "knowledge:<namespace>/<source_uri>#<section>",
    "loom decisions extract",
    "decision.record",
    "decision:proposed",
]

_RECENT = (
    "A document written in the last few seconds may not have every chunk yet; "
    "re-run before acting on a count of one."
)
# A document ingested from a file was cut from its extracted artifact: a text write
# (knowledge.upsert, loom rechunk) re-cuts its rendered text and clears its format,
# parser, coverage and ingestion, as knowledge.update_metadata refuses to.
_INGESTED = (
    " For a document ingested from a file, download its original, upload it again and "
    "ingest it: writing its text would turn it into a text document."
)
_INGESTED_REFS = [
    "ingested",
    "GET /v1/knowledge.download",
    "POST /v1/knowledge.upload",
    "knowledge.ingest",
]
_RECHUNK = "loom rechunk --org <org> --namespace <namespace> --source-uri <uri> --apply"


# code: (contract, health column, detail, remedy, refs); a hit is a warning, a clean check
# info. `refs` are the exact ops, settings, commands, fields and error codes the text names
# in words.
_CHECKS: dict[str, tuple[RuleContract, str, str, str, list[str]]] = {
    "documents_without_chunks": (
        RuleContract("system_state", 1, uncertainty=_RECENT),
        "without_chunks",
        "Live documents with no live chunk: search cannot return them. A write that stored "
        "the document and not its chunks leaves one.",
        "For a text document, write it again, or cut it into chunks again with the rechunk "
        "command." + _INGESTED,
        ["knowledge.search", "knowledge.upsert", _RECHUNK, *_INGESTED_REFS],
    ),
    "unembedded_chunks": (
        # No wire op writes a chunk without its vector, so a hit is never a write in flight.
        RuleContract("system_state", 1),
        "unembedded",
        "Live documents with a live chunk that has no vector: search does not return that chunk.",
        "For a text document, the rechunk command embeds it again." + _INGESTED,
        ["knowledge.search", _RECHUNK, *_INGESTED_REFS],
    ),
    "chunks_under_other_model": (
        RuleContract(
            "system_state",
            1,
            exceptions="A document with no live chunk at all is counted under Documents with no "
            "chunk.",
            uncertainty="A rolling change of the embedding model passes through this state.",
        ),
        "other_model",
        "Live documents whose live chunks were all embedded by a model other than this "
        "server's: search reads only the server's model, so it does not return them.",
        "For a text document, the rechunk command cuts and embeds it again under the "
        "current model." + _INGESTED,
        ["knowledge.search", "documents_without_chunks", _RECHUNK, *_INGESTED_REFS],
    ),
    "mixed_live_versions": (
        RuleContract(
            "usage_heuristic",
            1,
            uncertainty="Two versions kept on purpose read the same. An ingestion passes "
            "through this state when it publishes: it inserts the new version's chunks "
            "before it writes the document and retires the old ones, and an insert whose "
            "outcome it could not confirm leaves both live until the job's next run.",
        ),
        "mixed_versions",
        "Live documents with live chunks of more than one version under the server's "
        "model: search serves both texts. Adding a document again at the same source keeps "
        "the earlier version beside it; replacing the document at that source retires it.",
        "Replace a changed text document, not add it again. For an ingested one, an "
        "ingestion of the source still at its publish stage settles this state on its next "
        "run (its status, by the id the ingestion returned, shows the stage); only when no "
        "ingestion of the source is open, upload its original again and ingest it.",
        [
            "knowledge.search",
            "knowledge.add",
            "source_uri",
            "knowledge.upsert",
            "knowledge.ingest",
            "knowledge.ingest_status",
            "ingested",
            "POST /v1/knowledge.upload",
        ],
    ),
}


# Where the remedies below send a caller for one job, and how a failed source is ingested
# again.
_STATUS = (
    " A job's status, read by its id (listed with this finding, and under a listed "
    "document's jobs), shows its stage and error code."
)
_STATUS_REFS = ["knowledge.ingest_status", "ingestion_ids", "jobs", "error_code"]
_REINGEST = (
    "upload the original again (an upload expires) and ingest it: a failed job does not run again"
)
_REINGEST_REFS = ["POST /v1/knowledge.upload", "knowledge.ingest"]

# code: (contract, detail, remedy, refs); a hit is a warning, a clean check info. The
# predicates are store.INGEST_CHECKS; extraction_incomplete reads the document rows'
# coverage.
_INGEST_CHECKS: dict[str, tuple[RuleContract, str, str, list[str]]] = {
    "ingest_backlog": (
        RuleContract(
            "system_state",
            1,
            exceptions="An open job whose last attempt failed for the extractor or for "
            "access is that finding's, not this one's.",
            uncertainty="A job queued seconds ago waits for the worker's next pass (longer "
            "when the worker was idle), and the worker runs one job at a time, one source's "
            "jobs in order: a young backlog is the queue working.",
        ),
        "Sources with an ingestion job waiting or running: queued, running within its "
        "lease, stalled past it (the worker stopped; a later pass runs it again from its "
        "first stage), put back to retry after a store or embedder outage (reported as "
        "retrying), or running again after any outage (it keeps the last attempt's error "
        "code until the run ends). A waiting job with a cancel requested is cancelled, not "
        "counted. Search serves the source's earlier revision, or nothing for a first "
        "ingestion, until it publishes.",
        "Wait for the worker: it runs while document ingestion is turned on (this report "
        "says whether it is on the server that answered), and it retries a store or "
        "embedder outage up to the attempt limit." + _STATUS + " Cancel a job that is not wanted.",
        [
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
            *_STATUS_REFS,
            "knowledge.ingest_cancel",
            "extractor_unavailable",
            "ingest_missing_access",
        ],
    ),
    "extractor_unavailable": (
        RuleContract(
            "system_state",
            1,
            uncertainty="An extractor that was down when the job ran may be up now; a job "
            "waiting to retry runs again by itself.",
        ),
        "Sources whose job failed, or waits to retry, because no extractor could take its "
        "format: none is configured or routed for it, or the one routed to it does not take "
        "the format, is unreachable, is slower than the server's extractor timeout, is "
        "failing, or answered with an artifact the extractor contract does not allow.",
        "Configure an extractor for the format (a default extractor for every format without "
        "a route of its own or for its media type, or a route for this one; this report says "
        "whether the server that answered has any extractor configured) and make sure it "
        "answers within the server's extractor timeout with an artifact the extractor "
        "contract allows; then, for a failed job, " + _REINGEST + "." + _STATUS,
        [
            "LOOM_DOCUMENT_EXTRACTOR_URL",
            "LOOM_DOCUMENT_EXTRACTORS_JSON",
            "LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S",
            "ingestions.extractor_configured",
            "pending",
            "format_unavailable",
            "invalid_artifact",
            *_REINGEST_REFS,
            *_STATUS_REFS,
        ],
    ),
    "ingest_missing_access": (
        RuleContract("system_state", 1),
        "Sources whose job failed, or waits to retry, on access: the stored original could "
        "not be read, or the extractor refused the server's credential (it answered 401, "
        "403 or 407).",
        "For a refused credential, set the token the extractor expects (one token for every "
        "extractor, or one per extractor URL); for an original that could not be read, "
        + _REINGEST
        + "."
        + _STATUS,
        [
            "LOOM_DOCUMENT_EXTRACTOR_TOKEN",
            "LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON",
            "original_gone",
            *_REINGEST_REFS,
            *_STATUS_REFS,
        ],
    ),
    "ingest_content_rejected": (
        RuleContract(
            "contract_violation",
            1,
            exceptions="A job that published an incomplete extraction, as allowed, is counted "
            "under Extraction incomplete.",
        ),
        "Sources whose job failed on the file itself: the extractor refused it (a format "
        "that does not match, a bad encoding, a corrupt, unsupported or too large file, "
        "...), or the server refused what it made of it (an incomplete extraction not "
        "allowed to publish, no text, or more chunks than the server's limit, which is "
        "reported as too large as well). When a resource limit stopped it, the extractor "
        "passed its own memory or output limit, or its artifact was over the server's size "
        "cap. Search serves the source's earlier "
        "revision, if it has one.",
        "Read the job's error code in its status; fix the file or its options (format, "
        "encoding, SQL dialect), allow an incomplete extraction to publish, "
        "or raise the limit it passed: the extractor's own memory or output limit (set on "
        "the extractor service), the server's artifact size cap, or the server's chunk "
        "limit; then " + _REINGEST + ".",
        [
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
            *_REINGEST_REFS,
        ],
    ),
    "ingest_failed_on_server": (
        RuleContract(
            "system_state",
            1,
            exceptions="An outage of the extractor, or a credential it refused, is that finding's.",
        ),
        "Sources whose job failed on the server's side: a store or embedder outage that used "
        "up the attempt limit, or runs cut off before they finished (a pass past its time "
        "limit, a process that stopped), both reported as retries exhausted; or a server "
        "defect, reported as internal. Neither the file nor the extractor is known to be at "
        "fault.",
        "Once the store and embedder answer (or, for runs cut off, with the pass time limit "
        "above the server's extractor timeout plus the source's embedding), " + _REINGEST + ". For "
        "a server defect, the server log names the job's id with its traceback." + _STATUS,
        [
            "LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS",
            "LOOM_KNOWLEDGE_INGEST_MAX_PASS_SECONDS",
            "LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S",
            "retries_exhausted",
            "internal",
            *_REINGEST_REFS,
            *_STATUS_REFS,
            "extractor_unavailable",
            "ingest_missing_access",
        ],
    ),
    "extraction_incomplete": (
        RuleContract(
            "usage_heuristic",
            1,
            uncertainty="An extractor that does not handle a construct loses it as a file "
            "that breaks its format does; another extractor may keep it.",
        ),
        "Live documents ingested without all of their source: a unit not extracted or "
        "failed, or a capability lost (SQL structure, exact keys, section paths, ...). "
        "Search serves what was extracted.",
        "Read a listed document's extraction (what it lost and how many units it did not "
        "extract); fix the source, or route its format to an extractor that keeps it, "
        "and " + _REINGEST + ".",
        [
            "knowledge.search",
            "extraction",
            "extraction.lost",
            "sql_structure",
            "exact_keys",
            "section_path",
            "LOOM_DOCUMENT_EXTRACTORS_JSON",
            *_REINGEST_REFS,
        ],
    ),
}


def _job(row: dict[str, Any], prefix: str) -> IngestJob:
    """The job whose fields carry `prefix` (`latest_` or `open_`)."""
    f = {k[len(prefix) :]: v for k, v in row.items() if k.startswith(prefix)}
    state = str(f["state"])
    ended = state not in OPEN_STATES
    return IngestJob(
        ingestion_id=str(f["ingestion_id"]),
        state=state,
        stage=str(f["stage"]),
        attempts=int(f["attempts"]),
        error_code=str(f["error_code"]),
        failure_class=cast("FailureClass", str(f["failure_class"])),
        queued_at=f["queued_at"],
        updated_at=f["updated_at"],
        ended_at=f["updated_at"] if ended else None,
        next_attempt_at=f["next_attempt_at"] if state == "pending" else None,
        stalled=bool(int(f["stalled"])),
    )


def _source_jobs(row: dict[str, Any]) -> SourceJobs:
    latest = _job(row, "latest_")
    open_jobs = int(row["open_jobs"])
    current = _job(row, "open_") if open_jobs else latest
    return SourceJobs(
        source_uri=str(row["source_uri"]),
        jobs=int(row["jobs"]),
        open_jobs=open_jobs,
        open_ingestion_id=current.ingestion_id if open_jobs else "",
        latest=latest,
        current=current,
        replaced=bool(int(row["replaced"])),
    )


def _extraction(row: dict[str, Any]) -> ExtractionCoverage | None:
    if not int(row["has_coverage"]):
        return None
    total, extracted, failed = (
        int(row["cov_total"]),
        int(row["cov_extracted"]),
        int(row["cov_failed"]),
    )
    lost = [str(c) for c in row["cov_lost"]]
    return ExtractionCoverage(
        unit=str(row["cov_unit"]),
        total=total,
        extracted=extracted,
        failed=failed,
        lost=lost,
        complete=extracted >= total and not failed and not lost,
    )


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
        # Under Postgres, False once the document sections or the listing may miss what the
        # authority committed: unpublished commits, an outage, a dropped listed row or a
        # listing that could not be checked.
        self.documents_whole = True
        self.authority_down = False

    async def guarded[T](self, section: str, read: Callable[[], Awaitable[T]], default: T) -> T:
        """One section's read; a failure lands in `skipped` and the default stands. A
        store that is down or overloaded fails the call; a Postgres authority that does
        not answer is a section's failure (`pg.PostgresUnavailableError` is not in
        BACKEND_NOT_NOW)."""
        try:
            return await read()
        except BACKEND_NOT_NOW:
            raise
        except Exception as exc:  # noqa: BLE001 — a read-only report; one section must not sink the rest
            log.warning("knowledge.inspect %s skipped: %s", section, exc)
            self.skipped.append(f"{section}: {type(exc).__name__}")
            return default

    async def unpublished(self) -> None:
        """Under Postgres, the namespace's document commits ClickHouse does not have yet:
        the document sections read ClickHouse, so they may miss them, and `skipped` says
        how many. A Postgres that does not answer is said the same way."""
        if not pg.enabled():
            return
        i = self.inp
        try:
            owed, parked = await pg_documents.unpublished(i.org, i.namespace)
        except pg.PostgresUnavailableError:
            self.down()
            return
        if owed or parked:
            self.documents_whole = False
            self.skipped.append(
                f"{DOCUMENTS_PROJECTION}: {owed + parked} document commit(s) whose "
                f"publication to ClickHouse has not finished, {parked} of them parked after "
                "LOOM_PG_DELIVERY_MAX_ATTEMPTS refusals; the document sections may miss "
                "them or still count older versions"
            )

    def down(self) -> None:
        """The Postgres authority did not answer: said once, and not asked again in this
        call (the memory sections' wait included)."""
        self.documents_whole = False
        if self.authority_down:
            return
        self.authority_down = True
        self.skipped.append(
            f"{DOCUMENTS_PROJECTION}: POSTGRES_UNAVAILABLE: the Postgres authority did "
            "not answer; the document sections and the memory sections read what ClickHouse "
            "holds, which may miss committed writes"
        )

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
        for code, (contract, column, detail, remedy, refs) in _CHECKS.items():
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
                    refs=refs,
                    exceptions=contract.exceptions,
                    uncertainty=contract.uncertainty,
                )
            )
            self.rules.ran(code)
        return out

    def _finding(
        self,
        code: str,
        count: int,
        population: int,
        samples: list[tuple[str, str]],
        without_document: int = 0,
    ) -> IngestFinding:
        contract, detail, remedy, refs = _INGEST_CHECKS[code]
        cut = samples[: self.inp.sample_limit]
        self.rules.ran(code)
        return IngestFinding(
            code=code,
            kind=contract.kind,
            rule_version=contract.version,
            source_op=SOURCE_OP,
            severity="warning" if count else "info",
            count=count,
            population=population,
            share=min(count / population, 1.0) if population else 0.0,
            sample_limit=self.inp.sample_limit,
            samples=[uri for uri, _ in cut],
            ingestion_ids=[job for _, job in cut if job],
            samples_complete=len(cut) >= count,
            without_document=without_document,
            detail=detail,
            remedy=remedy,
            refs=refs,
            exceptions=contract.exceptions,
            uncertainty=contract.uncertainty,
        )

    async def ingestions(self) -> tuple[IngestionTotals, list[IngestFinding]]:
        i = self.inp
        out = IngestionTotals(
            worker_enabled=settings.knowledge_ingest_enabled,
            extractor_configured=await self.guarded(
                "ingestions.extractor_configured", _extractor_configured, None
            ),
        )
        health = await self.guarded(
            "ingestions",
            lambda: store.ingestion_health(i.org, i.namespace, lease_s=JOB_LEASE_S),
            None,
        )
        findings: list[IngestFinding] = []
        if health is None:
            for code in store.INGEST_CHECKS:
                self.rules.skip(code, "the job read failed")
        else:
            out.sources = int(health["sources"])
            out.without_document = int(health["without_document"])
            out.replaced = int(health["replaced"])
            out.pending, out.processing, out.stalled = (
                int(health[k]) for k in ("pending", "processing", "stalled")
            )
            out.ready, out.partial, out.failed, out.cancelled = (
                int(health[k]) for k in ("ready", "partial", "failed", "cancelled")
            )
            if int(health["open_jobs"]):
                out.oldest_open_queued_at = health["oldest_open_queued_at"]
            for code in store.INGEST_CHECKS:
                findings.append(
                    self._finding(
                        code,
                        int(health[code]),
                        out.sources,
                        [(str(u), str(j)) for u, j in health[f"{code}_samples"]],
                        int(health[f"{code}_without_document"]),
                    )
                )
        if i.sample_limit and out.without_document:
            rows = await self.guarded(
                "ingestions.without_document",
                lambda: store.sources_without_document(
                    i.org, i.namespace, limit=i.sample_limit, lease_s=JOB_LEASE_S
                ),
                None,
            )
            out.without_document_listed = [_source_jobs(r) for r in rows or []]
        return out, findings

    async def extraction(self) -> tuple[ExtractionTotals, IngestFinding | None]:
        i = self.inp
        out = ExtractionTotals()
        code = "extraction_incomplete"
        health = await self.guarded(
            "extraction", lambda: store.extraction_health(i.org, i.namespace), None
        )
        if health is None:
            self.rules.skip(code, "the coverage read failed")
            return out, None
        out.documents, out.incomplete = int(health["documents"]), int(health["incomplete"])
        names, counts = list(health["lost_names"]), list(health["lost_counts"])
        if len(names) > store.MAX_GROUPS:
            names, counts = names[: store.MAX_GROUPS], counts[: store.MAX_GROUPS]
            self.skipped.append(
                f"extraction: more than {store.MAX_GROUPS} lost capabilities; the counts "
                "cover the first"
            )
        out.lost = [
            DocumentCount(name=str(n), live=int(c), deleted=0)
            for n, c in zip(names, counts, strict=True)
        ]
        uris = [(str(u), "") for u in health["incomplete_uris"]]
        return out, self._finding(code, out.incomplete, out.documents, uris)

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
        rows = await self.served(rows)
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
        jobs = await self.guarded(
            "listed.jobs",
            lambda: store.source_jobs(i.org, i.namespace, uris, lease_s=JOB_LEASE_S),
            None,
        )
        jobs_by_uri = {str(j["source_uri"]): _source_jobs(j) for j in jobs or []}
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
                extraction=_extraction(r),
                jobs=jobs_by_uri.get(uri),
            )
            if chunks is not None:
                doc.chunks = _document_chunks(c)
            out.append(doc)
        return out

    async def served(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Under Postgres, the listed ClickHouse rows `knowledge.get` still serves at the
        version read, live: a document rewritten or deleted since in the authority is not
        listed, and `skipped` counts it. A failed read lists ClickHouse's rows as read."""
        if not pg.enabled() or self.authority_down:
            # Down already: one more read would wait out the pool again.
            return rows
        i = self.inp
        uris = [str(r["source_uri"]) for r in rows]
        # knowledge.get's own read, whole documents included (at most `sample_limit`): its
        # newer-of rule, with the head an older build purged, is what a reader is served,
        # and a version-only copy of that rule could drift from it.
        try:
            got = await documents_repo.get_many_by_uris(i.org, i.namespace, uris)
        except pg.PostgresUnavailableError:
            self.down()
            return rows
        except BACKEND_NOT_NOW:
            raise
        except Exception as exc:  # noqa: BLE001 — a read-only report; the listing stands as read
            log.warning("knowledge.inspect listed.heads skipped: %s", exc)
            self.skipped.append(f"{LISTED_HEADS}: {type(exc).__name__}")
            self.documents_whole = False
            return rows
        heads = {d.source_uri: d for d in got}
        kept = [
            r
            for r in rows
            if (d := heads.get(str(r["source_uri"]))) is not None and d.version == int(r["version"])
        ]
        if len(kept) < len(rows):
            self.documents_whole = False
            self.skipped.append(
                f"{LISTED_HEADS}: {len(rows) - len(kept)} document(s) rewritten or deleted in "
                "the Postgres authority since ClickHouse's copy; not listed"
            )
        return kept

    async def decisions(self, listed: list[InspectDocument]) -> DecisionTotals:
        i = self.inp
        out = DecisionTotals(definition=DECISIONS_DEFINITION, refs=_DECISIONS_REFS)
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
                "memory_findings: the namespace audit reads every user's memories, so it does "
                "not run for a token limited to one user or session"
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
                reason = skipped.get(code, "not reported by the namespace audit")
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
    await run.unpublished()
    documents = await run.documents()
    if model:
        chunks = await run.chunks(model)
        findings = await run.findings(model)
    else:
        # Without the server's model, nothing says which chunks search serves.
        chunks, findings = ChunkTotals(), []
        for code in _CHECKS:
            run.rules.skip(code, "the embedder could not be built")
    ingestions, ingest_findings = await run.ingestions()
    extraction, incomplete = await run.extraction()
    if incomplete is not None:
        ingest_findings.append(incomplete)
    listed = await run.listed(model)
    # The decision rows and the audit's findings are memory rows: under Postgres they are
    # read once ClickHouse holds what the namespace committed, in one wait the audit
    # does not repeat, and none at all once Postgres was found down (already said).
    published = pg_validation.published_reads(
        input.org,
        [input.namespace],
        lambda reason: run.skipped.append(f"{pg_validation.PROJECTION}: {reason}"),
        down=run.authority_down,
    )
    async with published:
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
        listed_complete=run.documents_whole
        and documents.live is not None
        and len(listed) >= documents.live,
        findings=findings,
        ingestions=ingestions,
        extraction=extraction,
        ingest_findings=ingest_findings,
        coverage=run.rules.coverage,
        memory_findings=memory_findings,
        skipped=run.skipped,
    )


async def _extractor_configured() -> bool:
    """An extractor URL is set: the default one, or a route. The routes are parsed from
    LOOM_DOCUMENT_EXTRACTORS_JSON, which raises on a value that is not a JSON object of
    strings."""
    return bool(settings.document_extractor_url or settings.document_extractor_routes())


async def _embedder_model() -> str:
    return str(get_embedder().model_name)
