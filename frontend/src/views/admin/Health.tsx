import { useState } from "react";
import { Activity as ActivityIcon, RefreshCw, Wrench, Layers, Sparkles, Shuffle } from "lucide-react";
import * as Tabs from "@radix-ui/react-tabs";
import { useStore, useIdentity } from "../../lib/state";
import { canCall } from "../../lib/canCall";
import { hasAdminClaim, hasOrgAdminClaim } from "../../lib/role";
import {
  getJson,
  listConsolidations,
  type ConsolidationRunRow,
  getExtractionQuality,
  type ExtractionQualityRow,
  getOpsHealth,
  type OpsHealthCheck,
  type OpMixData,
} from "../../api";

import { humanizeError, relativeTime } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { isRecord } from "../../lib/guards";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { PageContainer } from "../../layout/PageContainer";

// Friendly label + blurb per worker; unknown workers fall through to
// a default rendering (raw name + "background worker").
const WORKER_INFO: Record<string, { label: string; blurb: string }> = {
  embedding_worker: {
    label: "Memory indexing",
    blurb: "Embeds new memories so they show up in search.",
  },
  audit_verifier: {
    label: "Audit integrity",
    blurb: "Hourly tamper-check on the audit log.",
  },
  erasure_recovery: {
    label: "Erasure retry",
    blurb: "Re-runs failed data-erasure requests.",
  },
  entity_extractor: {
    label: "Entity extraction",
    blurb: "Tags people/places/things on new memories.",
  },
  dedup_worker: {
    label: "Memory dedup",
    blurb: "Merges semantically-near-duplicate memories.",
  },
  conflicts_worker: {
    label: "Conflict detection",
    blurb: "Flags memories that disagree with each other.",
  },
};

interface WorkerRow {
  name: string;
  effective: boolean;
  disabled_reason?: string;
  interval_seconds?: number;
  // last_run_at: latest tick regardless of outcome (success iff
  // consecutive_failures==0); last_error: set only on a failed tick.
  last_run_at?: string;
  last_duration_ms?: number;
  consecutive_failures?: number;
  total_runs?: number;
  last_error?: string;
  // The store's contribution. Under blue/green this process (the web tier) runs no
  // worker; the backend merges the freshest loom.worker_status row per worker in, and
  // says where it came from.
  last_pass_status?: PassStatus;
  reporting_from?: string;
  rows_written?: number;
  // The last namespace the pass reached ('' for workers with no notion of one) and why it
  // ran ("manual" = an operator ran it).
  namespace?: string;
  trigger?: string;
}

type PassStatus = "ok" | "failed" | "timeout" | "partial" | "cancelled" | "idle";

interface WorkersStore {
  table?: string;
  rows?: number;
  reporters: string[];
}

// Server bodies are narrowed, never cast: a field of the wrong type reads as absent and an
// entry that is not an object is dropped, so a malformed body renders empty, never throws.
const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const isPassStatus = (v: unknown): v is PassStatus => typeof v === "string" && Object.hasOwn(PASS_PILL, v);

function parseWorkerRow(name: string | undefined, b: unknown): WorkerRow | null {
  if (name === undefined || !isRecord(b)) return null;
  return {
    name,
    effective: b.effective === true,
    disabled_reason: str(b.disabled_reason),
    interval_seconds: num(b.interval_seconds),
    last_run_at: str(b.last_run_at),
    last_duration_ms: num(b.last_duration_ms),
    consecutive_failures: num(b.consecutive_failures),
    total_runs: num(b.total_runs),
    last_error: str(b.last_error),
    last_pass_status: isPassStatus(b.last_pass_status) ? b.last_pass_status : undefined,
    reporting_from: str(b.reporting_from),
    rows_written: num(b.rows_written),
    namespace: str(b.namespace),
    trigger: str(b.trigger),
  };
}

/** A `/v1/admin/workers.status` body: `workers` keyed by name (or a list of named rows),
 *  plus what `loom.worker_status` contributed. */
