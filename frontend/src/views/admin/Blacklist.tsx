import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import { listBlacklist, addBlacklist, removeBlacklist, type BlacklistRow, type BlacklistScope } from "../../api";
import { sweepBlacklistPattern } from "../../api/guardrails";
import { hasOrgAdminClaim } from "../../lib/role";
import { confirmAction } from "../../components/ConfirmDialog";
import { OrgGuardrails } from "./OrgGuardrails";
import { RowMenu } from "../../components/ui/RowMenu";
import { useStore, useIdentity } from "../../lib/state";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { toast } from "../../components/Toast";
import { humanizeError, parseChTime, relativeTime } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { canCall } from "../../lib/canCall";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";
import { oneOf } from "../../lib/oneOf";
import { IconButton } from "../../components/IconButton";
import { Chip, ListTime, LoadingSkeleton, PageHeader } from "../../components/ui";
import { ListState } from "../../components/ui/ListState";

/** What a visitor who cannot list the patterns reads instead of a request: the token's
 * role here is unknown, so the panel says who manages the
 *  patterns and asks the server nothing. */
export const PRIVACY_FILTERS_MANAGED_BY_ADMIN = "Refusal patterns are managed by an admin of this space.";
/** A viewer who may list but not add. */
export const PRIVACY_FILTERS_READ_ONLY =
  "You can see the patterns here but not change them — a writer adds, an admin removes.";

const SCOPES: readonly BlacklistScope[] = ["namespace", "org", "global"];
const SCOPE_LABELS: Record<BlacklistScope, string> = { namespace: "Namespace", org: "Org", global: "Global" };

/**
 * Page wrapper for /admin/guardrails. Renders
 * the inline form below with full page chrome.
 */
export function Blacklist() {
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const orgAdmin = useStore((s) => hasOrgAdminClaim(s.whoami));
  return (
    <PageContainer>
      <PageHeader title="Guardrails">
        <ScopeBadge kind="namespace" value={namespace} />
      </PageHeader>
      <PrivacyFilters />
      {orgAdmin && <OrgGuardrails />}
    </PageContainer>
  );
}

/**
 * Inline form — manages "patterns Loom should never store." Lives in
 * two places now: this admin page (for operator-level org/global
 * patterns) and Setup.tsx's "Privacy filters" section (for end users
 * who want to keep specific values out of their memories).
 */
/** A pattern offered as a one-click prefill of the add form. */
export interface PrivacyFilterStarter {
  pattern: string;
  label: string;
}

/** `compact` (the Settings → Memory tab): one add row, a divided list, one muted line
 *  when empty — no framed form, no icon block. The default shape is the Guardrails admin
 *  page's. */
