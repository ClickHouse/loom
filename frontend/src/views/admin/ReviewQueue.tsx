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
  listProposals,
  listAutoResolved,
  listUnsuperseded,
  auditInstant,
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
  type MemoryProposal,
  type ProposalList,
} from "../../api";
import { decideProposal } from "../../api/proposalDecisions";
import { useIdentity, useStore } from "../../lib/state";
import { IconButton } from "../../components/IconButton";
import { Panel } from "../../components/ui/Panel";
import { LoadingSkeleton, PageHeader, SectionHeading, TabTrigger } from "../../components/ui";
import { Skeleton, TableSkeleton } from "../../components/Skeleton";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { AgentBadge } from "../../components/AgentBadge";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { formatValue, truncate, humanizeError, SENSITIVE_READ_BY_KEY } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { canCall } from "../../lib/canCall";
import { sharedHasAutoResolved } from "../../lib/sharedReads";
import { useResetOnKey } from "../../lib/useResetOnKey";
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
 *   - Proposals:   facts an agent wrote with `memory.set(proposed=true)`, live only once
 *                  someone accepts them (`memory.proposal_decide`)
 *   - Auto-resolved: memories Loom retired on its own (`memory.auto_supersede`), each
 *                    with an Undo (`memory.restore`)
 *   - Exceptions:  allowlist of pairs we've explicitly said may coexist
 */
// Radix's tab panel attributes, cleared while Conflicts is the only panel.
const LONE_PANEL = { role: undefined, "aria-labelledby": undefined, tabIndex: undefined };

