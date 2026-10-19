import { useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
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
import { LoadingSkeleton, PageHeader, SectionHeading, TabTrigger } from "../../components/ui";
import { Skeleton, TableSkeleton } from "../../components/Skeleton";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { formatValue, truncate, humanizeError, SENSITIVE_READ_BY_KEY } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { useRowNav } from "../../lib/useRowNav";
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
  // A probe seeds its panel's first view once: a panel opened again (the tabs unmount an
  // inactive one) reads afresh, so a learning promoted or an exception removed since does
  // not come back from the probe.
  const [seedsUsed, setSeedsUsed] = useState({ learnings: false, exceptions: false });
  useAbortableEffect(
    (signal, live) => {
      setProbedLearnings(null);
      setProbedExceptions(null);
      setSeedsUsed({ learnings: false, exceptions: false });
      setTab("conflicts");
      listLearnings({ org: identity.org, namespace: identity.namespace }, "pending", LEARNINGS_READ, { signal })
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
            <LearningsPanel
              initialPending={seedsUsed.learnings ? null : probedLearnings}
              onSeedUsed={() => setSeedsUsed((u) => ({ ...u, learnings: true }))}
            />
          </Tabs.Content>
        )}
        {hasExceptions && (
          <Tabs.Content value="exceptions">
            <AllowlistPanel
              initial={seedsUsed.exceptions ? null : probedExceptions}
              onSeedUsed={() => setSeedsUsed((u) => ({ ...u, exceptions: true }))}
            />
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
  const [error, setError] = useState<unknown>(null);
  const [index, setIndex] = useState(0);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  // Bumped by Retry, and when a decision empties the loaded rows while more wait past
  // them: the refetch runs through the namespace-keyed effect, so a reply that lands after
  // a namespace switch is dropped.
  const [reload, setReload] = useState(0);
  // The reload keeps what is on screen until it answers; another namespace starts over,
  // during render, so the old queue (and its decision buttons) never paints under it. The
  // count and the cursor are the queue's too: none of them carries over.
  const queueKey = `${identity.org}/${identity.namespace}`;
  const [queueFor, setQueueFor] = useState(queueKey);
  // The visit to this namespace a decision or a Load more was made in, for an answer that
  // lands after: it changes nothing here, its busy flags included. A count, not the name:
  // a switch away and back is another visit.
  const visit = useRef(0);
  if (queueFor !== queueKey) {
    visit.current += 1;
    setQueueFor(queueKey);
    setRows(null);
    setTotal(null);
    setNext(null);
    setError(null);
    // A decision or a Load more still out was the old queue's: its controls are free here.
    setBusy(false);
    setLoadingMore(false);
  }
  // The last page removes Load more, which held the focus: it goes to the first conflict
  // that page added, else the last one in the queue.
  const queueBox = useRef<HTMLDivElement>(null);
  // The queue's keys are one tab stop, as every row list (lib/useRowNav).
  const queueNav = useRowNav({ enabled: (rows?.length ?? 0) > 0 });
  const focusAdded = useRef<number | null>(null);
  // Conflicts decided here, by id and the version decided, whichever visit: no read (a
  // re-read on a return, a Load more) offers that version again, even one the server
  // answered before the decision was visible. A later version is a new conflict (a write
  // flagged it again) and is offered.
  const decidedAt = useRef(new Map<string, number>());
  const undecided = (c: ConflictRow) => c.version > (decidedAt.current.get(c.memory_id) ?? -1);
  // Each read out (the queue's, a Load more) collects the ids a decision takes meanwhile,
  // for its own answer: a page the server read before the resolve wrote still lists them,
  // at versions the resolve answer does not name.
  const readsOut = useRef(new Set<Set<string>>());
  // The count is read fresh (no replica's cache) and a resolve answers the namespace's
  // count after its writes, which the queue takes as is; a count read still out then is
  // older, and its answer is dropped. From a server a release behind, which answers no
  // count, each count read out collects the rows decisions take meanwhile instead and
  // subtracts them from its answer.
  const countsOut = useRef(new Set<{ taken: number; stale: boolean }>());
  // The namespace on screen as of the latest render, for a resolve's count.
  const queueNow = useRef(queueKey);
  queueNow.current = queueKey;
  // The rows on screen as of the latest render, for an answer from another visit.
  const rowsNow = useRef(rows);
  rowsNow.current = rows;

  useAbortableEffect(
    (signal, live) => {
      focusAdded.current = null;
      setError(null);
      const goneHere = new Set<string>();
      readsOut.current.add(goneHere);
      listConflicts({ org: identity.org, namespace: identity.namespace }, { limit: CONFLICT_PAGE, signal })
        .then((r) => {
          if (live()) {
            setRows(r.rows.filter((c) => undecided(c) && !goneHere.has(c.memory_id)));
            setNext(r.next_before_version);
            setIndex(0);
            setReason("");
          }
        })
        .catch((e) => {
          if (live()) setError(e);
        })
        .finally(() => readsOut.current.delete(goneHere));
      const countHere = { taken: 0, stale: false };
      countsOut.current.add(countHere);
      countConflicts({ org: identity.org, namespace: identity.namespace }, { fresh: true, signal })
        .then((n) => {
          if (live() && !countHere.stale) setTotal(Math.max(n - countHere.taken, 0));
        })
        .catch(() => {
          if (live()) setTotal(null);
        })
        .finally(() => countsOut.current.delete(countHere));
    },
    [identity.org, identity.namespace, reload],
  );

  // A decision that empties the loaded queue while more wait past it reads the queue again.
  const decided = useRef(false);
  useEffect(() => {
    if (!decided.current || rows === null) return;
    decided.current = false;
    if (rows.length === 0 && next !== null) setReload((n) => n + 1);
  }, [rows, next]);

  useEffect(() => {
    const at = focusAdded.current;
    if (at === null || loadingMore) return;
    focusAdded.current = null;
    if (next !== null || document.activeElement !== document.body) return;
    const keys = queueBox.current?.querySelectorAll<HTMLElement>("tbody button") ?? [];
    (keys[at] ?? keys[keys.length - 1])?.focus();
  });

  // The next older page joins the queue; the decision in progress stays where it is.
  const loadMore = async () => {
    if (next === null || !rows) return;
    const myVisit = visit.current;
    focusAdded.current = rows.length;
    const goneHere = new Set<string>();
    readsOut.current.add(goneHere);
    setLoadingMore(true);
    try {
      const r = await listConflicts(
        { org: identity.org, namespace: identity.namespace },
        { limit: CONFLICT_PAGE, before_version: next },
      );
      if (visit.current !== myVisit) return;
      // Against the rows as they are now: a conflict decided while this read was out stays gone.
      setRows((prev) => {
        if (!prev) return prev;
        const have = new Set(prev.map((c) => c.memory_id));
        return [...prev, ...r.rows.filter((c) => !have.has(c.memory_id) && !goneHere.has(c.memory_id) && undecided(c))];
      });
      setNext(r.next_before_version);
    } catch (e) {
      toast(`Couldn't load more conflicts: ${humanizeError(e)}`, "error");
    } finally {
      readsOut.current.delete(goneHere);
      if (visit.current === myVisit) setLoadingMore(false);
    }
  };

  if (error) {
    return <ListState kind="error" things="conflicts" error={error} onRetry={() => setReload((n) => n + 1)} />;
  }
  // Emptied with more past it: the reload is reading them, so the queue is loading, not empty.
  if (rows === null || (rows.length === 0 && next !== null))
    return <QueueTable rows={[]} current={null} onPick={() => {}} loading />;
  if (rows.length === 0) {
    return (
      <ListState
        kind="empty"
        icon="secure"
        title="No conflicts"
        description="A conflict appears when two memories with the same key disagree — your agent saying 'user lives in X' once and 'user lives in Y' later. None right now means your agent's been consistent."
      />
    );
  }

  // A guard: the cursor can outrun the rows only between two batched updates.
  const at = Math.min(index, rows.length - 1);
  const current = rows[at];

  const peerId = current.conflict_with?.[0];
  // The server's count is cached for a minute; it is never less than the rows on hand.
  const held = total === null ? null : Math.max(total, rows.length);

  const decide = async (decision: ResolveDecision) => {
    const myVisit = visit.current;
    const myQueue = queueKey;
    setBusy(true);
    try {
      const res = await resolveConflict(
        { org: identity.org, namespace: identity.namespace },
        current.memory_id,
        decision,
        reason.trim() ? { reason: reason.trim() } : {},
      );
      // Nothing was resolved (the row is gone or out of scope): the queue is read again
      // rather than dropping a row on a claim.
      if (!res.resolved) {
        toast("That conflict could not be resolved; it may have changed. The queue was read again.", "error");
        if (visit.current === myVisit) setReload((n) => n + 1);
        return;
      }
      toast("Resolved", "ok");
      // The decided row and the peers the resolve wrote (affected_memory_ids: tombstoned or
      // made active) leave every list, on any visit; ids are org-wide, so no other queue
      // loses a row. Each is remembered at the version on screen, so a later re-flag shows.
      const gone = new Set([current.memory_id, ...res.affected_memory_ids]);
      for (const out of readsOut.current) for (const id of gone) out.add(id);
      decidedAt.current.set(current.memory_id, current.version);
      const shown = rowsNow.current ?? [];
      for (const c of shown) if (gone.has(c.memory_id)) decidedAt.current.set(c.memory_id, c.version);
      const dropped = shown.filter((c) => gone.has(c.memory_id)).length;
      // Rows dropped before a position move it up by as many: the conflict open now stays
      // open, and after this visit's own decision the next one takes its place.
      const before = (i: number) => shown.slice(0, i).filter((c) => gone.has(c.memory_id)).length;
      decided.current = true;
      setRows((prev) => prev && prev.filter((c) => !gone.has(c.memory_id)));
      // Kept on a row: a decision on the last one opens the one before it, so a Load more
      // after it does not move the open conflict.
      const left = shown.length - dropped;
      setIndex((i) => Math.max(Math.min(i - before(i), left - 1), 0));
      const sameVisit = visit.current === myVisit;
      if (typeof res.total === "number" && queueNow.current === myQueue) {
        // The resolve's own count, made after its writes, for the namespace on screen.
        const total = res.total;
        for (const out of countsOut.current) out.stale = true;
        setTotal(total);
      } else if (typeof res.total !== "number") {
        // A server a release behind: the rows taken off the screen come off the count as it
        // is now (another answer may have changed it meanwhile); a peer on a page not
        // loaded comes off at the next count read. On this visit the decided row was on
        // screen even if an earlier answer took it off.
        const taken = sameVisit ? Math.max(dropped, 1) : dropped;
        setTotal((t) => (t === null ? t : Math.max(t - taken, 0)));
        for (const out of countsOut.current) out.taken += taken;
      }
      // Another visit: the reason is not this decision's. An emptied page with more past
      // it reads the queue again (the decided flag).
      if (!sameVisit) return;
      setReason("");
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      if (visit.current === myVisit) setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-3 text-sm text-text-muted tabular">
        Conflict {at + 1} of {(held ?? rows.length).toLocaleString()} · resolve in order
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
            sensitive={current.sensitivity === "high"}
          />
          <Side label="B · conflicting peer" memKey={current.memory_key} value={null} version={null} peerId={peerId} />
        </div>
        <TextAreaField
          label="Reason for the decision"
          value={reason}
          onChange={(value) => setReason(value)}
          // Like the buttons, held while a decision is out: its answer moves on from here.
          disabled={busy}
          placeholder="Optional — recorded in the audit trail"
          rows={3}
        />
        <div className="mt-4 flex items-center justify-end gap-2 flex-wrap">
          <Button
            type="empty"
            htmlType="button"
            iconLeft="slide-in"
            onClick={() => setIndex((i) => Math.min(rows.length - 1, i + 1))}
            disabled={busy || at >= rows.length - 1}
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

      <SectionHeading
        label="Queue"
        level={2}
        className="mt-6"
        caption={held === null ? `${rows.length} loaded` : `${rows.length} of ${held} loaded`}
      />
      <div ref={queueBox}>
        <div ref={queueNav}>
          <QueueTable rows={rows} current={current.memory_id} onPick={setIndex} busy={loadingMore} picking={!busy} />
        </div>
      </div>
      {next !== null && (
        <BusyButton
          type="secondary"
          htmlType="button"
          fillWidth
          className="mt-3"
          busy={loadingMore}
          onClick={() => void loadMore()}
        >
          Load more
        </BusyButton>
      )}
    </div>
  );
}

/** The conflicts on hand, one row each; the one being decided is the active row, and a
 *  key opens that conflict. The first read is the table's header over a skeleton. */
function QueueTable({
  rows,
  current,
  onPick,
  loading = false,
  busy = false,
  picking = true,
}: {
  rows: ConflictRow[];
  current: string | null;
  onPick: (index: number) => void;
  loading?: boolean;
  busy?: boolean;
  /** Off while a decision is out: its answer picks the next conflict. */
  picking?: boolean;
}) {
  return (
    <Table
      aria-label="Conflicts"
      aria-busy={loading || busy || undefined}
      noDataMessage={loading ? <TableSkeleton label="Loading conflicts" /> : undefined}
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
              <Link
                component="button"
                type="button"
                data-row-open=""
                size="sm"
                weight="mono"
                className="min-w-0 max-w-full truncate text-left"
                aria-current={c.memory_id === current || undefined}
                aria-disabled={!picking || undefined}
                onClick={() => picking && onPick(i)}
              >
                {c.memory_key}
              </Link>
            ),
          },
          { label: <span className="tabular">v{c.version}</span> },
          { label: c.pinned ? "Pinned" : <span className="text-text-muted">—</span> },
          { label: <ListTime time={c.created_at} quiet className="text-text-muted" /> },
        ],
      }))}
    />
  );
}

