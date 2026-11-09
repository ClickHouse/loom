import { Badge } from "@clickhouse/click-ui/Badge";
import { Table } from "@clickhouse/click-ui/Table";
import type {
  DecisionStage,
  DocumentCount,
  DocumentFinding,
  FindingKind,
  IngestFinding,
  InspectDocument,
  KnowledgeInspection,
  SourceJobs,
} from "../../api";
import { SectionHeading } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";
import { fmtInt } from "../../lib/format";
import { ExactNames } from "./ExactNames";

// server/knowledge/app/tune.py _CAPABILITY_WORDS: a lost capability in words; another code an
// extractor declares is said with spaces for its underscores.
const CAPABILITY_WORDS: Record<string, string> = {
  sql_structure: "SQL structure",
  exact_keys: "exact keys",
  section_path: "section paths",
};
const capability = (code: string) => CAPABILITY_WORDS[code] ?? code.replaceAll("_", " ");

// The audit's two checks on memories shaped like documents, as its panel labels them
// (components/NamespaceAuditPanel.tsx CODE_LABELS; kept here so this page does not load the
// panel). A code the page does not know is shown by its code.
const MEMORY_CHECK_LABEL: Record<string, string> = {
  oversized_values: "Oversized values",
  episodic_blobs: "Episodic blobs",
};

// server/knowledge/app/inspect.py _CHECKS and _INGEST_CHECKS. A code the page does not know
// yet (a newer server) is shown by its code.
const FINDING_LABEL: Record<string, string> = {
  documents_without_chunks: "Documents with no chunk",
  unembedded_chunks: "Chunks with no vector",
  chunks_under_other_model: "Chunks under another embedding model",
  mixed_live_versions: "Two versions served",
  ingest_backlog: "Backlog",
  extractor_unavailable: "Extractor unavailable",
  ingest_missing_access: "Missing access",
  ingest_content_rejected: "Content rejected",
  ingest_failed_on_server: "Failed on the server's side",
  extraction_incomplete: "Extraction incomplete",
};

// The kinds the findings contract names: the deployment's state, a file that breaks the
// extractor's contract, a pattern in what was written or kept. One group each, so a
// backlog is never read as bad content.
const KIND_GROUP: Array<{ kind: FindingKind; label: string }> = [
  { kind: "system_state", label: "The deployment's state" },
  { kind: "contract_violation", label: "The files" },
  { kind: "usage_heuristic", label: "What was written or kept" },
];

/** A count the server could not read (or, `missing`, did not total) is null: said so, never
 *  shown as zero. */
