import { startTransition, useEffect, useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Table } from "@clickhouse/click-ui/Table";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { listAuditLog, listErasures, type AuditEntry, type ErasureListRow } from "../../api";
import { useStore, useIdentity } from "../../lib/state";
import { hasOrgAdminClaim } from "../../lib/role";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { relativeTime } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { isRecord } from "../../lib/guards";
import { PageContainer } from "../../layout/PageContainer";
import { TabTrigger, TipTarget } from "../../components/ui";

// Events recorded by the control plane that don't represent a user
// or agent action — operator observability that pollutes the user-
// facing activity feed if shown by default. The toggle below exposes
// them for operators who want them.
const SYSTEM_EVENT_TYPES = new Set<string>(["budget.violated", "ratelimit.exceeded", "worker.failed"]);

/** A row's `subject` is JSON text (an already-parsed object is taken as is). Anything that
 *  is not an object once parsed (garbage, "null", an array) reads as an empty subject. */
function subjectRecord(raw: unknown): Record<string, unknown> {
  let v = raw;
  if (typeof raw === "string") {
    try {
      v = JSON.parse(raw);
    } catch {
      return {};
    }
  }
  return isRecord(v) ? v : {};
}

/**
 * Format the audit row into one human-readable line.
 *
 * The raw `subject` blob is the operation's full input payload — useful
 * for debugging but unreadable in a feed. This formatter pulls 1-3
 * load-bearing fields per event type and synthesizes a sentence.
 *
 * Unknown event types fall through to a generic, readable fallback.
 */
function summarize(event_type: string, subject: Record<string, unknown>): string {
  const s = (k: string) => (subject[k] != null ? String(subject[k]) : "");

  switch (event_type) {
    case "namespace.register": {
      const ns = s("namespace");
      const vis = s("visibility") || "private";
      return `Created namespace ${ns} (${vis})`;
    }
    case "namespace.join": {
      const ns = s("namespace");
      const role = s("role");
      const status = s("status");
      const requested = s("requested_role");
      if (status === "pending" && requested) {
        return `Requested ${requested} on ${ns} (joined as ${role || "viewer"})`;
      }
      return role ? `Joined ${ns} as ${role}` : `Joined ${ns}`;
    }
    case "namespace.leave":
      return `Left ${s("namespace")}`;
    case "namespace.cancel_request": {
      const ns = s("namespace");
      const cleared = s("cleared_role");
      return cleared ? `Withdrew ${cleared} request on ${ns}` : `Withdrew pending request on ${ns}`;
    }
    case "namespace.delete":
      return `Deleted namespace ${s("namespace")}`;
    case "namespace.snapshot":
      return `Snapshotted ${s("namespace")}`;
    case "namespace.restore":
      return `Restored ${s("target_namespace") || s("namespace")} from snapshot`;
    case "memory.set":
    case "memory.pin": {
      const key = s("memory_key");
      return key ? `Saved memory "${key}"` : `Saved a memory`;
    }
    case "memory.delete": {
      const key = s("memory_key");
      const reason = s("reason");
      return reason ? `Deleted "${key}" — ${reason}` : `Deleted memory "${key}"`;
    }
    case "memory.clear":
      return `Cleared memories matching filter`;
    case "memory.redact":
      return `Redacted memory "${s("memory_key")}"`;
    case "memory.bulk_set": {
      const n = subject["count"] ?? subject["items"];
      return n != null ? `Bulk-saved ${n} memories` : `Bulk-saved memories`;
    }
    case "memory.promote":
      return `Promoted "${s("memory_key")}" to ${s("target_status") || "active"}`;
    case "memory.demote":
      return `Demoted "${s("memory_key")}" back to private`;
    case "memory.cross_promote":
      return `Cross-promoted "${s("memory_key")}" to ${s("target_namespace") || s("namespace")}`;
    case "memory.outcome":
      return `Recorded outcome for "${s("memory_key")}": ${s("outcome") || s("result")}`;
    case "memory.touch":
      return `Touched "${s("memory_key")}"`;
    case "knowledge.upsert":
      return `Uploaded document ${s("source_uri") || s("title")}`;
    case "knowledge.add":
      return `Added document ${s("source_uri") || s("title")}`;
    case "knowledge.delete":
      return `Deleted document ${s("source_uri")}`;
    case "knowledge.bulk_delete": {
      const n =
        subject["count"] ??
        (Array.isArray(subject["source_uris"]) ? (subject["source_uris"] as unknown[]).length : null);
      return n != null ? `Bulk-deleted ${n} documents` : `Bulk-deleted documents`;
    }
    case "conflicts.resolve":
      return `Resolved conflict — kept ${s("kept_memory_key") || s("winner")}`;
    case "grants.create":
      return `Granted ${s("role")} on ${s("namespace")} to ${s("subject")}`;
    case "grants.revoke":
      return `Revoked access for ${s("subject")} on ${s("namespace")}`;
    case "grants.approve": {
      const decision = s("decision");
      const subject = s("subject");
      const role = s("role");
      const ns = s("namespace");
      if (decision === "approve") {
        return role ? `Approved ${subject} as ${role} on ${ns}` : `Approved ${subject} on ${ns}`;
      }
      return `Rejected ${subject}'s request on ${ns}`;
    }
    case "blacklist.upsert":
      return `Added blacklist pattern`;
    case "blacklist.delete":
      return `Removed blacklist pattern`;
    case "erasure.request":
      return `Erasure request for ${s("subject_id") || s("user_id")}`;
    case "claim_release":
      return `Released namespace ${s("namespace")} for re-claim`;
    case "auth.signup":
      return `Signed up as ${s("namespace")}`;
    case "auth.login":
      return `Signed in`;
    case "budget.violated":
      return `Budget violated: ${s("op_name")} took ${s("elapsed_ms")}ms`;
    case "ratelimit.exceeded":
      return `Rate limit hit on ${s("op_name") || "op"}`;
    case "worker.failed":
      return `Worker ${s("worker") || "unknown"} failed: ${s("error") || "see logs"}`;
    default: {
      const hint = s("memory_key") || s("namespace") || s("source_uri") || s("subject");
      return hint ? `${event_type} — ${hint}` : event_type;
    }
  }
}

