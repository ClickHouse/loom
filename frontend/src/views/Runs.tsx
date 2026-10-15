import { useEffect, useMemo, useRef, useState } from "react";
import { ListChecks, X as XIcon, CheckCircle2, XCircle, CircleDashed, Ban, Layers } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { getRun, listRuns, type RunGetResponse, type RunRow, type RunStatus } from "../api";
import { useStore, useIdentity } from "../lib/state";
import { SkeletonCard } from "../components/Skeleton";
import { Chip, Panel, SectionHeading } from "../components/ui";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { displayAgent, humanizeError, relativeTime, truncate } from "../lib/format";
import { PageContainer } from "../layout/PageContainer";

// The nine fields a run row reads of the twenty a run carries; run.get carries the rest
// for the opened run.
export const RUNS_FIELDS = [
  "run_id",
  "agent",
  "session_id",
  "goal",
  "status",
  "started_at",
  "duration_ms",
  "memories_used",
  "memories_written",
  "metadata",
];

/**
 * Runs — the ledger of agent executions.
 *
 * One row per run: what the agent set out to do, how it ended, which
 * memories it RELIED on (its own report), what it wrote (derived from
 * loom.memory.run_id), and — in the replay — what it recalled, observed
 * and cost. Everything except goal / status / memories_used is derived at
 * read time by run.get, so this view never disagrees with the planes it
 * summarises.
 *
 * The run is the primary object; a SESSION is not a separate tab but a
 * grouping of the runs that shared one connection (1 session → N runs).
 * "Group by session" clusters the table under session headers. Runs loom
 * only inferred from a connection that did no memory work (implicit MCP
 * runs, idle-closed, nothing recalled or written) are "connection-only": hidden by
 * default so empty shells don't masquerade as real agent work, revealable with one
 * toggle.
 *
 * Each row's goal is a `<button>` that opens the replay — reachable by
 * Tab, activated by Enter or Space, with an accent focus ring. Clicking anywhere else on
 * the row still opens it, as a mouse convenience; the row itself is not a control.
 */

type Filter = "" | RunStatus;

const RUN_FILTERS: readonly Filter[] = ["", "active", "completed", "failed", "cancelled"];

const STATUS_META: Record<RunStatus, { label: string; cls: string; Icon: typeof CheckCircle2 }> = {
  active: { label: "active", cls: "text-[var(--color-text-muted)] border-[var(--color-border)]", Icon: CircleDashed },
  completed: { label: "completed", cls: "text-success border-success/40", Icon: CheckCircle2 },
  failed: { label: "failed", cls: "text-danger border-danger/40", Icon: XCircle },
  cancelled: { label: "cancelled", cls: "text-warn border-warn/40", Icon: Ban },
};

function StatusChip({ status, inferred }: { status: RunStatus; inferred?: boolean }) {
  const meta = STATUS_META[status] || STATUS_META.active;
  const Icon = meta.Icon;
  // An implicit run's "completed" only means the connection went idle, not
  // that the task succeeded — render it muted, not green.
  const cls = inferred && status === "completed" ? STATUS_META.active.cls : meta.cls;
  const label = inferred && status === "completed" ? "idle-closed" : meta.label;
  return (
    <span
      className={`inline-flex items-center gap-1 text-2xs px-2 py-0.5 rounded-full border ${cls}`}
      title={
        inferred && status === "completed"
          ? "loom inferred this run from a connection; closed on idle, not by run.complete — not a reported outcome"
          : undefined
      }
    >
      <Icon size={12} /> {label}
    </span>
  );
}

