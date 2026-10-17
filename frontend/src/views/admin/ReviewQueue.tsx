import { useEffect, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { Icon } from "@clickhouse/click-ui/Icon";
import {
  listConflicts,
  resolveConflict,
  listAllowlist,
  removeAllowlist,
  listLearnings,
  reviewLearning,
  listAutoResolved,
  listUnsuperseded,
  restoreMemory,
  getMemoryById,
  autoResolveReasonText,
  type MemoryById,
  type AutoResolvedRow,
  type ConflictRow,
  type AllowlistRow,
  type ResolveDecision,
  type LearningEntry,
  type LearningStatus,
} from "../../api";
import { useIdentity, useStore } from "../../lib/state";
import { IconButton } from "../../components/IconButton";
import { Panel } from "../../components/ui/Panel";
import { PageHeader, TabTrigger } from "../../components/ui";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { relativeTime, formatValue, truncate, humanizeError } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { canCall } from "../../lib/canCall";
import { sharedHasAutoResolved } from "../../lib/sharedReads";
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
 *   - Auto-resolved: memories Loom retired on its own (`memory.auto_supersede`), each
 *                    with an Undo (`memory.restore`)
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
  // The probe's yes/no, tagged with the namespace it read: the reset after a namespace
  // switch runs a tick after the first render of the new namespace, which must not show
  // the previous namespace's tab.
  const scopeKey = `${identity.org}/${identity.namespace}`;
  const [probedAuto, setProbedAuto] = useState<{ found: boolean; scope: string } | null>(null);
  const hasAuto = probedAuto?.scope === scopeKey && probedAuto.found;
  // The Auto-resolved panel reads its rows when the tab is first opened, then stays mounted.
  const [autoOpened, setAutoOpened] = useState(false);
  // audit.list is asked only by a role known to reach it.
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const auditReadable = canCall("audit.list", role, whoami);
  const tabbed = hasLearnings || hasExceptions || hasAuto;
  // A namespace switch starts from Conflicts: the previous namespace's probes (and a tab
  // they revealed) must not outlive it, or a namespace with neither sub-tab strands the
  // viewer on a hidden panel.
  const [tab, setTab] = useState("conflicts");
  useAbortableEffect(
    (signal, live) => {
      setProbedLearnings(null);
      setProbedExceptions(null);
      setTab("conflicts");
      setAutoOpened(false);
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
  useAbortableEffect(
    (_signal, live) => {
      setProbedAuto(null);
      if (!auditReadable) {
        // The Auto-resolved tab goes with the access: leave it for Conflicts.
        setTab((t) => (t === "auto" ? "conflicts" : t));
        setAutoOpened(false);
        return;
      }
      // The same question the sidebar asks, so the page and the sidebar share one read.
      sharedHasAutoResolved({ org: identity.org, namespace: identity.namespace })
        .then((found) => {
          if (live()) setProbedAuto({ found, scope: scopeKey });
        })
        .catch(() => {});
    },
    [identity.org, identity.namespace, auditReadable],
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
      <Tabs
        value={tab}
        onValueChange={(t) => {
          setTab(t);
          if (t === "auto") setAutoOpened(true);
        }}
      >
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
            {hasAuto && (
              <TabTrigger value="auto" icon="flash">
                Auto-resolved
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
        {hasAuto && autoOpened && (
          // Kept mounted once opened, so its loaded pages and Undo outcomes survive a tab
          // switch; a namespace switch drops it with the probe.
          <Tabs.Content value="auto" forceMount className="data-[state=inactive]:hidden">
            {/* Keyed by namespace: another namespace's rows are a new panel. */}
            <AutoResolvedPanel key={scopeKey} />
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
          <PeerSide rows={rows} peerId={peerId} />
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

// ─── Auto-resolved panel ───────────────────────────────────────────────

/** What became of a row after the Undo: restored, or `memory.restore` found it no longer
 *  retired behind that memory (restored, deleted or retired behind another since). */
type UndoOutcome = "restored" | "gone";

// One retirement record: a memory retired twice shows up once per record.
const entryKey = (r: AutoResolvedRow) => `${r.memory_id}@${r.ts}>${r.superseded_by}`;

// Audit times compared as instants: a whole second comes without a fraction
// ("…12:00:00Z"), and as text "." sorts before "Z".
const instant = (ts: string) => Date.parse(ts);

/** Newest restore per id across two reads. */
function mergeUndone(a: Map<string, string>, b: Map<string, string>): Map<string, string> {
  const out = new Map(a);
  for (const [id, ts] of b) {
    const prev = out.get(id);
    if (!prev || instant(ts) > instant(prev)) out.set(id, ts);
  }
  return out;
}

function AutoResolvedPanel() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const canRestore = canCall("memory.restore", role, whoami);
  const [rows, setRows] = useState<AutoResolvedRow[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  // The tab showed because a row exists, so a failed read must not look like an empty list.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  // Restores read so far (memory_id -> newest restore), and how far back that read
  // reaches: a row older than `undoneSince` is not known yet and offers no Undo.
  const [undone, setUndone] = useState<Map<string, string>>(new Map());
  const [undoneSince, setUndoneSince] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Map<string, UndoOutcome>>(new Map());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  // What the live region last said: one region for the panel, so rows changing are quiet.
  const [announcement, setAnnouncement] = useState("");
  // The panel is keyed by namespace, so a reply that lands after it goes is dropped.
  const mounted = useRef(true);
  const loadingRef = useRef(false);
  const oldest = (rows ?? []).reduce((m, r) => (m === "" || instant(r.ts) < instant(m) ? r.ts : m), "");

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useAbortableEffect(
    (signal, live) => {
      setLoadError(null);
      listAutoResolved({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (!live()) return;
          setRows(r.rows || []);
          setCursor(r.next_cursor);
        })
        .catch((e) => {
          if (live()) setLoadError(humanizeError(e));
        });
    },
    [identity.org, identity.namespace, reload],
  );

  useAbortableEffect(
    (signal, live) => {
      if (!oldest) return;
      // Re-reads from the oldest row on show up to now (audit.list has no upper bound);
      // capped at UNSUPERSEDED_MAX_PAGES, and what is known stays on screen meanwhile.
      listUnsuperseded({ org: identity.org, namespace: identity.namespace }, { since: oldest, signal })
        .then((m) => {
          if (!live()) return;
          setUndone((prev) => mergeUndone(prev, m));
          setUndoneSince(oldest);
        })
        .catch(() => {
          // Unknown: offer Undo; restoring a live row answers restored=false.
          if (live()) setUndoneSince(oldest);
        });
    },
    [identity.org, identity.namespace, oldest],
  );

  // A row retired again after an undo is retired, so the undo must be the later event.
  const undoneBefore = (r: AutoResolvedRow) => {
    const at = undone.get(r.memory_id);
    return at != null && instant(at) >= instant(r.ts);
  };
  const known = (r: AutoResolvedRow) => undoneSince != null && instant(r.ts) >= instant(undoneSince);

  const loadMore = async () => {
    if (!cursor || loadingRef.current) return;
    loadingRef.current = true;
    setLoadingMore(true);
    try {
      const page = await listAutoResolved({ org: identity.org, namespace: identity.namespace }, { cursor });
      if (!mounted.current) return;
      setRows((rs) => [...(rs ?? []), ...page.rows]);
      setCursor(page.next_cursor);
    } catch (e) {
      if (mounted.current) toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      loadingRef.current = false;
      if (mounted.current) setLoadingMore(false);
    }
  };

  const setBusyFor = (id: string, on: boolean) =>
    setBusy((s) => {
      const next = new Set(s);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const undo = async (r: AutoResolvedRow) => {
    const k = entryKey(r);
    if (busy.has(k)) return;
    setBusyFor(k, true);
    try {
      const out = await restoreMemory(
        { org: identity.org, namespace: identity.namespace, agent: r.agent || "-" },
        r.memory_key,
        { user_id: r.user_id, session_id: r.session_id, if_superseded_by: r.superseded_by },
      );
      if (!mounted.current) return;
      setOutcome((m) => new Map(m).set(k, out.restored ? "restored" : "gone"));
      const said = out.restored ? `Restored ${r.memory_key}` : `${r.memory_key} changed since; nothing undone`;
      // A screen reader repeats nothing when the text does not change: a trailing non-breaking
      // space makes a second Undo of the same key read again.
      setAnnouncement((prev) => (prev === said ? `${said}\u00a0` : said));
      if (out.restored) toast("Restored", "ok");
    } catch (e) {
      if (mounted.current) toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusyFor(k, false);
    }
  };

  const status = (r: AutoResolvedRow) => {
    const o = outcome.get(entryKey(r));
    if (o === "gone") {
      return <span className="text-sm text-text-muted px-2 py-1.5">Changed since</span>;
    }
    if (o === "restored" || undoneBefore(r)) {
      return (
        <span className="inline-flex items-center gap-1 text-sm text-success px-2 py-1.5">
          <Icon name="check" size="sm" aria-hidden="true" /> Restored
        </span>
      );
    }
    // Without the row it retired behind, an Undo could only be an unguarded restore.
    if (!canRestore || !r.superseded_by) return null;
    if (!known(r)) {
      return <span className="text-sm text-text-muted px-2 py-1.5">Checking…</span>;
    }
    return (
      <Button
        type="secondary"
        htmlType="button"
        iconLeft="history"
        onClick={() => undo(r)}
        disabled={busy.has(entryKey(r))}
        aria-label={`Undo retirement of ${r.memory_key}`}
      >
        Undo
      </Button>
    );
  };

  if (loadError != null) {
    return (
      <EmptyState
        icon="warning"
        title="Couldn't load retired memories"
        description={loadError}
        action={{ label: "Retry", onClick: () => setReload((n) => n + 1) }}
      />
    );
  }
  if (rows === null) return <Skeleton className="h-40 w-full" />;
  return (
    <div>
      <p className="text-sm text-text-muted mb-3 max-w-2xl">
        Memories Loom retired on its own in the last 30 days. They no longer come back in recall, but history keeps
        them. Undo brings one back.
      </p>
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
      <div className="space-y-1.5">
        {rows.map((r) => (
          <Panel key={entryKey(r)} padding="sm" bodyClassName="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <div className="font-mono text-base text-text truncate">{r.memory_key}</div>
              <div className="text-sm text-text mt-0.5">
                {autoResolveReasonText(r.reason) ?? "Retired automatically"}
              </div>
              <div className="text-xs text-text-muted tabular mt-1">
                Behind <span className="font-mono">{r.superseded_by_key || r.superseded_by || "—"}</span> ·{" "}
                {relativeTime(r.ts)}
                {r.unconfirmed ? " · may not have taken effect" : null}
              </div>
            </div>
            <div>{status(r)}</div>
          </Panel>
        ))}
      </div>
      {cursor ? (
        <Button type="secondary" htmlType="button" className="mt-3" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Load more"}
        </Button>
      ) : (
        <p className="mt-3 text-sm text-text-muted">Nothing older within 30 days.</p>
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

/** The peer a conflict names by id: its own key and value, from the queue when the peer
 *  is conflicted there too, else read by id. Until then (or when the id reads nothing)
 *  the card names the peer by its id only. */
function PeerSide({ rows, peerId }: { rows: ConflictRow[]; peerId?: string }) {
  const identity = useIdentity();
  const listed = peerId ? rows.find((r) => r.memory_id === peerId) : undefined;
  const [fetched, setFetched] = useState<{ id: string; row: MemoryById | null } | null>(null);
  useAbortableEffect(
    (signal, live) => {
      if (!peerId || listed) return;
      getMemoryById(identity.org, peerId, { signal })
        .then((r) => {
          if (live()) setFetched({ id: peerId, row: r.found ? r : null });
        })
        .catch(() => {
          if (live()) setFetched({ id: peerId, row: null });
        });
    },
    [identity.org, peerId, !!listed],
  );
  const read = fetched && fetched.id === peerId ? fetched.row : null;
  const peer = listed ?? read;
  if (!peer) {
    return <Side label="B · conflicting peer" memKey="—" value={null} version={null} peerId={peerId} />;
  }
  return (
    <Side
      label="B · conflicting peer"
      memKey={peer.memory_key}
      value={peer.value}
      version={peer.version}
      pinned={peer.pinned}
      createdAt={peer.created_at ?? undefined}
    />
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
