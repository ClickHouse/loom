import { useState, type ReactNode } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Table } from "@clickhouse/click-ui/Table";
import { Tabs } from "@clickhouse/click-ui/Tabs";
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
import { useAbortableEffect } from "../../lib/hooks";
import { isRecord } from "../../lib/guards";
import { Skeleton, TableSkeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ListState } from "../../components/ui/ListState";
import { ApiError } from "../../lib/errors";
import { ListTime } from "../../components/ui/ListTime";
import { parseChTime } from "../../lib/format";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { ScopeBadge } from "../../components/ScopeBadge";
import { PageContainer } from "../../layout/PageContainer";
import { Alert, LoadingSkeleton, PageHeader, Stat, TabTrigger } from "../../components/ui";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { NamespaceHealthPanel } from "./NamespaceHealth";
import { ProjectionFailuresPanel } from "./ProjectionFailures";
import { RepairsPanel } from "./Repairs";

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
  // The wait after a pass that found no work, when the worker has one.
  idle_interval_seconds?: number;
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
    idle_interval_seconds: num(b.idle_interval_seconds),
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

type HealthTab = "workers" | "extraction" | "opmix" | "consolidations" | "namespace" | "repairs" | "projections";
// One name per tab, for its trigger and the document title.
const HEALTH_TABS: Record<HealthTab, string> = {
  workers: "Workers",
  extraction: "Extraction quality",
  opmix: "Operation mix",
  consolidations: "Consolidations",
  namespace: "Namespace",
  repairs: "Repairs",
  projections: "Projection failures",
};