function fmtDuration(ms: number): string {
  if (!ms || ms <= 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 90) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function metaBool(m: Record<string, unknown> | undefined, k: string): boolean {
  return Boolean(m && m[k] === true);
}

/** A run with no memory activity: nothing relied on, nothing written under
 * its run_id. This holds for an implicit MCP run AND for a run a program opened and
 * closed with nothing threaded in between — thousands of such rows a week can read as
 * green "completed" work. Both are folded by default and revealed with one toggle; the
 * implicit ones keep their own "auto" / "idle-closed" wording. */
export function hasNoMemoryActivity(r: RunRow): boolean {
  // A projected row carries only the fields the page asked for; a list field the request
  // forgot reads as [] here, never as a crash.
  return (r.memories_used ?? []).length === 0 && (r.memories_written ?? []).length === 0;
}

/** What each empty field on the replay would have needed. The reader is usually the
 * person who can fix the caller, so every empty says who fills it and how, instead of a
 * dash. */
export const EMPTY_WHY = {
  used: "none reported — only the agent can say which memories it relied on; it names them in run.complete(memories_used)",
  written: "none — a write counts here when memory.set, memory.update or memory.link carried this run_id",
  recalledOff: "n/a — per-query recall telemetry (LOOM_SEARCH_TRACE_LOG_ENABLED) is off on this deployment",
  recalledZero:
    "the trace log is on and no memory.search carried this run — a read labels its run with the X-Loom-Run-Id header (the SDK's run scope and the plugin do), or the caller lists what it recalled in run.complete(recalled_keys)",
  observations: "observe.record rows tagged with this run_id (the plugin's capture hook writes them)",
  noSession:
    "run.begin carried no top-level session_id, so model turns and cost cannot be joined (a session_id inside metadata is not read)",
  sessionCost:
    "the turns the run's session made while the run was open, from the usage the Stop hook posts (cost.record_turns)",
  budget: "set by run.begin(budget_usd, budget_seconds); a signal for the agent, never a stop",
  unpriced: "turns from a model the price table does not know: they cost 0, so they never count toward budget_usd",
} as const;

/** The budget run.begin set and the time used against it, e.g. "$1.00 · 30m 0s · 42s
 * elapsed"; "—" when it set none. Read off run.get: run.list rows are projected and do
 * not carry it. */
function budgetText(detail: RunGetResponse): string {
  const run = detail.run;
  const parts: string[] = [];
  if (run?.budget_usd) parts.push(`$${run.budget_usd.toFixed(run.budget_usd < 0.01 ? 4 : 2)}`);
  if (run?.budget_seconds) {
    parts.push(fmtDuration(run.budget_seconds * 1000));
    const elapsed = detail.elapsed_seconds ?? 0;
    parts.push(`${elapsed > 0 ? fmtDuration(elapsed * 1000) : "0s"} elapsed`);
  }
  return parts.length > 0 ? parts.join(" · ") : "—";
}

function KeyChipButtons({ keys, empty }: { keys: string[]; empty: string }) {
  const inspect = useStore((s) => s.inspect);
  if (!keys.length) return <span className="text-xs text-[var(--color-text-subtle)]">{empty}</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {keys.map((k) => (
        <button
          key={k}
          type="button"
          onClick={() => inspect(k, { agent: "*" })}
          title="Open this memory"
          className="text-2xs font-mono px-2 py-0.5 rounded-full border border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-accent)] text-[var(--color-text)]"
        >
          {k}
        </button>
      ))}
    </div>
  );
}

interface SessionGroup {
  session_id: string;
  label: string;
  runs: RunRow[];
  used: number;
  written: number;
  latest: string;
}

function bySession(rows: RunRow[]): SessionGroup[] {
  const map = new Map<string, SessionGroup>();
  for (const r of rows) {
    const sid = r.session_id && r.session_id !== "-" ? r.session_id : "";
    const key = sid || "(no session)";
    let g = map.get(key);
    if (!g) {
      g = { session_id: sid, label: key, runs: [], used: 0, written: 0, latest: r.started_at };
      map.set(key, g);
    }
    g.runs.push(r);
    g.used += (r.memories_used ?? []).length;
    g.written += (r.memories_written ?? []).length;
    if (r.started_at > g.latest) g.latest = r.started_at;
  }
  return [...map.values()].sort((a, b) => (a.latest < b.latest ? 1 : -1));
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Panel tone="surface-2" header={<SectionHeading label={title} />}>
      {children}
    </Panel>
  );
}

