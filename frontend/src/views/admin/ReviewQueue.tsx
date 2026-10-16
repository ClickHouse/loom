import { useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import {
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
import { TabTrigger } from "../../components/ui";
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
        .catch(() => {});
      listAllowlist({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setProbedExceptions(r.rows || []);
        })
        .catch(() => {});
    },
    [identity.org, identity.namespace],
  );

  return (
    <PageContainer>
      <div className="flex items-baseline gap-3 mb-2">
        <h1 className="text-2xl inline-flex items-center gap-2">
          <Icon name="warning" size="md" color="var(--color-warn)" aria-hidden="true" /> Review queue
        </h1>
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <p className="text-sm text-text-muted mb-5 max-w-2xl">
        Memory disputes needing a human decision — your agent flip-flopped on a fact, or the extractor wants to promote
        one. Most users never see anything here; that's a feature.
      </p>
      <Tabs value={tab} onValueChange={setTab}>
        {(hasLearnings || hasExceptions) && (
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
        <Tabs.Content value="conflicts">
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

function ConflictsPanel() {
  const identity = useIdentity();
  const [rows, setRows] = useState<ConflictRow[] | null>(null);
  const [index, setIndex] = useState(0);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  // Bumped after a decision empties the queue: the refetch runs through the
  // namespace-keyed effect, so a reply that lands after a namespace switch is dropped.
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      listConflicts({ org: identity.org, namespace: identity.namespace }, { limit: 50, signal })
        .then((r) => {
          if (live()) {
            setRows(r.rows);
            setIndex(0);
            setReason("");
          }
        })
        .catch(() => {
          if (live()) setRows([]);
        });
    },
    [identity.org, identity.namespace, reload],
  );

  if (rows === null) return <Skeleton className="h-48 w-full" />;
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
      if (index + 1 < rows.length) setIndex(index + 1);
      else setReload((n) => n + 1);
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="text-sm text-text-muted tabular mb-3">
        Conflict {index + 1} of {rows.length} · resolve in order
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
    </div>
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

  useAbortableEffect(
    (signal, live) => {
      // The first "pending" view was already fetched by the page's probe.
      if (seeded.current && status === "pending") {
        seeded.current = false;
        return;
      }
      setRows(null);
      listLearnings({ org: identity.org, namespace: identity.namespace }, status, 50, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
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
      {rows === null && <Skeleton className="h-40 w-full" />}
      {rows !== null && rows.length === 0 && (
        <EmptyState
          icon="sparkle"
          title={`No ${status} learnings`}
          description="Learnings are facts noticed across several agents in your organisation and proposed as shared knowledge for everyone. Solo and development setups usually have none."
        />
      )}
      {rows !== null && rows.length > 0 && (
        <div className="space-y-2">
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

  useAbortableEffect(
    (signal, live) => {
      if (seeded.current) {
        seeded.current = false;
        return;
      }
      setRows(null);
      listAllowlist({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch(() => {
          if (live()) setRows([]);
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

  if (rows === null) return <Skeleton className="h-40 w-full" />;
  if (rows.length === 0) {
    return (
      <EmptyState
        icon="line-in-circle"
        title="No allowed exceptions"
        description="Pairs you explicitly said may coexist. Fills only as a side-effect of clicking 'Allow both' when resolving a conflict — there's nothing for you to add proactively."
      />
    );
  }
  return (
    <div className="space-y-1.5">
      {rows.map((r) => (
        <Panel key={`${r.memory_key_a}-${r.memory_key_b}`} padding="sm" bodyClassName="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 text-sm font-mono">
              <span className="text-text truncate">{r.memory_key_a}</span>
              <span className="text-text-muted">↔</span>
              <span className="text-text truncate">{r.memory_key_b}</span>
            </div>
            <div className="text-xs text-text-muted tabular mt-1">
              {r.allowed_by} · {relativeTime(r.allowed_at)}
              {r.reason && <> · "{r.reason}"</>}
            </div>
          </div>
          <IconButton
            icon="cross"
            type="danger"
            size="sm"
            label={`Remove exception ${r.memory_key_a} ↔ ${r.memory_key_b}`}
            onClick={() => remove(r.memory_key_a, r.memory_key_b)}
          />
        </Panel>
      ))}
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
