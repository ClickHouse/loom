import { fmtInt } from "../../lib/format";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Table } from "@clickhouse/click-ui/Table";
import type { ChunkStats, ChunkingSample, NamespaceTune, TuneApply, TuneProposal, TuneTarget } from "../../api";
import { AgentBadge } from "../../components/AgentBadge";
import { KeyButton } from "../../components/ui/KeyButton";
import { CodeBlock } from "./CodeBlock";
import { useStore } from "../../lib/state";

const TARGET_LABEL: Record<TuneTarget, string> = {
  "extraction_instructions.request": "Extraction instructions (per request)",
  "namespace.category": "Category",
  "namespace.guide": "Namespace guide",
  "settings.knowledge_chunking": "Chunk size and stride (deployment setting)",
  "settings.document_extractors_json": "Extractor routes (deployment setting)",
};

// The drafts whose target cuts or routes documents: they shape documents written after the
// change, not memories, and their evidence cites source URIs.
const DOCUMENT_TARGETS = new Set<TuneTarget>(["settings.knowledge_chunking", "settings.document_extractors_json"]);

/** The drafts namespace.tune returned. Each is a proposal only: the page applies none, so
 *  each card names the write and guard a person would make, and changes nothing. */
export function ReviewChanges({ tune }: { tune: NamespaceTune }) {
  return (
    <div className="space-y-4" data-testid="assistant-review">
      {tune.proposals.length === 0 ? (
        <p className="text-sm text-text-muted" role="status">
          {tune.skipped.length === 0
            ? "The evidence supports no change."
            : "No drafts. What was skipped, and why, is listed below."}
        </p>
      ) : (
        <ul className="space-y-4" aria-label="Pending proposals">
          {tune.proposals.map((p) => (
            <ProposalCard key={p.id} proposal={p} />
          ))}
        </ul>
      )}
      {tune.chunking && <SampleNote sample={tune.chunking} />}
      {/* With no chunking draft the sample's own measures are the evidence. */}
      {tune.chunking && !tune.proposals.some((p) => p.preview) && <SampleStats stats={tune.chunking.current} />}
      {tune.extraction_policy === null && tune.extraction_policy_unavailable && (
        <p className="text-xs text-text-muted">
          Operator extraction policy not read: {tune.extraction_policy_unavailable}
        </p>
      )}
      {tune.skipped.length > 0 && (
        <div className="text-xs text-text-muted" data-testid="tune-skipped">
          {/* A failed read (`<section>: <error>`, which may leave a draft without its cited rows
              or counts) or evidence that ruled a draft out. */}
          <p>Skipped:</p>
          <ul className="list-disc pl-5">
            {tune.skipped.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** What the counter count is for each target (server: TuneEvidence.counter_count). */
function counterText(target: TuneTarget, n: number): string {
  const rows = fmtInt(n);
  if (target === "namespace.category") return `${rows} already filed under another category`;
  if (target === "settings.knowledge_chunking") return `${rows} sampled documents it does not flag`;
  if (target === "settings.document_extractors_json") return `${rows} documents of these formats extracted whole`;
  return `at least ${rows} unflagged rows it must keep working for`;
}

/** The write as a person would make it: an op, an admin endpoint, or a variable set in the
 *  deployment. A server a release behind sends no `via`: its drafts are all ops. */
function writeName(a: TuneApply): string {
  if (a.via === "admin_route") return `POST /v1/admin/${a.op}`;
  if (a.via === "environment") return `${a.op} (environment)`;
  return a.op;
}

/** A required role as a reader says it: `platform_admin` is "platform admin". */
const roleName = (role: string) => role.replaceAll("_", " ");

/** Who makes the change and how, since this page applies no draft. */
function howToApply(p: TuneProposal): string {
  const a = p.apply;
  const role = roleName(a.required_role);
  const who = `${/^[aeiou]/i.test(role) ? "An" : "A"} ${role}`;
  if (a.via === "environment")
    return `${who} sets the variable below in the deployment; it takes effect once each server and worker restarts.`;
  if (a.via === "admin_route") return `${who} sends the admin request below.`;
  return a.args_complete ? `${who} on this namespace sends the request below.` : "No single request applies this.";
}

/** What the line above a write's arguments says they are. */
function argsCaption(a: TuneApply): string {
  if (a.via === "environment")
    return a.args_complete ? "Value to set:" : "Value to set, once each placeholder is filled in:";
  if (a.via === "admin_route") return a.args_complete ? "Arguments:" : "Arguments, once each placeholder is filled in:";
  return a.args_complete ? "Arguments:" : `Each writer adds these arguments to its own ${a.op} requests:`;
}

function ApplyBlock({ apply: a, testId }: { apply: TuneApply; testId: string }) {
  return (
    <div className="space-y-1" data-via={a.via ?? "op"}>
      <p className="text-xs text-text-muted">
        <span className="font-mono">{writeName(a)}</span> · {roleName(a.required_role)} · {a.guard}
      </p>
      <p className="text-xs text-text-muted">{argsCaption(a)}</p>
      <CodeBlock testId={testId}>{JSON.stringify(a.args, null, 2)}</CodeBlock>
    </div>
  );
}

/** The document sample a chunking draft is judged on: the namespace's documents re-chunked
 *  in process at the current values, capped; no model runs and no text is returned. */
function SampleNote({ sample: s }: { sample: ChunkingSample }) {
  return (
    <p className="text-xs text-text-muted" data-testid="tune-sample">
      Document sample: {fmtInt(s.documents)} of{" "}
      {s.population === null ? "an uncounted number of" : fmtInt(s.population)} live documents re-chunked at the current
      values (cap {fmtInt(s.cap)}, at most {fmtInt(s.load_cap)} characters and bytes loaded
      {s.unread ? `; ${fmtInt(s.unread)} picked and not read` : ""}) · embedder{" "}
      <span className="font-mono break-all">{s.embedder_model}</span>, input limit{" "}
      {s.input_token_limit == null ? "not known" : `${fmtInt(s.input_token_limit)} tokens`}. No model runs and nothing
      is written.
    </p>
  );
}

const PREVIEW_ROWS: Array<[string, (s: ChunkStats) => string]> = [
  ["Size / stride", (s) => `${fmtInt(s.size)} / ${fmtInt(s.stride)}`],
  ["Chunks", (s) => fmtInt(s.chunks)],
  [
    "Characters per chunk (min / median / p90 / max)",
    (s) => `${fmtInt(s.min_chars)} / ${fmtInt(s.median_chars)} / ${fmtInt(s.p90_chars)} / ${fmtInt(s.max_chars)}`,
  ],
  ["Sections cut by size", (s) => `${fmtInt(s.sections_cut)} of ${fmtInt(s.sections)}`],
  ["Largest embedding input (tokens)", (s) => fmtInt(s.max_input_tokens)],
  [
    "Over the embedder's input limit",
    (s) => (s.over_input_limit == null ? "limit not known" : fmtInt(s.over_input_limit)),
  ],
];

function SampleStats({ stats }: { stats: ChunkStats }) {
  return (
    <Table
      aria-label="The document sample at the current values"
      size="sm"
      headers={[{ label: "On the sample" }, { label: "Now" }]}
      rows={PREVIEW_ROWS.map(([label, value]) => ({
        id: label,
        items: [
          { label: <span className="whitespace-normal">{label}</span> },
          { label: <span className="tabular">{value(stats)}</span> },
        ],
      }))}
    />
  );
}

function ChunkPreview({ title, before, after }: { title: string; before: ChunkStats; after: ChunkStats }) {
  return (
    <Table
      aria-label={`Before and after: ${title}`}
      size="sm"
      headers={[{ label: "On the same sample" }, { label: "Now" }, { label: "Proposed" }]}
      rows={PREVIEW_ROWS.map(([label, value]) => ({
        id: label,
        items: [
          // A cell keeps to one line; a label this long wraps instead of widening the card.
          { label: <span className="whitespace-normal">{label}</span> },
          { label: <span className="tabular">{value(before)}</span> },
          { label: <span className="tabular">{value(after)}</span> },
        ],
      }))}
    />
  );
}

function ProposalCard({ proposal: p }: { proposal: TuneProposal }) {
  const inspect = useStore((s) => s.inspect);
  const head = `proposal-${p.id}`;
  const more = p.affected_agents_total === null ? 0 : Math.max(0, p.affected_agents_total - p.affected_agents.length);
  return (
    <li className="pane-card space-y-2 text-sm" aria-labelledby={head} data-proposal={p.id} data-target={p.target}>
      <div className="flex flex-wrap items-center gap-2">
        {/* A plain heading, not SectionHeading: a title carries a tag or prefix of any
            length and must wrap, and SectionHeading's label does not. */}
        <h3 id={head} className="font-semibold text-text break-words">
          {p.title}
        </h3>
        <Badge text="Draft, not approved" state="neutral" size="sm" />
      </div>
      <p className="text-xs text-text-muted">
        {TARGET_LABEL[p.target] ?? p.target} · {p.scope}
      </p>
      <p className="text-text">{p.rationale}</p>
      <div className="grid gap-2 md:grid-cols-2">
        <figure>
          <figcaption className="text-xs text-text-muted">
            Current{p.current_revision ? ` (revision ${p.current_revision})` : ""}
          </figcaption>
          <CodeBlock side="current">{p.current || "(empty)"}</CodeBlock>
        </figure>
        <figure>
          <figcaption className="text-xs text-text-muted">Proposed</figcaption>
          <CodeBlock side="proposed">{p.proposed}</CodeBlock>
        </figure>
      </div>
      <p className="text-xs text-text-muted">
        Evidence: {p.evidence.finding_codes.length > 0 && `${p.evidence.finding_codes.join(", ")} · `}
        {fmtInt(p.evidence.supporting_count)} supporting
        {p.evidence.counter_count !== null && ` · ${counterText(p.target, p.evidence.counter_count)}`}
      </p>
      {p.preview && <ChunkPreview title={p.title} before={p.preview.before} after={p.preview.after} />}
      {(p.evidence.documents?.length ?? 0) > 0 && (
        <ul className="space-y-0.5" aria-label={`Documents cited for ${p.title}`}>
          {p.evidence.documents?.map((uri) => (
            <li key={uri} className="text-xs font-mono break-all">
              {uri}
            </li>
          ))}
        </ul>
      )}
      {p.evidence.supporting.length > 0 && (
        <ul className="flex flex-wrap gap-x-3" aria-label={`Rows cited for ${p.title}`}>
          {p.evidence.supporting.map((r) => (
            <li key={r.memory_id}>
              <KeyButton
                memoryKey={r.memory_key}
                onInspect={(k) => inspect(k, { agent: r.agent, userId: r.user_id, sessionId: r.session_id })}
              />
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-text-muted" data-testid="affected-agents">
        <span>Affected agents:</span>
        {p.affected_agents_total === null ? (
          <span>the namespace's writers could not be read</span>
        ) : p.affected_agents.length === 0 ? (
          <span>none</span>
        ) : (
          <>
            {p.affected_agents.map((a) => (
              <AgentBadge key={a} agent={a} />
            ))}
            {more > 0 && <span>and {fmtInt(more)} more</span>}
          </>
        )}
      </div>
      <p className="text-xs text-text-muted">
        Applies to {DOCUMENT_TARGETS.has(p.target) ? "documents" : "memories"} written after the change. {p.historical}
        {p.limits && ` ${p.limits}`}
      </p>
      <div className="space-y-2">
        <p className="text-xs text-text" data-testid="how-to-apply">
          <span className="font-medium">How to apply:</span> {howToApply(p)}
        </p>
        <ApplyBlock apply={p.apply} testId="proposal-args" />
        {(p.apply_also?.length ?? 0) > 0 && (
          <div className="space-y-2" data-testid="proposal-also">
            <p className="text-xs text-text-muted">Then, in order:</p>
            {p.apply_also?.map((a, k) => (
              <ApplyBlock key={`${a.op}-${k}`} apply={a} testId="proposal-also-args" />
            ))}
          </div>
        )}
      </div>
    </li>
  );
}
