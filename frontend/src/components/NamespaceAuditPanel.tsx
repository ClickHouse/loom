import { useEffect, useRef, useState } from "react";
import { Activity, ChevronDown, ChevronRight } from "lucide-react";
import {
  auditNamespace,
  type Identity,
  type NamespaceAuditFinding,
  type NamespaceAuditResult,
  type NamespaceAuditSeverity,
} from "../api";
import { useStore } from "../lib/state";
import { useNamespaceRole } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { humanizeError } from "../lib/format";
import { Panel, SectionHeading } from "./ui";
import { toast } from "./Toast";

/**
 * "Usage audit" — the Memories view's namespace-level report. One click runs
 * `namespace.audit` for the current org/namespace over a 1 / 7 / 30-day ledger window and
 * renders what the CLI table prints: the summary line, the findings sorted critical →
 * warning → info, and the checks that could not run. It never runs on mount — the op is
 * ~14 reads with a 3 s budget — and it never writes: the remedy for what it names is the
 * optimize recipe the owner's agent runs with approval, not a button here.
 *
 * Samples are keys and counts only (the op never returns a value); a key
 * sample opens that memory in the inspector.
 */

const WINDOWS: Array<{ label: string; seconds: number }> = [
  { label: "1 day", seconds: 86_400 },
  { label: "7 days", seconds: 7 * 86_400 },
  { label: "30 days", seconds: 30 * 86_400 },
];

const SAMPLE_LIMIT = 5;

const SEVERITY_RANK: Record<NamespaceAuditSeverity, number> = { critical: 0, warning: 1, info: 2 };

/** The namespace audit's ten finding codes, as a person reads them. An unknown code (a
 *  check added later) falls back to its words. */
const CODE_LABELS: Record<string, string> = {
  exact_key_store: "Exact-key store",
  hand_rolled_index_rows: "Hand-rolled index rows",
  double_encoded_values: "Double-encoded values",
  json_prefix_excerpts: "JSON-prefix excerpts",
  oversized_values: "Oversized values",
  unembedded_rows: "Unembedded rows",
  hash_keys: "Hash keys",
  coordination_as_memory: "Coordination as memory",
  episodic_blobs: "Episodic blobs",
  version_churn: "Version churn",
};

function humanizeAuditCode(code: string): string {
  const known = CODE_LABELS[code];
  if (known) return known;
  const words = code.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : code;
}