function parseWorkersStatus(d: unknown): { rows: WorkerRow[]; store: WorkersStore | null } {
  if (!isRecord(d)) return { rows: [], store: null };
  const w = d.workers;
  const parsed = Array.isArray(w)
    ? w.map((b) => parseWorkerRow(isRecord(b) ? str(b.name) : undefined, b))
    : isRecord(w)
      ? Object.entries(w).map(([name, b]) => parseWorkerRow(name, b))
      : [];
  const s = d.store;
  const store = isRecord(s)
    ? {
        table: str(s.table),
        rows: num(s.rows),
        reporters: Array.isArray(s.reporters) ? s.reporters.filter((r): r is string => typeof r === "string") : [],
      }
    : null;
  return { rows: parsed.filter((r): r is WorkerRow => r !== null), store };
}

export function Health() {
  const identity = useIdentity();
  const [hasConsolidations, setHasConsolidations] = useState(false);
  // Each operator tab follows the gate of the endpoint it reads: Workers is
  // /v1/admin/workers.status (an ADMIN claim), Operation mix is ops.health (ORG_ADMIN).
  const whoami = useStore((s) => s.whoami);
  const seesWorkers = hasAdminClaim(whoami);
  const seesOpMix = hasOrgAdminClaim(whoami);

  // Drop the consolidations sub-tab entirely until a run has landed.
  useAbortableEffect(
    (signal, live) => {
      listConsolidations({ org: identity.org, namespace: identity.namespace }, { limit: 1, signal })
        .then((r) => {
          if (live()) setHasConsolidations((r.rows || []).length > 0);
        })
        .catch(() => {
          if (live()) setHasConsolidations(false);
        });
    },
    [identity.org, identity.namespace],
  );

  return (
    <PageContainer>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight inline-flex items-center gap-2">
          <ActivityIcon size={20} /> Health
        </h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-1 max-w-2xl">
          {seesOpMix
            ? "Operator view: worker schedules, operation mix + extraction quality across the org."
            : seesWorkers
              ? "Operator view: worker schedules + extraction quality for the namespaces you can access."
              : "Extraction quality for the namespaces you can access. Worker telemetry is hidden — that's the Loom operator's concern."}
        </p>
      </div>
      <Tabs.Root defaultValue={seesWorkers ? "workers" : "extraction"}>
        <Tabs.List className="flex border-b border-[var(--color-border)] mb-5">
          {seesWorkers && (
            <TabTrigger value="workers" icon={Wrench}>
              Workers
            </TabTrigger>
          )}
          <TabTrigger value="extraction" icon={Sparkles}>
            Extraction quality
          </TabTrigger>
          {seesOpMix && (
            <TabTrigger value="opmix" icon={Shuffle}>
              Operation mix
            </TabTrigger>
          )}
          {hasConsolidations && (
            <TabTrigger value="consolidations" icon={Layers}>
              Consolidations
            </TabTrigger>
          )}
        </Tabs.List>
        {seesWorkers && (
          <Tabs.Content value="workers" className="outline-none">
            <WorkersPanel />
          </Tabs.Content>
        )}
        <Tabs.Content value="extraction" className="outline-none">
          <ExtractionQualityPanel />
        </Tabs.Content>
        {seesOpMix && (
          <Tabs.Content value="opmix" className="outline-none">
            <OpMixPanel />
          </Tabs.Content>
        )}
        {hasConsolidations && (
          <Tabs.Content value="consolidations" className="outline-none">
            <ConsolidationsPanel />
          </Tabs.Content>
        )}
      </Tabs.Root>
    </PageContainer>
  );
}

function TabTrigger({
  value,
  icon: Icon,
  children,
}: {
  value: string;
  icon: typeof Wrench;
  children: React.ReactNode;
}) {
  return (
    <Tabs.Trigger
      value={value}
      className="inline-flex items-center gap-2 px-4 py-2.5 text-base font-medium text-[var(--color-text-muted)] hover:text-[var(--color-text)] data-[state=active]:text-[var(--color-text)] data-[state=active]:border-b-2 data-[state=active]:border-[var(--color-accent)] -mb-px outline-none transition-colors"
    >
      <Icon size={14} /> {children}
    </Tabs.Trigger>
  );
}

