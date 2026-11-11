import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { useCardLinked } from "./linked";
import { Link } from "@clickhouse/click-ui/Link";
import { Table } from "@clickhouse/click-ui/Table";
import type { MentorBriefResult, MentorHint, MentorPlane, MentorSeverity } from "../../../api";
import { isNamespaceAdmin } from "../../../lib/role";
import { useStore } from "../../../lib/state";
import { planePage } from "./planes";

/**
 * The mentor card's body: what `mentor.brief` says the store tells an agent working in this
 * namespace — the planes in use, available and never called; the hints with the counts they
 * fired on, each linked to the page that owns its plane; and adoption, sessions where a hint
 * reached the agent and how many then called a hinted op. Never a write: the mentor advises,
 * the agent acts.
 */

const SEVERITY_RANK: Record<MentorSeverity, number> = { critical: 0, warning: 1, info: 2 };

/** The mentor's hint codes, as a person reads them; an unknown code (a rule added later)
 *  falls back to its words. */
const CODE_LABELS: Record<string, string> = {
  recall_by_key_only: "Recall by key only",
  writes_never_read: "Writes never read",
  no_session_pack: "No session pack",
  code_index_unused: "Code index unused",
  tracker_unused: "Tracker record unused",
  bisect_available: "Bisect available",
  documents_as_memories: "Documents as memories",
  checkpoints_as_memory: "Checkpoints as memory",
  duplicates_written: "Duplicates written",
  conflicts_pending: "Conflicts pending",
  records_for_identities: "Records for identities",
  coordination_loop: "Coordination loop",
  facts_graph_unused: "Entity graph unused",
  runs_unused: "Runs unused",
  search_abstains: "Search abstains",
  agent_unnamed: "Agent unnamed",
  get_miss: "Get miss",
  search_identifier_query: "Identifier sent to memory",
  search_abstain_code: "Abstain: code index",
  search_abstain_knowledge: "Abstain: documents",
  set_duplicate: "Duplicate write",
  set_large_value: "Large value",
  set_churn: "Key churn",
  set_stack_trace: "Stack trace as memory",
  set_issue_reference: "Issue reference",
};

export function humanizeHintCode(code: string): string {
  const known = CODE_LABELS[code];
  if (known) return known;
  const words = code.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : code;
}

/** Critical first, then warning, then info; ties keep the server's order. */
export function sortHints(hints: readonly MentorHint[]): MentorHint[] {
  return [...hints].sort((a, b) => (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3));
}

export type PlaneState = "in use" | "available" | "unknown";

/** A plane the agent called is in use; one the namespace offers but the
 *  agent never called is the mentor's material — only for planes the mentor
 *  has a rule for; one the mentor could not tell about is unknown. Planes
 *  the namespace lacks, and un-mentored planes never called, are left out. */
export function planeState(p: MentorPlane): PlaneState | null {
  if (p.calls > 0) return "in use";
  if (p.mentored === false) return null;
  if (p.available === null) return "unknown";
  return p.available ? "available" : null;
}