/** Critical first, then warning, then info; ties by count, largest first. */
function sortFindings(findings: readonly NamespaceAuditFinding[]): NamespaceAuditFinding[] {
  return [...findings].sort((a, b) => {
    const s = (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3);
    return s !== 0 ? s : b.count - a.count;
  });
}

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 1024) return `${Math.max(0, Math.round(n))} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function pct(share: number): string {
  const v = Math.max(0, Math.min(1, share)) * 100;
  return `${v > 0 && v < 1 ? v.toFixed(1) : Math.round(v)}%`;
}

/** The key a sample names, when it names one (`memory_key`; a check added
 *  later may carry another shape). */
function sampleKey(sample: Record<string, unknown>): string | null {
  const k = sample["memory_key"];
  return typeof k === "string" && k ? k : null;
}

function sampleText(sample: Record<string, unknown>): string {
  return Object.entries(sample)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
}

const BTN =
  "inline-flex items-center justify-center gap-1.5 min-h-9 px-3 text-sm font-medium rounded-md transition-colors pointer-coarse:min-h-11";

export function NamespaceAuditPanel({ identity }: { identity: Identity }) {
  const whoami = useStore((s) => s.whoami);
  const inspect = useStore((s) => s.inspect);
  const role = useNamespaceRole(identity.namespace);
  const allowed = canCall("namespace.audit", role, whoami);
  const [windowSeconds, setWindowSeconds] = useState(WINDOWS[1].seconds);
  const [result, setResult] = useState<NamespaceAuditResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());

  // A different namespace is a different report; a run still in flight for the old one
  // must not land under the new one.
  const scope = `${identity.org}/${identity.namespace}`;
  const scopeRef = useRef(scope);
  useEffect(() => {
    scopeRef.current = scope;
    setResult(null);
    setError(null);
    setOpen(new Set());
  }, [scope]);

  if (!allowed) return null;

  const run = async () => {
    const started = scope;
    setRunning(true);
    setError(null);
    try {
      const r = await auditNamespace(
        { org: identity.org, namespace: identity.namespace },
        { windowSeconds, sampleLimit: SAMPLE_LIMIT },
      );
      if (scopeRef.current !== started) return;
      setResult(r);
      setOpen(new Set());
    } catch (e) {
      if (scopeRef.current !== started) return;
      const text = humanizeError(e);
      setError(text);
      toast(`Audit failed: ${text}`, "error");
    } finally {
      setRunning(false);
    }
  };

  const toggle = (code: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  };

  const findings = result ? sortFindings(result.findings) : [];
  const s = result?.summary;

  return (
    <Panel as="section" aria-labelledby="usage-audit-heading" className="mb-5" padding="md" data-testid="usage-audit">
      <SectionHeading
        id="usage-audit-heading"
        level={2}
        label="Usage audit"
        caption="how this namespace is being used"
        hint="Reads the ledger and the memory tables for this namespace and names the anti-patterns it finds — an exact-key store, double-encoded values, version churn. Read-only; nothing here rewrites a row."
        actions={
          <span className="inline-flex flex-wrap items-center gap-2 text-sm">
            <label className="inline-flex items-center gap-1.5 text-[var(--color-text-muted)]">
              <span className="sr-only">Audit window</span>
              <select
                aria-label="Audit window"
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
              <Activity size={13} aria-hidden="true" />{" "}
              {running ? "Running audit…" : result ? "Run again" : "Run audit"}
            </button>
          </span>
        }
      />

      {!result && !running && !error && (
        <p className="text-sm text-[var(--color-text-muted)] leading-relaxed max-w-2xl">
          Is this namespace being used as a memory, or as an object store? Run the audit to see reads against searches
          over the window, what the values look like, and which keys are read the most.
        </p>
      )}

      {running && !result && (
        <p role="status" className="text-sm text-[var(--color-text-muted)]">
          Reading the ledger and the memory tables…
        </p>
      )}

      {error && (
        <p role="alert" className="text-sm text-[var(--color-danger)]">
          Audit failed: {error}
        </p>
      )}

      {result && s && (
        <div className="space-y-3" aria-busy={running}>
          <p className="text-sm text-[var(--color-text)] leading-relaxed" data-testid="audit-summary">
            <span
              className={`font-semibold ${result.healthy ? "text-[var(--color-success)]" : "text-[var(--color-danger)]"}`}
            >
              {result.healthy ? "Healthy" : "Needs attention"}
            </span>
            <span className="text-[var(--color-text-muted)]">
              {" "}
              · {result.criticals} critical, {result.warnings} warning, {result.infos} info over{" "}
              {Math.round(result.window_seconds / 86_400)}d
            </span>
            <br />
            <span className="tabular">
              live {s.live_rows.toLocaleString()} rows / {formatBytes(s.live_bytes)} · versions{" "}
              {s.version_rows.toLocaleString()} rows / {formatBytes(s.version_bytes)} · unembedded{" "}
              {s.unembedded_rows.toLocaleString()} · gets {s.gets.toLocaleString()} / searches{" "}
              {s.searches.toLocaleString()} / sets {s.sets.toLocaleString()} (ratio{" "}
              {s.get_search_ratio.toLocaleString(undefined, { maximumFractionDigits: 1 })})
            </span>
          </p>

          {findings.length === 0 ? (
            <p className="text-sm text-[var(--color-text-muted)]" role="status">
              Nothing to flag in this window.
            </p>
          ) : (
            <div className="overflow-x-auto -mx-1 px-1">
              <table className="w-full text-sm border-collapse" aria-label="Audit findings">
                <thead>
                  <tr className="text-left text-2xs uppercase tracking-wider text-[var(--color-text-muted)]">
                    <th scope="col" className="py-1.5 pr-2 w-9">
                      <span className="sr-only">Details</span>
                    </th>
                    <th scope="col" className="py-1.5 pr-3">
                      Finding
                    </th>
                    <th scope="col" className="py-1.5 pr-3">
                      Severity
                    </th>
                    <th scope="col" className="py-1.5 pr-3 text-right">
                      Count
                    </th>
                    <th scope="col" className="py-1.5 pr-3 text-right">
                      Share
                    </th>
                    <th scope="col" className="py-1.5">
                      First sample
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {findings.map((f) => {
                    const isOpen = open.has(f.code);
                    const first = f.samples[0];
                    const firstKey = first ? sampleKey(first) : null;
                    const label = humanizeAuditCode(f.code);
                    return (
                      <FindingRows
                        key={f.code}
                        finding={f}
                        label={label}
                        isOpen={isOpen}
                        firstKey={firstKey}
                        firstText={first ? sampleText(first) : "—"}
                        onToggle={() => toggle(f.code)}
                        onInspect={inspect}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {result.skipped.length > 0 && (
            <p className="text-xs text-[var(--color-text-subtle)] leading-relaxed" data-testid="audit-skipped">
              Skipped: {result.skipped.join("; ")}
            </p>
          )}

          <p className="text-xs text-[var(--color-text-muted)] leading-relaxed max-w-2xl">
            To fix what the audit names, run loom-optimize with your agent — it plans the moves and applies them with
            your approval (<span className="font-mono">docs/guides/optimize-namespace.md</span>). The dashboard shows;
            it does not rewrite.
          </p>
        </div>
      )}
    </Panel>
  );
}

function SeverityBadge({ severity }: { severity: NamespaceAuditSeverity }) {
  const cls =
    severity === "critical"
      ? "bg-[var(--color-danger)]/15 text-[var(--color-danger)] border-[var(--color-danger)]/30"
      : severity === "warning"
        ? "bg-[var(--color-warn)]/15 text-[var(--color-warn)] border-[var(--color-warn)]/30"
        : "bg-[var(--color-surface-2)] text-[var(--color-text-muted)] border-[var(--color-border)]";
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full border text-2xs font-medium tracking-tight ${cls}`}
    >
      {severity}
    </span>
  );
}