function RunRowTr({ r, onOpen }: { r: RunRow; onOpen: (r: RunRow) => void }) {
  const inferred = metaBool(r.metadata, "implicit");
  const dim = hasNoMemoryActivity(r);
  return (
    <tr
      onClick={() => onOpen(r)}
      className={`border-t border-[var(--color-border)] hover:bg-[var(--color-surface-2)] cursor-pointer ${dim ? "opacity-55" : ""}`}
    >
      <td className="px-3 py-2 max-w-[48ch]">
        <div className="truncate flex items-center gap-2" title={r.goal}>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onOpen(r);
            }}
            className="min-w-0 max-w-full truncate text-left rounded outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
          >
            {r.goal || <span className="text-[var(--color-text-subtle)]">(no goal)</span>}
          </button>
          {inferred && (
            <span
              className="text-2xs px-1.5 py-px rounded border border-[var(--color-border)] text-[var(--color-text-subtle)] uppercase tracking-wide"
              title="loom inferred this run from a connection; the agent did not declare it with run.begin"
            >
              auto
            </span>
          )}
        </div>
        <div className="text-2xs font-mono text-[var(--color-text-subtle)] truncate">
          {dim ? (inferred ? "connection only — no memory activity" : "no memory activity") : r.run_id}
        </div>
      </td>
      <td className="px-3 py-2 whitespace-nowrap">{displayAgent(r.agent)}</td>
      <td className="px-3 py-2">
        <StatusChip status={r.status} inferred={inferred} />
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{(r.memories_used ?? []).length}</td>
      <td className="px-3 py-2 text-right tabular-nums">{(r.memories_written ?? []).length}</td>
      <td className="px-3 py-2 text-right tabular-nums whitespace-nowrap">{fmtDuration(r.duration_ms)}</td>
      <td className="px-3 py-2 text-right whitespace-nowrap text-[var(--color-text-muted)]" title={r.started_at}>
        {relativeTime(r.started_at)}
      </td>
    </tr>
  );
}

function RunTable({ rows, onOpen, head = true }: { rows: RunRow[]; onOpen: (r: RunRow) => void; head?: boolean }) {
  return (
    <table className="w-full text-sm">
      {head && (
        <thead className="bg-[var(--color-surface-2)] text-2xs uppercase tracking-wide text-[var(--color-text-muted)]">
          <tr>
            <th className="text-left px-3 py-2 font-medium">Goal</th>
            <th className="text-left px-3 py-2 font-medium">Agent</th>
            <th className="text-left px-3 py-2 font-medium">Status</th>
            <th className="text-right px-3 py-2 font-medium">Relied on</th>
            <th className="text-right px-3 py-2 font-medium">Wrote</th>
            <th className="text-right px-3 py-2 font-medium">Took</th>
            <th className="text-right px-3 py-2 font-medium">Started</th>
          </tr>
        </thead>
      )}
      <tbody>
        {rows.map((r) => (
          <RunRowTr key={r.run_id} r={r} onOpen={onOpen} />
        ))}
      </tbody>
    </table>
  );
}

