import { useState, type ReactNode } from "react";
import { DollarSign, AlertTriangle, Pencil, Check, X as XIcon } from "lucide-react";
import {
  listCostRollup,
  getOrgBudgetStatus,
  getNamespace,
  reregisterNamespace,
  type CostRow,
  type CostGroupDim,
  type BudgetStatus,
  type NamespaceInfo,
} from "../../api";
import { useStore, useIdentity } from "../../lib/state";
import { canUpdateNamespace, hasOrgAdminClaim } from "../../lib/role";
import { invalidateShared, sharedBudgetStatus } from "../../lib/sharedReads";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { displayAgent, humanizeError, SENTINEL_AGENTS } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { PageContainer } from "../../layout/PageContainer";

/**
 * Usage — LLM operations / cost rollup view.
 *
 * Field shape mirrors loom.agent_cost_rollup MV directly: `cost_usd`,
 * `tokens` (in+out combined), `embed_tokens`, `ops`, `errors`,
 * `latency_*`. Backend rows are already aggregated by (day, agent,
 * operation, ...); we group again by day for the chart and otherwise
 * render the raw breakdown.
 */
// How the cost table is pivoted. "namespace" is the operator's org-wide view
// (namespace omitted → backend scopes by role); "user" / "agent" route through
// the per-event scan. operation is the default per-namespace breakdown.
type Breakdown = "operation" | "agent" | "user" | "namespace";
const BREAKDOWN_QUERY: Record<Breakdown, { groupBy: CostGroupDim[]; orgWide?: boolean }> = {
  operation: { groupBy: ["day", "agent", "operation"] },
  agent: { groupBy: ["day", "agent"] },
  user: { groupBy: ["day", "user_id"] },
  namespace: { groupBy: ["day", "namespace"], orgWide: true },
};

function isBreakdown(v: string): v is Breakdown {
  return Object.hasOwn(BREAKDOWN_QUERY, v);
}

