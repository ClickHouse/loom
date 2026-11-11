import { useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import type { NamespaceAuditFinding, NamespaceAuditResult, NamespaceAuditSeverity } from "../../../api";
import { IconButton } from "../../../components/IconButton";
import { CardKey } from "./linked";
import { useCardSection } from "./section";
import { useStore } from "../../../lib/state";

/**
 * The audit card's body: what `namespace.audit` read over its window, as the CLI table
 * prints it — the summary line, the findings sorted critical → warning → info, and the
 * checks that could not run. Samples are keys and counts only (the op never returns a
 * value); a key sample opens that memory in the drawer. It never writes: the remedy for
 * what it names is the optimize recipe the owner's agent runs with approval.
 */

const SEVERITY_RANK: Record<NamespaceAuditSeverity, number> = { critical: 0, warning: 1, info: 2 };

/** The namespace audit's finding codes, as a person reads them. An unknown code (a
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
  telemetry_unavailable: "Telemetry unavailable",
  index_backlog: "Indexing backlog",
  intentional_exact_key_reads: "Procedure reads by key",
};

export function humanizeAuditCode(code: string): string {
  const known = CODE_LABELS[code];
  if (known) return known;
  const words = code.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : code;
}

/** Critical first, then warning, then info; ties by count, largest first. */
export function sortFindings(findings: readonly NamespaceAuditFinding[]): NamespaceAuditFinding[] {
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
export function sampleKey(sample: Record<string, unknown>): string | null {
  const k = sample["memory_key"];
  return typeof k === "string" && k ? k : null;
}

export function sampleText(sample: Record<string, unknown>): string {
  return Object.entries(sample)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
}

export function AuditBody({ result }: { result: NamespaceAuditResult }) {
  const inspect = useStore((s) => s.inspect);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (code: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  };
  const findings = sortFindings(result.findings);
  const s = result.summary;
  return (
    <div className="space-y-3">
      <p className="text-sm text-text leading-relaxed" data-testid="audit-summary">
        <span className={`font-semibold ${result.healthy ? "text-success" : "text-danger"}`}>
          {result.healthy ? "Healthy" : "Needs attention"}
        </span>
        <span className="text-text-muted">
          {" "}
          · {result.criticals} critical, {result.warnings} warning, {result.infos} info over{" "}
          {Math.round(result.window_seconds / 86_400)}d
        </span>
        <br />
        <span className="tabular">
          live {s.live_rows.toLocaleString()} rows / {formatBytes(s.live_bytes)} · versions{" "}
          {s.version_rows.toLocaleString()} rows / {formatBytes(s.version_bytes)} · unembedded{" "}
          {s.unembedded_rows.toLocaleString()} · gets {s.gets.toLocaleString()} / searches {s.searches.toLocaleString()}{" "}
          / sets {s.sets.toLocaleString()} (ratio{" "}
          {s.get_search_ratio.toLocaleString(undefined, { maximumFractionDigits: 1 })})
          {s.inspector_calls > 0 &&
            ` · ${s.inspector_calls.toLocaleString()} dashboard and assistant calls not counted`}
          {(s.typed_gets ?? 0) > 0 &&
            ` · ${(s.typed_gets ?? 0).toLocaleString()} procedure reads left out of the ratio`}
        </span>
      </p>

      {findings.length === 0 ? (
        <p className="text-sm text-text-muted" role="status">
          Nothing to flag in this window.
        </p>
      ) : (
        <div className="overflow-x-auto -mx-1 px-1">
          <table className="native-table text-sm" aria-label="Audit findings">
            <thead>
              <tr>
                <th scope="col" className="w-9">
                  <span className="sr-only">Details</span>
                </th>
                <th scope="col">Finding</th>
                <th scope="col">Severity</th>
                <th scope="col" className="text-right">
                  Count
                </th>
                <th scope="col" className="text-right">
                  Share
                </th>
                <th scope="col">First sample</th>
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
        <p className="text-xs text-text-muted leading-relaxed" data-testid="audit-skipped">
          Skipped: {result.skipped.join("; ")}
        </p>
      )}

      <p className="text-xs text-text-muted leading-relaxed max-w-2xl">
        To fix what the audit names, run loom-optimize with your agent — it plans the moves and applies them with your
        approval (<span className="font-mono">docs/guides/optimize-namespace.md</span>). The dashboard shows; it does
        not rewrite.
      </p>
    </div>
  );
}

const SEVERITY_STATE: Record<NamespaceAuditSeverity, BadgeState> = {
  critical: "danger",
  warning: "warning",
  info: "neutral",
};

// How each finding kind reads in the expanded row.
const KIND_LABEL: Record<NonNullable<NamespaceAuditFinding["kind"]>, string> = {
  contract_violation: "Contract violation",
  usage_heuristic: "Usage heuristic: a hit is not proof of misuse",
  system_state: "The deployment's state, not the writer's doing",
};

function SeverityBadge({ severity }: { severity: NamespaceAuditSeverity }) {
  return <Badge text={severity} state={SEVERITY_STATE[severity] ?? "neutral"} size="sm" />;
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
  // Unique in the conversation, which may hold two audit cards.
  const detailId = `${useCardSection().prefix}-audit-detail-${f.code}`;
  return (
    <>
      <tr
        className="border-t border-border align-top"
        data-testid="audit-finding"
        data-code={f.code}
        data-severity={f.severity}
      >
        <td>
          <IconButton
            icon={isOpen ? "chevron-down" : "chevron-right"}
            size="sm"
            label={`${isOpen ? "Hide" : "Show"} details for ${label}`}
            onClick={onToggle}
            aria-expanded={isOpen}
            aria-controls={detailId}
          />
        </td>
        <td className="font-medium text-text whitespace-nowrap">{label}</td>
        <td>
          <SeverityBadge severity={f.severity} />
        </td>
        <td className="text-right tabular">{f.count.toLocaleString()}</td>
        <td className="text-right tabular">{pct(f.share)}</td>
        <td className="min-w-0">
          {firstKey ? (
            <CardKey memoryKey={firstKey} onInspect={onInspect} />
          ) : (
            <span className="font-mono text-xs text-text-muted break-all">{firstText}</span>
          )}
        </td>
      </tr>
      {isOpen && (
        <tr id={detailId} className="bg-surface-2/60" data-testid="audit-finding-detail">
          <td />
          <td colSpan={5} className="text-sm leading-relaxed">
            <p className="text-text">{f.detail}</p>
            <p className="mt-1.5 text-text-muted">
              <span className="font-medium text-text">Remedy:</span> {f.remedy}
            </p>
            {/* A server one release older answers without the evidence fields. */}
            {f.kind && (
              <p className="mt-1.5 text-xs text-text-muted" data-testid="audit-finding-kind">
                {[
                  KIND_LABEL[f.kind],
                  `rule v${f.rule_version}`,
                  f.uncertainty && `May still be fine: ${f.uncertainty}`,
                  f.exceptions && `Scope: ${f.exceptions}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}
            {f.bytes > 0 && (
              <p className="mt-1.5 text-xs text-text-muted tabular">{formatBytes(f.bytes)} across the matching rows</p>
            )}
            {f.samples.length > 0 && (
              <div className="mt-2">
                <div className="text-xs text-text-muted mb-1">
                  Samples · {f.samples.length}
                  {f.samples_complete === false && " · not every match is listed"}
                </div>
                <ul className="space-y-0.5">
                  {f.samples.map((sample, i) => {
                    const key = sampleKey(sample);
                    // memory_id joins the sample to its evidence entry; the key names it here.
                    const rest = Object.entries(sample).filter(([k]) => k !== "memory_key" && k !== "memory_id");
                    return (
                      <li key={i} className="flex flex-wrap items-center gap-x-2 text-xs">
                        {key ? (
                          <CardKey memoryKey={key} onInspect={onInspect} />
                        ) : (
                          <span className="font-mono text-text break-all">{sampleText(sample)}</span>
                        )}
                        {key && rest.length > 0 && (
                          <span className="text-text-muted tabular">
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