function n(v: number | null | undefined, missing = "not read"): string {
  return v === null || v === undefined ? missing : fmtInt(v);
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${fmtInt(count)} ${count === 1 ? one : many}`;
}

/** Documents as an Inspect subject: knowledge.inspect's report on the namespace's
 *  documents, what search serves of them, their ingestion and extraction, and the decisions
 *  recorded from them.
 *
 *  Whether a section was read comes from the fields the server leaves null on a failed read,
 *  not from `skipped`: a `skipped` line under a section's name is also how the server says a
 *  section it read was cut ("documents: more than ... the counts cover the first"), so that
 *  line is shown beside the section's data as a note. */
export function DocumentInspection({ result }: { result: KnowledgeInspection }) {
  const notes = (section: string) =>
    result.skipped.filter((s) => s.startsWith(`${section}: `)).map((s) => s.slice(section.length + 2));
  const docs = result.documents;
  // A failed read leaves both lists empty; a cut one keeps them and only the totals null.
  const docsRead = docs.live !== null || docs.by_format.length > 0 || docs.by_source_scheme.length > 0;
  // A cut read lists the groups it read and totals none of them.
  const total = docsRead ? "not totalled" : "not read";
  // An unread job count (null) is not "no job": a first ingest queued or failed has one.
  const empty = docs.live === 0 && result.ingestions.sources === 0;
  return (
    <div className="space-y-5" data-testid="assistant-documents">
      <section aria-labelledby="docs-totals" className="space-y-2 text-sm">
        <SectionHeading id="docs-totals" level={3} label="What the namespace holds" />
        {empty ? (
          <p className="text-text-muted" role="status">
            This namespace holds no live document.
          </p>
        ) : null}
        <p className="text-text tabular" data-testid="docs-totals-line">
          Live documents: {n(docs.live, total)} · deleted: {n(docs.deleted, total)} · earlier revisions:{" "}
          {n(docs.earlier_revisions, total)} · retired chunk versions: {n(docs.retired_versions)}
          {docs.unnumbered ? ` · ${plural(docs.unnumbered, "document")} with unnumbered revisions` : ""}
        </p>
        {docs.newest_ingested_at && (
          <p className="text-xs text-text-muted">
            Newest written <ListTime time={docs.newest_ingested_at} />
          </p>
        )}
        {!docsRead ? (
          <p className="text-text-muted">The documents could not be counted by format and source.</p>
        ) : (
          !empty && (
            <div className="grid gap-3 md:grid-cols-2">
              <CountTable label="Documents by format" head="Format" rows={docs.by_format} />
              <CountTable label="Documents by source" head="Source scheme" rows={docs.by_source_scheme} />
            </div>
          )
        )}
        <Notes lines={docsRead ? notes("documents") : []} testId="docs-documents-note" />
      </section>

      {!empty && <ChunkSection result={result} />}

      <IngestionSection result={result} notes={notes} />

      {!empty && (
        <section aria-labelledby="docs-decisions" className="space-y-1 text-sm">
          <SectionHeading id="docs-decisions" level={3} label="Decisions recorded from documents" />
          <p className="text-xs text-text-muted">{result.decisions.definition}</p>
          <ExactNames refs={result.decisions.refs} />
          {result.decisions.all_documents ? (
            <>
              <p className="text-text tabular" data-testid="docs-decisions-total">
                {stageText("Decided", result.decisions.all_documents.decided)} ·{" "}
                {stageText("Proposed", result.decisions.all_documents.proposed)}
              </p>
              <Notes lines={notes("decisions")} testId="docs-decisions-note" />
            </>
          ) : (
            <p className="text-text-muted">The decisions could not be read.</p>
          )}
        </section>
      )}

      {!empty && <ListedSection result={result} />}

      <section aria-labelledby="docs-memories" className="space-y-1 text-sm">
        <SectionHeading id="docs-memories" level={3} label="Memories shaped like documents" />
        {/* The server sends each of the audit's two checks, a clean one at count 0, or names it
            in `skipped` ("memory_findings.<code>: <reason>"): none sent means none was read. */}
        {result.memory_findings.length === 0 ? (
          <p className="text-text-muted">The memory checks were not read; the reason is listed below.</p>
        ) : (
          <ul className="space-y-1" aria-label="Memory check findings">
            {result.memory_findings.map((f) => (
              <li key={f.code} data-code={f.code}>
                {MEMORY_CHECK_LABEL[f.code] ?? f.code} <span className="tabular">{plural(f.count, "row")}</span>
                {f.count > 0 && <span className="text-text-muted"> · {f.detail}</span>}
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-text-muted">The namespace audit lists the memories and their evidence.</p>
        <ExactNames refs={["namespace.audit", ...result.memory_findings.map((f) => f.code)]} />
      </section>

      {result.coverage.rules_skipped.length > 0 && (
        <>
          <p className="text-xs text-text-muted" data-testid="docs-rules-skipped">
            Checks not run: {result.coverage.rules_skipped.map((c) => FINDING_LABEL[c] ?? c).join(", ")}
          </p>
          <ExactNames refs={result.coverage.rules_skipped} />
        </>
      )}
      {result.skipped.length > 0 && (
        <p className="text-xs text-text-muted" data-testid="docs-skipped">
          Skipped: {result.skipped.join("; ")}
        </p>
      )}
    </div>
  );
}

function CountTable({ label, head, rows }: { label: string; head: string; rows: DocumentCount[] }) {
  if (rows.length === 0) return null;
  return (
    <Table
      aria-label={label}
      size="sm"
      headers={[{ label: head }, { label: "Live" }, { label: "Deleted" }]}
      rows={rows.map((r) => ({
        id: r.name,
        items: [
          { label: <span className="break-all">{r.name}</span> },
          { label: <span className="tabular">{fmtInt(r.live)}</span> },
          { label: <span className="tabular">{fmtInt(r.deleted)}</span> },
        ],
      }))}
    />
  );
}

/** What the server said about a section it read and cut short. */
function Notes({ lines, testId }: { lines: string[]; testId: string }) {
  if (lines.length === 0) return null;
  return (
    <p className="text-xs text-text-muted" data-testid={testId}>
      Note: {lines.join("; ")}
    </p>
  );
}

/** The server skips a rule when the read it runs on failed (`coverage.rules_skipped`). */
function checksSkipped(result: KnowledgeInspection, codes: string[]): boolean {
  return codes.some((c) => result.coverage.rules_skipped.includes(c));
}

// server/knowledge/app/inspect.py _CHECKS, skipped together when the chunk read fails.
const CHUNK_CHECKS = [
  "documents_without_chunks",
  "unembedded_chunks",
  "chunks_under_other_model",
  "mixed_live_versions",
];
// store.INGEST_CHECKS, skipped together when the job read fails; extraction_incomplete reads
// the coverage and is skipped on its own.
const JOB_CHECKS = [
  "ingest_backlog",
  "extractor_unavailable",
  "ingest_missing_access",
  "ingest_content_rejected",
  "ingest_failed_on_server",
];

function ChunkSection({ result }: { result: KnowledgeInspection }) {
  const c = result.chunks;
  return (
    <section aria-labelledby="docs-chunks" className="space-y-2 text-sm">
      <SectionHeading id="docs-chunks" level={3} label="What search serves" />
      {c.embedder_model === "" ? (
        <p className="text-text-muted">
          The server's embedder could not be built, so nothing says which chunks search serves.
        </p>
      ) : c.live === null ? (
        <p className="text-text-muted">The chunks could not be counted.</p>
      ) : (
        <>
          <p className="text-text tabular" data-testid="docs-searchable">
            {n(c.searchable)} of {n(c.live)} live chunks searchable under{" "}
            <span className="font-mono break-all">{c.embedder_model}</span> · without a vector: {n(c.unembedded)}
          </p>
          {c.by_model.length > 1 && (
            <p className="text-xs text-text-muted">
              By embedding model:{" "}
              {c.by_model.map((m) => `${m.model} ${fmtInt(m.live)} (${fmtInt(m.embedded)} embedded)`).join(", ")}
            </p>
          )}
        </>
      )}
      <FindingGroups
        findings={result.findings}
        noun="document"
        notRun={checksSkipped(result, CHUNK_CHECKS) ? ["The chunk checks did not run."] : []}
      />
    </section>
  );
}

function IngestionSection({ result, notes }: { result: KnowledgeInspection; notes: (section: string) => string[] }) {
  const i = result.ingestions;
  const x = result.extraction;
  return (
    <section aria-labelledby="docs-ingestion" className="space-y-2 text-sm">
      <SectionHeading id="docs-ingestion" level={3} label="Ingestion and extraction" />
      <p className="text-xs text-text-muted">
        This server: ingest worker {i.worker_enabled ? "on" : "off"} · extractor{" "}
        {i.extractor_configured === null
          ? "settings not read"
          : i.extractor_configured
            ? "configured"
            : "not configured"}
        . Another replica may be set otherwise; the jobs say what happened.
      </p>
      {i.sources === null ? (
        <p className="text-text-muted">The ingestion jobs could not be read.</p>
      ) : i.sources === 0 ? (
        <p className="text-text-muted">No ingestion job in the last 30 days.</p>
      ) : (
        <p className="text-text tabular" data-testid="docs-jobs">
          {plural(i.sources, "source")} with a job · pending {n(i.pending)} · processing {n(i.processing)}
          {i.stalled ? ` (${fmtInt(i.stalled)} stalled)` : ""} · ready {n(i.ready)} · partial {n(i.partial)} · failed{" "}
          {n(i.failed)} · cancelled {n(i.cancelled)}
          {i.oldest_open_queued_at && (
            <>
              {" "}
              · oldest open queued <ListTime time={i.oldest_open_queued_at} />
            </>
          )}
        </p>
      )}
      {/* A read of its own: when it fails the count stands and nothing is listed. */}
      {readFailed(result, "ingestions.without_document") && (
        <p className="text-xs text-text-muted" data-testid="docs-without-document-unlisted">
          Sources with a job and no live document ({fmtInt(i.without_document)}): not listed.
        </p>
      )}
      {i.without_document_listed.length > 0 && (
        <div>
          <p className="text-xs text-text-muted">Sources with a job and no live document ({n(i.without_document)}):</p>
          <ul className="space-y-0.5" aria-label="Sources with a job and no live document">
            {i.without_document_listed.map((s) => (
              <li key={s.source_uri} className="text-xs">
                <span className="font-mono break-all">{s.source_uri}</span> <JobLine jobs={s} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {x.documents === null ? (
        <p className="text-text-muted">The extraction coverage could not be read.</p>
      ) : (
        x.documents > 0 && (
          <>
            <p className="text-text tabular" data-testid="docs-extraction">
              Extraction coverage: {n(x.incomplete)} of {plural(x.documents, "ingested document")} not complete
              {x.lost.length > 0 &&
                ` · lost: ${x.lost.map((l) => `${capability(l.name)} ${fmtInt(l.live)}`).join(", ")}`}
            </p>
            <ExactNames refs={x.lost.map((l) => l.name)} />
            <Notes lines={notes("extraction")} testId="docs-extraction-note" />
          </>
        )
      )}
      <FindingGroups
        findings={result.ingest_findings}
        noun="source"
        notRun={[
          ...(checksSkipped(result, JOB_CHECKS) ? ["The job checks did not run."] : []),
          ...(checksSkipped(result, ["extraction_incomplete"]) ? ["The extraction coverage check did not run."] : []),
        ]}
      />
    </section>
  );
}

/** Findings, one group per kind; a check that found nothing is listed on one line, and the
 *  checks the server skipped (their read failed) are said not to have run. */
function FindingGroups({
  findings,
  noun,
  notRun,
}: {
  findings: Array<DocumentFinding | IngestFinding>;
  noun: string;
  notRun: string[];
}) {
  return (
    <div className="space-y-3">
      {notRun.map((line) => (
        <p key={line} className="text-text-muted">
          {line}
        </p>
      ))}
      {KIND_GROUP.map(({ kind, label }) => {
        const mine = findings.filter((f) => f.kind === kind);
        if (mine.length === 0) return null;
        const hits = mine.filter((f) => f.count > 0);
        const clean = mine.filter((f) => f.count === 0);
        return (
          <div key={kind} data-kind={kind} className="space-y-2">
            <p className="section-label">{label}</p>
            {hits.length > 0 && (
              <ul className="space-y-2" aria-label={label}>
                {hits.map((f) => (
                  <FindingCard key={f.code} finding={f} noun={f.code === "extraction_incomplete" ? "document" : noun} />
                ))}
              </ul>
            )}
            {clean.length > 0 && (
              <>
                <p className="text-xs text-text-muted">
                  Checked, none found: {clean.map((f) => FINDING_LABEL[f.code] ?? f.code).join(", ")}
                </p>
                <ExactNames refs={clean.map((f) => f.code)} />
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** A share with hits never reads "0%", nor one short of the whole "100%". */
function percent(count: number, share: number): string {
  const p = Math.round(share * 100);
  if (count > 0 && p === 0) return "<1%";
  return share < 1 && p === 100 ? ">99%" : `${p}%`;
}

function FindingCard({ finding: f, noun }: { finding: DocumentFinding | IngestFinding; noun: string }) {
  const head = `finding-${f.code}`;
  const ids = "ingestion_ids" in f ? f.ingestion_ids : [];
  const ingested = new Set("ingested_samples" in f ? f.ingested_samples : []);
  // The server samples the ingested documents apart, so some may not be in `samples`.
  const ingestedOnly = [...ingested].filter((uri) => !f.samples.includes(uri));
  const more = f.count - f.samples.length - ingestedOnly.length;
  return (
    <li className="pane-card space-y-1.5" aria-labelledby={head} data-code={f.code} data-kind={f.kind}>
      <div className="flex flex-wrap items-center gap-2">
        <h4 id={head} className="font-semibold text-text">
          {FINDING_LABEL[f.code] ?? f.code}
        </h4>
        {f.severity === "warning" && <Badge text="Warning" state="warning" size="sm" />}
        <span className="text-xs text-text-muted font-mono">{f.code}</span>
      </div>
      <p className="text-text tabular">
        {plural(f.count, noun)}
        {f.population !== null && ` of ${fmtInt(f.population)}`} ({percent(f.count, f.share)})
        {"without_document" in f && f.without_document > 0 && ` · ${fmtInt(f.without_document)} with no live document`}
        {/* The remedy differs: a text document is written again, an ingested one ingested again. */}
        {"ingested" in f && f.ingested > 0 && ` · ${fmtInt(f.ingested)} ingested from a file`}
      </p>
      <p className="text-xs text-text-muted">{f.detail}</p>
      {f.samples.length > 0 && (
        <ul className="space-y-0.5" aria-label={`Samples of ${FINDING_LABEL[f.code] ?? f.code}`}>
          {f.samples.map((uri, k) => (
            <li key={uri} className="text-xs">
              <span className="font-mono break-all">{uri}</span>
              {ids[k] && <span className="text-text-muted"> · job {ids[k]}</span>}
              {ingested.has(uri) && <span className="text-text-muted"> · ingested from a file</span>}
            </li>
          ))}
        </ul>
      )}
      {ingestedOnly.length > 0 && (
        <p className="text-xs text-text-muted">
          Also ingested from a file: <span className="font-mono break-all">{ingestedOnly.join(", ")}</span>
        </p>
      )}
      {!f.samples_complete && more > 0 && <p className="text-xs text-text-muted">and {fmtInt(more)} more not listed</p>}
      <p className="text-xs text-text">What to do: {f.remedy}</p>
      <ExactNames refs={f.refs} />
      {f.uncertainty && <p className="text-xs text-text-muted">{f.uncertainty}</p>}
    </li>
  );
}

function stageText(label: string, s: DecisionStage): string {
  if (s.total === 0) return `${label}: none`;
  return `${label}: ${fmtInt(s.total)} (${fmtInt(s.confirmed)} confirmed, ${fmtInt(s.unconfirmed)} unconfirmed, ${fmtInt(
    s.source_changed,
  )} source changed, ${fmtInt(s.outcome_known)} with an outcome)`;
}

function JobLine({ jobs }: { jobs: SourceJobs }) {
  const j = jobs.current;
  return (
    <span className="text-text-muted">
      · job {j.ingestion_id}: {j.state}
      {j.stage && ` at ${j.stage}`}
      {j.stalled && ", stalled"}
      {j.failure_class && ` (${j.failure_class}${j.error_code ? `: ${j.error_code}` : ""})`}
      {jobs.replaced && " · replaced by a later write"}
    </span>
  );
}

/** The listings and the listed jobs have no field a failed read leaves null, and the server
 *  writes nothing but a failed read under their names in `skipped` ("listed: TimeoutError"). */
function readFailed(
  result: KnowledgeInspection,
  section: "listed" | "listed.jobs" | "ingestions.without_document",
): boolean {
  return result.skipped.some((s) => s.startsWith(`${section}: `));
}

/** Under the Postgres authority the server names, in `skipped`, the document commits not yet
 *  published to ClickHouse, or an authority that did not answer ("projection: … the document
 *  sections …"), and the listed documents it dropped because they were rewritten, deleted or
 *  gone in Postgres ("listed.heads: <n> document(s) …"): the listing and its count may be
 *  short, not cut at the sample limit. A failed check ("listed.heads: ValueError") claims
 *  nothing of the kind. */
function documentsMayBeShort(result: KnowledgeInspection): boolean {
  return result.skipped.some(
    (s) => /^listed\.heads: \d+ document/.test(s) || (s.startsWith("projection: ") && s.includes("document sections")),
  );
}

function ListedSection({ result }: { result: KnowledgeInspection }) {
  const live = result.documents.live;
  const short = !result.listed_complete && documentsMayBeShort(result);
  return (
    <section aria-labelledby="docs-listed" className="space-y-2 text-sm">
      <SectionHeading id="docs-listed" level={3} label="Newest documents" count={result.listed.length || undefined} />
      {readFailed(result, "listed") ? (
        <p className="text-text-muted">The documents could not be listed.</p>
      ) : (
        <>
          {short ? (
            <p className="text-xs text-text-muted">
              Some documents may be missing: their latest writes are not in the counts yet.
            </p>
          ) : (
            !result.listed_complete &&
            result.listed.length > 0 && (
              <p className="text-xs text-text-muted">
                {live === null
                  ? `Newest ${fmtInt(result.listed.length)} listed; the total was not counted.`
                  : result.listed.length < live
                    ? `Newest ${fmtInt(result.listed.length)} of ${fmtInt(live)} listed.`
                    : "The listing could not be checked against the latest writes."}
              </p>
            )
          )}
          {result.listed.length === 0 ? (
            !short && <p className="text-text-muted">No live document to list.</p>
          ) : (
            <ul className="space-y-2" aria-label="Newest documents">
              {result.listed.map((d) => (
                <DocumentRow key={d.source_uri} doc={d} jobsUnread={readFailed(result, "listed.jobs")} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function DocumentRow({ doc: d, jobsUnread }: { doc: InspectDocument; jobsUnread: boolean }) {
  const c = d.chunks;
  const x = d.extraction;
  return (
    <li className="pane-card space-y-1" data-source={d.source_uri}>
      <div className="flex flex-wrap items-center gap-x-2">
        <span className="font-semibold text-text break-words">{d.title || d.source_uri}</span>
        <span className="text-xs text-text-muted">
          {d.format} · revision {d.revision || "unnumbered"}
        </span>
        <ListTime time={d.ingested_at} />
      </div>
      <p className="text-xs font-mono break-all text-text-muted">{d.source_uri}</p>
      <p className="text-xs text-text tabular">
        {c === null
          ? "Chunks: not read"
          : `Chunks: ${fmtInt(c.searchable)} of ${fmtInt(c.live)} searchable · ${fmtInt(c.unembedded)} without a vector${
              c.other_model ? ` · ${fmtInt(c.other_model)} under another model` : ""
            }${c.versions > 1 ? ` · ${fmtInt(c.versions)} versions served` : ""}${
              c.live
                ? ` · ${fmtInt(c.min_chars)}/${fmtInt(c.median_chars)}/${fmtInt(c.max_chars)} characters (min/median/max)`
                : ""
            }`}
        {d.retired_versions !== null && d.retired_versions > 0 && ` · ${plural(d.retired_versions, "retired version")}`}
      </p>
      <p className="text-xs text-text tabular">
        {d.decisions === null ? (
          "Decisions: not read"
        ) : (
          <>
            {stageText("Decided", d.decisions.decided)} · {stageText("Proposed", d.decisions.proposed)}
          </>
        )}
      </p>
      <p className="text-xs text-text-muted">
        {x === null
          ? "Extraction: none (a text write, which no extractor read)"
          : `Extraction: ${fmtInt(x.extracted)} of ${fmtInt(x.total)} ${x.unit} extracted${
              x.failed ? `, ${fmtInt(x.failed)} failed` : ""
            }${x.lost.length ? `, lost ${x.lost.map(capability).join(", ")}` : ""}${x.complete ? " (complete)" : ""}`}
      </p>
      {x && <ExactNames refs={x.lost} />}
      {d.jobs ? (
        <p className="text-xs">
          <JobLine jobs={d.jobs} />
        </p>
      ) : (
        jobsUnread && <p className="text-xs text-text-muted">Jobs: not read</p>
      )}
    </li>
  );
}