/** The Activity page shows the last seven days. */
const AUDIT_WINDOW_MS = 7 * 24 * 3600_000;
// The four fields an Activity row reads; row_hash and request_id alone are ~15 kB of a
// 106 kB page nothing renders.
const ACTIVITY_FIELDS = ["ts", "event_type", "actor", "subject"];

const FIRST_SCREEN_ROWS = 12;

export function Audit() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [showSystem, setShowSystem] = useState(false);
  const [onlyMe, setOnlyMe] = useState(false);
  // Free-text filter — matches against actor (email / agent name)
  // and event_type. Done client-side over the loaded window since
  // the API's filtering is event_type-exact only and the typical
  // audit question is partial-match: "who touched anything for
  // alice@" / "any redaction events recently".
  const [filterQuery, setFilterQuery] = useState("");

  // Org-wide for platform/org admin (they want the full audit trail
  // without per-tab navigation); per-namespace for everyone else.
  // namespace=undefined → handler self-scopes via resolve_org_scope, which
  // widens to the whole org only at ORG_ADMIN.
  const isPlatformAdmin = hasOrgAdminClaim(whoami);
  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      listAuditLog(isPlatformAdmin ? { org: identity.org } : { org: identity.org, namespace: identity.namespace }, {
        limit: 100,
        since: new Date(Date.now() - AUDIT_WINDOW_MS).toISOString(),
        signal,
        fields: ACTIVITY_FIELDS,
      })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
        });
    },
    [identity.org, identity.namespace, isPlatformAdmin],
  );

  const me = whoami?.subject || "";
  const q = filterQuery.trim().toLowerCase();
  // First screen first, the rest in a transition (277 ms of long tasks at 4x for a
  // hundred rows).
  const [firstScreenOnly, setFirstScreenOnly] = useState(true);
  useEffect(() => {
    if (!firstScreenOnly || rows === null) return;
    const raf = requestAnimationFrame(() => {
      startTransition(() => setFirstScreenOnly(false));
    });
    return () => cancelAnimationFrame(raf);
  }, [firstScreenOnly, rows]);
  const visibleRows = (rows || []).filter((r) => {
    if (!showSystem && SYSTEM_EVENT_TYPES.has(r.event_type)) return false;
    if (onlyMe && r.actor !== me) return false;
    if (q) {
      const haystack = `${r.actor} ${r.event_type}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });
  const hiddenSystemCount =
    (rows || []).length - (rows || []).filter((r) => showSystem || !SYSTEM_EVENT_TYPES.has(r.event_type)).length;

  return (
    <PageContainer>
      <div className="mb-6">
        <div className="flex items-baseline gap-3 mb-1">
          <h1 className="text-2xl inline-flex items-center gap-2">
            <Icon name="history" size="md" aria-hidden="true" /> Activity
          </h1>
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </div>
        <p className="text-sm text-text-muted max-w-2xl leading-relaxed">
          Audit trail of every meaningful write — deletes, promotions, conflict resolutions, erasures, redactions. Each
          row is content-hashed, and hourly checkpoints chain each closed window's row count and digest to the previous
          one, so edits and deletions are detectable. Use this when you need to answer "who did what and when" or trace
          a memory's lifecycle.
        </p>
      </div>

      <Tabs defaultValue="events">
        <Tabs.TriggersList className="mb-5" aria-label="Activity">
          <TabTrigger value="events" icon="history">
            Events
          </TabTrigger>
          <TabTrigger value="erasures" icon="trash">
            Erasures
          </TabTrigger>
        </Tabs.TriggersList>
        <Tabs.Content value="events">
          {rows !== null && rows.length > 0 && (
            <>
              <div className="mb-3">
                <SearchField
                  value={filterQuery}
                  onChange={(value) => setFilterQuery(value)}
                  placeholder="Filter by actor or event type — e.g. alice@, redact, promote…"
                  aria-label="Filter audit events"
                />
              </div>
              <div className="mb-3 flex items-center justify-end gap-4">
                {me && (
                  <Checkbox label="Only my actions" checked={onlyMe} onCheckedChange={(c) => setOnlyMe(c === true)} />
                )}
                <Checkbox
                  label={
                    hiddenSystemCount > 0 && !showSystem
                      ? `Include system events (${hiddenSystemCount} hidden — budget / rate-limit / worker)`
                      : "Include system events"
                  }
                  checked={showSystem}
                  onCheckedChange={(c) => setShowSystem(c === true)}
                />
              </div>
            </>
          )}

          {rows === null && <Skeleton className="h-40 w-full" />}
          {rows !== null && rows.length === 0 && (
            <EmptyState
              icon="history"
              title="No audit events"
              description="Memory deletes, promotions, conflict resolutions, erasures and redactions all land here once activity begins. Each row is content-hashed, and hourly checkpoints chain each closed window's row count and digest to the previous one."
            />
          )}
          {rows !== null && rows.length > 0 && visibleRows.length === 0 && (
            <div className="p-6 text-center text-sm text-text-muted bg-surface rounded-xl border border-border">
              Only system events in this window. Toggle "Include system events" to see them.
            </div>
          )}
          {rows !== null && visibleRows.length > 0 && (
            <div className="space-y-1">
              {(firstScreenOnly ? visibleRows.slice(0, FIRST_SCREEN_ROWS) : visibleRows).map((r, i) => (
                <AuditRow key={`${r.ts}-${i}`} row={r} />
              ))}
            </div>
          )}
        </Tabs.Content>
        <Tabs.Content value="erasures">
          <ErasuresPanel org={identity.org} />
        </Tabs.Content>
      </Tabs>
    </PageContainer>
  );
}

/**
 * ErasuresPanel — compliance audit roll-up: every right-to-erasure
 * request issued in this org, with status, requester, scope, and
 * step-completed progress. Visibility scoped by erasure.list itself
 * (platform_admin sees all; everyone else sees namespace-scoped
 * erasures on their accessible namespaces).
 */
function ErasuresPanel({ org }: { org: string }) {
  const [rows, setRows] = useState<ErasureListRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      listErasures({ org }, { limit: 100, signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) setError(String(e?.message || e));
        });
    },
    [org],
  );

  if (error) {
    return <EmptyState icon="trash" title="Couldn't load" description={error} />;
  }
  if (rows === null) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-12" />
        <Skeleton className="h-12" />
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        icon="trash"
        title="No erasures yet"
        description="When anyone issues an erasure.request (right-to-be-forgotten or operator-driven cleanup), it lands here with status + provenance. Tombstoned individual memories are recoverable from Memories → Recently deleted; this surface is the org-wide saga history."
      />
    );
  }

  return (
    <Table
      size="sm"
      headers={[
        { label: "Scope" },
        { label: "Requester" },
        { label: "Status" },
        { label: "Steps", width: "80px" },
        { label: "Requested", width: "120px" },
        { label: "Completed", width: "120px" },
      ]}
      rows={rows.map((r) => ({
        id: r.erasure_id,
        title: r.failure_reason || r.erasure_id,
        items: [
          {
            label: (
              <span className="font-mono text-xs">
                <span className="text-text-muted">{r.scope_type}=</span>
                {r.scope_value}
              </span>
            ),
          },
          { label: <span className="font-mono text-xs">{r.requester}</span> },
          {
            label: (
              <span className="inline-flex items-center gap-2">
                <Badge text={r.status} state={ERASURE_STATE[r.status] ?? "neutral"} size="sm" />
                {r.reversals_count > 0 && (
                  <TipTarget
                    tip={`${r.reversals_count} memor${r.reversals_count === 1 ? "y" : "ies"} restored via memory.restore — the "completed" status overstates what's actually erased.`}
                  >
                    <Badge text={`${r.reversals_count} reversed`} state="warning" size="sm" />
                  </TipTarget>
                )}
              </span>
            ),
          },
          { label: <span className="tabular">{r.steps_completed_count}</span> },
          { label: <span className="tabular">{relativeTime(r.requested_at)}</span> },
          { label: <span className="tabular">{r.completed_at ? relativeTime(r.completed_at) : "—"}</span> },
        ],
      }))}
    />
  );
}

