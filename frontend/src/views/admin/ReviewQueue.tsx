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
  proposeRevision,
  type RevisionSuggestion,
} from "../../api";
import { DuplicatesPanel } from "./Duplicates";
import { ProposalsPanel } from "./Proposals";
import { useIdentity, useStore } from "../../lib/state";
import { IconButton } from "../../components/IconButton";
import { Panel } from "../../components/ui/Panel";
import { LoadingSkeleton, PageHeader, SectionHeading, TabTrigger } from "../../components/ui";
import { Skeleton, TableSkeleton } from "../../components/Skeleton";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { ScopeBadge } from "../../components/ScopeBadge";
import { toast, toastError } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { formatValue, truncate, SENSITIVE_READ_BY_KEY } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { canCall } from "../../lib/canCall";
import { sharedHasAutoResolved } from "../../lib/sharedReads";
import { useRowNav } from "../../lib/useRowNav";
import { PageContainer } from "../../layout/PageContainer";

/**
 * Review queue — the memory decisions waiting for a person, one sub-tab each:
 *   - Conflicts:   pairs of memories that disagree on a value
 *   - Learnings:   facts the extractor proposed for promotion
 *   - Auto-resolved: memories Loom retired on its own (`memory.auto_supersede`), each
 *                    with an Undo (`memory.restore`)
 *   - Exceptions:  allowlist of pairs we've explicitly said may coexist
 *   - Proposals:   facts on trial, accepted or rejected by hand
 *   - Duplicates:  the dedup worker's pairs, memories and documents, and the repairs
 *                  (admin only)
 */
// Radix's tab panel attributes, cleared while Conflicts is the only panel.
const LONE_PANEL = { role: undefined, "aria-labelledby": undefined, tabIndex: undefined };