export function Runs() {
  const identity = useIdentity();
  const [rows, setRows] = useState<RunRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("");
  // Filter by agent; the options come from the last unfiltered read so the select does
  // not collapse to its own choice.
  const [agent, setAgent] = useState("");
  const [agents, setAgents] = useState<string[]>([]);
  const [groupBy, setGroupBy] = useState(false);
  const [hideConn, setHideConn] = useState(true);
  const openRecallForRun = useStore((s) => s.openRecallForRun);
  const [open, setOpen] = useState<RunRow | null>(null);
  // The goal button (or row) that opened the replay — refocused on close,
  // since a triggerless Radix dialog restores nothing on its own.
  const openerRef = useRef<HTMLElement | null>(null);
  const openRun = (r: RunRow) => {
    const el = document.activeElement;
    openerRef.current = el instanceof HTMLElement ? el : null;
    setOpen(r);
  };
  const [detail, setDetail] = useState<RunGetResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // A response that lands after the filter, agent or namespace moved on is dropped, so
  // A's rows never render under B's filter. The resets stay synchronous in the effect, so
  // the old rows never linger (clickable) for a tick after the change.
  useEffect(() => {
    let live = true;
    setRows(null);
    setError(null);
    // The no-memory-activity fold is a run.list filter, not a slice of the fetched page.
    // Folding after the server's LIMIT can leave a chatty consumer's page empty — its
    // newest 100 runs all hollow while hundreds of filled runs sit further back.
    listRuns(
      { org: identity.org, namespace: identity.namespace },
      { status: filter, agent, limit: 100, fields: RUNS_FIELDS, memoryActivity: hideConn ? "with" : "" },
    )
      .then((rs) => {
        if (!live) return;
        setRows(rs);
        if (!agent) setAgents([...new Set(rs.map((r) => r.agent).filter(Boolean))].sort());
      })
      .catch((e) => {
        if (!live) return;
        setError(humanizeError(e));
        setRows([]);
      });
    return () => {
      live = false;
    };
  }, [identity.org, identity.namespace, filter, agent, hideConn]);

  // Opening run Y after X drops X's run.get answer instead of showing it in Y's drawer.
  useEffect(() => {
    if (!open) return;
    let live = true;
    setDetail(null);
    setDetailLoading(true);
    getRun({ org: identity.org, namespace: identity.namespace }, open.run_id)
      .then((d) => {
        if (live) setDetail(d);
      })
      .catch(() => {
        if (live) setDetail(null);
      })
      .finally(() => {
        if (live) setDetailLoading(false);
      });
    return () => {
      live = false;
    };
  }, [open, identity.org, identity.namespace]);

  // The server already applied the fold (memory_activity: "with"); hollow rows
  // only arrive when the reader asked for them, and then they render dimmed.
  const visible = useMemo(() => rows || [], [rows]);
  const groups = useMemo(() => (groupBy ? bySession(visible) : []), [groupBy, visible]);

  const counts = { total: visible.length, completed: 0, failed: 0, used: 0, written: 0 };
  for (const r of visible) {
    if (r.status === "completed" && !metaBool(r.metadata, "implicit")) counts.completed += 1;
    if (r.status === "failed") counts.failed += 1;
    counts.used += (r.memories_used ?? []).length;
    counts.written += (r.memories_written ?? []).length;
  }

  return (
    <PageContainer>
      <div className="flex items-baseline gap-3 mb-2">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <ListChecks size={20} /> Runs
        </h1>
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <p className="text-sm text-[var(--color-text-muted)] mb-5 max-w-[70ch]">
        One row per agent execution: the goal, how it ended, the memories the agent relied on, and what it wrote. Open a
        run to replay what it recalled, observed and cost. A session is one connection's worth of runs — group by it to
        see a whole working sitting.
      </p>

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {RUN_FILTERS.map((f) => (
          <Chip key={f || "all"} label={f || "all"} selected={filter === f} onClick={() => setFilter(f)} />
        ))}
        <span className="w-px h-4 bg-[var(--color-border)] mx-1" />
        <Chip label="Group by session" selected={groupBy} onClick={() => setGroupBy((v) => !v)} />
        <Chip
          label={hideConn ? "Show runs with no memory activity" : "Hide runs with no memory activity"}
          selected={!hideConn}
          onClick={() => setHideConn((v) => !v)}
        />
        {(agents.length > 1 || agent) && (
          <select
            aria-label="Agent"
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            className="px-2 py-1 text-xs rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
          >
            <option value="">every agent</option>
            {agents.map((a) => (
              <option key={a} value={a}>
                {displayAgent(a)}
              </option>
            ))}
          </select>
        )}
        {rows && visible.length > 0 && (
          <span className="ml-auto text-xs text-[var(--color-text-muted)]">
            {counts.total} run{counts.total === 1 ? "" : "s"} · {counts.completed} completed · {counts.failed} failed ·{" "}
            {counts.used} memories relied on · {counts.written} written
          </span>
        )}
      </div>

      {rows === null && (
        <div className="space-y-3">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}
      {rows !== null && error && <div className="text-sm text-danger mb-4">{error}</div>}
      {rows !== null && !error && rows.length === 0 && !hideConn && (
        <EmptyState
          icon={ListChecks}
          title="No runs yet"
          description="Agents open a run with run.begin (the Claude Code plugin does it on the first prompt of a session) and close it with run.complete, naming the memories they relied on."
        />
      )}
      {rows !== null && !error && rows.length === 0 && hideConn && (
        <EmptyState
          icon={ListChecks}
          title="No runs with memory activity"
          description={
            'Runs that neither relied on nor wrote a memory under their run_id are folded. Use "Show runs with no memory activity" to see them. A run fills up when the caller passes run_id on memory.set and names what it relied on in run.complete(memories_used).'
          }
        />
      )}

      {/* Flat: run-first table. */}
      {rows !== null && !groupBy && visible.length > 0 && (
        <div className="rounded-xl border border-[var(--color-border)] overflow-hidden">
          <RunTable rows={visible} onOpen={openRun} />
        </div>
      )}

      {/* Grouped: sessions are headers, runs are their timeline. */}
      {rows !== null && groupBy && visible.length > 0 && (
        <div className="space-y-4">
          {groups.map((g) => (
            <div key={g.label} className="rounded-xl border border-[var(--color-border)] overflow-hidden">
              <div className="flex items-center justify-between gap-3 px-3 py-2 bg-[var(--color-surface-2)] border-b border-[var(--color-border)]">
                <div className="min-w-0 flex items-center gap-2 text-xs">
                  <Layers size={13} className="text-[var(--color-text-muted)] shrink-0" />
                  <span className="font-mono truncate" title={g.session_id || "(no session)"}>
                    {g.session_id ? truncate(g.session_id, 32) : "(no session)"}
                  </span>
                  <span className="text-[var(--color-text-subtle)]">·</span>
                  <span className="text-[var(--color-text-muted)]">
                    {g.runs.length} run{g.runs.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="text-2xs text-[var(--color-text-muted)] whitespace-nowrap">
                  {g.used} relied on · {g.written} written · {relativeTime(g.latest)}
                </div>
              </div>
              <RunTable rows={g.runs} onOpen={openRun} head={false} />
            </div>
          ))}
        </div>
      )}

      {/* Replay dialog: the stored row plus what run.get derives live. */}
      <Dialog.Root
        open={open !== null}
        onOpenChange={(v) => {
          if (!v) {
            setOpen(null);
            setDetail(null);
          }
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
          <Dialog.Content
            onCloseAutoFocus={(e) => {
              e.preventDefault();
              openerRef.current?.focus();
            }}
            className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(820px,94vw)] h-[min(85vh,800px)] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!"
          >
            <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
              <div className="min-w-0 flex-1">
                <Dialog.Title className="text-md font-semibold tracking-tight truncate">
                  {open?.goal || "Run"}
                </Dialog.Title>
                <div className="text-2xs text-[var(--color-text-muted)] mt-1 font-mono truncate">{open?.run_id}</div>
                {open && (
                  <div className="text-2xs text-[var(--color-text-muted)] mt-1 flex items-center gap-2 flex-wrap">
                    <StatusChip status={open.status} inferred={metaBool(open.metadata, "implicit")} />
                    <span>{displayAgent(open.agent)}</span>
                    {open.session_id && open.session_id !== "-" && (
                      <>
                        <span>·</span>
                        <span className="font-mono">session {truncate(open.session_id, 24)}</span>
                      </>
                    )}
                    <span>·</span>
                    <span>started {relativeTime(open.started_at)}</span>
                    {open.duration_ms > 0 && (
                      <>
                        <span>·</span>
                        <span>took {fmtDuration(open.duration_ms)}</span>
                      </>
                    )}
                    {open.confidence > 0 && (
                      <>
                        <span>·</span>
                        <span>confidence {open.confidence.toFixed(2)}</span>
                      </>
                    )}
                  </div>
                )}
              </div>
              <Dialog.Close
                className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Close"
              >
                <XIcon size={16} />
              </Dialog.Close>
            </div>
            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
              {open && hasNoMemoryActivity(open) && metaBool(open.metadata, "implicit") && (
                <div className="text-xs text-[var(--color-text-muted)] p-3 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)]">
                  This run was inferred from a connection (implicit MCP session) that did no memory work — it recalled
                  and wrote nothing, and closed on idle rather than by run.complete. Its "completed" status is not a
                  reported outcome.
                </div>
              )}
              {open && hasNoMemoryActivity(open) && !metaBool(open.metadata, "implicit") && (
                <div className="text-xs text-[var(--color-text-muted)] p-3 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)]">
                  This run reported no memory activity: the caller opened it with run.begin and closed it with
                  run.complete{open.duration_ms > 0 ? ` ${fmtDuration(open.duration_ms)} later` : ""}, naming nothing it
                  relied on, and no write carried its run_id. The fields below say what each one would have needed.
                </div>
              )}
              {open?.user_request && (
                <Section title="User request">
                  <div className="text-sm whitespace-pre-wrap">{open.user_request}</div>
                </Section>
              )}
              {open?.expected_outcome && (
                <Section title="Expected outcome">
                  <div className="text-sm whitespace-pre-wrap">{open.expected_outcome}</div>
                </Section>
              )}
              <Section title="Memories the agent relied on">
                <KeyChipButtons keys={open?.memories_used || []} empty={EMPTY_WHY.used} />
              </Section>
              <Section title="Memories this run wrote">
                <KeyChipButtons
                  keys={detail?.memories_written || open?.memories_written || []}
                  empty={EMPTY_WHY.written}
                />
              </Section>
              <Section title="Recalled, observed, cost">
                {detailLoading && <div className="text-xs text-[var(--color-text-muted)]">Loading…</div>}
                {!detailLoading &&
                  detail &&
                  open &&
                  (() => {
                    // The server's names_session: blank or "-" is no session.
                    const session = (open.session_id ?? "").trim();
                    const hasSession = session !== "" && session !== "-";
                    const over = detail.over_budget ?? [];
                    return (
                      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-sm">
                        <div>
                          <div className="text-2xs text-[var(--color-text-muted)]">recalled</div>
                          <div className="tabular-nums">
                            {detail.recalled < 0 ? (
                              <span title={EMPTY_WHY.recalledOff}>n/a</span>
                            ) : detail.recalled === 0 ? (
                              <span title={EMPTY_WHY.recalledZero} className="underline decoration-dotted">
                                0
                              </span>
                            ) : (
                              detail.recalled
                            )}
                          </div>
                        </div>
                        <div>
                          <div className="text-2xs text-[var(--color-text-muted)]">observations</div>
                          <div className="tabular-nums" title={EMPTY_WHY.observations}>
                            {detail.observations}
                          </div>
                        </div>
                        <div>
                          <div className="text-2xs text-[var(--color-text-muted)]">turns</div>
                          <div
                            className="tabular-nums"
                            title={hasSession ? EMPTY_WHY.sessionCost : EMPTY_WHY.noSession}
                          >
                            {hasSession ? (
                              <>
                                {detail.turns}
                                {(detail.unpriced_turns ?? 0) > 0 && (
                                  <span className="text-[var(--color-text-subtle)]" title={EMPTY_WHY.unpriced}>
                                    {" "}
                                    ({detail.unpriced_turns} unpriced)
                                  </span>
                                )}
                              </>
                            ) : (
                              <span className="text-[var(--color-text-subtle)]">no session</span>
                            )}
                          </div>
                        </div>
                        <div>
                          <div className="text-2xs text-[var(--color-text-muted)]">cost</div>
                          <div
                            className="tabular-nums"
                            title={hasSession ? EMPTY_WHY.sessionCost : EMPTY_WHY.noSession}
                          >
                            {!hasSession ? (
                              <span className="text-[var(--color-text-subtle)]">no session</span>
                            ) : detail.cost_usd > 0 ? (
                              `$${detail.cost_usd.toFixed(4)}`
                            ) : (
                              "—"
                            )}
                          </div>
                        </div>
                        <div>
                          <div className="text-2xs text-[var(--color-text-muted)]">budget</div>
                          <div className="tabular-nums" title={EMPTY_WHY.budget}>
                            {budgetText(detail)}
                            {over.length > 0 && (
                              <span className="text-[var(--color-danger)]"> · over {over.join(" and ")}</span>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })()}
                {!detailLoading && detail && detail.recalled_keys.length > 0 && (
                  <div className="mt-3">
                    <div className="text-2xs text-[var(--color-text-muted)] mb-1">recalled keys</div>
                    <KeyChipButtons keys={detail.recalled_keys} empty="" />
                  </div>
                )}
                {!detailLoading && detail && open && detail.recalled >= 0 && (
                  <button
                    type="button"
                    onClick={() => openRecallForRun(open.run_id)}
                    className="mt-3 text-xs text-[var(--color-accent)] hover:underline"
                  >
                    Recall history for this run →
                  </button>
                )}
                {!detailLoading && !detail && (
                  <div className="text-xs text-[var(--color-text-subtle)]">Could not load the replay.</div>
                )}
              </Section>
              {open?.final_response && (
                <Section title="Final response">
                  <div className="text-sm whitespace-pre-wrap">{open.final_response}</div>
                </Section>
              )}
              {open && Object.keys(open.metadata || {}).length > 0 && (
                <Section title="Metadata">
                  <pre className="text-2xs font-mono whitespace-pre-wrap break-all">
                    {JSON.stringify(open.metadata, null, 2)}
                  </pre>
                </Section>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </PageContainer>
  );
}
