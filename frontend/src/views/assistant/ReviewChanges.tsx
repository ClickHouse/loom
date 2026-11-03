import { fmtInt } from "../../lib/format";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import type { NamespaceTune, TuneProposal, TuneTarget } from "../../api";
import { Unavailable } from "../../components/ui";
import { AgentBadge } from "../../components/AgentBadge";
import { KeyButton } from "../../components/ui/KeyButton";
import { CodeBlock } from "./CodeBlock";
import { useStore } from "../../lib/state";

const TARGET_LABEL: Record<TuneTarget, string> = {
  "extraction_instructions.request": "Extraction instructions (per request)",
  "namespace.category": "Category",
  "namespace.guide": "Namespace guide",
};

/** The drafts namespace.tune returned. Each is a proposal only: Apply is not in this
 *  release, so the panel names the op and guard a person would run, and changes nothing. */
export function ReviewChanges({ tune }: { tune: NamespaceTune | null }) {
  if (!tune) {
    return (
      <p className="text-sm text-text-muted">
        No drafts yet. "Configure this project" reads the namespace and drafts changes to what creates its memories.
      </p>
    );
  }
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
  return target === "namespace.category"
    ? `${rows} already filed under another category`
    : `at least ${rows} unflagged rows it must keep working for`;
}

/** Why Apply is unavailable, and who would make the change instead. */
function applyReason(p: TuneProposal): string {
  return p.apply.args_complete
    ? `Apply is not in this release. A ${p.apply.required_role} on this namespace sends ${p.apply.op} with the arguments below.`
    : "Apply is not in this release, and no single call applies this.";
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
        Applies to memories written after the change. {p.historical}
        {p.limits && ` ${p.limits}`}
      </p>
      <div className="space-y-1">
        <div className="flex flex-wrap items-center gap-3">
          <Unavailable reason={applyReason(p)}>
            <Button type="secondary" htmlType="button">
              Apply
            </Button>
          </Unavailable>
          <p className="text-xs text-text-muted">
            <span className="font-mono">{p.apply.op}</span> · {p.apply.required_role} · {p.apply.guard}
          </p>
        </div>
        <p className="text-xs text-text-muted">
          {p.apply.args_complete ? "Arguments:" : `Each writer adds these arguments to its own ${p.apply.op} requests:`}
        </p>
        <CodeBlock testId="proposal-args">{JSON.stringify(p.apply.args, null, 2)}</CodeBlock>
      </div>
    </li>
  );
}