type QueueTab = "conflicts" | "learnings" | "auto" | "exceptions" | "proposals" | "duplicates";
// One name per tab, for its trigger and the document title.
const QUEUE_TABS: Record<QueueTab, string> = {
  conflicts: "Conflicts",
  learnings: "Learnings",
  auto: "Auto-resolved",
  exceptions: "Exceptions",
  proposals: "Proposals",
  duplicates: "Duplicates",
};

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
  // The tab lists retirements and reads their Undo records (audit.list): asked only by a
  // role known to reach both.
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const autoReadable = canCall("memory.auto_retirements", role, whoami) && canCall("audit.list", role, whoami);
  // The duplicate lists are ADMIN; the tab is there for an admin whether or not a pair waits,
  // since its repairs run over the whole namespace.
  const hasDuplicates = canCall("memory.dedup_list", role, whoami) && canCall("knowledge.dedup_list", role, whoami);
  // Proposals are offered to a role that reads them, with no probe: the tab reads when it
  // opens, so the page costs no read for them.
  const hasProposals = canCall("memory.proposals", role, whoami);
  const tabbed = hasLearnings || hasExceptions || hasAuto || hasProposals || hasDuplicates;
  const [tab, setTab] = useState<QueueTab>("conflicts");
  // A tab that is no longer offered (its probe found nothing, or its access went) hands the
  // page back to Conflicts.
  const offered: Record<QueueTab, boolean> = {
    conflicts: true,
    learnings: hasLearnings,
    auto: hasAuto,
    exceptions: hasExceptions,
    proposals: hasProposals,
    duplicates: hasDuplicates,
  };
  const tabOffered = offered[tab];
  useEffect(() => {
    if (!tabOffered) setTab("conflicts");
  }, [tabOffered]);
  // A probe seeds its panel's first view once: a panel opened again (the tabs unmount an
  // inactive one) reads afresh, so a learning promoted or an exception removed since does
  // not come back from the probe.
  const [seedsUsed, setSeedsUsed] = useState({ learnings: false, exceptions: false });
  useAbortableEffect(
    (signal, live) => {
      setProbedLearnings(null);
      setProbedExceptions(null);
      setSeedsUsed({ learnings: false, exceptions: false });
      // A namespace switch starts from Conflicts: the previous namespace's probes (and a tab
      // they revealed) must not outlive it.
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
  useAbortableEffect(
    (_signal, live) => {
      setProbedAuto(null);
      if (!autoReadable) {
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
      <PageHeader title="Review queue" tab={tabbed ? QUEUE_TABS[tab] : undefined}>
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-5 max-w-2xl">
        Memory decisions waiting for a person: facts that disagree, learnings and proposals to accept or reject, and
        duplicates to settle. Most of the time nothing waits here; that's a feature.
      </p>
      {/* Conflicts mounts (and reads) once, at once; the sub-tabs join it when a probe finds
          rows or the role reaches them. Until then the lone panel is the page itself, not a
          tab panel without tabs. */}
      <Tabs
        value={tab}
        onValueChange={(t) => {
          setTab(t as QueueTab);
          if (t === "auto") setAutoOpened(true);
        }}
      >
        {tabbed && (
          <Tabs.TriggersList className="mb-5" aria-label="Review queue">
            <TabTrigger value="conflicts" icon="warning">
              {QUEUE_TABS.conflicts}
            </TabTrigger>
            {hasLearnings && (
              <TabTrigger value="learnings" icon="sparkle">
                {QUEUE_TABS.learnings}
              </TabTrigger>
            )}
            {hasAuto && (
              <TabTrigger value="auto" icon="flash">
                {QUEUE_TABS.auto}
              </TabTrigger>
            )}
            {hasExceptions && (
              <TabTrigger value="exceptions" icon="line-in-circle">
                {QUEUE_TABS.exceptions}
              </TabTrigger>
            )}
            {hasProposals && (
              <TabTrigger value="proposals" icon="question">
                {QUEUE_TABS.proposals}
              </TabTrigger>
            )}
            {hasDuplicates && (
              <TabTrigger value="duplicates" icon="copy">
                {QUEUE_TABS.duplicates}
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
        {hasProposals && (
          <Tabs.Content value="proposals">
            {/* Keyed by namespace: another namespace's proposals are a new panel. */}
            <ProposalsPanel key={scopeKey} />
          </Tabs.Content>
        )}
        {hasDuplicates && (
          <Tabs.Content value="duplicates">
            <DuplicatesPanel />
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
      toastError("Couldn't load more conflicts", e, { read: true });
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
      toastError("Couldn't load more", e, { read: true });
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
        {/* conflicts.propose_revision reads no high row, so a sensitive conflict gets none. */}
        {current.sensitivity !== "high" && <RevisionHint key={current.memory_id} memoryId={current.memory_id} />}
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

/** A suggestion in the words of the buttons above it; every suggestion is about A, the
 *  conflict on screen. */
const SUGGESTION_LABEL: Record<RevisionSuggestion["decision"], string> = {
  revise: "Keep A",
  contract: "Drop A",
  allow: "Allow both",
};

/** conflicts.propose_revision, read when asked: which decision fits the conflict, over
 *  every peer. It changes nothing. */
function RevisionHint({ memoryId }: { memoryId: string }) {
  const identity = useIdentity();
  // null until asked; `gone` when the conflict is no longer open.
  const [hint, setHint] = useState<RevisionSuggestion[] | "gone" | null>(null);
  const [busy, setBusy] = useState(false);
  const region = useRef<HTMLDivElement>(null);
  const ask = async () => {
    setBusy(true);
    try {
      const r = await proposeRevision({ org: identity.org, namespace: identity.namespace }, memoryId);
      setHint(r.found ? r.suggestions : "gone");
      // The link that had focus is gone; the answer takes it.
      region.current?.focus();
    } catch (e) {
      toastError("Couldn't read a suggestion", e, { read: true });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mt-3 text-sm">
      {hint === null && (
        <Link component="button" size="sm" onClick={() => void ask()} disabled={busy}>
          Suggest a decision
        </Link>
      )}
      {/* Mounted empty, so the answer is announced when it fills in. */}
      <div ref={region} role="status" tabIndex={-1} className="outline-none">
        {hint === "gone" && "This conflict is no longer open."}
        {Array.isArray(hint) && hint.length === 0 && "No suggestion for this conflict."}
        {Array.isArray(hint) && hint.length > 0 && <p className="text-text-muted">“Target” in a reason means A.</p>}
        {Array.isArray(hint) &&
          hint.map((s) => (
            <p key={s.decision}>
              <strong>{SUGGESTION_LABEL[s.decision]}</strong>: {s.rationale}
            </p>
          ))}
      </div>
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
      toastError("Failed", e);
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
      if (mounted.current) toastError("Failed", e);
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
      if (mounted.current) toastError("Failed", e);
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
      toastError("Failed", e);
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