export function Spending() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const [rows, setRows] = useState<CostRow[] | null>(null);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [orgBudget, setOrgBudget] = useState<BudgetStatus | null>(null);
  const [breakdown, setBreakdown] = useState<Breakdown>("operation");
  // Default OFF: read ops (memory.list, audit.list, conflicts.list) dominate
  // the row count but have $0 cost and no tokens, drowning the few rows that
  // actually matter for spend. Toggle exposes them for performance debugging.
  const [showAllOps, setShowAllOps] = useState(false);

  // Org-wide breakdown is meaningful only for ORG_ADMIN+ (the operator); for
  // anyone below, resolve_org_scope narrows it to their own namespaces, so we
  // don't offer it.
  const isOrgAdmin = hasOrgAdminClaim(whoami);
  // The cap editor round-trips namespace.register, whose update path needs ADMIN on the
  // namespace or the owner of an auto-registered row, and on a restricted row a named,
  // directly granted admin; the row says which (owner, tags, classification).
  const myNamespaces = useStore((s) => s.myNamespaces);
  const [nsRow, setNsRow] = useState<NamespaceInfo | null>(null);
  useAbortableEffect(
    (signal, live) => {
      setNsRow(null);
      getNamespace({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((n) => {
          if (live()) setNsRow(n);
        })
        .catch(() => {});
    },
    [identity.org, identity.namespace],
  );
  const canEditCap = canUpdateNamespace(whoami, myNamespaces, identity.namespace, nsRow);

  // Rows re-fetch when the pivot changes; the budget bars don't.
  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      listCostRollup(
        { org: identity.org, namespace: identity.namespace },
        { days: 7, ...BREAKDOWN_QUERY[breakdown], signal },
      )
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
        });
    },
    [identity.org, identity.namespace, breakdown],
  );

  // Bumped after a cap save: the bars re-read the server's answer instead of guessing it.
  const [budgetReload, setBudgetReload] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      setBudget(null);
      setOrgBudget(null);
      // The sidebar's poll and this page share one read.
      sharedBudgetStatus({ org: identity.org, namespace: identity.namespace })
        .then((b) => {
          if (live()) setBudget(b);
        })
        .catch(() => {
          if (live()) setBudget(null);
        });
      getOrgBudgetStatus({ org: identity.org }, { signal })
        .then((b) => {
          if (live()) setOrgBudget(b);
        })
        .catch(() => {
          if (live()) setOrgBudget(null);
        });
    },
    [identity.org, identity.namespace, budgetReload],
  );

  // Dimension columns shown between Day and the metrics, per pivot.
  const dimColumns: { header: string; cell: (r: CostRow) => ReactNode; cls?: (r: CostRow) => string }[] =
    breakdown === "operation"
      ? [
          {
            header: "Agent",
            cell: (r) => (r.agent ? displayAgent(r.agent) : <span className="text-[var(--color-text-subtle)]">—</span>),
            cls: (r) => (r.agent && SENTINEL_AGENTS.has(r.agent) ? "italic text-[var(--color-text-muted)]" : ""),
          },
          { header: "Op", cell: (r) => <span className="font-mono text-xs">{r.operation}</span> },
        ]
      : breakdown === "agent"
        ? [
            {
              header: "Agent",
              cell: (r) =>
                r.agent ? displayAgent(r.agent) : <span className="text-[var(--color-text-subtle)]">—</span>,
              cls: (r) => (r.agent && SENTINEL_AGENTS.has(r.agent) ? "italic text-[var(--color-text-muted)]" : ""),
            },
          ]
        : breakdown === "user"
          ? [
              {
                header: "User",
                cell: (r) =>
                  r.user_id && r.user_id !== "-" ? (
                    <span className="font-mono text-xs">{r.user_id}</span>
                  ) : (
                    <span className="text-[var(--color-text-subtle)]">— unattributed</span>
                  ),
              },
            ]
          : [{ header: "Namespace", cell: (r) => <span className="font-mono text-xs">{r.namespace || "—"}</span> }];

  const visibleRows = (rows || []).filter((r) => showAllOps || r.cost_usd > 0 || r.tokens > 0 || r.embed_tokens > 0);
  const hiddenCount = (rows || []).length - visibleRows.length;

  // Aggregate by day from full row set so totals are accurate even when
  // hidden — visible filter affects the table, not the headline stats.
  const byDay = new Map<string, number>();
  let totalCost = 0;
  let totalTokens = 0;
  let totalEmbedTokens = 0;
  let totalOps = 0;
  for (const r of rows || []) {
    const day = r.day || "—";
    byDay.set(day, (byDay.get(day) || 0) + (r.cost_usd || 0));
    totalCost += r.cost_usd || 0;
    totalTokens += r.tokens || 0;
    totalEmbedTokens += r.embed_tokens || 0;
    totalOps += r.ops || 0;
  }
  const days = Array.from(byDay.entries()).sort();
  const maxCost = Math.max(0.0001, ...days.map(([, c]) => c));

  return (
    <PageContainer>
      <div className="flex items-baseline gap-3 mb-6">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <DollarSign size={20} /> Usage
        </h1>
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>

      {orgBudget && orgBudget.spent_24h_usd > 0 && (
        <div className="mb-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 flex items-baseline justify-between gap-3">
          <div>
            <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1">
              Org-wide 24h LLM spend
            </div>
            <div className="text-xl font-semibold tabular">${orgBudget.spent_24h_usd.toFixed(4)}</div>
          </div>
          <div className="text-right text-xs text-[var(--color-text-muted)]">
            across {orgBudget.namespaces_with_spend ?? 0} namespace
            {(orgBudget.namespaces_with_spend ?? 0) === 1 ? "" : "s"} in{" "}
            <span className="font-mono">{identity.org}</span>
          </div>
        </div>
      )}

      {budget && budget.cap_source !== "uncapped" && (
        <div
          className={`mb-5 rounded-xl border p-4 ${
            budget.over_cap
              ? "border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10"
              : "border-[var(--color-border)] bg-[var(--color-surface)]"
          }`}
        >
          <div className="flex items-baseline justify-between gap-3 mb-2">
            <div className="text-sm">
              {budget.over_cap ? (
                <span className="inline-flex items-center gap-1.5 text-[var(--color-warn)] font-medium">
                  <AlertTriangle size={13} /> Daily LLM cap reached — extraction paused for this namespace
                </span>
              ) : (
                <span className="font-medium">Daily LLM budget</span>
              )}
            </div>
            <div className="text-xs tabular text-[var(--color-text-muted)]">
              ${budget.spent_24h_usd.toFixed(4)} / ${budget.cap_usd.toFixed(2)} ({budget.cap_source})
            </div>
          </div>
          <div className="h-1.5 bg-[var(--color-surface-2)] rounded-full overflow-hidden">
            <div
              className={`h-full transition-all ${budget.over_cap ? "bg-[var(--color-warn)]" : "bg-[var(--color-accent)]"}`}
              style={{ width: `${Math.min(100, (budget.pct_of_cap ?? 0) * 100)}%` }}
              aria-label={`${Math.round((budget.pct_of_cap ?? 0) * 100)}% of daily LLM budget used`}
              role="progressbar"
              aria-valuenow={Math.round((budget.pct_of_cap ?? 0) * 100)}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
          <div className="mt-2 flex items-baseline justify-between gap-3">
            <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
              Rolling 24h sum of upstream LLM cost on this namespace. When the cap is hit,{" "}
              <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 py-0.5 rounded">
                entity_extractor
              </code>{" "}
              skip writes here until usage drops below the cap.
            </div>
            {canEditCap && (
              <BudgetCapEditor
                org={identity.org}
                namespace={identity.namespace}
                currentCap={budget.cap_source === "namespace" ? budget.cap_usd : 0}
                onSaved={() => setBudgetReload((n) => n + 1)}
              />
            )}
          </div>
        </div>
      )}

      {rows === null && <Skeleton className="h-40 w-full" />}
      {rows !== null && rows.length === 0 && (
        <EmptyState
          icon={DollarSign}
          title="No operations recorded"
          description="No API calls against this namespace in the last 7 days. Activity (reads + LLM-touching writes) shows up here once your agent runs."
        />
      )}
      {rows !== null && rows.length > 0 && totalCost === 0 && totalTokens === 0 && totalEmbedTokens === 0 && (
        <div className="mb-5 p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] text-sm text-[var(--color-text-muted)]">
          <span className="text-[var(--color-text)] font-medium">$0 in this window.</span> The{" "}
          {totalOps.toLocaleString()} operations counted are all read-only API calls (memory listing, audit reads, etc.)
          that don't hit a paid provider. Cost shows up here once your agent writes memories (embedder fires), or once
          the entity_extractor worker is enabled.
        </div>
      )}

      {rows !== null && rows.length > 0 && totalEmbedTokens > 0 && totalCost === 0 && (
        <div className="mb-5 p-3 rounded-xl border border-warn/60 bg-warn/10 text-sm text-warn">
          <span className="font-medium">{fmt(totalEmbedTokens)} embed tokens but $0 cost.</span> Your embedder fired but
          its upstream-cost field isn't yet attributed in the rollup — known gap, applies to every Loom deployment
          today. Once the attribution wiring lands, the number under Total cost will include embedder usage.
          (LLM-extraction worker costs are tracked correctly.)
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <>
          <div className="grid grid-cols-4 gap-3 mb-6">
            <Stat label="Operations (7d)" value={fmt(totalOps)} />
            <Stat
              label="Total cost"
              value={`$${totalCost.toFixed(4)}`}
              hint="LLM extraction + embedder (attribution coming)"
            />
            <Stat label="LLM tokens" value={fmt(totalTokens)} hint="extraction / classify / rerank" />
            <Stat label="Embed tokens" value={fmt(totalEmbedTokens)} hint="memory + knowledge writes" />
          </div>

          {/* Cost-by-day chart only renders when any row has a `day` value.
              When everything's null (e.g. all rows are aggregated-without-day
              groups), skip the empty chart entirely. */}
          {days.some(([d]) => d !== "—") && (
            <div className="rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 mb-6">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-4">
                Cost by day
              </h2>
              <div className="flex items-end gap-2 h-[140px]">
                {days.map(([day, cost]) => (
                  <div key={day} className="flex-1 flex flex-col items-center gap-1.5">
                    <div className="text-2xs tabular text-[var(--color-text-muted)]">${cost.toFixed(4)}</div>
                    <div
                      className="w-full bg-[var(--color-accent)] rounded-t"
                      style={{ height: `${(cost / maxCost) * 100}%`, minHeight: "2px" }}
                    />
                    <div className="text-2xs tabular text-[var(--color-text-subtle)]">
                      {day === "—" ? "—" : day.slice(5)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="flex items-center justify-between mb-2">
            <div className="inline-flex items-center gap-2">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">
                Break down by
              </h2>
              <select
                value={breakdown}
                onChange={(e) => {
                  if (isBreakdown(e.target.value)) setBreakdown(e.target.value);
                }}
                className="text-xs bg-[var(--color-bg)] border border-[var(--color-border)] rounded-md px-2 py-1"
                aria-label="Cost breakdown dimension"
              >
                <option value="operation">Operation</option>
                <option value="agent">Agent</option>
                <option value="user">User</option>
                {isOrgAdmin && <option value="namespace">Namespace (org-wide)</option>}
              </select>
            </div>
            <label className="inline-flex items-center gap-2 text-xs text-[var(--color-text-muted)] cursor-pointer">
              <input
                type="checkbox"
                checked={showAllOps}
                onChange={(e) => setShowAllOps(e.target.checked)}
                className="accent-[var(--color-accent)]"
              />
              Show all operations
              {hiddenCount > 0 && !showAllOps && (
                <span className="text-[var(--color-text-subtle)] tabular">({hiddenCount} zero-cost rows hidden)</span>
              )}
            </label>
          </div>
          {visibleRows.length === 0 && (
            <div className="p-6 text-center text-sm text-[var(--color-text-muted)] bg-[var(--color-surface)] rounded-xl border border-[var(--color-border)]">
              No cost-bearing operations in this window. Toggle "Show all operations" to see read-only API activity.
            </div>
          )}
          {visibleRows.length > 0 && (
            <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
              <table className="w-full text-sm">
                <thead className="text-2xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)]">
                  <tr>
                    <th className="text-left px-3 py-2">Day</th>
                    {dimColumns.map((c) => (
                      <th key={c.header} className="text-left px-3 py-2">
                        {c.header}
                      </th>
                    ))}
                    <th className="text-right px-3 py-2">Ops</th>
                    <th className="text-right px-3 py-2">Err</th>
                    <th className="text-right px-3 py-2">Tokens</th>
                    <th className="text-right px-3 py-2">p95 ms</th>
                    <th className="text-right px-3 py-2">$</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((r, i) => (
                    <tr key={i} className="border-t border-[var(--color-border)]">
                      <td className="px-3 py-2 font-mono text-2xs tabular text-[var(--color-text-muted)]">
                        {r.day || "—"}
                      </td>
                      {dimColumns.map((c, j) => (
                        <td key={j} className={`px-3 py-2 ${c.cls ? c.cls(r) : ""}`}>
                          {c.cell(r)}
                        </td>
                      ))}
                      <td className="px-3 py-2 text-right tabular">{fmt(r.ops)}</td>
                      <td
                        className={`px-3 py-2 text-right tabular ${r.errors > 0 ? "text-[var(--color-danger)]" : "text-[var(--color-text-subtle)]"}`}
                      >
                        {r.errors || ""}
                      </td>
                      <td className="px-3 py-2 text-right tabular text-2xs">
                        {fmt(r.tokens)}
                        {r.embed_tokens > 0 && (
                          <span className="text-[var(--color-text-subtle)]"> + {fmt(r.embed_tokens)} embed</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right tabular text-2xs text-[var(--color-text-muted)]">
                        {Math.round(r.latency_p95_ms)}
                      </td>
                      <td className="px-3 py-2 text-right tabular font-mono">
                        {r.cost_usd > 0 ? (
                          `$${r.cost_usd.toFixed(4)}`
                        ) : (
                          <span className="text-[var(--color-text-subtle)]">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </PageContainer>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
      <div className="text-xl font-semibold tabular tracking-tight">{value}</div>
      {hint && <div className="text-2xs text-[var(--color-text-subtle)] mt-1">{hint}</div>}
    </div>
  );
}

function fmt(n: number): string {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

/**
 * BudgetCapEditor — inline cap editor on the Usage tab, so a user staring at "you're over
 * the $1 cap" changes it where they read it instead of in NamespaceSettingsDialog.
 * Round-trips namespace.register, then the page re-reads the budget.
 */
function BudgetCapEditor({
  org,
  namespace,
  currentCap,
  onSaved,
}: {
  org: string;
  namespace: string;
  currentCap: number;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(String(currentCap || 0));
  const [busy, setBusy] = useState(false);

  if (!editing) {
    return (
      <button
        onClick={() => {
          setDraft(String(currentCap || 0));
          setEditing(true);
        }}
        className="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-medium rounded-md hover:bg-[var(--color-surface-2)] text-[var(--color-text)] shrink-0"
        title="Edit this namespace's daily LLM budget"
      >
        <Pencil size={11} /> Edit cap
      </button>
    );
  }

  const save = async () => {
    const v = parseFloat(draft);
    if (Number.isNaN(v) || v < 0) {
      toast("Cap must be a non-negative number", "error");
      return;
    }
    setBusy(true);
    try {
      // Fetch the full namespace row so we don't clobber other fields
      // on the ReplacingMergeTree write. namespace.register replaces
      // EVERY field in the row on insert; passing partial = wipes.
      const ns = await getNamespace({ org, namespace });
      if (!ns) {
        toast("Namespace not found", "error");
        return;
      }
      await reregisterNamespace({ org, namespace }, ns, { daily_cost_cap_usd: v });
      // The sidebar's badge and this page read budget status through one shared cache.
      invalidateShared(`budget:${org}/${namespace}`);
      toast(v > 0 ? `Cap set to $${v.toFixed(4)}/day` : "Cap removed (inherits global default)", "ok");
      setEditing(false);
      onSaved();
    } catch (e) {
      toast(`Couldn't save cap: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="inline-flex items-center gap-1.5 shrink-0">
      <span className="text-2xs text-[var(--color-text-muted)]">$</span>
      <input
        aria-label="Daily LLM budget cap in dollars"
        type="number"
        step="0.01"
        min="0"
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void save();
          if (e.key === "Escape") setEditing(false);
        }}
        disabled={busy}
        className="w-24 px-2 py-1 text-xs tabular bg-[var(--color-bg)] border border-[var(--color-border)] rounded-md disabled:opacity-50"
      />
      <button
        onClick={save}
        disabled={busy}
        className="p-1 rounded-md hover:bg-[var(--color-surface-2)] text-[var(--color-success)] disabled:opacity-50"
        aria-label="Save"
      >
        <Check size={13} />
      </button>
      <button
        onClick={() => setEditing(false)}
        disabled={busy}
        className="p-1 rounded-md hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
        aria-label="Cancel"
      >
        <XIcon size={13} />
      </button>
    </div>
  );
}
