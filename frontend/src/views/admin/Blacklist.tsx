import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import { listBlacklist, addBlacklist, removeBlacklist, type BlacklistRow, type BlacklistScope } from "../../api";
import { useStore, useIdentity } from "../../lib/state";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { toast } from "../../components/Toast";
import { relativeTime, humanizeError } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { canCall } from "../../lib/canCall";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";
import { oneOf } from "../../lib/oneOf";
import { IconButton } from "../../components/IconButton";
import { Chip } from "../../components/ui";

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
  return (
    <PageContainer>
      <h1 className="text-2xl inline-flex items-center gap-2 mb-1">
        <Icon name="line-in-circle" size="md" aria-hidden="true" /> Guardrails
      </h1>
      <div className="mb-6">
        <ScopeBadge kind="namespace" value={namespace} />
      </div>
      <PrivacyFilters />
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

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    if (!canList) return;
    setRows(null);
    listBlacklist({ org: identity.org, namespace: identity.namespace }, { signal })
      .then((r) => {
        if (live()) setRows(r.rows || []);
      })
      .catch(() => {
        if (live()) setRows([]);
      });
  };
  useAbortableEffect(load, [identity.org, identity.namespace, canList]);

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
      load();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  const remove = async (pattern_id: string) => {
    try {
      await removeBlacklist({ org: identity.org }, pattern_id);
      load();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
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
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-2xs text-text-muted">
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

      {rows === null && <Skeleton className={compact ? "h-10 w-full" : "h-40 w-full"} />}
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
                  <div className="text-2xs text-text-muted tabular flex items-center gap-x-1 flex-wrap">
                    <span>{r.pattern_type}</span>
                    <span>·</span>
                    <span>{r.scope}</span>
                    <span>·</span>
                    <span>added {relativeTime(r.created_at)}</span>
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
    if (!r.last_hit_at) continue;
    const t = Date.parse(r.last_hit_at);
    if (!Number.isFinite(t)) continue;
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

function PrivacyFilterHits({ row }: { row: BlacklistRow }) {
  if (row.hit_count === 0) {
    return <span className="text-text-subtle">never matched</span>;
  }
  const lastHitText = row.last_hit_at ? <>last {relativeTime(row.last_hit_at)}</> : <>last hit unknown</>;
  return (
    <>
      <span className="text-text font-medium">{row.hit_count}</span>
      <span>&nbsp;match{row.hit_count === 1 ? "" : "es"}</span>
      <span>·</span>
      <span>{lastHitText}</span>
    </>
  );
}
