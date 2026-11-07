import { memo, startTransition, useDeferredValue, useEffect, useMemo, useRef, useState, type Ref } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Table, type TableRowType } from "@clickhouse/click-ui/Table";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { autoResolveReasonText, listAuditLog, listErasures, type AuditEntry, type ErasureListRow } from "../../api";
import { useStore, useIdentity } from "../../lib/state";
import { hasOrgAdminClaim } from "../../lib/role";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { toastError } from "../../components/Toast";
import { IconButton } from "../../components/IconButton";
import { ScopeBadge } from "../../components/ScopeBadge";
import { TableSkeleton } from "../../components/Skeleton";
import { useAbortableEffect } from "../../lib/hooks";
import { isRecord } from "../../lib/guards";
import { PageContainer } from "../../layout/PageContainer";
import { PHONE_QUERY, useMedia } from "../../lib/useMedia";
import { PageHeader, TabTrigger, TipTarget } from "../../components/ui";

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
 * The raw `subject` blob is the event's subject: for an audited op without
 * its own `audit_subject`, its input with text values hashed
 * (`audit.input_subject`) — useful for debugging but unreadable in a feed.
 * This formatter pulls 1-3 load-bearing fields per event type and
 * synthesizes a sentence.
 *
 * Unknown event types fall through to a generic, readable fallback.
 */