const ERASURE_STATE: Record<string, BadgeState> = { completed: "success", failed: "danger" };

function AuditRow({ row }: { row: AuditEntry }) {
  const [expanded, setExpanded] = useState(false);
  // Parsed once, so the summary and the "Details" expansion read the same structured data.
  const parsed = subjectRecord(row.subject);
  const summary = summarize(row.event_type, parsed);
  const hasDetails = Object.keys(parsed).length > 0;

  return (
    <div className="rounded-xl border border-border bg-surface">
      <div className="flex items-start gap-3 p-3">
        <div className="w-2 h-2 rounded-full bg-accent mt-1.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-text">{summary}</span>
            <span className="text-text-subtle ml-auto tabular text-2xs">{relativeTime(row.ts)}</span>
          </div>
          <div className="flex items-center gap-2 text-2xs text-text-muted mt-0.5">
            <span className="font-mono">{row.event_type}</span>
            <span>·</span>
            {/* "by" is load-bearing. This column is the ACTOR, but subjects and
                  namespaces share a naming space (a user `san` and a namespace
                  `san`), so a bare chip on a namespace-scoped row reads as
                  "activity from namespace san" — easily mistaken for a
                  cross-tenant leak when it is a correctly scoped
                  `namespace.join` by that user. The User icon alone does not
                  carry the distinction at 11px. */}
            <span className="inline-flex items-center gap-1">
              <Icon name="user" size="xs" aria-hidden="true" /> by {row.actor || "—"}
            </span>
            {hasDetails && (
              <Button
                type="empty"
                htmlType="button"
                className="ml-auto"
                iconLeft={expanded ? "chevron-down" : "chevron-right"}
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
              >
                {expanded ? "Hide" : "Details"}
              </Button>
            )}
          </div>
          {expanded && hasDetails && (
            <pre className="mt-2 p-2 text-2xs font-mono text-text-muted bg-surface-2 rounded overflow-x-auto leading-relaxed">
              {JSON.stringify(parsed, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
