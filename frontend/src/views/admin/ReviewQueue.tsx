import { useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Table } from "@clickhouse/click-ui/Table";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import {
  countConflicts,
  listConflicts,
  resolveConflict,
  listAllowlist,
  removeAllowlist,
  listLearnings,
  reviewLearning,
  type ConflictRow,
  type AllowlistRow,
  type ResolveDecision,
  type LearningEntry,
  type LearningStatus,
} from "../../api";
import { useIdentity } from "../../lib/state";
import { IconButton } from "../../components/IconButton";
import { Panel } from "../../components/ui/Panel";
import { LoadingSkeleton, PageHeader, TabTrigger } from "../../components/ui";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { relativeTime, formatValue, truncate, humanizeError } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { PageContainer } from "../../layout/PageContainer";

/**
 * Review queue — single operator surface for "memory decisions needing
 * a human."
 *
 * One page for Conflicts and Learnings: both are "operator looks at a list and
 * approves/rejects." Same gesture, same audience, same cadence. Two empty-ish peer tabs
 * forced the operator into the same workflow twice; one tabbed page does it once.
 *
 * Sub-tabs:
 *   - Conflicts:   pairs of memories that disagree on a value
 *   - Learnings:   facts the extractor proposed for promotion
 *   - Exceptions:  allowlist of pairs we've explicitly said may coexist
 */
// Radix's tab panel attributes, cleared while Conflicts is the only panel.
const LONE_PANEL = { role: undefined, "aria-labelledby": undefined, tabIndex: undefined };

export function ReviewQueue() {
  const identity = useIdentity();
  // Probe whether the two niche sub-tabs (Learnings, Exceptions) have
  // any content before showing them. Learnings is empty by default in
  // single-user / dev mode. Exceptions only populates as a side-effect
  // of resolving conflicts. Hiding empty sub-tabs until they have
  // content reduces noise; they reappear automatically.
  // The probe's answers seed the panels, so opening a tab does not ask the same question
  // a second time.
  const [probedLearnings, setProbedLearnings] = useState<LearningEntry[] | null>(null);
  const [probedExceptions, setProbedExceptions] = useState<AllowlistRow[] | null>(null);
  const hasLearnings = (probedLearnings?.length ?? 0) > 0;
  const hasExceptions = (probedExceptions?.length ?? 0) > 0;
  const tabbed = hasLearnings || hasExceptions;
  // A namespace switch starts from Conflicts: the previous namespace's probes (and a tab
  // they revealed) must not outlive it, or a namespace with neither sub-tab strands the
  // viewer on a hidden panel.
  const [tab, setTab] = useState("conflicts");
  useAbortableEffect(
    (signal, live) => {
      setProbedLearnings(null);
      setProbedExceptions(null);
      setTab("conflicts");
      listLearnings({ org: identity.org, namespace: identity.namespace }, "pending", 50, { signal })
        .then((r) => {
          if (live()) setProbedLearnings(r.rows || []);
        })
        .catch(() => {
          if (live()) setProbedLearnings([]);
        });
      listAllowlist({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setProbedExceptions(r.rows || []);
        })
        .catch(() => {
          if (live()) setProbedExceptions([]);
        });
    },
    [identity.org, identity.namespace],
  );

  return (
    <PageContainer>
      <PageHeader title="Review queue">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-5 max-w-2xl">
        Memory disputes needing a human decision — your agent flip-flopped on a fact, or the extractor wants to promote
        one. Most users never see anything here; that's a feature.
      </p>
      {/* Conflicts mounts (and reads) once, at once; the sub-tabs join it when a probe finds
          rows. Until then the lone panel is the page itself, not a tab panel without tabs. */}
      <Tabs value={tab} onValueChange={setTab}>
        {tabbed && (
          <Tabs.TriggersList className="mb-5" aria-label="Review queue">
            <TabTrigger value="conflicts" icon="warning">
              Conflicts
            </TabTrigger>
            {hasLearnings && (
              <TabTrigger value="learnings" icon="sparkle">
                Learnings
              </TabTrigger>
            )}
            {hasExceptions && (
              <TabTrigger value="exceptions" icon="line-in-circle">
                Exceptions
              </TabTrigger>
            )}
          </Tabs.TriggersList>
        )}
        <Tabs.Content value="conflicts" {...(!tabbed && LONE_PANEL)}>
          <ConflictsPanel />
        </Tabs.Content>
        {hasLearnings && (
          <Tabs.Content value="learnings">
            <LearningsPanel initialPending={probedLearnings} />
          </Tabs.Content>
        )}
        {hasExceptions && (
          <Tabs.Content value="exceptions">
            <AllowlistPanel initial={probedExceptions} />
          </Tabs.Content>
        )}
      </Tabs>
    </PageContainer>
  );
}