export function PrivacyFilters({
  compact = false,
  starters = [],
}: {
  compact?: boolean;
  starters?: PrivacyFilterStarter[];
} = {}) {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const [rows, setRows] = useState<BlacklistRow[] | null>(null);
  const [pattern, setPattern] = useState("");
  const [scope, setScope] = useState<BlacklistScope>("namespace");
  const [reason, setReason] = useState("");

  // `blacklist.list` is a namespace-scoped VIEWER op (server/memory/api/ops_blacklist.py); a
  // token with no standing on this namespace would draw AUTH_INSUFFICIENT on every Setup
  // load. The read runs only when the role here is known; the add form and the remove
  // buttons appear only for a role that reaches `blacklist.add` (WRITER) /
  // `blacklist.remove` (ADMIN).
  const role = useNamespaceRole(identity.namespace);
  const canList = canCall("blacklist.list", role, whoami);
  const canAdd = canCall("blacklist.add", role, whoami);
  const canRemove = canCall("blacklist.remove", role, whoami);
  // Who may sweep which pattern: `handle_blacklist_sweep`.
  const canSweepNamespace = canCall("blacklist.sweep", role, whoami);
  const orgAdmin = hasOrgAdminClaim(whoami);
  const canSweep = (r: BlacklistRow) => (r.scope === "namespace" ? canSweepNamespace : orgAdmin);

  // A reload after an add or a remove keeps the patterns on screen, marked busy. It runs
  // through the read effect, so an answer for a namespace the page has left is dropped.
  const [refreshing, setRefreshing] = useState(false);
  // A failed read is said as a failure, with Retry: never as "no patterns".
  const [loadError, setLoadError] = useState<unknown>(null);
  const [reads, setReads] = useState(0);
  const reload = () => setReads((n) => n + 1);
  // Another namespace's patterns are not this one's: start from the skeleton, during
  // render, so the old namespace's rows (and their Remove buttons) never paint under it.
  const scopeKey = `${identity.org}/${identity.namespace}`;
  const [rowsFor, setRowsFor] = useState(scopeKey);
  if (rowsFor !== scopeKey) {
    setRowsFor(scopeKey);
    setRows(null);
    setLoadError(null);
  }
  useAbortableEffect(
    (signal, live) => {
      if (!canList) return;
      setRefreshing(true);
      setLoadError(null);
      listBlacklist({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (!live()) return;
          setLoadError(e);
          // Not an empty list: Retry shows the skeleton until the read answers.
          setRows(null);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [scopeKey, canList, reads],
  );

  const add = async () => {
    if (!pattern.trim()) return;
    try {
      await addBlacklist({ org: identity.org, namespace: identity.namespace }, pattern.trim(), {
        pattern_type: "substring",
        scope,
        reason: reason.trim() || undefined,
      });
      setPattern("");
      setReason("");
      toast("Pattern added", "ok");
      reload();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  const remove = async (pattern_id: string) => {
    try {
      await removeBlacklist({ org: identity.org }, pattern_id);
      reload();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  // A pattern stops new writes; a sweep tombstones what it already matches.
  const [sweeping, setSweeping] = useState<string | null>(null);
  const sweep = async (r: BlacklistRow) => {
    const inNamespace = r.scope === "namespace";
    const ok = await confirmAction({
      title: `Delete the stored memories "${r.pattern}" matches ${inNamespace ? "in this namespace" : `across ${identity.org}`}?`,
      message: inNamespace
        ? "They can be restored for 14 days from Memories → Recently deleted."
        : "They can be restored for 14 days from Memories → Recently deleted in each namespace they were in.",
      confirmLabel: "Delete matches",
      danger: true,
    });
    if (!ok) return;
    setSweeping(r.pattern_id);
    try {
      const s = await sweepBlacklistPattern(identity, r.pattern_id);
      if (s.error) throw new Error(s.error);
      if (s.matched === 0) {
        toast("No stored memory matches this pattern", "info");
        return;
      }
      const more = s.truncated ? "; more remain, sweep again" : "";
      toast(`Deleted ${s.tombstoned} of ${s.matched} matching memories${more}`, "ok");
    } catch (e) {
      toast(`Sweep failed: ${humanizeError(e)}`, "error");
    } finally {
      setSweeping(null);
    }
  };

  if (!canList) {
    return (
      <p role="note" className="text-sm text-text-muted max-w-2xl leading-relaxed">
        {PRIVACY_FILTERS_MANAGED_BY_ADMIN}
      </p>
    );
  }

  return (
    <>
      {!canAdd && (
        <p role="note" className="text-xs text-text-muted mb-3 max-w-2xl leading-relaxed">
          {PRIVACY_FILTERS_READ_ONLY}
        </p>
      )}
      {canAdd && (
        <div className={compact ? "mb-3" : "rounded-xl border border-border bg-surface p-4 mb-4"}>
          {!compact && <div className="text-xs font-medium text-text-muted mb-2">Add pattern</div>}
          {!compact && (
            <div className="text-xs text-text-muted mb-3">
              Values matching any pattern below will be silently dropped before they're written. Use it for secrets,
              PII, or anything you don't want the agent to remember.
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-0 flex-1 basis-full sm:basis-0">
              <TextField
                aria-label="Substring to block"
                value={pattern}
                onChange={setPattern}
                placeholder="Substring to block…"
              />
            </div>
            <div className="w-36">
              <Select
                triggerProps={{ "aria-label": "Blocklist scope" }}
                value={scope}
                onSelect={(value) => {
                  const v = oneOf(SCOPES, value);
                  if (v !== null) setScope(v);
                }}
              >
                {SCOPES.map((s) => (
                  <Select.Item key={s} value={s}>
                    {SCOPE_LABELS[s]}
                  </Select.Item>
                ))}
              </Select>
            </div>
            <Button type="primary" htmlType="button" iconLeft="plus" onClick={add}>
              Add
            </Button>
          </div>
          <div className="mt-2">
            <TextField aria-label="Reason" value={reason} onChange={setReason} placeholder="Optional reason…" />
          </div>
          {starters.length > 0 && (
            // A starter fills the form; it never copies to the clipboard.
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-text-muted">
              <span>Suggested:</span>
              {starters.map((st) => (
                <Chip
                  key={st.pattern}
                  label={st.label}
                  tip={st.pattern}
                  onClick={() => {
                    setPattern(st.pattern);
                    setReason(st.label);
                  }}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Mounted before the read answers, so the count is announced when it lands. */}
      {!compact && (
        <p className="mb-2 text-xs text-text-muted tabular" aria-live="polite">
          {rows !== null && loadError === null ? `${rows.length} ${rows.length === 1 ? "pattern" : "patterns"}` : ""}
        </p>
      )}
      {rows === null && loadError === null && (
        <LoadingSkeleton label="Loading privacy filters">
          <Skeleton className={compact ? "h-10 w-full" : "h-40 w-full"} />
        </LoadingSkeleton>
      )}
      {loadError !== null && <ListState kind="error" things="privacy filters" error={loadError} onRetry={reload} />}
      {rows !== null && rows.length === 0 && compact && (
        <p className="text-xs text-text-muted" role="status">
          {canAdd ? "No patterns yet." : "Nothing is refused here yet."}
        </p>
      )}
      {rows !== null && rows.length === 0 && !compact && (
        <EmptyState
          icon="line-in-circle"
          title="No patterns"
          description={
            canAdd
              ? "Add a pattern above to prevent matching memories from being written."
              : "Nothing is refused here yet."
          }
        />
      )}
      {rows !== null && rows.length > 0 && (
        <>
          <PrivacyFiltersHitSummary rows={rows} />
          <div
            className={compact ? "rounded-lg border border-border bg-surface divide-y divide-border" : "space-y-1.5"}
            aria-busy={refreshing || undefined}
          >
            {rows.map((r) => (
              <div
                key={r.pattern_id}
                className={
                  compact
                    ? "flex items-center gap-3 px-3 py-2"
                    : "flex items-center gap-3 p-3 rounded-xl border border-border bg-surface"
                }
              >
                <div className="flex-1 min-w-0">
                  <div className="font-mono text-sm text-text truncate">{r.pattern}</div>
                  <div className="text-xs text-text-muted tabular flex items-center gap-x-1 flex-wrap">
                    <span>{r.pattern_type}</span>
                    <span>·</span>
                    <span>{r.scope}</span>
                    <span>·</span>
                    <span>
                      added <ListTime time={r.created_at} quiet />
                    </span>
                    <span>·</span>
                    <PrivacyFilterHits row={r} />
                    {r.reason && (
                      <>
                        <span>·</span>
                        <span className="italic min-w-0 break-words">“{r.reason}”</span>
                      </>
                    )}
                  </div>
                </div>
                {canSweep(r) && (
                  <RowMenu
                    name={r.pattern}
                    items={[
                      {
                        label: sweeping === r.pattern_id ? "Deleting matches…" : "Delete matches",
                        danger: true,
                        disabled: sweeping !== null,
                        onSelect: () => void sweep(r),
                      },
                    ]}
                  />
                )}
                {canRemove && (
                  <IconButton
                    icon="cross"
                    type="danger"
                    size="sm"
                    label={`Remove pattern ${r.pattern}`}
                    tooltip="Remove"
                    onClick={() => remove(r.pattern_id)}
                  />
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}

/**
 * "Are my filters even doing anything?" — one-line summary above the
 * pattern list when at least one filter has matched something. Backed
 * by the lifetime hit_count plus last_hit_at.
 *
 * Two messages:
 *   - some pattern matched in the last 7d → emerald, count + last-seen
 *   - all hits are stale (>7d) → muted, "rules haven't matched lately"
 * No row at all → the filters exist but never caught anything; silent
 * is fine here, the per-row "never matched" pill already conveys that.
 */
function PrivacyFiltersHitSummary({ rows }: { rows: BlacklistRow[] }) {
  const totalHits = rows.reduce((acc, r) => acc + (r.hit_count || 0), 0);
  if (totalHits === 0) return null;

  const now = Date.now();
  const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000;
  let mostRecent = 0;
  let activePatterns = 0;
  for (const r of rows) {
    const hit = parseChTime(r.last_hit_at);
    if (!hit) continue;
    const t = hit.getTime();
    if (t > mostRecent) mostRecent = t;
    if (now - t <= SEVEN_DAYS) activePatterns += 1;
  }

  if (activePatterns > 0 && mostRecent > 0) {
    return (
      <div className="mb-3 text-xs text-success leading-relaxed">
        Your filters caught <span className="font-medium">{totalHits}</span> write{totalHits === 1 ? "" : "s"} total —
        <span className="font-medium"> {activePatterns}</span> pattern{activePatterns === 1 ? "" : "s"} still matching
        this week (last hit {relativeTime(new Date(mostRecent).toISOString())}).
      </div>
    );
  }

  return (
    <div className="mb-3 text-xs text-text-muted leading-relaxed">
      <span className="font-medium text-text">{totalHits}</span> lifetime match
      {totalHits === 1 ? "" : "es"}, none in the last 7 days — these rules haven't caught anything lately. Safe to keep
      as defense-in-depth, or prune any that are clearly stale.
    </div>
  );
}

/** A pattern's hits: how many writes it refused, and the last one. */
export function PrivacyFilterHits({ row }: { row: BlacklistRow }) {
  if (row.hit_count === 0) {
    return <span className="text-text-muted">never matched</span>;
  }
  const lastHitText = row.last_hit_at ? (
    <>
      last <ListTime time={row.last_hit_at} quiet />
    </>
  ) : (
    <>last hit unknown</>
  );
  return (
    <>
      <span className="text-text font-medium">{row.hit_count}</span>
      <span>&nbsp;match{row.hit_count === 1 ? "" : "es"}</span>
      <span>·</span>
      <span>{lastHitText}</span>
    </>
  );
}
