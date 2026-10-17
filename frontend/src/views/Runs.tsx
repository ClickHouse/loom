import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Alert } from "../components/ui/Alert";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { Table, type TableRowType } from "@clickhouse/click-ui/Table";
import { getRun, listRuns, type RunGetResponse, type RunRow, type RunStatus } from "../api";
import { useStore, useIdentity } from "../lib/state";
import { SkeletonCard } from "../components/Skeleton";
import { Chip, PageHeader, Panel, SectionHeading, Tip, TipTarget } from "../components/ui";
import { IconButton } from "../components/IconButton";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { displayAgent, humanizeError, relativeTime, truncate } from "../lib/format";
import { PageContainer } from "../layout/PageContainer";
import { Modal } from "../components/ui/Modal";

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

// Every agent filter; "" is not a value the Select can hold.
const ALL_AGENTS = "*";

const STATUS_META: Record<RunStatus, { label: string; state: BadgeState; icon: IconName }> = {
  active: { label: "Active", state: "neutral", icon: "sand-glass" },
  completed: { label: "Completed", state: "success", icon: "check-in-circle" },
  failed: { label: "Failed", state: "danger", icon: "cross" },
  cancelled: { label: "Cancelled", state: "warning", icon: "line-in-circle" },
};

function StatusBadge({ status, inferred }: { status: RunStatus; inferred?: boolean }) {
  // An implicit run's "completed" only means the connection went idle, not
  // that the task succeeded — render it neutral, not green.
  const idleClosed = inferred && status === "completed";
  const meta = idleClosed ? STATUS_META.active : STATUS_META[status] || STATUS_META.active;
  if (!idleClosed) return <Badge text={meta.label} state={meta.state} size="sm" />;
  return (
    <TipBadge
      text="Idle-closed"
      tip="Loom inferred this run from a connection; closed on idle, not by run.complete — not a reported outcome"
    />
  );
}

/** A neutral badge whose tooltip carries the detail; the wrapper takes focus so keyboard
 *  users reach the tip. */
function TipBadge({ text, tip }: { text: string; tip: string }) {
  return (
    <TipTarget tip={tip}>
      <Badge text={text} state="neutral" size="sm" />
    </TipTarget>
  );
}