function evidenceText(evidence: Record<string, unknown>): string {
  return Object.entries(evidence)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("|") : String(v)}`)
    .join(", ");
}

const SEVERITY_STATE: Record<MentorSeverity, BadgeState> = {
  critical: "danger",
  warning: "warning",
  info: "neutral",
};

export function SeverityBadge({ severity }: { severity: MentorSeverity }) {
  return <Badge text={severity} state={SEVERITY_STATE[severity] ?? "neutral"} size="sm" />;
}

/** `namespace` is the conversation's, which the hints were read in: an admin page link
 *  shows only to an admin of it. */
export function MentorBody({ result, namespace }: { result: MentorBriefResult; namespace: string }) {
  const whoami = useStore((s) => s.whoami);
  const mine = useStore((s) => s.myNamespaces);
  const admin = isNamespaceAdmin(whoami, mine, namespace);
  const hints = sortHints(result.hints);
  const planes = result.planes.map((p) => ({ plane: p, state: planeState(p) })).filter((x) => x.state);
  const inUse = planes.filter((x) => x.state === "in use");
  const available = planes.filter((x) => x.state === "available");
  return (
    <div className="space-y-3">
      <p className="text-sm text-text leading-relaxed" data-testid="mentor-summary">
        <span className={`font-semibold ${result.healthy ? "text-success" : "text-warn"}`}>
          {result.healthy ? "Using the planes it has" : "Has planes it never uses"}
        </span>
        <span className="text-text-muted">
          {" "}
          · agent <span className="font-mono">{result.agent}</span>
          {result.agent_defaulted && <> (the most active writer here; pick another above)</>} ·{" "}
          {result.sessions.toLocaleString()} sessions over {Math.round(result.window_seconds / 86_400)}d · in use{" "}
          {inUse.map((x) => x.plane.plane).join(", ") || "none"}
          {available.length > 0 && <> · never called {available.map((x) => x.plane.plane).join(", ")}</>}
        </span>
      </p>
      {hints.length === 0 ? (
        <p className="text-sm text-text-muted" role="status">
          Nothing to say in this window.
        </p>
      ) : (
        <ul className="space-y-2" aria-label="Mentor hints">
          {hints.map((h) => (
            <HintRow key={h.code} hint={h} admin={admin} />
          ))}
        </ul>
      )}
      {result.adoption.length > 0 && (
        <Table
          aria-label="Hint adoption"
          data-testid="mentor-adoption"
          size="sm"
          headers={[{ label: "Hint" }, { label: "Shown", width: "96px" }, { label: "Followed", width: "96px" }]}
          rows={result.adoption.map((a) => ({
            id: a.code,
            items: [
              { label: humanizeHintCode(a.code) },
              { label: <span className="tabular">{a.shown_sessions.toLocaleString()}</span> },
              { label: <span className="tabular">{a.followed_sessions.toLocaleString()}</span> },
            ],
          }))}
        />
      )}
      {result.skipped.length > 0 && (
        <p className="text-xs text-text-muted" data-testid="mentor-skipped">
          Could not read: {result.skipped.join("; ")}
        </p>
      )}
      <p className="text-xs text-text-muted leading-relaxed">
        The same hints reach the agent unasked — up to three in <code>context.pack</code> at session start and one per
        call when a rule fires. The <code>loom-mentor</code> skill has the table; <code>loom-optimize</code> reworks the
        rows a data-shape hint names, with your approval. The dashboard shows; it does not rewrite.
      </p>
    </div>
  );
}

function HintRow({ hint: h, admin }: { hint: MentorHint; admin: boolean }) {
  const linked = useCardLinked();
  const page = linked ? planePage(h.plane, admin) : null;
  return (
    <li
      data-testid="mentor-hint"
      data-code={h.code}
      data-severity={h.severity}
      className="rounded-md border border-border bg-surface-2/60 p-2.5 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={h.severity} />
        <span className="font-medium text-text">{humanizeHintCode(h.code)}</span>
        {h.ops.length > 0 && <span className="font-mono text-xs text-text-muted">{h.ops.join(" · ")}</span>}
      </div>
      <p className="mt-1 text-text leading-relaxed">{h.text}</p>
      {Object.keys(h.evidence).length > 0 && (
        <p className="mt-1 font-mono text-xs text-text-muted">evidence: {evidenceText(h.evidence)}</p>
      )}
      {page && (
        <Link
          component="button"
          type="button"
          size="sm"
          onClick={() =>
            useStore.getState().navigate(page.view, {
              ...(page.query ? { query: page.query } : {}),
              ...(page.arriveFocus ? { arriveFocus: page.arriveFocus } : {}),
            })
          }
        >
          {`Open in ${page.page}`}
        </Link>
      )}
    </li>
  );
}