function fmtInterval(seconds: number): string {
  if (seconds <= 0) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} hr`;
  return `${Math.round(seconds / 86400)} d`;
}

type WorkerStatus = "healthy" | "degraded" | "stalled" | "disabled" | "waiting";

function statusOf(w: WorkerRow): WorkerStatus {
  if (!w.effective) return "disabled";
  if ((w.consecutive_failures ?? 0) > 0) return "degraded";
  // The store's verdict on the last pass counts like a registry failure — a timed-out or
  // failed pass in the workers colour is an issue here, where the operator looks.
  if (w.last_pass_status === "failed" || w.last_pass_status === "timeout" || w.last_pass_status === "partial") {
    return "degraded";
  }
  if (!w.last_run_at) return "waiting";
  // Stalled = last tick older than 3× the interval. Unknown interval → healthy.
  if (w.interval_seconds && w.interval_seconds > 0) {
    const ageMs = Date.now() - new Date(w.last_run_at).getTime();
    if (ageMs > w.interval_seconds * 1000 * 3) return "stalled";
  }
  return "healthy";
}

// Pill per pass verdict. `ok` needs no pill — the dot says it.
const PASS_PILL: Record<PassStatus, { label: string; cls: string } | null> = {
  ok: null,
  idle: { label: "Idle — ran, nothing to do", cls: "bg-[var(--color-surface-2)] text-[var(--color-text-muted)]" },
  partial: { label: "Partial — a leg failed", cls: "bg-warn/15 text-warn" },
  timeout: { label: "Timed out at its deadline", cls: "bg-danger/15 text-danger" },
  failed: { label: "Last pass failed", cls: "bg-danger/15 text-danger" },
  cancelled: { label: "Cancelled — process stopped mid-pass", cls: "bg-warn/15 text-warn" },
};

// `deploy:workers:green` → "green"; `iso:w6v3:7864` and other labels as-is.
export function colourOf(label: string | undefined | null): string {
  if (!label) return "";
  const m = /^deploy:(?:web|workers|dev)(?::([a-z0-9-]+))?$/i.exec(label);
  if (m) return m[1] || label;
  return label;
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60000)} min`;
}