/** One replay figure: its label, an (i) with what fills it, and the value. */
function Stat({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted flex items-center gap-0.5">
        {label}
        {help && <IconButton icon="info-in-circle" size="xs" label={`About ${label}`} tooltip={help} />}
      </dt>
      <dd className="tabular-nums m-0">{children}</dd>
    </div>
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

/** Dollars to the 6 places cost and budgets are kept to, so a spend just past its budget
 * never reads as at it: "$2.50", "$1.000004". */
function usd(amount: number): string {
  const text = amount.toLocaleString("en-US", { minimumFractionDigits: 6, maximumFractionDigits: 6 });
  return `$${text.replace(/0{1,4}$/, "")}`;
}

/** The budget run.begin set and the time used against it, e.g. "$1.00 · 30m 0s · 42s
 * elapsed"; "—" when it set none. Read off run.get: run.list rows are projected and do
 * not carry it. */
function budgetText(detail: RunGetResponse): string {
  const run = detail.run;
  const parts: string[] = [];
  if (run?.budget_usd) parts.push(usd(run.budget_usd));
  if (run?.budget_seconds) {
    parts.push(fmtDuration(run.budget_seconds * 1000));
    const elapsed = detail.elapsed_seconds ?? 0;
    parts.push(`${elapsed > 0 ? fmtDuration(elapsed * 1000) : "0s"} elapsed`);
  }
  return parts.length > 0 ? parts.join(" · ") : "—";
}

function KeyChipButtons({ keys, empty }: { keys: string[]; empty: string }) {
  const inspect = useStore((s) => s.inspect);
  if (!keys.length) return <span className="text-xs text-text-muted">{empty}</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {keys.map((k) => (
        <Chip key={k} label={k} mono tip="Open this memory" onClick={() => inspect(k, { agent: "*" })} />
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

// Under 769 px Click UI's Table lays each row out as a card of wrapping cells. There the
// goal takes the whole first line and the rest pair up two to a line, left-aligned; on
// the table, counts and durations read down a right edge.
const CARD = "[@media(max-width:768px)]:min-w-0 [@media(max-width:768px)]:basis-1/2";
const CARD_GOAL = "[@media(max-width:768px)]:basis-full";
const NUMERIC = "block w-full text-right [@media(max-width:768px)]:text-left tabular-nums";
const RUN_HEADERS = [
  { label: "Goal" },
  { label: "Agent", width: "160px" },
  { label: "Status", width: "140px" },
  { label: <span className={NUMERIC}>Relied on</span>, width: "96px" },
  { label: <span className={NUMERIC}>Wrote</span>, width: "80px" },
  { label: <span className={NUMERIC}>Took</span>, width: "96px" },
  { label: "Started", width: "120px" },
];

function runRow(r: RunRow, onOpen: (r: RunRow) => void): TableRowType {
  const inferred = metaBool(r.metadata, "implicit");
  const dim = hasNoMemoryActivity(r);
  return {
    id: r.run_id,
    onClick: () => onOpen(r),
    className: "cursor-pointer",
    items: [
      {
        className: CARD_GOAL,
        label: (
          <div className="min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <Tip content={r.goal} disabled={!r.goal}>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpen(r);
                  }}
                  className={`min-w-0 max-w-full truncate text-left rounded ${dim ? "text-text-muted" : ""}`}
                >
                  {r.goal || <span className="text-text-muted">(no goal)</span>}
                </button>
              </Tip>
              {inferred && (
                <TipBadge
                  text="Auto"
                  tip="Loom inferred this run from a connection; the agent did not declare it with run.begin"
                />
              )}
            </div>
            <div className="text-xs font-mono text-text-muted truncate">
              {dim ? (inferred ? "connection only — no memory activity" : "no memory activity") : r.run_id}
            </div>
          </div>
        ),
      },
      { className: CARD, label: displayAgent(r.agent) },
      { className: CARD, label: <StatusBadge status={r.status} inferred={inferred} /> },
      { className: CARD, label: <span className={NUMERIC}>{(r.memories_used ?? []).length}</span> },
      { className: CARD, label: <span className={NUMERIC}>{(r.memories_written ?? []).length}</span> },
      { className: CARD, label: <span className={NUMERIC}>{fmtDuration(r.duration_ms)}</span> },
      {
        className: CARD,
        label: (
          <time dateTime={r.started_at}>
            {relativeTime(r.started_at)}
            <span className="sr-only"> ({r.started_at})</span>
          </time>
        ),
      },
    ],
  };
}

// Grouped by session, every session's table keeps its header: a table without one leaves
// its cells unlabelled for a screen reader.
function RunTable({ rows, onOpen }: { rows: RunRow[]; onOpen: (r: RunRow) => void }) {
  return <Table headers={RUN_HEADERS} rows={rows.map((r) => runRow(r, onOpen))} size="sm" />;
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
      <PageHeader title="Runs">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-5 max-w-[70ch]">
        One row per agent execution: the goal, how it ended, the memories the agent relied on, and what it wrote. Open a
        run to replay what it recalled, observed and cost. A session is one connection's worth of runs — group by it to
        see a whole working sitting.
      </p>

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {/* Five statuses are wider than a phone: the group scrolls sideways, not the page. */}
        <div className="max-w-full overflow-x-auto">
          <ButtonGroup
            aria-label="Filter runs by status"
            selected={filter}
            onClick={(value) => setFilter(value as Filter)}
            options={RUN_FILTERS.map((f) => ({ value: f, label: f ? STATUS_META[f].label : "All" }))}
          />
        </div>
        <span className="w-px h-4 bg-border mx-1 max-md:hidden" />
        <Chip label="Group by session" selected={groupBy} onClick={() => setGroupBy((v) => !v)} />
        {/* One label; pressed means the folded runs are shown. */}
        <Chip label="Show runs with no memory activity" selected={!hideConn} onClick={() => setHideConn((v) => !v)} />
        {(agents.length > 1 || agent) && (
          <Select
            value={agent || ALL_AGENTS}
            onSelect={(v) => setAgent(v === ALL_AGENTS ? "" : v)}
            triggerProps={{ "aria-label": "Agent" }}
            className="w-48"
          >
            <Select.Item value={ALL_AGENTS}>Every agent</Select.Item>
            {agents.map((a) => (
              <Select.Item key={a} value={a}>
                {displayAgent(a)}
              </Select.Item>
            ))}
          </Select>
        )}
        {rows && visible.length > 0 && (
          <span className="ml-auto text-xs text-text-muted">
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
          icon="list-numbered"
          title="No runs yet"
          description="Agents open a run with run.begin (the Claude Code plugin does it on the first prompt of a session) and close it with run.complete, naming the memories they relied on."
        />
      )}
      {rows !== null && !error && rows.length === 0 && hideConn && (
        <EmptyState
          icon="list-numbered"
          title="No runs with memory activity"
          description={
            'Runs that neither relied on nor wrote a memory under their run_id are folded. Use "Show runs with no memory activity" to see them. A run fills up when the caller passes run_id on memory.set and names what it relied on in run.complete(memories_used).'
          }
        />
      )}

      {/* Flat: run-first table. */}
      {rows !== null && !groupBy && visible.length > 0 && <RunTable rows={visible} onOpen={openRun} />}

      {/* Grouped: sessions are headers, runs are their timeline. */}
      {rows !== null && groupBy && visible.length > 0 && (
        <div className="space-y-4">
          {groups.map((g) => (
            <section key={g.label} aria-label={`Session ${g.label}`}>
              <div className="flex items-center justify-between gap-3 px-3 py-2">
                <div className="min-w-0 flex items-center gap-2 text-xs">
                  <Icon name="cards" size="xs" aria-hidden="true" className="shrink-0" />
                  {g.session_id.length > 32 ? (
                    <TipTarget tip={g.session_id}>
                      <span className="font-mono">{truncate(g.session_id, 32)}</span>
                    </TipTarget>
                  ) : (
                    <span className="font-mono truncate">{g.session_id || "(no session)"}</span>
                  )}
                  <span className="text-text-muted">·</span>
                  <span className="text-text-muted">
                    {g.runs.length} run{g.runs.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="text-xs text-text-muted whitespace-nowrap">
                  {g.used} relied on · {g.written} written · {relativeTime(g.latest)}
                </div>
              </div>
              <RunTable rows={g.runs} onOpen={openRun} />
            </section>
          ))}
        </div>
      )}

      {/* Replay dialog: the stored row plus what run.get derives live. */}
      <Modal
        open={open !== null}
        onOpenChange={(v) => {
          if (!v) {
            setOpen(null);
            setDetail(null);
          }
        }}
        title={open?.goal || "Run"}
        className="max-w-[820px] w-full"
        initialFocus="dialog"
        onCloseAutoFocus={(e) => {
          e.preventDefault();
          openerRef.current?.focus();
        }}
      >
        <div className="text-xs text-text-muted font-mono truncate">{open?.run_id}</div>
        {open && (
          <div className="text-xs text-text-muted mt-1 mb-4 flex items-center gap-2 flex-wrap">
            <StatusBadge status={open.status} inferred={metaBool(open.metadata, "implicit")} />
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
        <div className="space-y-3">
          {open && hasNoMemoryActivity(open) && metaBool(open.metadata, "implicit") && (
            <Alert
              state="neutral"
              size="small"
              text={
                'This run was inferred from a connection (implicit MCP session) that did no memory work — it recalled and wrote nothing, and closed on idle rather than by run.complete. Its "completed" status is not a reported outcome.'
              }
            />
          )}
          {open && hasNoMemoryActivity(open) && !metaBool(open.metadata, "implicit") && (
            <Alert
              state="neutral"
              size="small"
              text={`This run reported no memory activity: the caller opened it with run.begin and closed it with run.complete${
                open.duration_ms > 0 ? ` ${fmtDuration(open.duration_ms)} later` : ""
              }, naming nothing it relied on, and no write carried its run_id. The fields below say what each one would have needed.`}
            />
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
            <KeyChipButtons keys={detail?.memories_written || open?.memories_written || []} empty={EMPTY_WHY.written} />
          </Section>
          <Section title="Recalled, observed, cost">
            {detailLoading && <div className="text-xs text-text-muted">Loading…</div>}
            {!detailLoading &&
              detail &&
              open &&
              (() => {
                // The server's names_session: blank or "-" is no session.
                const session = (open.session_id ?? "").trim();
                const hasSession = session !== "" && session !== "-";
                const over = detail.over_budget ?? [];
                return (
                  <dl className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-sm m-0">
                    <Stat
                      label="recalled"
                      help={
                        detail.recalled < 0
                          ? EMPTY_WHY.recalledOff
                          : detail.recalled === 0
                            ? EMPTY_WHY.recalledZero
                            : undefined
                      }
                    >
                      {detail.recalled < 0 ? "n/a" : detail.recalled}
                    </Stat>
                    <Stat label="observations" help={EMPTY_WHY.observations}>
                      {detail.observations}
                    </Stat>
                    <Stat
                      label="turns"
                      help={
                        !hasSession
                          ? EMPTY_WHY.noSession
                          : (detail.unpriced_turns ?? 0) > 0
                            ? `${EMPTY_WHY.sessionCost}. Unpriced: ${EMPTY_WHY.unpriced}`
                            : EMPTY_WHY.sessionCost
                      }
                    >
                      {hasSession ? (
                        <>
                          {detail.turns}
                          {(detail.unpriced_turns ?? 0) > 0 && (
                            <span className="text-text-muted"> ({detail.unpriced_turns} unpriced)</span>
                          )}
                        </>
                      ) : (
                        <span className="text-text-muted">no session</span>
                      )}
                    </Stat>
                    <Stat label="cost" help={hasSession ? EMPTY_WHY.sessionCost : EMPTY_WHY.noSession}>
                      {!hasSession ? (
                        <span className="text-text-muted">no session</span>
                      ) : detail.cost_usd > 0 ? (
                        usd(detail.cost_usd)
                      ) : (
                        "—"
                      )}
                    </Stat>
                    <Stat label="budget" help={EMPTY_WHY.budget}>
                      {budgetText(detail)}
                      {over.length > 0 && <span className="text-danger"> · over {over.join(" and ")}</span>}
                    </Stat>
                  </dl>
                );
              })()}
            {!detailLoading && detail && detail.recalled_keys.length > 0 && (
              <div className="mt-3">
                <div className="text-xs text-text-muted mb-1">recalled keys</div>
                <KeyChipButtons keys={detail.recalled_keys} empty="" />
              </div>
            )}
            {!detailLoading && detail && open && detail.recalled >= 0 && (
              <Button
                type="empty"
                htmlType="button"
                iconRight="arrow-right"
                onClick={() => openRecallForRun(open.run_id)}
                className="mt-3"
              >
                Recall history for this run
              </Button>
            )}
            {!detailLoading && !detail && <div className="text-xs text-text-muted">Could not load the replay.</div>}
          </Section>
          {open?.final_response && (
            <Section title="Final response">
              <div className="text-sm whitespace-pre-wrap">{open.final_response}</div>
            </Section>
          )}
          {open && Object.keys(open.metadata || {}).length > 0 && (
            <Section title="Metadata">
              <pre className="text-xs font-mono whitespace-pre-wrap break-all">
                {JSON.stringify(open.metadata, null, 2)}
              </pre>
            </Section>
          )}
        </div>
      </Modal>
    </PageContainer>
  );
}
