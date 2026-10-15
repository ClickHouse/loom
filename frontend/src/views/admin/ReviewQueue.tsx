import { useEffect, useRef, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { ShieldCheck, AlertTriangle, Sparkles, Check, X, SkipForward, ShieldOff, Undo2, Wand } from "lucide-react";
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
  autoResolveReasonText,
  type AutoResolvedRow,
  type ConflictRow,
  type AllowlistRow,
  type ResolveDecision,
  type LearningEntry,
  type LearningStatus,
} from "../../api";
import { useIdentity, useStore } from "../../lib/state";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { relativeTime, formatValue, truncate, humanizeError } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { canCall } from "../../lib/canCall";
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
  // The probe's page, tagged with the namespace it read: the reset after a namespace
  // switch runs a tick after the first render of the new namespace, which must not show
  // (or seed a panel with) the previous namespace's page.
  const scopeKey = `${identity.org}/${identity.namespace}`;
  const [probedAuto, setProbedAuto] = useState<(AutoResolvedPage & { scope: string }) | null>(null);
  const autoPage = probedAuto?.scope === scopeKey ? probedAuto : null;
  const hasAuto = (autoPage?.rows.length ?? 0) > 0;
  // audit.list is asked only by a role known to reach it.
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const auditReadable = canCall("audit.list", role, whoami);
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
  useAbortableEffect(
    (signal, live) => {
      setProbedAuto(null);
      if (!auditReadable) {
        // The Auto-resolved tab goes with the access: leave it for Conflicts.
        setTab((t) => (t === "auto" ? "conflicts" : t));
        return;
      }
      listAutoResolved({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setProbedAuto({ rows: r.rows || [], next_cursor: r.next_cursor, scope: scopeKey });
        })
        .catch(() => {});
    },
    [identity.org, identity.namespace, auditReadable],
  );

  return (
    <PageContainer>
      <div className="flex items-baseline gap-3 mb-2">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <AlertTriangle size={20} className="text-[var(--color-warn)]" /> Review queue
        </h1>
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <p className="text-sm text-[var(--color-text-muted)] mb-5 max-w-2xl">
        Memory disputes needing a human decision — your agent flip-flopped on a fact, or the extractor wants to promote
        one. Most users never see anything here; that's a feature.
      </p>
      <Tabs.Root value={tab} onValueChange={setTab}>
        {(hasLearnings || hasExceptions || hasAuto) && (
          <Tabs.List className="flex border-b border-[var(--color-border)] mb-5">
            <TabTrigger value="conflicts" icon={AlertTriangle}>
              Conflicts
            </TabTrigger>
            {hasLearnings && (
              <TabTrigger value="learnings" icon={Sparkles}>
                Learnings
              </TabTrigger>
            )}
            {hasAuto && (
              <TabTrigger value="auto" icon={Wand}>
                Auto-resolved
              </TabTrigger>
            )}
            {hasExceptions && (
              <TabTrigger value="exceptions" icon={ShieldOff}>
                Exceptions
              </TabTrigger>
            )}
          </Tabs.List>
        )}
        <Tabs.Content value="conflicts" className="outline-none">
          <ConflictsPanel />
        </Tabs.Content>
        {hasLearnings && (
          <Tabs.Content value="learnings" className="outline-none">
            <LearningsPanel initialPending={probedLearnings} />
          </Tabs.Content>
        )}
        {hasAuto && autoPage && (
          // Kept mounted while another tab is open, so its loaded pages and Undo outcomes
          // survive a tab switch; a namespace switch drops it with the probe.
          <Tabs.Content value="auto" forceMount className="outline-none data-[state=inactive]:hidden">
            {/* Keyed by the namespace it read: another namespace's page is a new panel. */}
            <AutoResolvedPanel key={autoPage.scope} initial={autoPage} />
          </Tabs.Content>
        )}
        {hasExceptions && (
          <Tabs.Content value="exceptions" className="outline-none">
            <AllowlistPanel initial={probedExceptions} />
          </Tabs.Content>
        )}
      </Tabs.Root>
    </PageContainer>
  );
}