export function ReviewQueue() {
  const identity = useIdentity();
  // Probe whether the sub-tabs (Learnings, Proposals, Auto-resolved, Exceptions) have
  // any content before showing them. Learnings is empty by default in
  // single-user / dev mode. Exceptions only populates as a side-effect
  // of resolving conflicts. Hiding empty sub-tabs until they have
  // content reduces noise; they reappear automatically.
  // The probe's answers seed the panels, so opening a tab does not ask the same question
  // a second time.
  const [probedLearnings, setProbedLearnings] = useState<LearningEntry[] | null>(null);
  const [probedExceptions, setProbedExceptions] = useState<AllowlistRow[] | null>(null);
  const [probedProposals, setProbedProposals] = useState<(ProposalList & { scope: string }) | null>(null);
  const hasLearnings = (probedLearnings?.length ?? 0) > 0;
  const hasExceptions = (probedExceptions?.length ?? 0) > 0;
  // The probe's yes/no, tagged with the namespace it read: the reset after a namespace
  // switch runs a tick after the first render of the new namespace, which must not show
  // the previous namespace's tab.
  const scopeKey = `${identity.org}/${identity.namespace}`;
  const hasProposals = probedProposals?.scope === scopeKey && probedProposals.proposals.length > 0;
  const [probedAuto, setProbedAuto] = useState<{ found: boolean; scope: string } | null>(null);
  const hasAuto = probedAuto?.scope === scopeKey && probedAuto.found;
  // The Auto-resolved panel reads its rows when the tab is first opened, then stays mounted.
  const [autoOpened, setAutoOpened] = useState(false);
  // The tab lists retirements and reads their Undo records (audit.list): asked only by a
  // role known to reach both.
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const autoReadable = canCall("memory.auto_retirements", role, whoami) && canCall("audit.list", role, whoami);
  const tabbed = hasLearnings || hasProposals || hasExceptions || hasAuto;
  // A namespace switch starts from Conflicts: the previous namespace's probes (and a tab
  // they revealed) must not outlive it, or a namespace with neither sub-tab strands the
  // viewer on a hidden panel.
  const [tab, setTab] = useState("conflicts");
  // A probe seeds its panel's first view once: a panel opened again (the tabs unmount an
  // inactive one) reads afresh, so a learning promoted or an exception removed since does
  // not come back from the probe.
  const [seedsUsed, setSeedsUsed] = useState({ learnings: false, proposals: false, exceptions: false });
  useAbortableEffect(
    (signal, live) => {
      setProbedLearnings(null);
      setProbedExceptions(null);
      setSeedsUsed({ learnings: false, proposals: false, exceptions: false });
      setTab("conflicts");
      setAutoOpened(false);
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
  const proposalsReadable = canCall("memory.proposals", role, whoami);
  useAbortableEffect(
    (signal, live) => {
      setProbedProposals(null);
      if (!proposalsReadable) {
        setTab((t) => (t === "proposals" ? "conflicts" : t));
        return;
      }
      listProposals({ org: identity.org, namespace: identity.namespace }, PROPOSALS_READ, { signal })
        .then((r) => {
          if (live()) setProbedProposals({ proposals: r.proposals || [], total: r.total ?? 0, scope: scopeKey });
        })
        .catch(() => {
          if (live()) setProbedProposals({ proposals: [], total: 0, scope: scopeKey });
        });
    },
    [identity.org, identity.namespace, proposalsReadable],
  );
  useAbortableEffect(
    (_signal, live) => {
      setProbedAuto(null);
      if (!autoReadable) {
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
    [identity.org, identity.namespace, autoReadable],
  );

  return (
    <PageContainer>
      <PageHeader title="Review queue">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-5 max-w-2xl">
        Memory decisions needing a human — your agent flip-flopped on a fact, proposed one for review, or the extractor
        wants to promote one. Most users never see anything here; that's a feature.
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
            {hasProposals && (
              <TabTrigger value="proposals" icon="user">
                Proposals
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
            <LearningsPanel
              initialPending={seedsUsed.learnings ? null : probedLearnings}
              onSeedUsed={() => setSeedsUsed((u) => ({ ...u, learnings: true }))}
            />
          </Tabs.Content>
        )}
        {hasProposals && (
          <Tabs.Content value="proposals">
            <ProposalsPanel
              initial={seedsUsed.proposals ? null : probedProposals}
              onSeedUsed={() => setSeedsUsed((u) => ({ ...u, proposals: true }))}
            />
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
          <PeerSide rows={rows} peerId={peerId} />
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

const plural = (n: number, one: string) => `${n} ${n === 1 ? one : `${one}s`}`;

// A rule a bot stated about itself in conversations (`assistant_turn`).
const isAgentRule = (r: LearningEntry) => r.source_kind === "assistant_turn";

// What filed a candidate, in words; an unknown producer shows its own name.
const SOURCE_LABEL: Record<string, string> = {
  assistant_turn: "A bot's rule about itself",
  conversation_seeder: "Seen in conversations",
  memory_supporters: "Backed by several writers",
};
const sourceLabel = (r: LearningEntry) => SOURCE_LABEL[r.source_kind || "memory_supporters"] ?? r.source_kind;

// Who a promoted rule applies to: its agent, or the namespace when it names none.
const agentLabel = (r: LearningEntry) =>
  r.agent && r.agent !== "-" ? `agent ${r.agent}` : "every agent in this namespace";

// What promoting does: a named bot's reads serve the rule, and so do reads under
// agent * and knowledge.search, which are not filtered by agent.
const promoteNote = (r: LearningEntry) =>
  r.agent && r.agent !== "-"
    ? `Promote publishes this rule to the namespace's knowledge for ${r.agent}, for every user; reads under agent * and knowledge.search also find it.`
    : "Promote publishes this rule to the namespace's knowledge for every agent and every user.";

// A bot's rule is backed by distinct users, its sessions and writers beside them
// (one token can send many user_ids); every other candidate by its supporters. A
// server a release behind sends no supporter_sessions, so its rows show the stored
// supporter count.
function supportLabel(r: LearningEntry): string {
  if (isAgentRule(r) && r.supporter_sessions !== undefined) {
    const support = `${plural(r.supporter_count, "user")}, ${plural(r.supporter_sessions, "session")}`;
    return r.supporter_writers === undefined ? support : `${support}, ${plural(r.supporter_writers, "writer")}`;
  }
  return plural(r.supporter_count, "supporter");
}

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
  useResetOnKey(`${identity.org}/${identity.namespace}/${status}`, () => {
    setRows(null);
    setError(null);
  });

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
          description="A learning is a candidate waiting for a person's decision: a fact several writers back, a fact seen in conversations, or a rule a bot stated about itself (when the deployment files those). Nothing is promoted without a review."
        />
      )}
      {!error && rows !== null && rows.length > 0 && (
        <div className="space-y-2" aria-busy={refreshing || undefined}>
          {rows.map((r) => (
            <Panel key={r.candidate_id}>
              <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
                <span className="min-w-0 break-all font-mono text-sm text-text-muted">{r.memory_key}</span>
                <span className="text-xs text-text-muted tabular">
                  {supportLabel(r)} · conf {(r.confidence_avg ?? 0).toFixed(2)} ·{" "}
                  <ListTime time={r.proposed_at} quiet />
                </span>
              </div>
              <div className="text-xs text-text-muted mb-2">
                {sourceLabel(r)}
                {isAgentRule(r) && r.agent !== undefined && <> · {agentLabel(r)}</>}
              </div>
              {isAgentRule(r) && (r.proposed_value === null || r.proposed_value === "") ? (
                // A user erasure took the rule's last supporter: only the decision is kept.
                <div className="text-sm text-text-muted mb-2">Rule removed by a user erasure; the decision stands.</div>
              ) : (
                <div className="text-sm text-text mb-2">{truncate(formatValue(r.proposed_value), 240)}</div>
              )}
              {/* A bot rule's rationale quotes the user turn, then what the assistant said, one per line. */}
              {r.rationale && (
                <div className="text-sm text-text-muted mb-2 italic whitespace-pre-line">{r.rationale}</div>
              )}
              {status === "pending" && (
                <>
                  {isAgentRule(r) && r.agent !== undefined && (
                    <div className="text-sm text-text-muted mb-2">{promoteNote(r)}</div>
                  )}
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

// ─── Proposals panel ───────────────────────────────────────────────────

// Proposals read per view; the toolbar says when the list may hold only the newest.
const PROPOSALS_READ = 50;

/** One line on how the proposal has done on trial. */
function trialLabel(r: MemoryProposal): string {
  const passes = r.graded_passes ?? 0;
  const graded = `${passes} graded ${passes === 1 ? "pass" : "passes"}, ${plural(r.graded_fails ?? 0, "fail")}`;
  return r.ready ? `${graded} · the policy would ${r.ready} it` : graded;
}

// The characters of a proposal's value `memory.proposals` sends.
const PREVIEW_CHARS = 400;

// A key's proposals as one row. Several identities may propose the same key, and
// `memory.proposal_decide` decides the newest version (the listing's first): the row
// shows that one and counts the older ones behind it.
type ProposalRow = MemoryProposal & { older: number };

function oneRowPerKey(read: MemoryProposal[]): ProposalRow[] {
  const rows = new Map<string, ProposalRow>();
  for (const p of read) {
    const row = rows.get(p.memory_key);
    if (row) row.older += 1;
    else rows.set(p.memory_key, { ...p, older: 0 });
  }
  return [...rows.values()];
}

const proposalsIn = (rows: ProposalRow[]) => rows.reduce((n, r) => n + 1 + r.older, 0);

function ProposalsPanel({ initial, onSeedUsed }: { initial?: ProposalList | null; onSeedUsed: () => void }) {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const inspect = useStore((s) => s.inspect);
  const role = useNamespaceRole(identity.namespace);
  const mayDecide = canCall("memory.proposal_decide", role, whoami);
  // Proposals this panel decided, by memory id: a re-read from a replica that has not
  // seen the decision yet must not bring them back. Each is kept until a read drops it.
  const decided = useRef(new Set<string>());
  const [rows, setRows] = useState<ProposalRow[] | null>(initial ? oneRowPerKey(initial.proposals) : null);
  const [total, setTotal] = useState(initial?.total ?? 0);
  const [reason, setReason] = useState<Record<string, string>>({});
  // The keys a decision is out for: their buttons wait, so a second click sends nothing.
  const [deciding, setDeciding] = useState<ReadonlySet<string>>(new Set());
  // Where focus goes once the decided row leaves the list: the row that took its place,
  // else the last row, else the list itself.
  const focusAt = useRef<number | null>(null);
  const listBox = useRef<HTMLElement>(null);
  // A failed read that replaces a list holding focus hands it to the error's section.
  const errorBox = useRef<HTMLElement>(null);
  const rowBoxes = useRef(new Map<string, HTMLElement>());
  // The rows as last rendered, for a decision whose answer arrives after others changed them.
  const rowsNow = useRef(rows);
  rowsNow.current = rows;
  const focusError = useRef(false);
  const seeded = useRef(initial != null);
  const [reload, setReload] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // A namespace switch unmounts this panel (the page's probe is tagged with its
  // namespace), so it never holds another namespace's rows.

  useAbortableEffect(
    (signal, live) => {
      // The first view was already fetched by the page's probe.
      if (seeded.current) {
        seeded.current = false;
        onSeedUsed();
        return;
      }
      setRefreshing(true);
      setError(null);
      listProposals({ org: identity.org, namespace: identity.namespace }, PROPOSALS_READ, { signal })
        .then((r) => {
          if (!live()) return;
          const read = r.proposals || [];
          const stale = read.filter((p) => decided.current.has(p.memory_id));
          decided.current = new Set(stale.map((p) => p.memory_id));
          setRows(oneRowPerKey(read.filter((p) => !decided.current.has(p.memory_id))));
          setTotal(Math.max(0, (r.total ?? 0) - stale.length));
        })
        .catch((e) => {
          if (!live()) return;
          focusError.current = !!listBox.current?.contains(document.activeElement);
          setError(e);
        })
        .finally(() => {
          if (live()) setRefreshing(false);
        });
    },
    [identity.org, identity.namespace, reload],
  );

  useEffect(() => {
    const at = focusAt.current;
    if (at === null || rows === null) return;
    focusAt.current = null;
    const fields = [...(listBox.current?.querySelectorAll<HTMLElement>("textarea") ?? [])];
    (fields[at] ?? fields.at(-1) ?? listBox.current)?.focus();
  }, [rows]);

  useEffect(() => {
    if (!error || !focusError.current) return;
    focusError.current = false;
    errorBox.current?.focus();
  }, [error]);

  const decide = async (key: string, decision: "accept" | "reject") => {
    const row = rowBoxes.current.get(key);
    setDeciding((d) => new Set(d).add(key));
    try {
      const out = await decideProposal(
        { org: identity.org, namespace: identity.namespace },
        key,
        decision,
        reason[key] || "",
      );
      const result = out.results?.[0];
      if (result && !result.decided) toast(`Not decided: ${result.reason || "no longer a proposal"}`, "error");
      else if (result?.status === "conflicted")
        toast("Accepted; it contradicts a live fact, which now waits in the Conflicts tab", "ok");
      else toast(decision === "accept" ? "Accepted" : "Rejected", "ok");
      // Where the decided row is now, not where it was when clicked: another decision may
      // have taken a row above it off the list meanwhile, or a re-read the row itself.
      const at = Math.max(0, rowsNow.current?.findIndex((r) => r.memory_key === key) ?? 0);
      const gone = rowsNow.current?.find((r) => r.memory_key === key);
      if (gone) decided.current.add(gone.memory_id);
      // Off the list at once, so its buttons cannot send the decision again.
      setRows((rs) => rs?.filter((r) => r.memory_key !== key) ?? rs);
      setReason(({ [key]: _gone, ...rest }) => rest);
      setTotal((t) => Math.max(0, t - 1));
      // Only when focus is still in the decided row or was lost: never pull it away from
      // a field the person moved to while the decision was out.
      const active = document.activeElement;
      if (!active || active === document.body || row?.contains(active)) focusAt.current = at;
      setReload((n) => n + 1);
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setDeciding((d) => {
        const next = new Set(d);
        next.delete(key);
        return next;
      });
    }
  };

  const shown = rows ? proposalsIn(rows) : 0;
  return (
    <div>
      <ListToolbar
        label="Proposals"
        search={false}
        count={
          !error && rows !== null
            ? total > shown
              ? `the newest ${shown} of ${total} proposals read`
              : `${shown} ${shown === 1 ? "proposal" : "proposals"}`
            : undefined
        }
      />
      {!!error && (
        <section ref={errorBox} tabIndex={-1} aria-label="Proposals">
          <ListState kind="error" things="proposals" error={error} onRetry={() => setReload((n) => n + 1)} />
        </section>
      )}
      {!error && rows === null && (
        <LoadingSkeleton label="Loading proposals">
          <Skeleton className="h-40 w-full" />
        </LoadingSkeleton>
      )}
      {!error && rows !== null && (
        // One section for the empty state and the rows, so focus on it survives either
        // turning into the other.
        <section
          ref={listBox}
          tabIndex={-1}
          aria-label="Proposals"
          className="space-y-2"
          aria-busy={refreshing || undefined}
        >
          {rows.length === 0 && (
            <ListState
              kind="empty"
              icon="user"
              title="No proposals"
              description="A proposal is a fact an agent wrote for review (memory.set with proposed=true). Searches leave it out until someone accepts it here."
            />
          )}
          {rows.map((r) => (
            <Panel
              key={r.memory_key}
              ref={(el) => {
                if (el) rowBoxes.current.set(r.memory_key, el);
                else rowBoxes.current.delete(r.memory_key);
              }}
            >
              <div className="flex flex-wrap items-center justify-between gap-3 mb-2">
                <Link
                  component="button"
                  type="button"
                  size="sm"
                  weight="mono"
                  className="min-w-0 break-all text-left"
                  aria-label={`Open ${r.memory_key} in full`}
                  onClick={() => inspect(r.memory_key, { agent: r.agent })}
                >
                  {r.memory_key}
                </Link>
                <span className="flex items-center gap-2 text-xs text-text-muted tabular">
                  {r.agent && <AgentBadge agent={r.agent} quiet />}
                  {r.created_at && <ListTime time={r.created_at} quiet />}
                </span>
              </div>
              {r.older > 0 && (
                <div className="text-xs text-text-muted mb-2">
                  {plural(r.older, "older proposal")} of this key; a decision applies to the newest, shown here.
                </div>
              )}
              {r.sensitivity === "high" ? (
                <div className="text-sm text-text-muted mb-2">{SENSITIVE_READ_BY_KEY}</div>
              ) : (
                <div className="text-sm text-text mb-2 whitespace-pre-wrap break-words">
                  {r.value_preview ?? ""}
                  {/* The server sends the first 400 characters, so a value of exactly 400 is
                      marked too: the key opens the whole value. */}
                  {[...(r.value_preview ?? "")].length >= PREVIEW_CHARS && "…"}
                </div>
              )}
              <div className="text-xs text-text-muted mb-2">
                {trialLabel(r)}
                {(r.conflict_with?.length ?? 0) > 0 && (
                  <> · contradicts {r.conflict_with?.length === 1 ? "1 fact" : `${r.conflict_with?.length} facts`}</>
                )}
              </div>
              {mayDecide && (
                <>
                  <TextAreaField
                    label={`Reason for the decision on ${r.memory_key}`}
                    value={reason[r.memory_key] || ""}
                    onChange={(value) => setReason((m) => ({ ...m, [r.memory_key]: value }))}
                    placeholder="Optional — its first 200 characters go in the audit trail; a rejection keeps it whole"
                    rows={2}
                    maxLength={512}
                  />
                  <div className="mt-2 flex items-center justify-end gap-2">
                    <BusyButton
                      busy={deciding.has(r.memory_key)}
                      type="secondary"
                      htmlType="button"
                      iconLeft="cross"
                      aria-label={`Reject ${r.memory_key}`}
                      onClick={() => decide(r.memory_key, "reject")}
                    >
                      Reject
                    </BusyButton>
                    <BusyButton
                      busy={deciding.has(r.memory_key)}
                      type="secondary"
                      htmlType="button"
                      iconLeft="check"
                      aria-label={`Accept ${r.memory_key}`}
                      onClick={() => decide(r.memory_key, "accept")}
                    >
                      Accept
                    </BusyButton>
                  </div>
                </>
              )}
            </Panel>
          ))}
        </section>
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

/** The earlier of two audit times; "" is none yet. */
const earlierTime = (a: string, b: string): string => (a === "" || auditInstant(b) < auditInstant(a) ? b : a);

/** Newest restore per id across two reads. */
function mergeUndone(a: Map<string, string>, b: Map<string, string>): Map<string, string> {
  const out = new Map(a);
  for (const [id, ts] of b) {
    const prev = out.get(id);
    if (!prev || auditInstant(ts) > auditInstant(prev)) out.set(id, ts);
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
  const [loadError, setLoadError] = useState<unknown>(null);
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
  // The last page removes Load more, which held the focus: it goes to the first Undo that
  // page added, else the last one in the list.
  const listBox = useRef<HTMLDivElement>(null);
  const focusAdded = useRef<number | null>(null);
  const oldest = (rows ?? []).map((r) => r.ts).reduce(earlierTime, "");

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
          if (live()) setLoadError(e);
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
          // Unknown: offer Undo. A row an Undo restored answers restored; one changed since
          // answers restored=false.
          if (live()) setUndoneSince(oldest);
        });
    },
    [identity.org, identity.namespace, oldest],
  );

  // A row retired again after an undo is retired, so the undo must be the later event, and
  // the row must no longer be retired behind that memory: an Undo record whose restore
  // failed names a row still retired. With the row's state unknown, Undo is offered: a
  // repeat Undo of a restored row answers restored.
  const undoneBefore = (r: AutoResolvedRow) => {
    const at = undone.get(r.memory_id);
    const stillRetired =
      r.current == null || (r.current.status === "superseded" && r.current.superseded_by === r.superseded_by);
    return at != null && auditInstant(at) >= auditInstant(r.ts) && !stillRetired;
  };
  const known = (r: AutoResolvedRow) => undoneSince != null && auditInstant(r.ts) >= auditInstant(undoneSince);

  useEffect(() => {
    const at = focusAdded.current;
    if (at === null || loadingMore) return;
    focusAdded.current = null;
    if (cursor !== null || document.activeElement !== document.body) return;
    const added = listBox.current?.querySelectorAll<HTMLElement>("tbody tr") ?? [];
    for (const tr of [...added].slice(at)) {
      const button = tr.querySelector<HTMLElement>("button");
      if (button) return button.focus();
    }
    [...(listBox.current?.querySelectorAll<HTMLElement>("tbody button") ?? [])].at(-1)?.focus();
  });

  const loadMore = async () => {
    if (!cursor || loadingRef.current) return;
    loadingRef.current = true;
    focusAdded.current = rows?.length ?? 0;
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
        { org: identity.org, namespace: identity.namespace, agent: r.agent },
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
      return <span className="text-sm text-text-muted">Changed since</span>;
    }
    if (o === "restored" || undoneBefore(r)) {
      return (
        <span className="inline-flex items-center gap-1 text-sm text-success">
          <Icon name="check" size="sm" aria-hidden="true" /> Restored
        </span>
      );
    }
    // Without the row it retired behind, an Undo could only be an unguarded restore.
    if (!canRestore || !r.superseded_by) return null;
    if (!known(r)) {
      return <span className="text-sm text-text-muted">Checking…</span>;
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
      <ListState kind="error" things="retired memories" error={loadError} onRetry={() => setReload((n) => n + 1)} />
    );
  }
  if (rows !== null && rows.length === 0 && !cursor) {
    return (
      <ListState
        kind="empty"
        icon="secure"
        title="Nothing retired automatically"
        description="Memories Loom retires on its own in the last 30 days show here, each with an Undo."
      />
    );
  }
  return (
    <div>
      <p className="text-sm text-text-muted mb-3 max-w-2xl">
        Memories Loom retired on its own in the last 30 days. They no longer come back in recall, but history keeps
        them. Undo brings one back.
      </p>
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
      {/* The first read is the table's header over a skeleton. */}
      <div ref={listBox}>
        <Table
          aria-label="Auto-resolved memories"
          aria-busy={rows === null || loadingMore || undefined}
          noDataMessage={rows === null ? <TableSkeleton label="Loading retired memories" /> : undefined}
          size="sm"
          headers={[
            { label: "Key" },
            { label: "Why" },
            { label: "Behind" },
            { label: "Retired", width: "120px" },
            { label: <span className="sr-only">Undo</span>, width: "140px" },
          ]}
          rows={(rows ?? []).map((r) => ({
            id: entryKey(r),
            items: [
              { label: <span className="block min-w-0 truncate font-mono text-xs">{r.memory_key}</span> },
              {
                label: (
                  <span className="text-sm">
                    {autoResolveReasonText(r.reason) ?? "Retired automatically"}
                    {r.unconfirmed ? <span className="text-text-muted"> · may not have taken effect</span> : null}
                  </span>
                ),
              },
              {
                label: (
                  <span className="block min-w-0 truncate font-mono text-xs">
                    {r.superseded_by_key || r.superseded_by || "—"}
                  </span>
                ),
              },
              { label: <ListTime time={r.ts} quiet className="text-text-muted" /> },
              { label: status(r) },
            ],
          }))}
        />
      </div>
      {cursor ? (
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
      ) : (
        rows !== null && <p className="mt-3 text-sm text-text-muted">Nothing older within 30 days.</p>
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
  useResetOnKey(`${identity.org}/${identity.namespace}`, () => {
    setRows(null);
    setError(null);
  });

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
      // The card never shows a high peer's value, so the answer never carries one.
      getMemoryById(identity.org, peerId, { signal, omit_high_value: true })
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
      // A high peer's value is read by key in Memories, as side A's is.
      sensitive={peer.sensitivity === "high"}
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