// ─── Learnings panel ───────────────────────────────────────────────────

// One read of learnings: a list as long as this may hold more.
const LEARNINGS_READ = 50;
const LEARNING_STATUSES: readonly LearningStatus[] = ["pending", "promoted", "rejected"];
const STATUS_LABEL: Record<LearningStatus, string> = { pending: "Pending", promoted: "Promoted", rejected: "Rejected" };

function LearningsPanel({
  initialPending,
  onSeedUsed,
}: {
  initialPending?: LearningEntry[] | null;
  onSeedUsed: () => void;
}) {
  const identity = useIdentity();
  const [status, setStatus] = useState<LearningStatus>("pending");
  const [rows, setRows] = useState<LearningEntry[] | null>(initialPending ?? null);
  const [reason, setReason] = useState<Record<string, string>>({});
  const seeded = useRef(initialPending != null);
  const [reload, setReload] = useState(0);
  // A reload after a decision keeps the list on screen, marked busy; another namespace or
  // status is another list, so it starts from the skeleton.
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Another namespace or status starts from the skeleton, during render, so the old list
  // (and its Promote / Reject buttons) never paints under it.
  const listKey = `${identity.org}/${identity.namespace}/${status}`;
  const [listFor, setListFor] = useState(listKey);
  if (listFor !== listKey) {
    setListFor(listKey);
    setRows(null);
    setError(null);
  }

  useAbortableEffect(
    (signal, live) => {
      // The first "pending" view was already fetched by the page's probe.
      if (seeded.current && status === "pending") {
        seeded.current = false;
        onSeedUsed();
        return;
      }
      setRefreshing(true);
      setError(null);
      listLearnings({ org: identity.org, namespace: identity.namespace }, status, LEARNINGS_READ, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) setError(e);
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
      <ListToolbar
        label="Learning status"
        search={false}
        // A full read may not be every learning: it says it holds the newest.
        count={
          !error && rows !== null
            ? `${rows.length >= LEARNINGS_READ ? "the newest " : ""}${rows.length} ${rows.length === 1 ? "learning" : "learnings"}`
            : undefined
        }
      >
        <ButtonGroup
          aria-label="Learning status"
          options={LEARNING_STATUSES.map((s) => ({ value: s, label: STATUS_LABEL[s] }))}
          selected={status}
          onClick={(value) => {
            const next = LEARNING_STATUSES.find((s) => s === value);
            if (next) setStatus(next);
          }}
        />
      </ListToolbar>
      {!!error && <ListState kind="error" things="learnings" error={error} onRetry={() => setReload((n) => n + 1)} />}
      {!error && rows === null && (
        <LoadingSkeleton label="Loading learnings">
          <Skeleton className="h-40 w-full" />
        </LoadingSkeleton>
      )}
      {!error && rows !== null && rows.length === 0 && (
        <ListState
          kind="empty"
          icon="sparkle"
          title={`No ${status} learnings`}
          description="Learnings are facts noticed across several agents in your organisation and proposed as shared knowledge for everyone. Solo and development setups usually have none."
        />
      )}
      {!error && rows !== null && rows.length > 0 && (
        <div className="space-y-2" aria-busy={refreshing || undefined}>
          {rows.map((r) => (
            <Panel key={r.candidate_id}>
              <div className="flex items-center justify-between gap-3 mb-2">
                <span className="font-mono text-sm text-text-muted">{r.memory_key}</span>
                <span className="text-xs text-text-muted tabular">
                  {r.supporter_count} supporters · conf {(r.confidence_avg ?? 0).toFixed(2)} ·{" "}
                  <ListTime time={r.proposed_at} quiet />
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
                  {status === "promoted" ? "Promoted" : "Rejected"}
                  {r.decided_at && (
                    <>
                      {" "}
                      <ListTime time={r.decided_at} quiet />
                    </>
                  )}
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

function AllowlistPanel({ initial, onSeedUsed }: { initial?: AllowlistRow[] | null; onSeedUsed: () => void }) {
  const identity = useIdentity();
  const [rows, setRows] = useState<AllowlistRow[] | null>(initial ?? null);
  const seeded = useRef(initial != null);
  const [reload, setReload] = useState(0);
  // A reload after a removal keeps the list on screen, marked busy; another namespace
  // starts from the skeleton.
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Another namespace starts from the skeleton, during render, so the old pairs (and
  // their Remove buttons) never paint under it.
  const listKey = `${identity.org}/${identity.namespace}`;
  const [listFor, setListFor] = useState(listKey);
  if (listFor !== listKey) {
    setListFor(listKey);
    setRows(null);
    setError(null);
  }

  useAbortableEffect(
    (signal, live) => {
      if (seeded.current) {
        seeded.current = false;
        onSeedUsed();
        return;
      }
      setRefreshing(true);
      setError(null);
      listAllowlist({ org: identity.org, namespace: identity.namespace }, { signal })
        .then((r) => {
          if (live()) setRows(r.rows || []);
        })
        .catch((e) => {
          if (live()) setError(e);
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

  if (error) {
    return <ListState kind="error" things="allowed exceptions" error={error} onRetry={() => setReload((n) => n + 1)} />;
  }
  if (rows !== null && rows.length === 0) {
    return (
      <ListState
        kind="empty"
        icon="line-in-circle"
        title="No allowed exceptions"
        description="Pairs you explicitly said may coexist. Fills only as a side-effect of clicking 'Allow both' when resolving a conflict — there's nothing for you to add proactively."
      />
    );
  }
  // The first read is the table's header over a skeleton; a reload keeps the rows, marked busy.
  return (
    <div data-testid="allowlist" aria-busy={refreshing || undefined}>
      <Table
        aria-label="Allowed exceptions"
        aria-busy={rows === null || undefined}
        noDataMessage={rows === null ? <TableSkeleton label="Loading allowed exceptions" /> : undefined}
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
            { label: <ListTime time={r.allowed_at} quiet className="text-xs text-text-muted" /> },
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
  sensitive,
}: {
  label: string;
  memKey: string;
  value: unknown;
  version: number | null;
  pinned?: boolean;
  createdAt?: string;
  peerId?: string;
  sensitive?: boolean;
}) {
  return (
    <Panel tone="surface-2" padding="sm">
      <div className="text-xs text-text-muted mb-1.5">{label}</div>
      <div className="font-mono text-xs text-text mb-2 truncate">{memKey}</div>
      <div className="text-sm text-text mb-2 leading-snug min-h-10">
        {sensitive ? (
          <span className="text-text-muted">{SENSITIVE_READ_BY_KEY}</span>
        ) : value == null ? (
          <span className="text-text-muted">
            Peer: <span className="font-mono">{peerId?.slice(0, 12)}…</span>
          </span>
        ) : (
          truncate(formatValue(value), 200)
        )}
      </div>
      <div className="flex items-center gap-2 text-xs text-text-muted tabular">
        {version != null && <span>v{version}</span>}
        {pinned && <span>Pinned</span>}
        {createdAt && <ListTime time={createdAt} quiet />}
      </div>
    </Panel>
  );
}