function summarize(event_type: string, subject: Record<string, unknown>): string {
  // A hashed text value is `{sha256, chars}`: it reads as no value, never "[object Object]".
  const s = (k: string) => {
    const v = subject[k];
    return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : "";
  };

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
    case "memory.auto_supersede": {
      // A retirement the re-read after the write could not confirm is not counted as done.
      const entries = Array.isArray(subject["retired"]) ? (subject["retired"] as unknown[]) : [];
      const unsure = entries.filter((e) => isRecord(e) && e["unconfirmed"] === "true").length;
      const done = Number(subject["count"] ?? 0) - unsure;
      const many = (k: number) => (k === 1 ? "1 memory" : `${k} memories`);
      const text = autoResolveReasonText(s("reason"));
      const why = text ? ` (${text.charAt(0).toLowerCase() + text.slice(1)})` : "";
      if (done <= 0) return `Loom may have retired ${many(unsure)} automatically${why}`;
      const more = unsure ? `, and may have retired ${unsure} more` : "";
      return `Loom retired ${many(done)} automatically${why}${more}`;
    }
    case "memory.unsupersede":
      return `Restored "${s("memory_key")}" after it was retired`;
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

type ActivityTab = "events" | "erasures";
// One name per tab, for its trigger and the document title.
const ACTIVITY_TABS: Record<ActivityTab, string> = { events: "Events", erasures: "Erasures" };

export function Audit() {
  const identity = useIdentity();
  const [tab, setTab] = useState<ActivityTab>("events");
  const whoami = useStore((s) => s.whoami);
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  // Bumped by Retry after a failed first read.
  const [reload, setReload] = useState(0);
  // The next, older page's cursor; null once the last page is shown.
  const [older, setOlder] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [showSystem, setShowSystem] = useState(false);
  const phone = useMedia(PHONE_QUERY);
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
  const scope = isPlatformAdmin ? { org: identity.org } : { org: identity.org, namespace: identity.namespace };
  // The window the first page read; older pages keep to it.
  const since = useRef("");
  // Bumped by every first read: an older page asked for under another scope is dropped.
  const scopeGen = useRef(0);
  const olderCtl = useRef<AbortController | null>(null);
  // Where focus goes once an older page lands (the first new row's index).
  const focusAfterLoad = useRef<number | null>(null);
  const eventsTable = useRef<HTMLTableElement>(null);
  useEffect(() => () => olderCtl.current?.abort(), []);
  useAbortableEffect(
    (signal, live) => {
      scopeGen.current += 1;
      olderCtl.current?.abort();
      focusAfterLoad.current = null;
      setLoadingOlder(false);
      setRows(null);
      setError(null);
      setOlder(null);
      since.current = new Date(Date.now() - AUDIT_WINDOW_MS).toISOString();
      listAuditLog(scope, { limit: 100, since: since.current, signal, fields: ACTIVITY_FIELDS })
        .then((r) => {
          if (!live()) return;
          setRows(r.rows || []);
          setOlder(r.next_cursor ?? null);
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [identity.org, identity.namespace, isPlatformAdmin, reload],
  );

  // 100 more, older than the last row shown; the rows already shown stay.
  const loadOlder = async () => {
    if (!older) return;
    const gen = scopeGen.current;
    const ctl = new AbortController();
    olderCtl.current = ctl;
    focusAfterLoad.current = visibleRows.length;
    setLoadingOlder(true);
    try {
      const r = await listAuditLog(scope, {
        limit: 100,
        since: since.current,
        cursor: older,
        fields: ACTIVITY_FIELDS,
        signal: ctl.signal,
      });
      if (gen !== scopeGen.current) return;
      setRows((prev) => [...(prev ?? []), ...(r.rows || [])]);
      setOlder(r.next_cursor ?? null);
    } catch (e) {
      if (gen === scopeGen.current) toastError("Loading older events failed", e);
    } finally {
      if (gen === scopeGen.current) setLoadingOlder(false);
    }
  };

  // The button keeps focus while the page reads; the last page removes it, so focus, if it
  // fell to the page, goes to the first row that page added — or, when it added none that
  // are shown (all system events), the last row shown, else the table. Only the render
  // where a load ends moves focus: another render in between (the deferred rows'
  // transition) must not spend it.
  const wasLoadingOlder = useRef(false);
  useEffect(() => {
    const ended = wasLoadingOlder.current && !loadingOlder;
    wasLoadingOlder.current = loadingOlder;
    const at = focusAfterLoad.current;
    if (!ended || at === null) return;
    focusAfterLoad.current = null;
    if (older || document.activeElement !== document.body) return;
    const shown = eventsTable.current?.querySelectorAll<HTMLElement>("tbody tr") ?? [];
    const target = shown[at] ?? shown[shown.length - 1] ?? eventsTable.current;
    if (target) {
      target.tabIndex = -1;
      target.focus();
    }
  });

  const me = whoami?.subject || "";
  // The rows filter by the text a render behind the box, so a keystroke paints the box
  // before the table renders again (memoised, it renders only when its rows change).
  const listQuery = useDeferredValue(filterQuery).trim();
  const q = listQuery.toLowerCase();
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
  const visibleRows = useMemo(
    () =>
      (rows || []).filter((r) => {
        if (!showSystem && SYSTEM_EVENT_TYPES.has(r.event_type)) return false;
        if (onlyMe && r.actor !== me) return false;
        if (q) {
          const haystack = `${r.actor} ${r.event_type}`.toLowerCase();
          if (!haystack.includes(q)) return false;
        }
        return true;
      }),
    [rows, showSystem, onlyMe, me, q],
  );
  const tableRows = useMemo(
    () => (firstScreenOnly ? visibleRows.slice(0, FIRST_SCREEN_ROWS) : visibleRows),
    [firstScreenOnly, visibleRows],
  );
  const hiddenSystemCount =
    (rows || []).length - (rows || []).filter((r) => showSystem || !SYSTEM_EVENT_TYPES.has(r.event_type)).length;

  // The filters in force sit in the bar with the search. The count of hidden system events
  // rides beside its checkbox, whose label stays the same.
  const filters =
    !error && rows !== null && rows.length > 0 ? (
      <>
        {me && <Checkbox label="Only my actions" checked={onlyMe} onCheckedChange={(c) => setOnlyMe(c === true)} />}
        <span className="flex shrink-0 items-center gap-1.5">
          <Checkbox
            label="Include system events"
            checked={showSystem}
            onCheckedChange={(c) => setShowSystem(c === true)}
          />
          {hiddenSystemCount > 0 && !showSystem && (
            <span className="text-xs text-text-muted tabular">{hiddenSystemCount} hidden</span>
          )}
        </span>
      </>
    ) : null;

  return (
    <PageContainer>
      <div className="mb-6">
        <PageHeader title="Activity" tab={ACTIVITY_TABS[tab]}>
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <div className="text-sm text-text-muted max-w-[72ch]">
          Who did what and when: every delete, promotion, conflict resolution, erasure and redaction.{" "}
          <span className="inline-flex align-middle">
            <IconButton
              icon="info-in-circle"
              size="xs"
              label="About activity"
              tooltip="Each row is content-hashed, and hourly checkpoints chain each closed window's row count and digest to the previous one, so edits and deletions are detectable."
            />
          </span>
        </div>
      </div>

      <Tabs value={tab} onValueChange={(v) => setTab(v as ActivityTab)}>
        <Tabs.TriggersList className="mb-5" aria-label="Activity">
          <TabTrigger value="events" icon="history">
            {ACTIVITY_TABS.events}
          </TabTrigger>
          <TabTrigger value="erasures" icon="trash">
            {ACTIVITY_TABS.erasures}
          </TabTrigger>
        </Tabs.TriggersList>
        <Tabs.Content value="events">
          <ListToolbar
            label="Filter audit events"
            count={
              // With older events still to load, the count is of those loaded: "100+ events".
              !error && rows !== null
                ? `${visibleRows.length}${older ? "+" : ""} ${visibleRows.length === 1 && !older ? "event" : "events"}`
                : undefined
            }
          >
            <div className="min-w-0 flex-1">
              <SearchField
                value={filterQuery}
                onChange={(value) => setFilterQuery(value)}
                placeholder="Filter by actor or event type — e.g. alice@, redact, promote…"
                aria-label="Filter audit events"
              />
            </div>
            {!phone && filters}
          </ListToolbar>
          {/* On a phone the filters take a row of their own under the bar. */}
          {phone && filters && <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">{filters}</div>}

          {!!error && (
            <ListState kind="error" things="audit events" error={error} onRetry={() => setReload((n) => n + 1)} />
          )}
          {!error && rows === null && <EventsTable rows={[]} loading />}
          {rows !== null && rows.length === 0 && (
            <ListState
              kind="empty"
              icon="history"
              title="No audit events"
              description="Memory deletes, promotions, conflict resolutions, erasures and redactions all land here once activity begins. Each row is content-hashed, and hourly checkpoints chain each closed window's row count and digest to the previous one."
            />
          )}
          {rows !== null && rows.length > 0 && visibleRows.length === 0 && (
            <ListState
              kind="filtered"
              // The filter covers the events loaded so far; older ones may still match.
              things={older ? "loaded events" : "events"}
              filter={listQuery || undefined}
              onClear={() => {
                setFilterQuery("");
                setOnlyMe(false);
                setShowSystem(true);
              }}
            />
          )}
          {rows !== null && visibleRows.length > 0 && <EventsTable tableRef={eventsTable} rows={tableRows} />}
          {rows !== null && older && (
            <div className="mt-4 flex justify-center">
              <BusyButton type="secondary" htmlType="button" busy={loadingOlder} onClick={loadOlder}>
                Load more
              </BusyButton>
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
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setRows(null);
      listErasures({ org }, { limit: 100, signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [org, reload],
  );

  if (error) {
    return <ListState kind="error" things="erasures" error={error} onRetry={() => setReload((n) => n + 1)} />;
  }
  if (rows !== null && rows.length === 0) {
    return (
      <ListState
        kind="empty"
        icon="trash"
        title="No erasures yet"
        description="When anyone issues an erasure.request (right-to-be-forgotten or operator-driven cleanup), it lands here with status + provenance. Tombstoned individual memories are recoverable from Memories → Recently deleted; this surface is the org-wide saga history."
      />
    );
  }

  // The first read is the table's header over a skeleton.
  return (
    <Table
      aria-label="Erasures"
      aria-busy={rows === null || undefined}
      noDataMessage={rows === null ? <TableSkeleton label="Loading erasures" /> : undefined}
      size="sm"
      headers={[
        { label: "Scope" },
        { label: "Requester" },
        { label: "Status" },
        { label: "Steps", width: "80px" },
        { label: "Requested", width: "120px" },
        { label: "Completed", width: "120px" },
      ]}
      rows={(rows ?? []).map((r) => ({
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
          { label: <ListTime time={r.requested_at} quiet /> },
          { label: r.completed_at ? <ListTime time={r.completed_at} quiet /> : "—" },
        ],
      }))}
    />
  );
}

const ERASURE_STATE: Record<string, BadgeState> = { completed: "success", failed: "danger" };

const EVENT_HEADERS = [{ label: "Event" }, { label: "By", width: "220px" }, { label: "When", width: "120px" }];

/** The events, one row each: what happened, who did it and when. The first read is the
 *  table's header over a skeleton. */
const EventsTable = memo(function EventsTable({
  rows,
  loading = false,
  tableRef,
}: {
  rows: AuditEntry[];
  loading?: boolean;
  tableRef?: Ref<HTMLTableElement>;
}) {
  return (
    <Table
      ref={tableRef}
      aria-label="Audit events"
      aria-busy={loading || undefined}
      noDataMessage={loading ? <TableSkeleton label="Loading audit events" /> : undefined}
      size="sm"
      headers={EVENT_HEADERS}
      rows={rows.map(eventRow)}
    />
  );
});

function eventRow(row: AuditEntry, i: number): TableRowType {
  return {
    id: `${row.ts}-${i}`,
    items: [
      { label: <AuditEvent row={row} />, overflowMode: "wrap" },
      {
        // The column says this is the ACTOR. Subjects and namespaces share a naming space
        // (a user `san` and a namespace `san`), so a bare name on a namespace-scoped row
        // could read as "activity from namespace san"; Click UI prints the column's name
        // beside the cell on a phone too.
        label: (
          <span className="inline-flex min-w-0 items-center gap-1 text-xs text-text-muted">
            <Icon name="user" size="xs" aria-hidden="true" className="shrink-0" />
            <span className="truncate">{row.actor || "—"}</span>
          </span>
        ),
      },
      {
        label: <ListTime time={row.ts} quiet className="text-xs text-text-muted" />,
      },
    ],
  };
}

/** One event's summary and type, and its parsed subject behind "Details". */
function AuditEvent({ row }: { row: AuditEntry }) {
  const [expanded, setExpanded] = useState(false);
  // Parsed once, so the summary and the "Details" expansion read the same structured data.
  const parsed = subjectRecord(row.subject);
  const summary = summarize(row.event_type, parsed);
  const hasDetails = Object.keys(parsed).length > 0;

  return (
    <div className="min-w-0">
      <div className="text-sm text-text">{summary}</div>
      <div className="flex items-center gap-2 text-xs text-text-muted">
        <span className="font-mono">{row.event_type}</span>
        {hasDetails && (
          <Button
            type="empty"
            htmlType="button"
            iconLeft={expanded ? "chevron-down" : "chevron-right"}
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
          >
            Details
          </Button>
        )}
      </div>
      {expanded && hasDetails && (
        <pre className="mt-2 max-w-full p-2 text-xs font-mono text-text-muted bg-surface-2 rounded overflow-x-auto leading-relaxed">
          {JSON.stringify(parsed, null, 2)}
        </pre>
      )}
    </div>
  );
}