// ─── Conflicts panel ───────────────────────────────────────────────────

const CONFLICT_PAGE = 50;

function ConflictsPanel() {
  const identity = useIdentity();
  const [rows, setRows] = useState<ConflictRow[] | null>(null);
  // The next older page (conflicts.list is keyed by version), and how many the namespace
  // holds in all.
  const [next, setNext] = useState<number | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [index, setIndex] = useState(0);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  // Bumped after a decision empties the queue: the refetch runs through the
  // namespace-keyed effect, so a reply that lands after a namespace switch is dropped.
  const [reload, setReload] = useState(0);
  // The reload keeps what is on screen until it answers; another namespace starts over.
  const rowsFor = useRef("");

  useAbortableEffect(
    (signal, live) => {
      if (rowsFor.current !== `${identity.org}/${identity.namespace}`) {
        rowsFor.current = `${identity.org}/${identity.namespace}`;
        setRows(null);
      }
      listConflicts({ org: identity.org, namespace: identity.namespace }, { limit: CONFLICT_PAGE, signal })
        .then((r) => {
          if (live()) {
            setRows(r.rows);
            setNext(r.next_before_version);
            setIndex(0);
            setReason("");
          }
        })
        .catch(() => {
          if (live()) setRows([]);
        });
      countConflicts({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((n) => {
          if (live()) setTotal(n);
        })
        .catch(() => {
          if (live()) setTotal(null);
        });
    },
    [identity.org, identity.namespace, reload],
  );

  // The next older page joins the queue; the decision in progress stays where it is.
  const loadMore = async () => {
    if (next === null || !rows) return;
    const key = rowsFor.current;
    setLoadingMore(true);
    try {
      const r = await listConflicts(
        { org: identity.org, namespace: identity.namespace },
        { limit: CONFLICT_PAGE, before_version: next },
      );
      if (rowsFor.current !== key) return;
      // Against the rows as they are now: a conflict decided while this read was out stays gone.
      setRows((prev) => {
        if (!prev) return prev;
        const have = new Set(prev.map((c) => c.memory_id));
        return [...prev, ...r.rows.filter((c) => !have.has(c.memory_id))];
      });
      setNext(r.next_before_version);
    } catch (e) {
      toast(`Couldn't load more conflicts: ${humanizeError(e)}`, "error");
    } finally {
      setLoadingMore(false);
    }
  };

  if (rows === null) return <QueueTable rows={[]} current={null} onPick={() => {}} loading />;
  if (rows.length === 0) {
    return (
      <EmptyState
        icon="secure"
        title="No conflicts"
        description="A conflict appears when two memories with the same key disagree — your agent saying 'user lives in X' once and 'user lives in Y' later. None right now means your agent's been consistent."
      />
    );
  }

  const current = rows[index];
  if (!current) return <EmptyState icon="secure" title="All clear" description="You worked through the queue." />;

  const peerId = current.conflict_with?.[0];
  // The server's count is cached for a minute; it is never less than the rows on hand.
  const held = total === null ? null : Math.max(total, rows.length);

  const decide = async (decision: ResolveDecision) => {
    setBusy(true);
    try {
      await resolveConflict(
        { org: identity.org, namespace: identity.namespace },
        current.memory_id,
        decision,
        reason.trim() ? { reason: reason.trim() } : {},
      );
      toast("Resolved", "ok");
      setReason("");
      // The decided conflict leaves the queue; the next one takes its place.
      const rest = rows.filter((c) => c.memory_id !== current.memory_id);
      if (held !== null) setTotal(held - 1);
      if (rest.length > 0) {
        setRows(rest);
        setIndex(Math.min(index, rest.length - 1));
      } else setReload((n) => n + 1);
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-3 flex items-center gap-3 text-sm text-text-muted tabular">
        <span>
          Conflict {index + 1} of {rows.length} · resolve in order
        </span>
        <span className="ml-auto text-xs">
          {held === null ? `${rows.length} loaded` : `${rows.length} of ${held}`}
          <span className="sr-only"> conflicts loaded</span>
        </span>
      </div>
      <Panel>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
          <Side
            label="A · this memory"
            memKey={current.memory_key}
            value={current.value}
            version={current.version}
            pinned={current.pinned}
            createdAt={current.created_at}
          />
          <Side label="B · conflicting peer" memKey={current.memory_key} value={null} version={null} peerId={peerId} />
        </div>
        <TextAreaField
          label="Reason for the decision"
          value={reason}
          onChange={(value) => setReason(value)}
          placeholder="Optional — recorded in the audit trail"
          rows={3}
        />
        <div className="mt-4 flex items-center justify-end gap-2 flex-wrap">
          <Button
            type="empty"
            htmlType="button"
            iconLeft="slide-in"
            onClick={() => setIndex((i) => Math.min(rows.length - 1, i + 1))}
            disabled={busy || index >= rows.length - 1}
          >
            Skip
          </Button>
          <Button type="secondary" htmlType="button" onClick={() => decide("allow")} disabled={busy}>
            Allow both
          </Button>
          <Button
            type="secondary"
            htmlType="button"
            iconLeft="cross"
            onClick={() => decide("contract")}
            disabled={busy}
          >
            Drop A
          </Button>
          <Button type="primary" htmlType="button" iconLeft="check" onClick={() => decide("revise")} disabled={busy}>
            Keep A
          </Button>
        </div>
      </Panel>

      <h2 className="mt-6 mb-2 text-sm font-semibold">Queue</h2>
      <QueueTable rows={rows} current={current.memory_id} onPick={setIndex} busy={loadingMore} />
      {next !== null && (
        <Button
          type="secondary"
          htmlType="button"
          fillWidth
          className="mt-3"
          loading={loadingMore}
          disabled={loadingMore}
          onClick={() => void loadMore()}
        >
          Load more
        </Button>
      )}
    </div>
  );
}

/** The conflicts on hand, one row each; the one being decided is the active row, and a
 *  key opens that conflict. The first read is Click UI's own loading table. */
function QueueTable({
  rows,
  current,
  onPick,
  loading = false,
  busy = false,
}: {
  rows: ConflictRow[];
  current: string | null;
  onPick: (index: number) => void;
  loading?: boolean;
  busy?: boolean;
}) {
  return (
    <Table
      aria-label="Conflicts"
      aria-busy={loading || busy || undefined}
      loading={loading}
      size="sm"
      headers={[
        { label: "Key" },
        { label: "Version", width: "90px" },
        { label: "Pinned", width: "90px" },
        { label: "Written", width: "120px" },
      ]}
      rows={rows.map((c, i) => ({
        id: c.memory_id,
        isActive: c.memory_id === current,
        items: [
          {
            label: (
              <button
                type="button"
                className="min-w-0 max-w-full truncate rounded text-left font-mono text-xs"
                aria-current={c.memory_id === current || undefined}
                onClick={() => onPick(i)}
              >
                {c.memory_key}
              </button>
            ),
          },
          { label: <span className="tabular">v{c.version}</span> },
          { label: c.pinned ? "Pinned" : <span className="text-text-muted">—</span> },
          { label: <span className="tabular text-text-muted">{relativeTime(c.created_at)}</span> },
        ],
      }))}
    />
  );
}

// ─── Learnings panel ───────────────────────────────────────────────────

const LEARNING_STATUSES: readonly LearningStatus[] = ["pending", "promoted", "rejected"];
const STATUS_LABEL: Record<LearningStatus, string> = { pending: "Pending", promoted: "Promoted", rejected: "Rejected" };

function LearningsPanel({ initialPending }: { initialPending?: LearningEntry[] | null }) {
  const identity = useIdentity();
  const [status, setStatus] = useState<LearningStatus>("pending");
  const [rows, setRows] = useState<LearningEntry[] | null>(initialPending ?? null);
  const [reason, setReason] = useState<Record<string, string>>({});
  const seeded = useRef(initialPending != null);
  const [reload, setReload] = useState(0);
  // A reload after a decision keeps the list on screen, marked busy; another namespace or
  // status is another list, so it starts from the skeleton.
  const [refreshing, setRefreshing] = useState(false);
  const rowsFor = useRef("");

  useAbortableEffect(
    (signal, live) => {
      const key = `${identity.org}/${identity.namespace}/${status}`;
      // The first "pending" view was already fetched by the page's probe.
      if (seeded.current && status === "pending") {
        seeded.current = false;
        rowsFor.current = key;
        return;
      }
      if (rowsFor.current !== key) {
        rowsFor.current = key;
        setRows(null);
      }
      setRefreshing(true);
      listLearnings({ org: identity.org, namespace: identity.namespace }, status, 50, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [identity.org, identity.namespace, status, reload],
  );

  const decide = async (id: string, decision: "promote" | "reject") => {
    try {
      await reviewLearning({ org: identity.org, namespace: identity.namespace }, id, decision, reason[id] || "");
      toast(decision === "promote" ? "Promoted" : "Rejected", "ok");
      setReload((n) => n + 1);
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  return (
    <div>
      <ButtonGroup
        aria-label="Learning status"
        className="mb-4"
        options={LEARNING_STATUSES.map((s) => ({ value: s, label: STATUS_LABEL[s] }))}
        selected={status}
        onClick={(value) => {
          const next = LEARNING_STATUSES.find((s) => s === value);
          if (next) setStatus(next);
        }}
      />
      {rows === null && (
        <LoadingSkeleton label="Loading learnings">
          <Skeleton className="h-40 w-full" />
        </LoadingSkeleton>
      )}
      {rows !== null && rows.length === 0 && (
        <EmptyState
          icon="sparkle"
          title={`No ${status} learnings`}
          description="Learnings are facts noticed across several agents in your organisation and proposed as shared knowledge for everyone. Solo and development setups usually have none."
        />
      )}
      {rows !== null && rows.length > 0 && (
        <div className="space-y-2" aria-busy={refreshing || undefined}>
          {rows.map((r) => (
            <Panel key={r.candidate_id}>
              <div className="flex items-center justify-between gap-3 mb-2">
                <span className="font-mono text-sm text-text-muted">{r.memory_key}</span>
                <span className="text-xs text-text-muted tabular">
                  {r.supporter_count} supporters · conf {(r.confidence_avg ?? 0).toFixed(2)} ·{" "}
                  {relativeTime(r.proposed_at)}
                </span>
              </div>
              <div className="text-sm text-text mb-2">{truncate(formatValue(r.proposed_value), 240)}</div>
              {r.rationale && <div className="text-sm text-text-muted mb-2 italic">{r.rationale}</div>}
              {status === "pending" && (
                <>
                  <TextAreaField
                    label="Reason for the decision"
                    value={reason[r.candidate_id] || ""}
                    onChange={(value) => setReason((m) => ({ ...m, [r.candidate_id]: value }))}
                    placeholder="Optional — recorded in the audit trail"
                    rows={2}
                  />
                  <div className="mt-2 flex items-center justify-end gap-2">
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="cross"
                      onClick={() => decide(r.candidate_id, "reject")}
                    >
                      Reject
                    </Button>
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="check"
                      onClick={() => decide(r.candidate_id, "promote")}
                    >
                      Promote
                    </Button>
                  </div>
                </>
              )}
              {status !== "pending" && (
                <div className="mt-1 text-sm text-text-muted">
                  {status === "promoted"
                    ? `Promoted ${r.decided_at ? relativeTime(r.decided_at) : ""}`
                    : `Rejected ${r.decided_at ? relativeTime(r.decided_at) : ""}`}
                  {r.decided_by && <> by {r.decided_by}</>}
                </div>
              )}
            </Panel>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Exceptions (allowlist) panel ──────────────────────────────────────

function AllowlistPanel({ initial }: { initial?: AllowlistRow[] | null }) {
  const identity = useIdentity();
  const [rows, setRows] = useState<AllowlistRow[] | null>(initial ?? null);
  const seeded = useRef(initial != null);
  const [reload, setReload] = useState(0);
  // A reload after a removal keeps the list on screen, marked busy; another namespace
  // starts from the skeleton.
  const [refreshing, setRefreshing] = useState(false);
  const rowsFor = useRef("");

  useAbortableEffect(
    (signal, live) => {
      const key = `${identity.org}/${identity.namespace}`;
      if (seeded.current) {
        seeded.current = false;
        rowsFor.current = key;
        return;
      }
      if (rowsFor.current !== key) {
        rowsFor.current = key;
        setRows(null);
      }
      setRefreshing(true);
      listAllowlist({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [identity.org, identity.namespace, reload],
  );

  const remove = async (a: string, b: string) => {
    if (
      !(await confirmAction({
        title: "Remove this allowlist entry?",
        message:
          "The pair will conflict again on the next write — you'll have to resolve them again from the review queue.",
        confirmLabel: "Remove",
        danger: true,
      }))
    )
      return;
    try {
      await removeAllowlist({ org: identity.org, namespace: identity.namespace }, a, b);
      toast("Removed", "ok");
      setReload((n) => n + 1);
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  if (rows !== null && rows.length === 0) {
    return (
      <EmptyState
        icon="line-in-circle"
        title="No allowed exceptions"
        description="Pairs you explicitly said may coexist. Fills only as a side-effect of clicking 'Allow both' when resolving a conflict — there's nothing for you to add proactively."
      />
    );
  }
  // The first read is Click UI's own loading table; a reload keeps the rows, marked busy.
  return (
    <div data-testid="allowlist" aria-busy={refreshing || undefined}>
      <Table
        aria-label="Allowed exceptions"
        aria-busy={rows === null || undefined}
        loading={rows === null}
        size="sm"
        headers={[
          { label: "Pair" },
          { label: "Allowed by", width: "180px" },
          { label: "When", width: "110px" },
          { label: "Reason" },
          { label: <span className="sr-only">Remove</span>, width: "56px" },
        ]}
        rows={(rows ?? []).map((r) => ({
          id: `${r.memory_key_a}-${r.memory_key_b}`,
          items: [
            {
              label: (
                <span className="flex min-w-0 items-center gap-2 font-mono text-xs">
                  <span className="truncate">{r.memory_key_a}</span>
                  <Icon name="resize-arrows-horizontal" size="xs" aria-hidden="true" className="shrink-0" />
                  <span className="sr-only"> and </span>
                  <span className="truncate">{r.memory_key_b}</span>
                </span>
              ),
            },
            { label: <span className="text-xs">{r.allowed_by}</span> },
            { label: <span className="tabular text-xs text-text-muted">{relativeTime(r.allowed_at)}</span> },
            {
              label: r.reason ? (
                <span className="text-xs">{r.reason}</span>
              ) : (
                <span className="text-text-muted">—</span>
              ),
            },
            {
              label: (
                <IconButton
                  icon="cross"
                  type="danger"
                  size="sm"
                  label={`Remove exception ${r.memory_key_a} and ${r.memory_key_b}`}
                  onClick={() => remove(r.memory_key_a, r.memory_key_b)}
                />
              ),
            },
          ],
        }))}
      />
    </div>
  );
}

function Side({
  label,
  memKey,
  value,
  version,
  pinned,
  createdAt,
  peerId,
}: {
  label: string;
  memKey: string;
  value: unknown;
  version: number | null;
  pinned?: boolean;
  createdAt?: string;
  peerId?: string;
}) {
  return (
    <Panel tone="surface-2" padding="sm">
      <div className="text-xs text-text-muted mb-1.5">{label}</div>
      <div className="font-mono text-xs text-text mb-2 truncate">{memKey}</div>
      <div className="text-sm text-text mb-2 leading-snug min-h-10">
        {value == null ? (
          <span className="text-text-muted">
            Peer: <span className="font-mono">{peerId?.slice(0, 12)}…</span>
          </span>
        ) : (
          truncate(formatValue(value), 200)
        )}
      </div>
      <div className="flex items-center gap-2 text-xs text-text-muted tabular">
        {version != null && <span>v{version}</span>}
        {pinned && <Badge text="Pinned" state="warning" size="sm" />}
        {createdAt && <span>{relativeTime(createdAt)}</span>}
      </div>
    </Panel>
  );
}
