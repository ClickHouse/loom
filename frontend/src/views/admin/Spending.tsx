import { useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { NumberField } from "@clickhouse/click-ui/NumberField";
import { ProgressBar } from "@clickhouse/click-ui/ProgressBar";
import { Select } from "@clickhouse/click-ui/Select";
import { Table } from "@clickhouse/click-ui/Table";
import { Title } from "@clickhouse/click-ui/Title";
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
import { hasOrgAdminClaim } from "../../lib/role";
import { canUpdateNamespace } from "../../lib/namespaceUpdate";
import { invalidateShared, sharedBudgetStatus } from "../../lib/sharedReads";
import { Skeleton } from "../../components/Skeleton";
import { ListState } from "../../components/ui/ListState";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { PHONE_QUERY, useMedia } from "../../lib/useMedia";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast, toastError } from "../../components/Toast";
import { IconButton } from "../../components/IconButton";
import { Tip } from "../../components/ui/Tooltip";
import { displayAgent, SENTINEL_AGENTS } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { PageContainer } from "../../layout/PageContainer";
import { AgentSpend } from "./AgentSpend";
import { Alert, LoadingSkeleton, PageHeader, Stat } from "../../components/ui";

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
  // The breakdown the rows on screen were read by: another one's rows are not drawn under
  // the toolbar, which stays (with its focused Select) while the new pivot reads.
  const [rowsBy, setRowsBy] = useState<Breakdown | null>(null);
  // Default OFF: read ops (memory.list, audit.list, conflicts.list) dominate
  // the row count but have $0 cost and no tokens, drowning the few rows that
  // actually matter for spend. Toggle exposes them for performance debugging.
  const [showAllOps, setShowAllOps] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Bumped by Retry after a failed read.
  const [reload, setReload] = useState(0);
  const phone = useMedia(PHONE_QUERY);

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
      getNamespace({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((n) => {
          if (live()) setNsRow(n);
        })
        .catch(() => {});
    },
    [identity.org, identity.namespace],
  );
  const canEditCap = canUpdateNamespace(whoami, myNamespaces, identity, nsRow);

  // Rows re-fetch when the pivot changes; the budget bars don't. Another namespace starts
  // from the skeleton, during render, so its usage never paints under the new badge.
  const rowsKey = `${identity.org}/${identity.namespace}`;
  const [rowsFor, setRowsFor] = useState(rowsKey);
  if (rowsFor !== rowsKey) {
    setRowsFor(rowsKey);
    setRows(null);
    setError(null);
    // The budget and its editor are the namespace's too; the org's budget is not.
    setBudget(null);
    setNsRow(null);
  }
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      listCostRollup(
        { org: identity.org, namespace: identity.namespace },
        { days: 7, ...BREAKDOWN_QUERY[breakdown], signal },
      )
        .then((r) => {
          if (live()) {
            setRows(r.rows || []);
            setRowsBy(breakdown);
          }
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [identity.org, identity.namespace, breakdown, reload],
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
  const agentColumn = { header: "Agent", cell: agentCell };
  const dimColumns: { header: string; cell: (r: CostRow) => ReactNode }[] =
    breakdown === "operation"
      ? [agentColumn, { header: "Op", cell: (r) => <span className="font-mono text-xs">{r.operation}</span> }]
      : breakdown === "agent"
        ? [agentColumn]
        : breakdown === "user"
          ? [
              {
                header: "User",
                cell: (r) =>
                  r.user_id && r.user_id !== "-" ? (
                    <span className="font-mono text-xs">{r.user_id}</span>
                  ) : (
                    <span className="text-text-muted">— unattributed</span>
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

  const allOps = (
    <Checkbox
      label={
        hiddenCount > 0 && !showAllOps
          ? `Show all operations (${hiddenCount} zero-cost rows hidden)`
          : "Show all operations"
      }
      checked={showAllOps}
      onCheckedChange={(c) => setShowAllOps(c === true)}
    />
  );

  // The share of the daily cap spent, in percent; past 100 once over the cap.
  const pct = Math.round((budget?.pct_of_cap ?? 0) * 100);

  return (
    <PageContainer>
      <PageHeader title="Usage">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>

      {orgBudget && orgBudget.spent_24h_usd > 0 && (
        <div className="mb-4 rounded-xl border border-border bg-surface p-4 flex items-baseline justify-between gap-3">
          <div>
            <div className="text-xs text-text-muted mb-1">Org-wide 24h LLM spend</div>
            <div className="text-xl font-semibold tabular">${orgBudget.spent_24h_usd.toFixed(4)}</div>
          </div>
          <div className="text-right text-xs text-text-muted">
            across {orgBudget.namespaces_with_spend ?? 0} namespace
            {(orgBudget.namespaces_with_spend ?? 0) === 1 ? "" : "s"} in{" "}
            <span className="font-mono">{identity.org}</span>
          </div>
        </div>
      )}

      {budget && budget.cap_source !== "uncapped" && (
        <div className="mb-5 rounded-xl border border-border bg-surface p-4">
          {budget.over_cap && (
            <Alert
              className="mb-3"
              state="warning"
              size="small"
              showIcon
              text="Daily LLM cap reached — extraction paused for this namespace"
            />
          )}
          <div className="flex items-baseline justify-between gap-3 mb-2">
            <div className="text-sm font-medium">Daily LLM budget</div>
            <div className="text-xs tabular text-text-muted">
              ${budget.spent_24h_usd.toFixed(4)} / ${budget.cap_usd.toFixed(2)} ({budget.cap_source})
            </div>
          </div>
          {/* Click UI's ProgressBar sets no role or value, so the meter's ARIA is passed in.
              Past the cap the bar stays full and the value text says by how much. */}
          <ProgressBar
            type="small"
            progress={Math.min(100, pct)}
            role="progressbar"
            aria-label="Daily LLM budget used"
            aria-valuenow={Math.min(100, pct)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={`${pct}% — $${budget.spent_24h_usd.toFixed(4)} of the $${budget.cap_usd.toFixed(2)} cap`}
          />
          <div className="mt-2 flex items-baseline justify-between gap-3">
            <div className="text-xs text-text-muted leading-relaxed">
              Rolling 24h sum of upstream LLM cost on this namespace. When the cap is hit,{" "}
              <code className="code-inline text-xs">entity_extractor</code> skip writes here until usage drops below the
              cap.
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

      {!!error && <ListState kind="error" things="usage" error={error} onRetry={() => setReload((n) => n + 1)} />}
      {!error && rows === null && (
        <LoadingSkeleton label="Loading usage">
          <Skeleton className="h-40 w-full" />
        </LoadingSkeleton>
      )}
      {rows !== null && rows.length === 0 && (
        <ListState
          kind="empty"
          icon="dollar"
          title="No operations recorded"
          description="No API calls against this namespace in the last 7 days. Activity (reads + LLM-touching writes) shows up here once your agent runs."
        />
      )}
      {rows !== null && rows.length > 0 && totalCost === 0 && totalTokens === 0 && totalEmbedTokens === 0 && (
        <Alert
          className="mb-5"
          state="neutral"
          showIcon
          text={
            <>
              <strong>$0 in this window.</strong> The {totalOps.toLocaleString()} operations counted are all read-only
              API calls (memory listing, audit reads, etc.) that don't hit a paid provider. Cost shows up here once your
              agent writes memories (embedder fires), or once the entity_extractor worker is enabled.
            </>
          }
        />
      )}

      {rows !== null && rows.length > 0 && totalEmbedTokens > 0 && totalCost === 0 && (
        <Alert
          className="mb-5"
          state="warning"
          showIcon
          text={
            <>
              <strong>{fmt(totalEmbedTokens)} embed tokens but $0 cost.</strong> Your embedder fired but its
              upstream-cost field isn't yet attributed in the rollup — known gap, applies to every Loom deployment
              today. Once the attribution wiring lands, the number under Total cost will include embedder usage.
              (LLM-extraction worker costs are tracked correctly.)
            </>
          }
        />
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
            <div className="rounded-2xl border border-border bg-surface p-5 mb-6">
              <Title type="h2" size="sm" className="mb-4">
                Cost by day
              </Title>
              <div className="flex items-end gap-2 h-[140px]">
                {days.map(([day, cost]) => (
                  <div key={day} className="flex-1 flex flex-col items-center gap-1.5">
                    <div className="text-xs tabular text-text-muted">${cost.toFixed(4)}</div>
                    <div
                      className="w-full bg-accent rounded-t"
                      style={{ height: `${(cost / maxCost) * 100}%`, minHeight: "2px" }}
                    />
                    <div className="text-xs tabular text-text-muted">{day === "—" ? "—" : day.slice(5)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <ListToolbar
            label="Usage breakdown"
            search={false}
            count={
              rowsBy === breakdown
                ? `${visibleRows.length.toLocaleString()} ${visibleRows.length === 1 ? "row" : "rows"}`
                : undefined
            }
          >
            <Select
              value={breakdown}
              onSelect={(value) => {
                if (isBreakdown(value)) setBreakdown(value);
              }}
              triggerProps={{ "aria-label": "Break down by" }}
              className={phone ? "min-w-0 flex-1" : "w-60 shrink-0"}
            >
              <Select.Item value="operation">By operation</Select.Item>
              <Select.Item value="agent">By agent</Select.Item>
              <Select.Item value="user">By user</Select.Item>
              {isOrgAdmin && <Select.Item value="namespace">By namespace (org-wide)</Select.Item>}
            </Select>
            {!phone && allOps}
          </ListToolbar>
          {/* A phone's toolbar has room for the breakdown only; the switch sits under it. */}
          {phone && <div className="-mt-1 mb-3">{allOps}</div>}
          {rowsBy !== breakdown && !error && (
            <LoadingSkeleton label="Loading usage">
              <Skeleton className="h-40 w-full" />
            </LoadingSkeleton>
          )}
          {rowsBy === breakdown && visibleRows.length === 0 && (
            <ListState
              kind="filtered"
              // The filter in force is the hidden no-cost operations: Clear filters shows them.
              things="operations"
              among="this window"
              onClear={() => setShowAllOps(true)}
            />
          )}
          {rowsBy === breakdown && visibleRows.length > 0 && (
            <Table
              aria-label={`Usage by day and ${breakdown}`}
              size="sm"
              headers={[
                { label: "Day", width: "110px" },
                ...dimColumns.map((c) => ({ label: c.header })),
                { label: "Ops", width: "70px" },
                { label: "Err", width: "60px" },
                { label: "Tokens" },
                { label: "p95 ms", width: "80px" },
                { label: "$", width: "90px" },
              ]}
              rows={visibleRows.map((r, i) => ({
                id: i,
                items: [
                  { label: <span className="font-mono text-xs tabular text-text-muted">{r.day || "—"}</span> },
                  ...dimColumns.map((c) => ({ label: c.cell(r) })),
                  { label: <span className="tabular">{fmt(r.ops)}</span> },
                  {
                    label: (
                      <span className={`tabular ${r.errors > 0 ? "text-danger" : "text-text-muted"}`}>
                        {r.errors || ""}
                      </span>
                    ),
                  },
                  {
                    label: (
                      <span className="tabular text-xs">
                        {fmt(r.tokens)}
                        {r.embed_tokens > 0 && <span className="text-text-muted"> + {fmt(r.embed_tokens)} embed</span>}
                      </span>
                    ),
                  },
                  { label: <span className="tabular text-xs text-text-muted">{Math.round(r.latency_p95_ms)}</span> },
                  {
                    label:
                      r.cost_usd > 0 ? (
                        <span className="tabular font-mono">${r.cost_usd.toFixed(4)}</span>
                      ) : (
                        <span className="text-text-muted">—</span>
                      ),
                  },
                ],
              }))}
            />
          )}
        </>
      )}
      {/* Keyed so one namespace's spend never paints under another's badge. */}
      <AgentSpend key={rowsKey} />
    </PageContainer>
  );
}

function agentCell(r: CostRow): ReactNode {
  if (!r.agent) return <span className="text-text-muted">—</span>;
  return SENTINEL_AGENTS.has(r.agent) ? (
    <span className="italic text-text-muted">{displayAgent(r.agent)}</span>
  ) : (
    displayAgent(r.agent)
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
      <Tip content="Edit this namespace's daily LLM budget">
        <Button
          type="empty"
          htmlType="button"
          iconLeft="pencil"
          className="shrink-0"
          onClick={() => {
            setDraft(String(currentCap || 0));
            setEditing(true);
          }}
        >
          Edit cap
        </Button>
      </Tip>
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
      toastError("Couldn't save cap", e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="inline-flex items-center gap-1.5 shrink-0">
      <div className="w-32">
        <NumberField
          aria-label="Daily LLM budget cap in dollars"
          startContent="$"
          step="0.01"
          min="0"
          autoFocus
          value={draft}
          onChange={(value) => setDraft(value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
            if (e.key === "Escape") setEditing(false);
          }}
          disabled={busy}
        />
      </div>
      <IconButton label="Save" icon="check" size="sm" onClick={save} disabled={busy} />
      <IconButton label="Cancel" icon="cross" size="sm" onClick={() => setEditing(false)} disabled={busy} />
    </div>
  );
}