export function WorkersPanel() {
  const [rows, setRows] = useState<WorkerRow[] | null>(null);
  const [store, setStore] = useState<WorkersStore | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [showAll, setShowAll] = useState(false);
  // workers.status is a /v1/admin/* handler: the JWT claim alone opens it. Without the
  // claim there is no request — the panel reads "not reachable from here" (the dashboard
  // never issues a request the token cannot make).
  const whoami = useStore((s) => s.whoami);
  const canReadWorkers = canCall("admin/workers.status", null, whoami);

  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      if (!canReadWorkers) {
        setRows([]);
        setStore(null);
        return;
      }
      getJson<unknown>("/v1/admin/workers.status", { signal })
        .then((d) => {
          if (!live()) return;
          const parsed = parseWorkersStatus(d);
          setStore(parsed.store);
          setRows(parsed.rows);
        })
        .catch(() => {
          if (live()) {
            setRows([]);
            setStore(null);
          }
        });
    },
    [refreshKey, canReadWorkers],
  );

  const issues = (rows || []).filter((w) => {
    const s = statusOf(w);
    return s === "degraded" || s === "stalled";
  });
  const effective = (rows || []).filter((w) => w.effective);
  // "all systems running" needs a report from somewhere — this process's registry or the
  // store — not merely a configured worker list.
  const reporting = effective.filter((w) => !!w.last_run_at);
  const allHealthy = rows !== null && issues.length === 0 && reporting.length > 0;
  const colours = Array.from(new Set((store?.reporters ?? []).filter(Boolean).map(colourOf)));

  // Hide disabled-by-config rows unless "Show all" is on.
  const visible = (rows || []).filter(
    (w) => showAll || w.effective || (w.disabled_reason && w.disabled_reason !== "disabled_by_setting"),
  );
  const hiddenByConfig = (rows || []).length - visible.length;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3 flex-wrap">
          <ScopeBadge kind="loom-wide" />
          {rows !== null &&
            (allHealthy ? (
              <span className="inline-flex items-center gap-2 px-3 py-1 text-sm rounded-md bg-success/15 text-success">
                <span className="w-2 h-2 rounded-full bg-success" />
                All systems running
              </span>
            ) : issues.length > 0 ? (
              <span className="inline-flex items-center gap-2 px-3 py-1 text-sm rounded-md bg-danger/15 text-danger">
                <span className="w-2 h-2 rounded-full bg-danger" />
                {issues.length} {issues.length === 1 ? "issue" : "issues"}
              </span>
            ) : (
              <span className="text-sm text-[var(--color-text-muted)]">No workers reporting</span>
            ))}
          {rows !== null && colours.length > 0 && (
            <span className="text-xs text-[var(--color-text-muted)]" data-testid="reporting-colours">
              Reporting from {colours.join(", ")}
              {store?.rows ? (
                <>
                  {" "}
                  · {store.rows} {store.rows === 1 ? "row" : "rows"} in {store.table || "loom.worker_status"}
                </>
              ) : null}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => setRefreshKey((k) => k + 1)}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)] rounded-md hover:bg-[var(--color-surface-2)]"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>
      {rows === null && <Skeleton className="h-40 w-full" />}
      {rows !== null && (rows.length === 0 || visible.length === 0) && (
        <EmptyState
          icon={Wrench}
          title={rows.length === 0 ? "No workers reporting" : "Nothing to show"}
          description={
            rows.length === 0
              ? "Neither this process nor loom.worker_status has a worker pass on record. Either no workers colour is running, or /v1/admin/workers.status is not reachable from here."
              : "All workers are disabled in config. Toggle Show all to see them."
          }
        />
      )}
      {rows !== null && rows.length > 0 && reporting.length === 0 && effective.length > 0 && (
        <div
          className="mb-3 p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] text-xs text-[var(--color-text-muted)]"
          role="status"
        >
          No worker has reported a pass yet. This process runs no workers (LOOM_BACKGROUND_WORKERS=off under
          blue/green); the workers colour writes each finished pass to loom.worker_status and nothing has landed there
          in the last 7 days.
        </div>
      )}
      {rows !== null && visible.length > 0 && (
        <div className="space-y-1.5">
          {visible.map((w) => {
            const status = statusOf(w);
            const info = WORKER_INFO[w.name] || {
              label: w.name.replace(/_/g, " "),
              blurb: "Background worker.",
            };
            const pill = w.last_pass_status ? PASS_PILL[w.last_pass_status] : null;
            const from = colourOf(w.reporting_from);
            const written = w.rows_written ?? 0;
            return (
              <div
                key={w.name}
                className="flex items-start gap-3 p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]"
              >
                <div
                  className={`w-2 h-2 rounded-full mt-1.5 shrink-0 ${
                    status === "healthy"
                      ? "bg-success"
                      : status === "degraded"
                        ? "bg-danger"
                        : status === "stalled"
                          ? "bg-warn"
                          : "bg-text-subtle"
                  }`}
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-base font-medium text-[var(--color-text)]">{info.label}</span>
                    <span className="text-2xs text-[var(--color-text-subtle)] font-mono">{w.name}</span>
                    {(w.consecutive_failures ?? 0) > 0 && (
                      <span className="text-2xs px-1.5 py-0.5 rounded bg-danger/15 text-danger">
                        {w.consecutive_failures} consecutive failure{(w.consecutive_failures ?? 0) === 1 ? "" : "s"}
                        {(w.consecutive_failures ?? 0) >= 3 ? " — DEGRADED" : ""}
                      </span>
                    )}
                    {pill && status !== "disabled" && (
                      <span className={`text-2xs px-1.5 py-0.5 rounded ${pill.cls}`}>{pill.label}</span>
                    )}
                    {status === "stalled" && (
                      <span className="text-2xs px-1.5 py-0.5 rounded bg-warn/15 text-warn">
                        Stalled — overdue for a tick
                      </span>
                    )}
                    {status === "disabled" && (
                      <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
                        {w.disabled_reason === "disabled_by_setting"
                          ? "Off in config"
                          : w.disabled_reason || "Disabled"}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-[var(--color-text-muted)] mt-1">{info.blurb}</div>
                  {status !== "disabled" && (
                    <div className="text-2xs text-[var(--color-text-subtle)] mt-1 tabular">
                      {w.last_run_at ? (
                        <>
                          Last ran {relativeTime(w.last_run_at)}
                          {w.last_duration_ms != null && <> · took {fmtDuration(w.last_duration_ms)}</>}
                          {written > 0 && <> · {written.toLocaleString()} rows written</>}
                          {w.total_runs != null && <> · {w.total_runs.toLocaleString()} runs total</>}
                          {w.interval_seconds ? <> · every {fmtInterval(w.interval_seconds)}</> : null}
                          {from && (
                            <>
                              {" "}
                              · reporting from{" "}
                              <span className="font-mono" title={w.reporting_from}>
                                {from}
                              </span>
                            </>
                          )}
                          {w.namespace && (
                            <>
                              {" "}
                              · reached:{" "}
                              <span className="font-mono" title="the last namespace the pass reached">
                                {w.namespace}
                              </span>
                            </>
                          )}
                          {w.trigger === "manual" && <> · manual run</>}
                        </>
                      ) : (
                        <>
                          No pass reported yet
                          {w.interval_seconds ? <> (every {fmtInterval(w.interval_seconds)})</> : null}
                        </>
                      )}
                      {w.last_error && status !== "healthy" && (
                        <span className="block text-danger mt-0.5">Error: {w.last_error.slice(0, 200)}</span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          {hiddenByConfig > 0 && !showAll && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="w-full text-2xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] py-2 hover:bg-[var(--color-surface-2)] rounded-md"
            >
              + {hiddenByConfig} worker{hiddenByConfig === 1 ? "" : "s"} disabled in config (click to show)
            </button>
          )}
          {showAll && hiddenByConfig === 0 && (
            <button
              type="button"
              onClick={() => setShowAll(false)}
              className="w-full text-2xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] py-2 hover:bg-[var(--color-surface-2)] rounded-md"
            >
              Hide config-disabled workers
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// How agents in this namespace spend their calls. A healthy memory policy reads less and
// revises more; an append-only bank or a polling read load shows up here before it shows
// up as stale answers.
const CLASS_INFO: Record<string, { label: string; blurb: string }> = {
  create: { label: "Create", blurb: "New rows: set, set_from_messages, bulk_set, record.write" },
  read: { label: "Read", blurb: "get, search, ask, list, context.pack, facts.*" },
  update: { label: "Update", blurb: "Revisions of an existing key: update, update_by_id, apply" },
  annotate: { label: "Annotate", blurb: "Edges and marks on existing rows: link, evidence, verify, pin" },
  delete: { label: "Delete", blurb: "delete, clear, demote, redact" },
  scratchpad: { label: "Scratchpad", blurb: "working_memory.* — the run's own notes" },
  maintain: { label: "Maintain", blurb: "consolidate, dedup, reindex, sweep" },
  other: { label: "Other", blurb: "Anything not classified above" },
};

/** The `op_mix` check's data block, or null when it is not one (a failed check carries none,
 *  or a proxy rewrote it): the panel then reads "check failed" instead of throwing mid-render. */
export function parseOpMix(d: unknown): OpMixData | null {
  if (!isRecord(d)) return null;
  const window_days = num(d.window_days);
  const writes = num(d.writes);
  const agents = num(d.agents);
  if (window_days === undefined || writes === undefined || agents === undefined) return null;
  const counts: Record<string, number> = {};
  if (isRecord(d.counts)) {
    for (const [k, v] of Object.entries(d.counts)) {
      const n = num(v);
      if (n !== undefined) counts[k] = n;
    }
  }
  const top: OpMixData["top"] = [];
  if (Array.isArray(d.top)) {
    for (const t of d.top) {
      if (!isRecord(t)) continue;
      const operation = str(t.operation);
      const cls = str(t.class);
      const count = num(t.count);
      if (operation !== undefined && cls !== undefined && count !== undefined)
        top.push({ operation, class: cls, count });
    }
  }
  return {
    window_days,
    counts,
    writes,
    reads_per_write: num(d.reads_per_write) ?? null,
    update_share: num(d.update_share) ?? null,
    agents,
    top,
  };
}

/** ops.health's op_mix answer: a report ({check, data}), a check the server ran but could
 *  not compute (data: null — status "fail", detail says why), or null when the check is
 *  absent or the request was refused. */
export function OpMixPanel() {
  const identity = useIdentity();
  const [op, setOp] = useState<{ check: OpsHealthCheck; data: OpMixData | null } | null | undefined>(undefined);
  const [scope, setScope] = useState<"namespace" | "org">("namespace");

  useAbortableEffect(
    (signal, live) => {
      setOp(undefined);
      getOpsHealth(
        { org: identity.org, namespace: identity.namespace },
        { namespace: scope === "namespace" ? identity.namespace : null, signal },
      )
        .then((r) => {
          if (!live()) return;
          const check = r.checks.find((c) => c.name === "op_mix");
          setOp(check ? { check, data: parseOpMix(check.data) } : null);
        })
        .catch(() => {
          if (live()) setOp(null);
        });
    },
    [identity.org, identity.namespace, scope],
  );

  if (op === undefined) return <Skeleton className="h-40 w-full" />;
  if (op === null) {
    return (
      <EmptyState
        icon={Shuffle}
        title="Operation mix unavailable"
        description="ops.health needs an org-admin token, or the server predates the op_mix check."
      />
    );
  }
  const { check, data } = op;
  if (data === null) {
    return (
      <EmptyState
        icon={Shuffle}
        title="Operation mix check failed"
        description={check.repair ? `${check.detail} — ${check.repair}` : check.detail}
      />
    );
  }
  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
  const classes = ["create", "read", "update", "annotate", "delete", "scratchpad", "maintain", "other"].filter(
    (k) => (data.counts[k] ?? 0) > 0,
  );
  const tone = check.status === "ok" ? "bg-success" : check.status === "warn" ? "bg-warn" : "bg-danger";
  const fmt = (n: number) => n.toLocaleString();
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        {scope === "namespace" ? (
          <ScopeBadge kind="namespace" value={identity.namespace} />
        ) : (
          <span className="text-xs px-2 py-0.5 rounded-md bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
            org · {identity.org}
          </span>
        )}
        <button
          type="button"
          onClick={() => setScope(scope === "namespace" ? "org" : "namespace")}
          className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline-offset-2 hover:underline"
        >
          {scope === "namespace" ? "Show whole org" : "Show this namespace"}
        </button>
        <span className="text-xs text-[var(--color-text-muted)]">
          last {data.window_days} d · {fmt(total)} calls · {data.agents} agent{data.agents === 1 ? "" : "s"}
        </span>
      </div>

      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <div className="flex items-start gap-2">
          <span className={`mt-1.5 inline-block h-2 w-2 rounded-full ${tone}`} />
          <div className="text-sm">
            <div className="font-medium">{check.detail}</div>
            {check.repair && <div className="text-[var(--color-text-muted)] mt-0.5">{check.repair}</div>}
          </div>
        </div>
        <div className="mt-4 grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
          <Stat
            label="Reads per write"
            value={data.reads_per_write === null ? "—" : data.reads_per_write.toFixed(1)}
            hint="a well-tuned agent reads less per write over time"
          />
          <Stat
            label="Update share"
            value={data.update_share === null ? "—" : `${(data.update_share * 100).toFixed(2)}%`}
            hint="revisions ÷ (creates + revisions); a bank that never revises only grows"
          />
          <Stat label="Deletes" value={fmt(data.counts.delete ?? 0)} hint="deletes are normally a small share" />
        </div>
      </div>

      {total > 0 && (
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
          <div className="flex h-3 w-full overflow-hidden rounded-full bg-[var(--color-surface-2)]">
            {classes.map((k) => (
              <div
                key={k}
                title={`${CLASS_INFO[k].label}: ${fmt(data.counts[k])}`}
                className={`${CLASS_BAR[k]} h-full`}
                style={{ width: `${(100 * data.counts[k]) / total}%` }}
              />
            ))}
          </div>
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2 text-xs">
            {classes.map((k) => (
              <div key={k} className="flex items-start gap-2">
                <span className={`mt-1 inline-block h-2 w-2 rounded-sm ${CLASS_BAR[k]}`} />
                <div>
                  <div className="font-medium">
                    {CLASS_INFO[k].label}{" "}
                    <span className="text-[var(--color-text-muted)] tabular-nums">{fmt(data.counts[k])}</span>
                  </div>
                  <div className="text-[var(--color-text-muted)]">{CLASS_INFO[k].blurb}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {data.top.length > 0 && (
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
          <table className="w-full text-sm">
            <thead className="text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)]">
              <tr>
                <th className="text-left px-4 py-2 font-medium">Operation</th>
                <th className="text-left px-4 py-2 font-medium">Class</th>
                <th className="text-right px-4 py-2 font-medium">Calls</th>
              </tr>
            </thead>
            <tbody>
              {data.top.map((t) => (
                <tr key={t.operation} className="border-t border-[var(--color-border)]">
                  <td className="px-4 py-1.5 font-mono text-xs">{t.operation}</td>
                  <td className="px-4 py-1.5 text-[var(--color-text-muted)]">
                    {CLASS_INFO[t.class]?.label ?? t.class}
                  </td>
                  <td className="px-4 py-1.5 text-right tabular-nums">{fmt(t.count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const CLASS_BAR: Record<string, string> = {
  create: "bg-cat-4",
  read: "bg-text-subtle",
  update: "bg-cat-6",
  annotate: "bg-cat-5",
  delete: "bg-cat-7",
  scratchpad: "bg-cat-3",
  maintain: "bg-text-subtle",
  other: "bg-text-muted",
};

function ConsolidationsPanel() {
  const identity = useIdentity();
  const [rows, setRows] = useState<ConsolidationRunRow[] | null>(null);

  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      listConsolidations({ org: identity.org, namespace: identity.namespace }, { limit: 50, signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
        });
    },
    [identity.org, identity.namespace],
  );

  if (rows === null) return <Skeleton className="h-40 w-full" />;
  return (
    <div>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <ConsolidationsTable rows={rows} />
    </div>
  );
}

function ConsolidationsTable({ rows }: { rows: ConsolidationRunRow[] }) {
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Layers}
        title="No consolidation runs yet"
        description="The background consolidator merges semantically-similar memories. Runs appear here once it fires."
      />
    );
  }
  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
      <table className="w-full text-sm">
        <thead className="text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)]">
          <tr>
            <th className="text-left px-3 py-2">Status</th>
            <th className="text-left px-3 py-2">Output key</th>
            <th className="text-right px-3 py-2">Sources</th>
            <th className="text-right px-3 py-2">Tokens</th>
            <th className="text-right px-3 py-2">Duration</th>
            <th className="text-right px-3 py-2">When</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.run_id} className="border-t border-[var(--color-border)]">
              <td className="px-3 py-2">
                <span
                  className={`inline-flex items-center gap-1.5 text-xs font-medium ${
                    r.status === "written"
                      ? "text-[var(--color-success)]"
                      : r.status === "failed"
                        ? "text-[var(--color-danger)]"
                        : "text-[var(--color-text-muted)]"
                  }`}
                >
                  {r.status}
                </span>
              </td>
              <td className="px-3 py-2 font-mono text-xs text-[var(--color-text)] truncate max-w-[280px]">
                {r.output_memory_key ?? "—"}
              </td>
              <td className="px-3 py-2 text-right tabular">{r.cluster_size}</td>
              <td className="px-3 py-2 text-right tabular">{(r.tokens_in ?? 0) + (r.tokens_out ?? 0)}</td>
              <td className="px-3 py-2 text-right tabular">{r.duration_ms}ms</td>
              <td className="px-3 py-2 text-right tabular text-[var(--color-text-muted)]">{relativeTime(r.ran_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Per-namespace approve/reject ratio for the learnings queue.
function ExtractionQualityPanel() {
  const identity = useStore((s) => s.identity);
  const [rows, setRows] = useState<ExtractionQualityRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Refresh re-runs the effect, so a refresh superseded by an org switch is aborted and its
  // answer dropped like any other stale read.
  const [refreshKey, setRefreshKey] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      if (!identity) return;
      setError(null);
      getExtractionQuality({ org: identity.org }, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e: unknown) => {
          if (live()) setError(humanizeError(e));
        });
    },
    [identity?.org, refreshKey],
  );

  if (!identity) return null;
  if (error) {
    return <EmptyState icon={Sparkles} title="Couldn't load extraction quality" description={error} />;
  }
  if (rows === null) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10" />
        <Skeleton className="h-10" />
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Sparkles}
        title="No extraction activity yet"
        description="Nothing has been extracted from saved conversations yet. Once it starts, this panel shows how many candidates were proposed, approved and rejected, so you can tune extraction for signal."
        details={<>Candidates land in the review queue and the approve / reject counts are tracked here.</>}
      />
    );
  }

  const totalPending = rows.reduce((a, r) => a + r.pending, 0);
  const totalPromoted = rows.reduce((a, r) => a + r.promoted, 0);
  const totalRejected = rows.reduce((a, r) => a + r.rejected, 0);
  const totalDecided = totalPromoted + totalRejected;
  const orgRatio = totalDecided > 0 ? totalPromoted / totalDecided : 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-4 gap-3">
        <Stat label="Pending" value={totalPending} hint="awaiting review" />
        <Stat label="Promoted" value={totalPromoted} hint="approved → memory" />
        <Stat label="Rejected" value={totalRejected} hint="dropped on review" />
        <Stat
          label="Approve ratio"
          value={totalDecided > 0 ? (orgRatio * 100).toFixed(0) + "%" : "—"}
          hint={totalDecided < 10 ? `${totalDecided}/10 decisions for signal` : orgRatio < 0.3 ? "low" : "healthy"}
          tone={totalDecided >= 10 && orgRatio < 0.3 ? "warn" : "default"}
        />
      </div>
      <div className="flex items-center justify-between">
        <div className="text-xs text-[var(--color-text-muted)]">By namespace, org={identity.org}</div>
        <button
          type="button"
          onClick={() => setRefreshKey((k) => k + 1)}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-2)]"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>
      <table className="w-full text-sm border-separate border-spacing-0">
        <thead>
          <tr className="text-left text-xs text-[var(--color-text-muted)]">
            <th className="px-3 py-2 border-b border-[var(--color-border)]">Namespace</th>
            <th className="px-3 py-2 border-b border-[var(--color-border)] text-right">Pending</th>
            <th className="px-3 py-2 border-b border-[var(--color-border)] text-right">Promoted</th>
            <th className="px-3 py-2 border-b border-[var(--color-border)] text-right">Rejected</th>
            <th className="px-3 py-2 border-b border-[var(--color-border)] text-right">Approve</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const decided = r.promoted + r.rejected;
            const ratioPct = decided > 0 ? (r.approve_ratio * 100).toFixed(0) + "%" : "—";
            const low = decided >= 10 && r.approve_ratio < 0.3;
            return (
              <tr key={r.namespace}>
                <td className="px-3 py-2 font-mono text-xs border-b border-[var(--color-border)]">{r.namespace}</td>
                <td className="px-3 py-2 text-right tabular border-b border-[var(--color-border)]">{r.pending}</td>
                <td className="px-3 py-2 text-right tabular border-b border-[var(--color-border)]">{r.promoted}</td>
                <td className="px-3 py-2 text-right tabular border-b border-[var(--color-border)]">{r.rejected}</td>
                <td
                  className={`px-3 py-2 text-right tabular border-b border-[var(--color-border)] ${low ? "text-[var(--color-warn)]" : ""}`}
                >
                  {ratioPct}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: number | string;
  hint?: string;
  tone?: "default" | "warn";
}) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-3">
      <div className="text-xs text-[var(--color-text-muted)]">{label}</div>
      <div className={`text-xl font-semibold tabular mt-0.5 ${tone === "warn" ? "text-[var(--color-warn)]" : ""}`}>
        {value}
      </div>
      {hint && <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">{hint}</div>}
    </div>
  );
}