function TabTrigger({
  value,
  icon: Icon,
  children,
}: {
  value: string;
  icon: typeof AlertTriangle;
  children: React.ReactNode;
}) {
  return (
    <Tabs.Trigger
      value={value}
      className="inline-flex items-center gap-2 px-4 py-2.5 text-base font-medium text-[var(--color-text-muted)] hover:text-[var(--color-text)] data-[state=active]:text-[var(--color-text)] data-[state=active]:border-b-2 data-[state=active]:border-[var(--color-accent)] -mb-px outline-none transition-colors"
    >
      <Icon size={14} /> {children}
    </Tabs.Trigger>
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
        icon={ShieldCheck}
        title="No conflicts"
        description="A conflict appears when two memories with the same key disagree — your agent saying 'user lives in X' once and 'user lives in Y' later. None right now means your agent's been consistent."
      />
    );
  }

  const current = rows[index];
  if (!current) return <EmptyState icon={ShieldCheck} title="All clear" description="You worked through the queue." />;

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
      <div className="text-sm text-[var(--color-text-muted)] tabular mb-3">
        Conflict {index + 1} of {rows.length} · resolve in order
      </div>
      <div className="rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
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
        <textarea
          aria-label="Reason for the decision"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Optional reason (audit trail)…"
          className="w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] resize-y min-h-[60px] outline-none focus:border-[var(--color-accent)]"
        />
        <div className="mt-4 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => decide("revise")}
            disabled={busy}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
          >
            <Check size={13} /> Keep A
          </button>
          <button
            onClick={() => decide("contract")}
            disabled={busy}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border border-[var(--color-border-strong)] rounded-md hover:bg-[var(--color-surface-2)] disabled:opacity-50"
          >
            <X size={13} /> Drop A
          </button>
          <button
            onClick={() => decide("allow")}
            disabled={busy}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border border-[var(--color-border-strong)] rounded-md hover:bg-[var(--color-surface-2)] disabled:opacity-50"
          >
            Allow both
          </button>
          <div className="flex-1" />
          <button
            onClick={() => setIndex((i) => Math.min(rows.length - 1, i + 1))}
            disabled={busy || index >= rows.length - 1}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md disabled:opacity-30"
          >
            <SkipForward size={13} /> Skip
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Learnings panel ───────────────────────────────────────────────────

