import { useEffect, useRef, useState } from "react";
import { GraduationCap } from "lucide-react";
import {
  mentorBrief,
  type Identity,
  type MentorBriefResult,
  type MentorHint,
  type MentorPlane,
  type MentorSeverity,
} from "../api";
import { useStore } from "../lib/state";
import { useNamespaceRole } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { humanizeError } from "../lib/format";
import { Panel, SectionHeading } from "./ui";
import { toast } from "./Toast";

/**
 * "Mentor" — the owner's view of what the store tells an agent working in
 * this namespace. One click runs `mentor.brief` for the current org/namespace and an
 * agent name (empty = the caller's own) over a 7 / 30-day ledger window and renders what
 * the CLI table prints: the planes in use, available and never called; the hints with the
 * counts they fired on; and adoption — sessions where a hint reached the agent and how
 * many then called a hinted op. Never on mount (one bounded ledger read per agent, cached
 * an hour server-side); never a write: the mentor advises, the agent acts.
 */

const WINDOWS: Array<{ label: string; seconds: number }> = [
  { label: "7 days", seconds: 7 * 86_400 },
  { label: "30 days", seconds: 30 * 86_400 },
];

const SEVERITY_RANK: Record<MentorSeverity, number> = { critical: 0, warning: 1, info: 2 };

/** The mentor's hint codes, as a person reads them; an unknown code (a rule added later)
 *  falls back to its words. */