function KeyButton({ memoryKey, onInspect }: { memoryKey: string; onInspect: (key: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onInspect(memoryKey)}
      className="inline-flex items-center min-h-9 max-w-full font-mono text-xs text-[var(--color-text)] hover:text-[var(--color-accent)] hover:underline text-left break-all pointer-coarse:min-h-11"
      aria-label={`Open memory ${memoryKey}`}
    >
      {memoryKey}
    </button>
  );
}

function FindingRows({
  finding: f,
  label,
  isOpen,
  firstKey,
  firstText,
  onToggle,
  onInspect,
}: {
  finding: NamespaceAuditFinding;
  label: string;
  isOpen: boolean;
  firstKey: string | null;
  firstText: string;
  onToggle: () => void;
  onInspect: (key: string) => void;
}) {
  const detailId = `audit-detail-${f.code}`;
  return (
    <>
      <tr
        className="border-t border-[var(--color-border)] align-top"
        data-testid="audit-finding"
        data-code={f.code}
        data-severity={f.severity}
      >
        <td className="py-1 pr-2">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={isOpen}
            aria-controls={detailId}
            aria-label={`${isOpen ? "Hide" : "Show"} details for ${label}`}
            className="inline-flex items-center justify-center min-w-9 min-h-9 rounded-md text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] pointer-coarse:min-w-11 pointer-coarse:min-h-11"
          >
            {isOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          </button>
        </td>
        <td className="py-2 pr-3 font-medium text-[var(--color-text)] whitespace-nowrap">{label}</td>
        <td className="py-2 pr-3">
          <SeverityBadge severity={f.severity} />
        </td>
        <td className="py-2 pr-3 text-right tabular">{f.count.toLocaleString()}</td>
        <td className="py-2 pr-3 text-right tabular">{pct(f.share)}</td>
        <td className="py-1 min-w-0">
          {firstKey ? (
            <KeyButton memoryKey={firstKey} onInspect={onInspect} />
          ) : (
            <span className="font-mono text-xs text-[var(--color-text-muted)] break-all">{firstText}</span>
          )}
        </td>
      </tr>
      {isOpen && (
        <tr id={detailId} className="bg-[var(--color-surface-2)]/60" data-testid="audit-finding-detail">
          <td />
          <td colSpan={5} className="py-3 pr-3 text-sm leading-relaxed">
            <p className="text-[var(--color-text)]">{f.detail}</p>
            <p className="mt-1.5 text-[var(--color-text-muted)]">
              <span className="font-medium text-[var(--color-text)]">Remedy:</span> {f.remedy}
            </p>
            {f.bytes > 0 && (
              <p className="mt-1.5 text-xs text-[var(--color-text-subtle)] tabular">
                {formatBytes(f.bytes)} across the matching rows
              </p>
            )}
            {f.samples.length > 0 && (
              <div className="mt-2">
                <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1">
                  Samples · {f.samples.length}
                </div>
                <ul className="space-y-0.5">
                  {f.samples.map((sample, i) => {
                    const key = sampleKey(sample);
                    const rest = Object.entries(sample).filter(([k]) => k !== "memory_key");
                    return (
                      <li key={i} className="flex flex-wrap items-center gap-x-2 text-xs">
                        {key ? (
                          <KeyButton memoryKey={key} onInspect={onInspect} />
                        ) : (
                          <span className="font-mono text-[var(--color-text)] break-all">{sampleText(sample)}</span>
                        )}
                        {key && rest.length > 0 && (
                          <span className="text-[var(--color-text-muted)] tabular">
                            {rest.map(([k, v]) => `${k} ${String(v)}`).join(" · ")}
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