const LEARNING_STATUSES: readonly LearningStatus[] = ["pending", "promoted", "rejected"];

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
      <div className="inline-flex gap-1 mb-4 p-1 bg-[var(--color-surface-2)] rounded-lg">
        {LEARNING_STATUSES.map((s) => (
          <button
            key={s}
            onClick={() => setStatus(s)}
            className={`px-3 py-1 text-sm rounded-md capitalize transition-colors ${
              status === s
                ? "bg-[var(--color-surface)] text-[var(--color-text)] shadow-sm"
                : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            }`}
          >
            {s}
          </button>
        ))}
      </div>
      {rows === null && <Skeleton className="h-40 w-full" />}
      {rows !== null && rows.length === 0 && (
        <EmptyState
          icon={Sparkles}
          title={`No ${status} learnings`}
          description="Learnings are facts noticed across several agents in your organisation and proposed as shared knowledge for everyone. Solo and development setups usually have none."
        />
      )}
      {rows !== null && rows.length > 0 && (
        <div className="space-y-2">
          {rows.map((r) => (
            <div
              key={r.candidate_id}
              className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]"
            >
              <div className="flex items-center justify-between gap-3 mb-2">
                <span className="font-mono text-sm text-[var(--color-text-muted)]">{r.memory_key}</span>
                <span className="text-xs text-[var(--color-text-muted)] tabular">
                  {r.supporter_count} supporters · conf {(r.confidence_avg ?? 0).toFixed(2)} ·{" "}
                  {relativeTime(r.proposed_at)}
                </span>
              </div>
              <div className="text-base text-[var(--color-text)] mb-2">
                {truncate(formatValue(r.proposed_value), 240)}
              </div>
              {r.rationale && <div className="text-sm text-[var(--color-text-muted)] mb-2 italic">{r.rationale}</div>}
              {status === "pending" && (
                <>
                  <textarea
                    aria-label="Reason for the decision"
                    value={reason[r.candidate_id] || ""}
                    onChange={(e) => setReason((m) => ({ ...m, [r.candidate_id]: e.target.value }))}
                    placeholder="Optional decision reason…"
                    className="w-full mt-2 px-2.5 py-1.5 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                    rows={2}
                  />
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={() => decide(r.candidate_id, "promote")}
                      className="inline-flex items-center gap-1 px-3 py-1.5 text-sm font-medium bg-[var(--color-success)] text-[var(--color-on-accent)] rounded-md hover:opacity-90"
                    >
                      <Check size={12} /> Promote
                    </button>
                    <button
                      onClick={() => decide(r.candidate_id, "reject")}
                      className="inline-flex items-center gap-1 px-3 py-1.5 text-sm font-medium border border-[var(--color-border-strong)] text-[var(--color-text)] rounded-md hover:bg-[var(--color-surface-2)]"
                    >
                      <X size={12} /> Reject
                    </button>
                  </div>
                </>
              )}
              {status !== "pending" && (
                <div className="mt-1 text-sm text-[var(--color-text-muted)]">
                  {status === "promoted"
                    ? `Promoted ${r.decided_at ? relativeTime(r.decided_at) : ""}`
                    : `Rejected ${r.decided_at ? relativeTime(r.decided_at) : ""}`}
                  {r.decided_by && <> by {r.decided_by}</>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Auto-resolved panel ───────────────────────────────────────────────

interface AutoResolvedPage {
  rows: AutoResolvedRow[];
  next_cursor: string | null;
}

/** What became of a row after the Undo: restored, or `memory.restore` found it no longer
 *  retired behind that memory (restored, deleted or retired behind another since). */
type UndoOutcome = "restored" | "gone";

// One retirement record: a memory retired twice shows up once per record.
const entryKey = (r: AutoResolvedRow) => `${r.memory_id}@${r.ts}>${r.superseded_by}`;

/** Newest restore per id across two reads. */
function mergeUndone(a: Map<string, string>, b: Map<string, string>): Map<string, string> {
  const out = new Map(a);
  for (const [id, ts] of b) {
    const prev = out.get(id);
    if (!prev || ts > prev) out.set(id, ts);
  }
  return out;
}

function AutoResolvedPanel({ initial }: { initial: AutoResolvedPage }) {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const canRestore = canCall("memory.restore", role, whoami);
  const [rows, setRows] = useState<AutoResolvedRow[]>(initial.rows);
  const [cursor, setCursor] = useState<string | null>(initial.next_cursor);
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
  const oldest = rows.reduce((m, r) => (m === "" || r.ts < m ? r.ts : m), "");

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

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
    return at != null && at >= r.ts;
  };
  const known = (r: AutoResolvedRow) => undoneSince != null && r.ts >= undoneSince;

  const loadMore = async () => {
    if (!cursor || loadingRef.current) return;
    loadingRef.current = true;
    setLoadingMore(true);
    try {
      const page = await listAutoResolved({ org: identity.org, namespace: identity.namespace }, { cursor });
      if (!mounted.current) return;
      setRows((rs) => [...rs, ...page.rows]);
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
      setAnnouncement(out.restored ? `Restored ${r.memory_key}` : `${r.memory_key} changed since; nothing undone`);
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
      return <span className="text-sm text-[var(--color-text-muted)] px-2 py-1.5">Changed since</span>;
    }
    if (o === "restored" || undoneBefore(r)) {
      return (
        <span className="inline-flex items-center gap-1 text-sm text-[var(--color-success)] px-2 py-1.5">
          <Check size={13} aria-hidden="true" /> Restored
        </span>
      );
    }
    // Without the row it retired behind, an Undo could only be an unguarded restore.
    if (!canRestore || !r.superseded_by) return null;
    if (!known(r)) {
      return <span className="text-sm text-[var(--color-text-muted)] px-2 py-1.5">Checking…</span>;
    }
    return (
      <button
        onClick={() => undo(r)}
        disabled={busy.has(entryKey(r))}
        aria-label={`Undo retirement of ${r.memory_key}`}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border border-[var(--color-border-strong)] rounded-md hover:bg-[var(--color-surface-2)] disabled:opacity-50"
      >
        <Undo2 size={13} /> Undo
      </button>
    );
  };

  return (
    <div>
      <p className="text-sm text-[var(--color-text-muted)] mb-3 max-w-2xl">
        Memories Loom retired on its own in the last 30 days. They no longer come back in recall, but history keeps
        them. Undo brings one back.
      </p>
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
      <div className="space-y-1.5">
        {rows.map((r) => (
          <div
            key={entryKey(r)}
            className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-start gap-3"
          >
            <div className="flex-1 min-w-0">
              <div className="font-mono text-base text-[var(--color-text)] truncate">{r.memory_key}</div>
              <div className="text-sm text-[var(--color-text)] mt-0.5">
                {autoResolveReasonText(r.reason, r.label) ?? "Retired automatically"}
              </div>
              <div className="text-xs text-[var(--color-text-muted)] tabular mt-1">
                Behind <span className="font-mono">{r.superseded_by_key || r.superseded_by || "—"}</span> ·{" "}
                {relativeTime(r.ts)}
                {r.unconfirmed ? " · may not have taken effect" : null}
              </div>
            </div>
            <div>{status(r)}</div>
          </div>
        ))}
      </div>
      {cursor ? (
        <button
          onClick={loadMore}
          disabled={loadingMore}
          className="mt-3 px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md disabled:opacity-50"
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      ) : (
        <p className="mt-3 text-sm text-[var(--color-text-muted)]">Nothing older within 30 days.</p>
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
        icon={ShieldOff}
        title="No allowed exceptions"
        description="Pairs you explicitly said may coexist. Fills only as a side-effect of clicking 'Allow both' when resolving a conflict — there's nothing for you to add proactively."
      />
    );
  }
  return (
    <div className="space-y-1.5">
      {rows.map((r) => (
        <div
          key={`${r.memory_key_a}-${r.memory_key_b}`}
          className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-start gap-3"
        >
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 text-base font-mono">
              <span className="text-[var(--color-text)] truncate">{r.memory_key_a}</span>
              <span className="text-[var(--color-text-muted)]">↔</span>
              <span className="text-[var(--color-text)] truncate">{r.memory_key_b}</span>
            </div>
            <div className="text-xs text-[var(--color-text-muted)] tabular mt-1">
              {r.allowed_by} · {relativeTime(r.allowed_at)}
              {r.reason && <> · "{r.reason}"</>}
            </div>
          </div>
          <button
            onClick={() => remove(r.memory_key_a, r.memory_key_b)}
            aria-label={`Remove exception ${r.memory_key_a} ↔ ${r.memory_key_b}`}
            className="p-1.5 text-[var(--color-danger)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors"
          >
            <X size={14} />
          </button>
        </div>
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
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface-2)] p-3">
      <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5">{label}</div>
      <div className="font-mono text-xs text-[var(--color-text)] mb-2 truncate">{memKey}</div>
      <div className="text-base text-[var(--color-text)] mb-2 leading-snug min-h-[40px]">
        {value == null ? (
          <span className="text-[var(--color-text-subtle)]">
            Peer: <span className="font-mono">{peerId?.slice(0, 12)}…</span>
          </span>
        ) : (
          truncate(formatValue(value), 200)
        )}
      </div>
      <div className="flex items-center gap-2 text-2xs text-[var(--color-text-muted)] tabular">
        {version != null && <span>v{version}</span>}
        {pinned && <span className="text-warn">pinned</span>}
        {createdAt && <span>{relativeTime(createdAt)}</span>}
      </div>
    </div>
  );
}