const CODE_LABELS: Record<string, string> = {
  recall_by_key_only: "Recall by key only",
  writes_never_read: "Writes never read",
  no_session_pack: "No session pack",
  code_index_unused: "Code index unused",
  tracker_unused: "tracker record unused",
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

const BTN =
  "inline-flex items-center justify-center gap-1.5 min-h-9 px-3 text-sm font-medium rounded-md transition-colors pointer-coarse:min-h-11";

function SeverityBadge({ severity }: { severity: MentorSeverity }) {
  const tone =
    severity === "critical"
      ? "bg-[var(--color-danger-soft)] text-[var(--color-danger)]"
      : severity === "warning"
        ? "bg-[var(--color-warning-soft)] text-[var(--color-warning)]"
        : "bg-[var(--color-surface-2)] text-[var(--color-text-muted)]";
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-2xs uppercase tracking-wider font-semibold ${tone}`}>
      {severity}
    </span>
  );
}

export function MentorPanel({ identity }: { identity: Identity }) {
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const allowed = canCall("mentor.brief", role, whoami);
  const [windowSeconds, setWindowSeconds] = useState(WINDOWS[0].seconds);
  const [agent, setAgent] = useState("");
  const [result, setResult] = useState<MentorBriefResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A different namespace is a different brief; a run still in flight for the old one
  // must not land under the new one.
  const scope = `${identity.org}/${identity.namespace}`;
  const scopeRef = useRef(scope);
  useEffect(() => {
    scopeRef.current = scope;
    setResult(null);
    setError(null);
  }, [scope]);

  if (!allowed) return null;

  const run = async () => {
    const started = scope;
    setRunning(true);
    setError(null);
    try {
      const r = await mentorBrief(
        { org: identity.org, namespace: identity.namespace },
        { agent: agent.trim() || undefined, windowSeconds },
      );
      if (scopeRef.current !== started) return;
      setResult(r);
    } catch (e) {
      if (scopeRef.current !== started) return;
      const text = humanizeError(e);
      setError(text);
      toast(`Mentor brief failed: ${text}`, "error");
    } finally {
      setRunning(false);
    }
  };

  const hints = result ? sortHints(result.hints) : [];
  const planes = result ? result.planes.map((p) => ({ plane: p, state: planeState(p) })).filter((x) => x.state) : [];
  const inUse = planes.filter((x) => x.state === "in use");
  const available = planes.filter((x) => x.state === "available");

  return (
    <Panel as="section" aria-labelledby="mentor-heading" className="mb-5" padding="md" data-testid="mentor-panel">
      <SectionHeading
        id="mentor-heading"
        level={2}
        label="Mentor"
        caption="what the store tells an agent working here"
        hint="Reads one agent's calls in this namespace and names the planes it never used — the code index, the tracker source, documents, records, offload, the entity graph — with the counts behind each hint, and whether past hints were followed. Read-only; the agent acts, the mentor advises."
        actions={
          <span className="inline-flex flex-wrap items-center gap-2 text-sm">
            <label className="inline-flex items-center gap-1.5 text-[var(--color-text-muted)]">
              <span className="sr-only">Agent</span>
              {result && result.agents.length > 0 ? (
                <select
                  aria-label="Agent"
                  value={agent || result.agent}
                  onChange={(e) => setAgent(e.target.value)}
                  disabled={running}
                  className="min-h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 text-sm text-[var(--color-text)] pointer-coarse:min-h-11"
                >
                  {[result.agent, ...result.agents.filter((a) => a !== result.agent)].map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  aria-label="Agent"
                  placeholder="agent (default: most active writer)"
                  value={agent}
                  onChange={(e) => setAgent(e.target.value)}
                  disabled={running}
                  className="min-h-9 w-52 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 text-sm text-[var(--color-text)] pointer-coarse:min-h-11"
                />
              )}
            </label>
            <label className="inline-flex items-center gap-1.5 text-[var(--color-text-muted)]">
              <span className="sr-only">Mentor window</span>
              <select
                aria-label="Mentor window"
                value={windowSeconds}
                onChange={(e) => setWindowSeconds(Number(e.target.value))}
                disabled={running}
                className="min-h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 text-sm text-[var(--color-text)] pointer-coarse:min-h-11"
              >
                {WINDOWS.map((w) => (
                  <option key={w.seconds} value={w.seconds}>
                    {w.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={run}
              disabled={running}
              aria-busy={running}
              className={`${BTN} bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] disabled:opacity-60 disabled:cursor-wait`}
            >
              <GraduationCap size={13} aria-hidden="true" />{" "}
              {running ? "Asking the mentor…" : result ? "Ask again" : "Ask the mentor"}
            </button>
          </span>
        }
      />

      {!result && !running && !error && (
        <p className="text-sm text-[var(--color-text-muted)] leading-relaxed max-w-2xl">
          Which loom planes does an agent working here never touch? Ask the mentor to see the planes it uses, the ones
          this namespace offers that it never called, and whether the hints it was given were followed. Leave the agent
          empty for the namespace's most active writer; the dashboard's own traffic is never the subject.
        </p>
      )}

      {running && !result && (
        <p role="status" className="text-sm text-[var(--color-text-muted)]">
          Reading the agent's ledger…
        </p>
      )}

      {error && (
        <p role="alert" className="text-sm text-[var(--color-danger)]">
          Mentor brief failed: {error}
        </p>
      )}

      {result && (
        <div className="space-y-3" aria-busy={running}>
          <p className="text-sm text-[var(--color-text)] leading-relaxed" data-testid="mentor-summary">
            <span
              className={`font-semibold ${result.healthy ? "text-[var(--color-success)]" : "text-[var(--color-warning)]"}`}
            >
              {result.healthy ? "Using the planes it has" : "Has planes it never uses"}
            </span>
            <span className="text-[var(--color-text-muted)]">
              {" "}
              · agent <span className="font-mono">{result.agent}</span>
              {result.agent_defaulted && <> (the most active writer here; pick another above)</>} ·{" "}
              {result.sessions.toLocaleString()} sessions over {Math.round(result.window_seconds / 86_400)}d · in use{" "}
              {inUse.map((x) => x.plane.plane).join(", ") || "none"}
              {available.length > 0 && <> · never called {available.map((x) => x.plane.plane).join(", ")}</>}
            </span>
          </p>

          {hints.length === 0 ? (
            <p className="text-sm text-[var(--color-text-muted)]" role="status">
              Nothing to say in this window.
            </p>
          ) : (
            <ul className="space-y-2" aria-label="Mentor hints">
              {hints.map((h) => (
                <li
                  key={h.code}
                  data-testid="mentor-hint"
                  data-code={h.code}
                  data-severity={h.severity}
                  className="rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)]/60 p-2.5 text-sm"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityBadge severity={h.severity} />
                    <span className="font-medium text-[var(--color-text)]">{humanizeHintCode(h.code)}</span>
                    {h.ops.length > 0 && (
                      <span className="font-mono text-2xs text-[var(--color-text-muted)]">{h.ops.join(" · ")}</span>
                    )}
                  </div>
                  <p className="mt-1 text-[var(--color-text)] leading-relaxed">{h.text}</p>
                  {Object.keys(h.evidence).length > 0 && (
                    <p className="mt-1 font-mono text-2xs text-[var(--color-text-muted)]">
                      evidence: {evidenceText(h.evidence)}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}

          {result.adoption.length > 0 && (
            <div className="overflow-x-auto -mx-1 px-1">
              <table className="text-sm border-collapse" aria-label="Hint adoption" data-testid="mentor-adoption">
                <thead>
                  <tr className="text-left text-2xs uppercase tracking-wider text-[var(--color-text-muted)]">
                    <th scope="col" className="py-1.5 pr-4">
                      Hint
                    </th>
                    <th scope="col" className="py-1.5 pr-4 text-right">
                      Shown
                    </th>
                    <th scope="col" className="py-1.5 text-right">
                      Followed
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.adoption.map((a) => (
                    <tr key={a.code} className="border-t border-[var(--color-border)]">
                      <td className="py-1.5 pr-4">{humanizeHintCode(a.code)}</td>
                      <td className="py-1.5 pr-4 text-right tabular">{a.shown_sessions.toLocaleString()}</td>
                      <td className="py-1.5 text-right tabular">{a.followed_sessions.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {result.skipped.length > 0 && (
            <p className="text-xs text-[var(--color-text-muted)]" data-testid="mentor-skipped">
              Could not read: {result.skipped.join("; ")}
            </p>
          )}

          <p className="text-xs text-[var(--color-text-muted)] leading-relaxed">
            The same hints reach the agent unasked — up to three in <code>context.pack</code> at session start and one
            per call when a rule fires. The <code>loom-mentor</code> skill has the table; <code>loom-optimize</code>{" "}
            reworks the rows a data-shape hint names, with your approval. The dashboard shows; it does not rewrite.
          </p>
        </div>
      )}
    </Panel>
  );
}