export function Health() {
  const identity = useIdentity();
  const [hasConsolidations, setHasConsolidations] = useState(false);
  // Each operator tab follows the gate of the endpoint it reads: Workers is
  // /v1/admin/workers.status (an ADMIN claim), Operation mix is ops.health (ORG_ADMIN).
  const whoami = useStore((s) => s.whoami);
  const seesWorkers = hasAdminClaim(whoami);
  const seesOpMix = hasOrgAdminClaim(whoami);
  // Namespace reads memory.health (VIEWER); every repair is ADMIN, as memory.sweep_expired
  // is, and none reads on open; Projection failures is ORG_ADMIN, like Operation mix.
  const role = useNamespaceRole(identity.namespace);
  const seesNamespace = canCall("memory.health", role, whoami);
  const seesRepairs = canCall("memory.sweep_expired", role, whoami);
  const [tab, setTab] = useState<HealthTab>(seesWorkers ? "workers" : "extraction");

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

  // A tab gated by the namespace's role can vanish on a switch; Extraction quality is always there.
  const shown: Record<HealthTab, boolean> = {
    workers: seesWorkers,
    extraction: true,
    opmix: seesOpMix,
    consolidations: hasConsolidations,
    namespace: seesNamespace,
    repairs: seesRepairs,
    projections: seesOpMix,
  };
  const open = shown[tab] ? tab : "extraction";

  return (
    <PageContainer>
      <div className="mb-6">
        <PageHeader title="Health" tab={HEALTH_TABS[open]} />
        <p className="text-sm text-text-muted max-w-2xl">
          How Loom's workers, extraction and this namespace's memories are doing, and the repairs your role allows.
        </p>
      </div>
      <Tabs value={open} onValueChange={(v) => setTab(v as HealthTab)}>
        <Tabs.TriggersList className="mb-5" aria-label="Health">
          {seesWorkers && (
            <TabTrigger value="workers" icon="settings">
              {HEALTH_TABS.workers}
            </TabTrigger>
          )}
          <TabTrigger value="extraction" icon="sparkle">
            {HEALTH_TABS.extraction}
          </TabTrigger>
          {seesOpMix && (
            <TabTrigger value="opmix" icon="pie-chart">
              {HEALTH_TABS.opmix}
            </TabTrigger>
          )}
          {hasConsolidations && (
            <TabTrigger value="consolidations" icon="cards">
              {HEALTH_TABS.consolidations}
            </TabTrigger>
          )}
          {seesNamespace && (
            <TabTrigger value="namespace" icon="database">
              {HEALTH_TABS.namespace}
            </TabTrigger>
          )}
          {seesRepairs && (
            <TabTrigger value="repairs" icon="refresh">
              {HEALTH_TABS.repairs}
            </TabTrigger>
          )}
          {seesOpMix && (
            <TabTrigger value="projections" icon="warning">
              {HEALTH_TABS.projections}
            </TabTrigger>
          )}
        </Tabs.TriggersList>
        {seesWorkers && (
          <Tabs.Content value="workers">
            <WorkersPanel />
          </Tabs.Content>
        )}
        <Tabs.Content value="extraction">
          <ExtractionQualityPanel />
        </Tabs.Content>
        {seesOpMix && (
          <Tabs.Content value="opmix">
            <OpMixPanel />
          </Tabs.Content>
        )}
        {hasConsolidations && (
          <Tabs.Content value="consolidations">
            <ConsolidationsPanel />
          </Tabs.Content>
        )}
        {seesNamespace && (
          <Tabs.Content value="namespace">
            <NamespaceHealthPanel />
          </Tabs.Content>
        )}
        {seesRepairs && (
          <Tabs.Content value="repairs">
            <RepairsPanel />
          </Tabs.Content>
        )}
        {seesOpMix && (
          <Tabs.Content value="projections">
            <ProjectionFailuresPanel />
          </Tabs.Content>
        )}
      </Tabs>
    </PageContainer>
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
  const lastRun = parseChTime(w.last_run_at);
  if (!lastRun) return "waiting";
  // Stalled = last tick older than 3× the longer of its waits: the interval, or the idle
  // interval a worker that runs back to back (interval 0) keeps when it finds no work.
  // Neither known → healthy.
  const cadence = Math.max(w.interval_seconds ?? 0, w.idle_interval_seconds ?? 0);
  if (cadence > 0) {
    const ageMs = Date.now() - lastRun.getTime();
    if (ageMs > cadence * 1000 * 3) return "stalled";
  }
  return "healthy";
}

// Pill per pass verdict. `ok` needs no pill — the dot says it.
const PASS_PILL: Record<PassStatus, { label: string; state: BadgeState } | null> = {
  ok: null,
  idle: { label: "Idle — ran, nothing to do", state: "neutral" },
  partial: { label: "Partial — a leg failed", state: "warning" },
  timeout: { label: "Timed out at its deadline", state: "danger" },
  failed: { label: "Last pass failed", state: "danger" },
  cancelled: { label: "Cancelled — process stopped mid-pass", state: "warning" },
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

// A worker's interval is in its own column ("Every"), so the pass line keeps to the pass.
const WORKER_HEADERS = [
  { label: "Worker" },
  { label: "Status", width: "300px" },
  { label: "Last pass" },
  { label: "Every", width: "80px" },
];

export function WorkersPanel() {
  const [rows, setRows] = useState<WorkerRow[] | null>(null);
  const [store, setStore] = useState<WorkersStore | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // workers.status is a /v1/admin/* handler: the JWT claim alone opens it. Without the
  // claim there is no request — the panel reads "not reachable from here" (the dashboard
  // never issues a request the token cannot make).
  const whoami = useStore((s) => s.whoami);
  const canReadWorkers = canCall("admin/workers.status", null, whoami);
  // Refresh keeps the workers on screen, marked busy, until the read answers.
  const [refreshing, setRefreshing] = useState(false);

  useAbortableEffect(
    (signal, live) => {
      if (!canReadWorkers) {
        setRows([]);
        setStore(null);
        return;
      }
      setRefreshing(true);
      setError(null);
      getJson<unknown>("/v1/admin/workers.status", { signal })
        .then((d) => {
          if (!live()) return;
          const parsed = parseWorkersStatus(d);
          setStore(parsed.store);
          setRows(parsed.rows);
        })
        .catch((e) => {
          if (live()) setError(e);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
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
      <ListToolbar
        label="Workers status"
        search={false}
        count={
          !error && rows !== null && visible.length > 0
            ? `${visible.length} ${visible.length === 1 ? "worker" : "workers"}`
            : undefined
        }
      >
        <ScopeBadge kind="loom-wide" />
        {/* With no rows at all the empty state below says it. */}
        {!error &&
          rows !== null &&
          rows.length > 0 &&
          (allHealthy ? (
            <Badge text="All systems running" state="success" />
          ) : issues.length > 0 ? (
            <Badge text={`${issues.length} ${issues.length === 1 ? "issue" : "issues"}`} state="danger" />
          ) : (
            <span className="truncate text-sm text-text-muted">No workers reporting</span>
          ))}
        <BusyButton
          type="secondary"
          htmlType="button"
          iconLeft="refresh"
          onClick={() => setRefreshKey((k) => k + 1)}
          busy={refreshing}
          className="ml-auto shrink-0"
        >
          Refresh
        </BusyButton>
      </ListToolbar>
      {!error && rows !== null && colours.length > 0 && (
        <p className="mb-3 text-xs text-text-muted" data-testid="reporting-colours">
          Reporting from {colours.join(", ")}
          {store?.rows ? (
            <>
              {" "}
              · {store.rows} {store.rows === 1 ? "row" : "rows"} in {store.table || "loom.worker_status"}
            </>
          ) : null}
        </p>
      )}
      {!!error && <ListState kind="error" things="workers" error={error} onRetry={() => setRefreshKey((k) => k + 1)} />}
      {!error && rows === null && (
        <Table
          aria-label="Workers"
          aria-busy
          size="sm"
          headers={WORKER_HEADERS}
          rows={[]}
          noDataMessage={<TableSkeleton label="Loading workers" />}
        />
      )}
      {!error && rows !== null && (rows.length === 0 || visible.length === 0) && (
        <ListState
          kind="empty"
          icon="settings"
          title={rows.length === 0 ? "No workers reporting" : "Nothing to show"}
          description={
            rows.length === 0
              ? "Neither this process nor loom.worker_status has a worker pass on record. Either no workers colour is running, or /v1/admin/workers.status is not reachable from here."
              : "All workers are disabled in config."
          }
          action={rows.length > 0 ? { label: "Show all", onClick: () => setShowAll(true) } : undefined}
        />
      )}
      {!error && rows !== null && rows.length > 0 && reporting.length === 0 && effective.length > 0 && (
        <Alert
          role="status"
          className="mb-3"
          state="neutral"
          size="small"
          text={
            <>
              <strong>No worker has reported a pass yet.</strong> This process runs no workers
              (LOOM_BACKGROUND_WORKERS=off under blue/green); the workers colour writes each finished pass to
              loom.worker_status and nothing has landed there in the last 7 days.
            </>
          }
        />
      )}
      {!error && rows !== null && visible.length > 0 && (
        <div className="space-y-1.5" data-testid="workers-list" aria-busy={refreshing || undefined}>
          <Table
            aria-label="Workers"
            size="sm"
            headers={WORKER_HEADERS}
            rows={visible.map((w) => {
              const status = statusOf(w);
              const info = WORKER_INFO[w.name] || {
                label: w.name.replace(/_/g, " "),
                blurb: "Background worker.",
              };
              const pill = w.last_pass_status ? PASS_PILL[w.last_pass_status] : null;
              const from = colourOf(w.reporting_from);
              const written = w.rows_written ?? 0;
              return {
                id: w.name,
                isDisabled: status === "disabled",
                items: [
                  {
                    overflowMode: "wrap",
                    label: (
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-medium text-text">{info.label}</span>
                          <span className="text-xs text-text-muted font-mono">{w.name}</span>
                        </div>
                        <div className="text-xs text-text-muted mt-0.5">{info.blurb}</div>
                      </div>
                    ),
                  },
                  {
                    overflowMode: "wrap",
                    label: (
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {status === "healthy" && !pill && <Badge size="sm" state="success" text="Healthy" />}
                        {(w.consecutive_failures ?? 0) > 0 && (
                          <Badge
                            size="sm"
                            state="danger"
                            text={`${w.consecutive_failures} consecutive failure${(w.consecutive_failures ?? 0) === 1 ? "" : "s"}${(w.consecutive_failures ?? 0) >= 3 ? ", degraded" : ""}`}
                          />
                        )}
                        {pill && status !== "disabled" && <Badge size="sm" state={pill.state} text={pill.label} />}
                        {status === "stalled" && (
                          <Badge size="sm" state="warning" text="Stalled — overdue for a tick" />
                        )}
                        {status === "disabled" && (
                          <Badge
                            size="sm"
                            state="disabled"
                            text={
                              w.disabled_reason === "disabled_by_setting"
                                ? "Off in config"
                                : w.disabled_reason || "Disabled"
                            }
                          />
                        )}
                      </div>
                    ),
                  },
                  {
                    overflowMode: "wrap",
                    label:
                      status === "disabled" ? (
                        <span className="text-xs text-text-muted">—</span>
                      ) : (
                        <div className="text-xs text-text-muted tabular">
                          {w.last_run_at ? (
                            <>
                              Last ran <ListTime time={w.last_run_at} quiet />
                              {w.last_duration_ms != null && <> · took {fmtDuration(w.last_duration_ms)}</>}
                              {written > 0 && <> · {written.toLocaleString()} rows written</>}
                              {w.total_runs != null && <> · {w.total_runs.toLocaleString()} runs total</>}
                              {from && (
                                <>
                                  {" "}
                                  · reporting from{" "}
                                  <span className="font-mono">
                                    {from}
                                    {from !== w.reporting_from && (
                                      <span className="sr-only"> ({w.reporting_from})</span>
                                    )}
                                  </span>
                                </>
                              )}
                              {w.namespace && (
                                <>
                                  {" "}
                                  · last namespace reached: <span className="font-mono">{w.namespace}</span>
                                </>
                              )}
                              {w.trigger === "manual" && <> · manual run</>}
                            </>
                          ) : (
                            <>No pass reported yet</>
                          )}
                          {w.last_error && status !== "healthy" && (
                            <span className="block text-danger mt-0.5">Error: {w.last_error.slice(0, 200)}</span>
                          )}
                        </div>
                      ),
                  },
                  {
                    label: (
                      <span className="text-xs text-text-muted tabular">
                        {w.interval_seconds === 0
                          ? "back to back"
                          : w.interval_seconds
                            ? fmtInterval(w.interval_seconds)
                            : "—"}
                      </span>
                    ),
                  },
                ],
              };
            })}
          />
          {hiddenByConfig > 0 && !showAll && (
            <Button type="empty" htmlType="button" fillWidth iconLeft="eye" onClick={() => setShowAll(true)}>
              {`Show ${hiddenByConfig} worker${hiddenByConfig === 1 ? "" : "s"} disabled in config`}
            </Button>
          )}
          {showAll && hiddenByConfig === 0 && (
            <Button type="empty" htmlType="button" fillWidth iconLeft="eye-closed" onClick={() => setShowAll(false)}>
              Hide config-disabled workers
            </Button>
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

/** ops.health's op_mix answer, with the scope it was read for: a report ({check, data}), a
 *  check the server ran but could not compute (data: null — status "fail", detail says
 *  why), or null when the request was refused (an answer always carries the op_mix
 *  check). Any other failed read is said as one. */
export function OpMixPanel() {
  const identity = useIdentity();
  const [scope, setScope] = useState<"namespace" | "org">("namespace");
  const [op, setOp] = useState<
    { check: OpsHealthCheck; data: OpMixData | null; scope: typeof scope } | null | undefined
  >(undefined);
  const [error, setError] = useState<unknown>(null);
  // Retry reads again through the effect.
  const [reads, setReads] = useState(0);
  // A scope toggle keeps the mix (and the toggle) on screen, marked busy, until it answers;
  // another namespace starts over, during render, so its figures never draw under the new
  // namespace's badge.
  const [refreshing, setRefreshing] = useState(false);
  const opKey = `${identity.org}/${identity.namespace}`;
  const [opFor, setOpFor] = useState(opKey);
  if (opFor !== opKey) {
    setOpFor(opKey);
    setOp(undefined);
    setError(null);
  }

  useAbortableEffect(
    (signal, live) => {
      setRefreshing(true);
      setError(null);
      const asked = scope;
      getOpsHealth(
        { org: identity.org, namespace: identity.namespace },
        { namespace: scope === "namespace" ? identity.namespace : null, signal },
      )
        .then((r) => {
          if (!live()) return;
          const check = r.checks.find((c) => c.name === "op_mix");
          // Every answer carries the op_mix check: one without it is a failed read.
          if (check) setOp({ check, data: parseOpMix(check.data), scope: asked });
          else setError(new Error("ops.health answered without its op_mix check"));
        })
        .catch((e) => {
          if (!live()) return;
          // A refusal is the role, said as such; any other failure is a failed read.
          if (e instanceof ApiError && e.status === 403) setOp(null);
          else setError(e);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [identity.org, identity.namespace, scope, reads],
  );

  if (op === undefined && !error)
    return (
      <LoadingSkeleton label="Loading the operation mix">
        <Skeleton className="h-40 w-full" />
      </LoadingSkeleton>
    );
  // The scope row stays above every state: a scope whose read failed is left with it. Its
  // badge names the scope of what is shown: the read's scope over a report or a failed
  // check (a toggle's read has not yet replaced it), the asked one over a failed read.
  const scopeRow = (shown: typeof scope, summary?: ReactNode) => (
    <div className="flex flex-wrap items-center gap-3">
      {shown === "namespace" ? (
        <ScopeBadge kind="namespace" value={identity.namespace} />
      ) : (
        <Badge size="sm" state="neutral" text={`org · ${identity.org}`} />
      )}
      <ButtonGroup
        aria-label="Operation mix scope"
        options={[
          { value: "namespace", label: "This namespace" },
          { value: "org", label: "Whole org" },
        ]}
        selected={scope}
        onClick={(value) => setScope(value === "org" ? "org" : "namespace")}
      />
      {summary}
    </div>
  );
  const withScope = (shown: typeof scope, body: ReactNode) => (
    <div className="space-y-4" aria-busy={refreshing || undefined}>
      {scopeRow(shown)}
      {body}
    </div>
  );
  if (error)
    return withScope(
      scope,
      <ListState kind="error" things="the operation mix" error={error} onRetry={() => setReads((n) => n + 1)} />,
    );
  if (!op) {
    return withScope(
      scope,
      <EmptyState
        icon="pie-chart"
        title="Operation mix unavailable"
        description="ops.health needs an org-admin token."
      />,
    );
  }
  const { check, data } = op;
  if (data === null) {
    // The check's detail is the read for op.scope, which the badge names.
    return withScope(
      op.scope,
      <EmptyState
        icon="pie-chart"
        title="Operation mix check failed"
        description={check.repair ? `${check.detail} — ${check.repair}` : check.detail}
      />,
    );
  }
  const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
  const classes = ["create", "read", "update", "annotate", "delete", "scratchpad", "maintain", "other"].filter(
    (k) => (data.counts[k] ?? 0) > 0,
  );
  const tone = check.status === "ok" ? "bg-success" : check.status === "warn" ? "bg-warn" : "bg-danger";
  const fmt = (n: number) => n.toLocaleString();
  return (
    <div className="space-y-4" aria-busy={refreshing || undefined}>
      {scopeRow(
        op.scope,
        <span className="text-xs text-text-muted">
          last {data.window_days} d · {fmt(total)} calls · {data.agents} agent{data.agents === 1 ? "" : "s"}
        </span>,
      )}

      <div className="rounded-xl border border-border bg-surface p-4">
        <div className="flex items-start gap-2">
          <span className={`mt-1.5 inline-block h-2 w-2 rounded-full ${tone}`} />
          <div className="text-sm">
            <div className="font-medium">{check.detail}</div>
            {check.repair && <div className="text-text-muted mt-0.5">{check.repair}</div>}
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
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface-2">
            {classes.map((k) => (
              <div
                key={k}
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
                    {CLASS_INFO[k].label} <span className="text-text-muted tabular-nums">{fmt(data.counts[k])}</span>
                  </div>
                  <div className="text-text-muted">{CLASS_INFO[k].blurb}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {data.top.length > 0 && (
        <Table
          size="sm"
          headers={[{ label: "Operation" }, { label: "Class" }, { label: "Calls", width: "120px" }]}
          rows={data.top.map((t) => ({
            id: t.operation,
            items: [
              { label: <span className="font-mono text-xs">{t.operation}</span> },
              { label: CLASS_INFO[t.class]?.label ?? t.class },
              { label: <span className="tabular-nums">{fmt(t.count)}</span> },
            ],
          }))}
        />
      )}
    </div>
  );
}

const CLASS_BAR: Record<string, string> = {
  create: "bg-cat-4",
  read: "bg-text-muted",
  update: "bg-cat-6",
  annotate: "bg-cat-5",
  delete: "bg-cat-7",
  scratchpad: "bg-cat-3",
  maintain: "bg-text-muted",
  other: "bg-text-muted",
};

function ConsolidationsPanel() {
  const identity = useIdentity();
  const [rows, setRows] = useState<ConsolidationRunRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      setError(null);
      listConsolidations({ org: identity.org, namespace: identity.namespace }, { limit: 50, signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [identity.org, identity.namespace, reload],
  );

  if (error) {
    return <ListState kind="error" things="consolidations" error={error} onRetry={() => setReload((n) => n + 1)} />;
  }
  if (rows === null)
    return (
      <LoadingSkeleton label="Loading consolidations">
        <Skeleton className="h-40 w-full" />
      </LoadingSkeleton>
    );
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
      <ListState
        kind="empty"
        icon="cards"
        title="No consolidation runs yet"
        description="The background consolidator merges semantically-similar memories. Runs appear here once it fires."
      />
    );
  }
  return (
    <Table
      size="sm"
      headers={[
        { label: "Status", width: "110px" },
        { label: "Output key" },
        { label: "Sources", width: "90px" },
        { label: "Tokens", width: "90px" },
        { label: "Duration", width: "100px" },
        { label: "When", width: "120px" },
      ]}
      rows={rows.map((r) => ({
        id: r.run_id,
        items: [
          { label: <Badge size="sm" text={r.status} state={RUN_STATE[r.status] ?? "neutral"} /> },
          { label: <span className="font-mono text-xs">{r.output_memory_key ?? "—"}</span> },
          { label: <span className="tabular">{r.cluster_size}</span> },
          { label: <span className="tabular">{(r.tokens_in ?? 0) + (r.tokens_out ?? 0)}</span> },
          { label: <span className="tabular">{r.duration_ms}ms</span> },
          { label: <ListTime time={r.ran_at} quiet className="text-text-muted" /> },
        ],
      }))}
    />
  );
}

const RUN_STATE: Record<string, BadgeState> = { written: "success", failed: "danger" };

// Per-namespace approve/reject ratio for the learnings queue.
function ExtractionQualityPanel() {
  const identity = useStore((s) => s.identity);
  const [rows, setRows] = useState<ExtractionQualityRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  // Refresh re-runs the effect, so a refresh superseded by an org switch is aborted and its
  // answer dropped like any other stale read.
  const [refreshKey, setRefreshKey] = useState(0);
  // Refresh keeps the figures on screen, marked busy, until the read answers.
  const [refreshing, setRefreshing] = useState(false);

  useAbortableEffect(
    (signal, live) => {
      if (!identity) return;
      setError(null);
      setRefreshing(true);
      getExtractionQuality({ org: identity.org }, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e: unknown) => {
          if (live()) setError(e);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [identity?.org, refreshKey],
  );

  if (!identity) return null;
  if (error) {
    return (
      <ListState kind="error" things="extraction quality" error={error} onRetry={() => setRefreshKey((k) => k + 1)} />
    );
  }
  if (rows === null) {
    return (
      <LoadingSkeleton label="Loading extraction quality">
        <div className="space-y-2">
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      </LoadingSkeleton>
    );
  }
  if (rows.length === 0) {
    return (
      <ListState
        kind="empty"
        icon="sparkle"
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
    <div className="space-y-4" aria-busy={refreshing || undefined}>
      <div className="grid grid-cols-4 gap-3">
        <Stat label="Pending" value={totalPending} hint="awaiting review" />
        <Stat label="Promoted" value={totalPromoted} hint="approved → memory" />
        <Stat label="Rejected" value={totalRejected} hint="dropped on review" />
        <Stat
          label="Approve ratio"
          value={totalDecided > 0 ? (orgRatio * 100).toFixed(0) + "%" : "—"}
          hint={totalDecided < 10 ? `${totalDecided}/10 decisions for signal` : orgRatio < 0.3 ? "low" : "healthy"}
          error={totalDecided >= 10 && orgRatio < 0.3}
        />
      </div>
      <div className="flex items-center justify-between">
        <div className="text-xs text-text-muted">By namespace, org={identity.org}</div>
        <BusyButton
          type="secondary"
          htmlType="button"
          iconLeft="refresh"
          onClick={() => setRefreshKey((k) => k + 1)}
          busy={refreshing}
        >
          Refresh
        </BusyButton>
      </div>
      <Table
        size="sm"
        headers={[
          { label: "Namespace" },
          { label: "Pending", width: "100px" },
          { label: "Promoted", width: "100px" },
          { label: "Rejected", width: "100px" },
          { label: "Approve", width: "100px" },
        ]}
        rows={rows.map((r) => {
          const decided = r.promoted + r.rejected;
          const low = decided >= 10 && r.approve_ratio < 0.3;
          return {
            id: r.namespace,
            items: [
              { label: <span className="font-mono text-xs">{r.namespace}</span> },
              { label: <span className="tabular">{r.pending}</span> },
              { label: <span className="tabular">{r.promoted}</span> },
              { label: <span className="tabular">{r.rejected}</span> },
              {
                label: (
                  <span className={`tabular ${low ? "text-warn" : ""}`}>
                    {decided > 0 ? (r.approve_ratio * 100).toFixed(0) + "%" : "—"}
                  </span>
                ),
              },
            ],
          };
        })}
      />
    </div>
  );
}
