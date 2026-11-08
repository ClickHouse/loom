import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Alert } from "../components/ui/Alert";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import type { IconName } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { Table, type TableColumnConfigProps, type TableRowType } from "@clickhouse/click-ui/Table";
import {
  getRun,
  listRunsPage,
  RUN_LIST_MAX_OFFSET,
  type RunGetResponse,
  type RunOrder,
  type RunRow,
  type RunStatus,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import {
  Chip,
  ListRow,
  ListTime,
  Loading,
  LoadingSkeleton,
  PageHeader,
  Panel,
  SectionHeading,
  Tip,
  TipTarget,
  useClamped,
} from "../components/ui";
import { IconButton } from "../components/IconButton";
import { MenuChoice, MenuChoiceGroup } from "../components/MenuChoice";
import { MenuTrigger } from "../components/ui/Menu";
import { RowSkeleton, TableSkeleton } from "../components/Skeleton";
import { ScopeBadge } from "../components/ScopeBadge";
import { displayAgent, listTime, parseChTime, relativeTime, truncate } from "../lib/format";
import { useRowNav } from "../lib/useRowNav";
import { PageContainer } from "../layout/PageContainer";
import { Modal } from "../components/ui/Modal";
import { ListPager, usePageSize } from "../components/ui/ListPager";
import { ListState } from "../components/ui/ListState";
import { ListToolbar, ListToolbarSearch } from "../components/ui/ListToolbar";
import { DraftSearch } from "../components/ui/DraftSearch";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { RunGrade } from "../flows/RunGrade";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";

// The ten fields a run row reads of the twenty a run carries; the replay reads the rest
// (the request, the expected outcome, the response, the confidence) from run.get.
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
 * run.list pages, sorts and searches on the server: the page asks for one page (offset,
 * limit, order_by, q and the filters) and shows the server's total. A server a release
 * behind ignores offset, order_by and q and sends no total: the page then reads its newest
 * 200 runs once and pages, searches and sorts those here (`local`), and when it holds more
 * it says the list is the newest 200 instead of counting 200 as every run. Only another
 * namespace reads a page at a time again.
 *
 * The run is the primary object; a SESSION is not a separate tab but a
 * grouping of the runs that shared one connection (1 session → N runs).
 * "Group by session" puts the page's runs under a heading per session. Runs loom
 * only inferred from a connection that did no memory work (implicit MCP
 * runs, idle-closed, nothing recalled or written) are "connection-only": hidden by
 * default so empty shells don't masquerade as real agent work, revealable with one
 * toggle.
 *
 * Each row's goal is a `<button>` that opens the replay — reachable by
 * Tab, activated by Enter or Space, with an accent focus ring. Clicking anywhere else on
 * the row still opens it, as a mouse convenience; the row itself is not a control. Under
 * 769 px the runs are list rows (one tab stop, arrow keys between them) instead of Click
 * UI's stacked table cards. On a phone the sort, the grouping and the hidden runs fold into
 * one View menu beside the filters, so the first run starts within the chrome budget.
 */

type Filter = "" | RunStatus;

const RUN_FILTERS: readonly Filter[] = ["", "active", "completed", "failed", "cancelled"];

// Every agent filter, and every status on a phone; "" is not a value the Select can hold.
const ALL_AGENTS = "*";
const ALL_STATUSES = "*";

// The search reaches the server once typing pauses this long.
const SEARCH_DEBOUNCE_MS = 250;

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
function ReplayFigure({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-text-muted flex items-center gap-0.5">
        {label}
        {help && <IconButton icon="info-in-circle" size="sm" label={`About ${label}`} tooltip={help} />}
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
    const elapsed = detail.elapsed_seconds;
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
  // In the page's own order (the server's sort): the session of its first run leads.
  return [...map.values()];
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Panel tone="surface-2" header={<SectionHeading label={title} />}>
      {children}
    </Panel>
  );
}

const ABOUT = "One row per agent execution: the goal, how it ended, the memories it relied on and what it wrote.";
const ABOUT_MORE =
  "Open a run to replay what it recalled, observed and cost. A session is one connection's worth of runs: group by it to see a whole working sitting.";

// The most runs a server that does not page answers (its run.list cap).
const LOCAL_LIMIT = 200;

/** A server that does not page: runs whose goal, id or agent holds `q`, any case. */
function searchRuns(runs: RunRow[], q: string): RunRow[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return runs;
  return runs.filter((r) => [r.goal, r.run_id, r.agent].some((v) => (v || "").toLowerCase().includes(needle)));
}

/** A server that does not page: the runs in `order`, ties by start then id, as run.list does. */
function sortRuns(runs: RunRow[], order: RunOrder): RunRow[] {
  const [key, dir] = splitOrder(order);
  const started = (r: RunRow) => parseChTime(r.started_at)?.getTime() ?? 0;
  const value = (r: RunRow) =>
    key === "duration" ? (r.duration_ms ?? 0) : key === "relied_on" ? (r.memories_used ?? []).length : started(r);
  const sign = dir === "asc" ? 1 : -1;
  // run_id compares by code unit, as ClickHouse compares the bytes of an ASCII id.
  const byId = (a: RunRow, b: RunRow) => (a.run_id < b.run_id ? -1 : a.run_id > b.run_id ? 1 : 0);
  return [...runs].sort(
    (a, b) => sign * (value(a) - value(b)) || sign * (started(a) - started(b)) || sign * byId(a, b),
  );
}

// The columns run.list sorts by, and each one's order in each direction.
type SortKey = "started" | "duration" | "relied_on";
type SortDir = "asc" | "desc";

function splitOrder(order: RunOrder): [SortKey, SortDir] {
  const dir: SortDir = order.endsWith("_asc") ? "asc" : "desc";
  return [order.slice(0, order.lastIndexOf("_")) as SortKey, dir];
}

// A phone has no column headers to sort by: there the order is a select.
const ORDER_LABELS: Record<RunOrder, string> = {
  started_desc: "Newest first",
  started_asc: "Oldest first",
  duration_desc: "Longest first",
  duration_asc: "Shortest first",
  relied_on_desc: "Most relied on",
  relied_on_asc: "Least relied on",
};

const NUMERIC = "block w-full text-right tabular-nums";

/**
 * The columns. Relied on, Took and Started sort on the server, one at a time: Click UI
 * draws the arrow and calls `onSort` from its header; the label is a button so the sort
 * is reachable by keyboard (Click UI's header is a clickable `div`), and `aria-sort` on
 * the header says which column orders the rows. A header kept only for a screen reader
 * (grouped tables after the first) keeps `aria-sort` but no button, so nothing focusable
 * hides in it.
 */
function runHeaders(order: RunOrder, sortable: boolean): TableColumnConfigProps[] {
  const [key, dir] = splitOrder(order);
  const column = (label: string, k: SortKey, width: string, numeric: boolean): TableColumnConfigProps => ({
    label: sortable ? (
      <button type="button" className="rounded">
        {label}
      </button>
    ) : (
      label
    ),
    width,
    isSortable: sortable,
    sortDir: key === k ? dir : undefined,
    // The arrow leads a right-aligned header, so the labels line up over their numbers.
    sortPosition: numeric ? "start" : "end",
    className: numeric ? "runs-numeric-header" : undefined,
    "aria-sort": key === k ? (dir === "asc" ? "ascending" : "descending") : "none",
  });
  return [
    { label: "Goal" },
    { label: "Status", width: "140px" },
    { label: "Agent", width: "160px" },
    column("Relied on", "relied_on", "104px", true),
    { label: <span className={NUMERIC}>Wrote</span>, width: "80px" },
    column("Took", "duration", "96px", true),
    column("Started", "started", "120px", false),
  ];
}

// Header index → the sort it carries (runHeaders' order).
const SORT_AT: Record<number, SortKey> = { 3: "relied_on", 5: "duration", 6: "started" };

/** The goal, as the button that opens the replay. A goal the row cuts short shows in
 *  full in a tooltip; the button's name already reads all of it, so the tip is no
 *  description. */
function GoalButton({ goal, dim, onClick }: { goal: string; dim: boolean; onClick: (e: MouseEvent) => void }) {
  const [el, setEl] = useState<HTMLButtonElement | null>(null);
  const clamped = useClamped(el, goal);
  return (
    <Tip content={goal} disabled={!clamped}>
      <button
        ref={setEl}
        type="button"
        data-row-open=""
        onClick={onClick}
        className={`min-w-0 max-w-full truncate text-left rounded ${dim ? "text-text-muted" : ""}`}
        aria-describedby={undefined}
      >
        {goal || <span className="text-text-muted">(no goal)</span>}
      </button>
    </Tip>
  );
}

function runRow(r: RunRow, onOpen: (r: RunRow) => void): TableRowType {
  const inferred = metaBool(r.metadata, "implicit");
  const dim = hasNoMemoryActivity(r);
  return {
    id: r.run_id,
    onClick: () => onOpen(r),
    className: "cursor-pointer",
    items: [
      {
        label: (
          <div className="min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <GoalButton
                goal={r.goal}
                dim={dim}
                onClick={(e) => {
                  e.stopPropagation();
                  onOpen(r);
                }}
              />
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
      { label: <StatusBadge status={r.status} inferred={inferred} /> },
      { label: displayAgent(r.agent) },
      { label: <span className={NUMERIC}>{(r.memories_used ?? []).length}</span> },
      { label: <span className={NUMERIC}>{(r.memories_written ?? []).length}</span> },
      { label: <span className={NUMERIC}>{fmtDuration(r.duration_ms)}</span> },
      { label: <ListTime time={r.started_at} quiet className="text-text-muted" /> },
    ],
  };
}

interface RunListProps {
  label: string;
  rows: RunRow[];
  onOpen: (r: RunRow) => void;
  order: RunOrder;
  onOrder: (order: RunOrder) => void;
  /** A refresh: the rows stay, marked busy. */
  busy?: boolean;
  /** Grouped by session, only the first table draws its header; the rest keep theirs
   *  out of sight, since a table without one leaves its cells unlabelled for a screen
   *  reader. */
  hideHeader?: boolean;
  /** The first read: the header over a skeleton. */
  loading?: boolean;
}

function RunTable({ label, rows, onOpen, order, onOrder, busy, hideHeader = false, loading = false }: RunListProps) {
  // Click UI's sort arrow is named after its icon, so the header would read "Started
  // arrow-down"; `aria-sort` already says it. Click UI draws a new arrow on every sort.
  const table = useRef<HTMLTableElement>(null);
  useLayoutEffect(() => {
    for (const svg of table.current?.querySelectorAll("thead svg") ?? []) svg.setAttribute("aria-hidden", "true");
  });
  return (
    <Table
      ref={table}
      aria-label={label}
      aria-busy={loading || busy || undefined}
      noDataMessage={loading ? <TableSkeleton label="Loading runs" /> : undefined}
      headers={runHeaders(order, !hideHeader)}
      onSort={(dir, _header, index) => {
        const key = SORT_AT[index];
        if (!key) return;
        // A column not sorted yet starts at its largest (newest, longest, most relied on).
        onOrder(`${key}_${splitOrder(order)[0] === key ? dir : "desc"}` as RunOrder);
      }}
      rows={rows.map((r) => runRow(r, onOpen))}
      size="sm"
      className={hideHeader ? "table-header-sr-only" : undefined}
    />
  );
}

/** Under 769 px: a list row per run. Line 1 the goal and its status; line 2 agent ·
 *  started · took. */
function RunRows({ label, rows, onOpen, busy }: RunListProps) {
  return (
    <ul aria-label={label} aria-busy={busy || undefined} className="space-y-2">
      {rows.map((r) => {
        const inferred = metaBool(r.metadata, "implicit");
        return (
          <li key={r.run_id}>
            <ListRow>
              <div className="flex min-w-0 items-center gap-2">
                <button
                  type="button"
                  data-row-open=""
                  onClick={() => onOpen(r)}
                  className={`list-row-open min-w-0 flex-1 truncate text-sm font-medium ${
                    hasNoMemoryActivity(r) ? "text-text-muted" : "text-text"
                  }`}
                >
                  {r.goal || "(no goal)"}
                </button>
                <span className="list-row-control shrink-0">
                  <StatusBadge status={r.status} inferred={inferred} />
                </span>
              </div>
              <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-text-muted">
                <span className="min-w-0 truncate">{displayAgent(r.agent)}</span>
                <span aria-hidden="true">·</span>
                <ListTime time={r.started_at} quiet />
                <span aria-hidden="true">·</span>
                <span className="tabular-nums whitespace-nowrap">
                  <span className="sr-only">took </span>
                  {fmtDuration(r.duration_ms)}
                </span>
              </div>
            </ListRow>
          </li>
        );
      })}
    </ul>
  );
}

/** The toolbar's count: "1–50 of 1,234", "1–50" without a total, "12
 *  matches" for a search the first page holds whole; nothing for an empty page. */
function countText(page: number, pageSize: number, shown: number, total: number | null, searching: boolean): string {
  if (shown === 0) return "";
  if (searching && total !== null && page === 0 && shown === total) {
    return `${total.toLocaleString()} ${total === 1 ? "match" : "matches"}`;
  }
  const range = `${(page * pageSize + 1).toLocaleString()}–${(page * pageSize + shown).toLocaleString()}`;
  return total !== null ? `${range} of ${total.toLocaleString()}` : range;
}

/** One page of runs, as the server answered it: `page` is the page it is, so the count
 *  keeps saying what is on screen while the next one loads. */
interface Shown {
  runs: RunRow[];
  total: number | null;
  page: number;
  pageSize: number;
  /** What the rows were read for (filters, search, order, page size): the page above means
   *  something only while it is still what the list shows. */
  key: string;
  /** The filters alone: the total holds across a page-size change. */
  filterKey: string;
}

export function Runs() {
  const identity = useIdentity();
  const roleHere = useNamespaceRole(identity.namespace);
  const whoami = useStore((s) => s.whoami);
  const canGrade = canCall("run.grade", roleHere, whoami);
  // Below Tailwind's sm breakpoint the toolbar holds the search alone; below Click UI's
  // table breakpoint the runs are list rows.
  const phone = useMedia(PHONE_QUERY);
  const narrow = useMedia("(max-width: 768px)");
  const [shown, setShown] = useState<Shown | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  const [filter, setFilter] = useState<Filter>("");
  // Filter by agent; the options are every agent the pages read so far showed, so the
  // select does not collapse to its own choice.
  const [agent, setAgent] = useState("");
  const [agents, setAgents] = useState<string[]>([]);
  const [groupBy, setGroupBy] = useState(false);
  const [hideConn, setHideConn] = useState(true);
  // The search sent (trimmed, once typing pauses); the box keeps what is typed, so a
  // keystroke does not render the table. A Clear filters bumps `searchBox` to empty it.
  const [q, setQ] = useState("");
  const [searchBox, setSearchBox] = useState(0);
  const [order, setOrder] = useState<RunOrder>("started_desc");
  // The server answered without a total: it does not page, search or sort (a release behind).
  const [local, setLocal] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = usePageSize("runs");
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

  // Every change of what the list holds starts again from its first page.
  const refilter =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setPage(0);
    };

  const search = (text: string) => {
    const next = text.trim();
    if (next === q) return;
    setQ(next);
    setPage(0);
  };

  const scope = `${identity.org}\n${identity.namespace}`;
  // Another namespace is another list: its first page, every agent, nothing on screen,
  // before its first read or paint.
  const [shownFor, setShownFor] = useState(scope);
  if (shownFor !== scope) {
    setShownFor(scope);
    setShown(null);
    setAgents([]);
    setPage(0);
    setLocal(false); // the next read may reach a build that pages
    setAgent("");
  }
  // What the list holds (a change starts it again from page 1), and what one read asked for.
  const filterKey = [scope, filter, agent, hideConn, q, order].join("\n");
  const readKey = `${filterKey}\n${pageSize}\n${local}`;
  // With `local`, the page, the search and the order are applied here: they read nothing.
  const readQ = local ? "" : q;
  const readOrder = local ? "" : order;
  const readPage = local ? 0 : page;
  const readSize = local ? 0 : pageSize;

  // A response that lands after the page, a filter or the namespace moved on is dropped,
  // so A's rows never render under B's filter. Within a namespace the rows on screen stay,
  // marked busy, until the next page answers.
  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    // The no-memory-activity fold is a run.list filter, not a slice of the fetched page.
    // Folding after the server's LIMIT can leave a chatty consumer's page empty — its
    // newest runs all hollow while hundreds of filled runs sit further back.
    listRunsPage(
      { org: identity.org, namespace: identity.namespace },
      local
        ? { status: filter, agent, limit: LOCAL_LIMIT, fields: RUNS_FIELDS, memoryActivity: hideConn ? "with" : "" }
        : {
            status: filter,
            agent,
            q,
            orderBy: order,
            limit: pageSize,
            offset: page * pageSize,
            fields: RUNS_FIELDS,
            memoryActivity: hideConn ? "with" : "",
          },
    )
      .then((p) => {
        if (!live) return;
        // A server that does not page: read its newest runs once and page here, still
        // loading, with nothing from the page that answered on screen. Only another namespace
        // leaves `local`: behind one URL a release and the one before it answer in turn, and
        // flipping back on each answer never settles.
        if (!p.paged && !local) {
          setShown(null);
          setLocal(true);
          return;
        }
        // A full page after the first carries no total: it keeps the one its filters counted.
        setShown((prev) => ({
          runs: p.runs,
          total: p.total ?? (prev?.filterKey === filterKey ? prev.total : null),
          page,
          pageSize,
          key: readKey,
          filterKey,
        }));
        setLoading(false);
        const seen = p.runs.map((r) => r.agent).filter(Boolean);
        setAgents((prev) => (seen.every((a) => prev.includes(a)) ? prev : [...new Set([...prev, ...seen])].sort()));
      })
      .catch((e: unknown) => {
        if (!live) return;
        setError(e);
        setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [identity.org, identity.namespace, filter, agent, hideConn, readQ, readOrder, readPage, readSize, local, retry]);

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

  // Against a server that does not page: its newest runs, searched, sorted and paged here.
  const localRows = useMemo(
    () => (local && shown ? sortRuns(searchRuns(shown.runs, q), order) : null),
    [local, shown, q, order],
  );
  // It answered as many as it sends: the server holds more than these.
  const capped = local && shown !== null && shown.runs.length >= LOCAL_LIMIT;
  const rows = localRows ? localRows.slice(page * pageSize, (page + 1) * pageSize) : (shown?.runs ?? []);
  const busy = loading && shown !== null;
  // The opened run as run.get answers it: the list row carries only RUNS_FIELDS.
  // A run.get answer for the run opened before is not this one's.
  const replayed = detail?.run && detail.run.run_id === open?.run_id ? detail.run : open;
  // The page the rows on screen are: the pager pages from it while they are what the list
  // shows, so a second Next during a read asks for the same page; for another filter or
  // page size it pages from the page asked for.
  const current = localRows !== null || shown?.key === readKey;
  const pagerPage = !localRows && shown?.key === readKey ? shown.page : page;
  const searching = q !== "" || filter !== "" || agent !== "";
  // The read of what is asked now answered with no rows, on the first page shown.
  const nothingShown =
    error === null && shown !== null && !loading && rows.length === 0 && (localRows ? page : shown.page) === 0;
  // Rows read for another search or filter are not counted as this one's matches.
  const count =
    shown && !error && (localRows !== null || shown.filterKey === filterKey)
      ? countText(
          localRows ? page : shown.page,
          localRows ? pageSize : shown.pageSize,
          rows.length,
          localRows ? (capped && q === "" ? null : localRows.length) : shown.total,
          q !== "",
        ) || undefined
      : undefined;
  const groups = groupBy ? bySession(rows) : [];
  // One tab stop for every run on the page, grouped by session or not (lib/useRowNav).
  const nav = useRowNav({ enabled: open === null && rows.length > 0 });
  const clearFilters = () => {
    setSearchBox((n) => n + 1);
    setQ("");
    setFilter("");
    setAgent("");
    setPage(0);
  };

  const list = (props: Omit<RunListProps, "order" | "onOrder" | "onOpen">) =>
    narrow ? (
      <RunRows {...props} order={order} onOrder={refilter(setOrder)} onOpen={openRun} />
    ) : (
      <RunTable {...props} order={order} onOrder={refilter(setOrder)} onOpen={openRun} />
    );

  // Five statuses are wider than a phone: there the status is a select beside the agent's.
  const filters = (
    <>
      {phone ? (
        <Select
          value={filter || ALL_STATUSES}
          onSelect={(v) => refilter(setFilter)(v === ALL_STATUSES ? "" : (v as Filter))}
          triggerProps={{ "aria-label": "Filter runs by status" }}
          className="min-w-0 flex-1"
        >
          {RUN_FILTERS.map((f) => (
            <Select.Item key={f || ALL_STATUSES} value={f || ALL_STATUSES}>
              {f ? STATUS_META[f].label : "Every status"}
            </Select.Item>
          ))}
        </Select>
      ) : (
        <ButtonGroup
          aria-label="Filter runs by status"
          selected={filter}
          onClick={(value) => refilter(setFilter)(value as Filter)}
          options={RUN_FILTERS.map((f) => ({ value: f, label: f ? STATUS_META[f].label : "All" }))}
        />
      )}
      {(agents.length > 1 || agent) && (
        <Select
          value={agent || ALL_AGENTS}
          onSelect={(v) => refilter(setAgent)(v === ALL_AGENTS ? "" : v)}
          triggerProps={{ "aria-label": "Agent" }}
          className={phone ? "min-w-0 flex-1" : "w-44 shrink-0"}
        >
          <Select.Item value={ALL_AGENTS}>Every agent</Select.Item>
          {agents.map((a) => (
            <Select.Item key={a} value={a}>
              {displayAgent(a)}
            </Select.Item>
          ))}
        </Select>
      )}
    </>
  );

  // A phone's chrome budget has no room for a row of toggles under the filters (44 px each
  // under a touch pointer): the sort, the grouping and the hidden runs are one View menu.
  const viewMenu = (
    <Dropdown modal={false}>
      <MenuTrigger>
        {/* An icon: with a word beside them the two selects cut their values ("Every s…"). */}
        <IconButton
          icon="eye"
          label="View"
          tooltip="Sort, group by session, show runs with no memory activity"
          className="shrink-0"
        />
      </MenuTrigger>
      <Dropdown.Content align="end" aria-label="View" aria-labelledby={undefined}>
        <MenuChoiceGroup label="Sort">
          {(Object.keys(ORDER_LABELS) as RunOrder[]).map((o) => (
            <MenuChoice key={o} radio checked={order === o} onSelect={() => refilter(setOrder)(o)}>
              {ORDER_LABELS[o]}
            </MenuChoice>
          ))}
        </MenuChoiceGroup>
        <MenuChoiceGroup label="Show">
          <MenuChoice checked={groupBy} onSelect={() => setGroupBy((v) => !v)}>
            Group by session
          </MenuChoice>
          <MenuChoice checked={!hideConn} onSelect={() => refilter(setHideConn)(!hideConn)}>
            Show runs with no memory activity
          </MenuChoice>
        </MenuChoiceGroup>
      </Dropdown.Content>
    </Dropdown>
  );

  return (
    <PageContainer>
      <PageHeader
        title="Runs"
        actions={
          // On a phone the description waits behind the info button, so the runs start
          // higher; the top bar already names the namespace.
          phone && <IconButton icon="info-in-circle" size="sm" label="About runs" tooltip={`${ABOUT} ${ABOUT_MORE}`} />
        }
      >
        <span className="hidden sm:contents">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </span>
      </PageHeader>
      {/* The icon sits beside the sentence's box, so a wrapped sentence never strands it. */}
      {!phone && (
        <div className="mb-4 flex items-start gap-1">
          <p className="max-w-[72ch] text-sm text-text-muted">{ABOUT}</p>
          <IconButton icon="info-in-circle" size="sm" label="About runs" tooltip={ABOUT_MORE} />
        </div>
      )}

      <ListToolbar label="Search and filters" count={count}>
        <ListToolbarSearch>
          <DraftSearch
            key={searchBox}
            onSearch={search}
            settle={{ debounceMs: SEARCH_DEBOUNCE_MS }}
            placeholder="Search runs by goal, id or agent"
            aria-label="Search runs"
          />
        </ListToolbarSearch>
        {/* The filters scroll sideways in their own box; its 4 px padding (outside the bar's
            layout, by the negative margin) is the focus ring's room. */}
        {!phone && <div className="-m-1 flex min-w-0 items-center gap-2 overflow-x-auto p-1">{filters}</div>}
      </ListToolbar>
      {/* A phone's toolbar has room for the search only; the filters and the View menu sit
          under it. */}
      {phone && (
        <div className="mb-3 flex items-center gap-2">
          {filters}
          {viewMenu}
        </div>
      )}

      {!phone && (
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <Checkbox label="Group by session" checked={groupBy} onCheckedChange={(c) => setGroupBy(c === true)} />
          {/* No column headers to sort by: the order is a select. */}
          {narrow && (
            <Select
              value={order}
              onSelect={(v) => refilter(setOrder)(v as RunOrder)}
              triggerProps={{ "aria-label": "Sort runs" }}
              className="ml-auto w-40 shrink-0"
            >
              {(Object.keys(ORDER_LABELS) as RunOrder[]).map((o) => (
                <Select.Item key={o} value={o}>
                  {ORDER_LABELS[o]}
                </Select.Item>
              ))}
            </Select>
          )}
          <Checkbox
            label="Show runs with no memory activity"
            checked={!hideConn}
            onCheckedChange={(c) => refilter(setHideConn)(c !== true)}
          />
        </div>
      )}

      {error !== null && <ListState kind="error" things="runs" error={error} onRetry={() => setRetry((n) => n + 1)} />}
      {error === null &&
        shown === null &&
        (narrow ? (
          <LoadingSkeleton label="Loading runs">
            <div className="space-y-2">
              <RowSkeleton />
              <RowSkeleton />
              <RowSkeleton />
            </div>
          </LoadingSkeleton>
        ) : (
          <RunTable label="Runs" rows={[]} onOpen={openRun} order={order} onOrder={() => {}} loading />
        ))}
      {/* Said only of a read of what is asked now: a new filter's read in flight is not it. */}
      {nothingShown && searching && (
        <ListState
          kind="filtered"
          things={hideConn ? "runs with memory activity" : "runs"}
          // The typed words alone; with a status or agent picked too, "the filters in force".
          filter={(!filter && !agent && q) || undefined}
          onClear={clearFilters}
        />
      )}
      {nothingShown && !searching && (
        <ListState
          kind="empty"
          icon="list-numbered"
          {...(hideConn
            ? {
                title: "No runs with memory activity",
                description: `Runs that neither relied on nor wrote a memory under their run_id are folded. Use "Show runs with no memory activity"${phone ? " in the View menu" : ""} to see them. A run fills up when the caller passes run_id on memory.set and names what it relied on in run.complete(memories_used).`,
              }
            : {
                title: "No runs yet",
                description:
                  "Agents open a run with run.begin (the Claude Code plugin does it on the first prompt of a session) and close it with run.complete, naming the memories they relied on.",
              })}
        />
      )}

      {error === null && capped && (
        <p className="mb-3 text-xs text-text-muted">
          Showing the newest {LOCAL_LIMIT} runs: this server sends no more, so the search and the sort cover these.
        </p>
      )}

      {/* A new page scrolls here and takes focus. */}
      <div id="runs-list" ref={nav} tabIndex={-1}>
        {error === null && rows.length > 0 && !groupBy && list({ label: "Runs", rows, busy })}

        {/* Grouped: a heading per session, its runs under it. */}
        {error === null && rows.length > 0 && groupBy && (
          <div className="space-y-5" aria-busy={busy || undefined}>
            {groups.map((g, i) => (
              <section key={g.label} aria-labelledby={`runs-session-${i}`}>
                <SectionHeading
                  level={2}
                  id={`runs-session-${i}`}
                  label={truncate(g.label, 32)}
                  caption={`${g.runs.length} run${g.runs.length === 1 ? "" : "s"} · ${g.used} relied on · ${g.written} written · ${listTime(g.latest)}`}
                />
                {list({ label: `Runs in session ${g.label}`, rows: g.runs, hideHeader: i > 0 })}
              </section>
            ))}
          </div>
        )}
      </div>
      {/* Kept on an empty later page (runs finished or left the filter meanwhile), so it steps
          back to the last page. */}
      {error === null && shown !== null && (rows.length > 0 || pagerPage > 0) && (
        <ListPager
          label="Runs pages"
          page={pagerPage}
          resetKey={filterKey}
          busy={loading}
          pageSize={pageSize}
          // Rows read for another filter or page size are not this list's to count or page.
          // Another filter's total is not either, so the pager waits for the new list's first
          // page; a new page size keeps the total, and with it the size choice just used.
          shown={current ? rows.length : 0}
          total={localRows ? localRows.length : shown.filterKey === filterKey ? (shown.total ?? undefined) : undefined}
          capped={capped}
          maxOffset={RUN_LIST_MAX_OFFSET}
          onPage={setPage}
          onPageSize={refilter(setPageSize)}
          topId="runs-list"
        />
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
            {(replayed?.confidence ?? 0) > 0 && (
              <>
                <span>·</span>
                <span>confidence {replayed?.confidence.toFixed(2)}</span>
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
          {replayed?.user_request && (
            <Section title="User request">
              <div className="text-sm whitespace-pre-wrap">{replayed.user_request}</div>
            </Section>
          )}
          {replayed?.expected_outcome && (
            <Section title="Expected outcome">
              <div className="text-sm whitespace-pre-wrap">{replayed.expected_outcome}</div>
            </Section>
          )}
          <Section title="Memories the agent relied on">
            <KeyChipButtons keys={open?.memories_used || []} empty={EMPTY_WHY.used} />
          </Section>
          <Section title="Memories this run wrote">
            <KeyChipButtons keys={detail?.memories_written || open?.memories_written || []} empty={EMPTY_WHY.written} />
          </Section>
          <Section title="Recalled, observed, cost">
            {detailLoading && <Loading layout="inline" label="Loading the run's figures" />}
            {!detailLoading &&
              detail &&
              open &&
              (() => {
                // The server's names_session: blank or "-" is no session.
                const session = (open.session_id ?? "").trim();
                const hasSession = session !== "" && session !== "-";
                const over = detail.over_budget;
                return (
                  <dl className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-sm m-0">
                    <ReplayFigure
                      label="Recalled"
                      help={
                        detail.recalled < 0
                          ? EMPTY_WHY.recalledOff
                          : detail.recalled === 0
                            ? EMPTY_WHY.recalledZero
                            : undefined
                      }
                    >
                      {detail.recalled < 0 ? "n/a" : detail.recalled}
                    </ReplayFigure>
                    <ReplayFigure label="Observations" help={EMPTY_WHY.observations}>
                      {detail.observations}
                    </ReplayFigure>
                    <ReplayFigure
                      label="Turns"
                      help={
                        !hasSession
                          ? EMPTY_WHY.noSession
                          : detail.unpriced_turns > 0
                            ? `${EMPTY_WHY.sessionCost}. Unpriced: ${EMPTY_WHY.unpriced}`
                            : EMPTY_WHY.sessionCost
                      }
                    >
                      {hasSession ? (
                        <>
                          {detail.turns}
                          {detail.unpriced_turns > 0 && (
                            <span className="text-text-muted"> ({detail.unpriced_turns} unpriced)</span>
                          )}
                        </>
                      ) : (
                        <span className="text-text-muted">no session</span>
                      )}
                    </ReplayFigure>
                    <ReplayFigure label="Cost" help={hasSession ? EMPTY_WHY.sessionCost : EMPTY_WHY.noSession}>
                      {!hasSession ? (
                        <span className="text-text-muted">no session</span>
                      ) : detail.cost_usd > 0 ? (
                        usd(detail.cost_usd)
                      ) : (
                        "—"
                      )}
                    </ReplayFigure>
                    <ReplayFigure label="Budget" help={EMPTY_WHY.budget}>
                      {budgetText(detail)}
                      {over.length > 0 && <span className="text-danger"> · over {over.join(" and ")}</span>}
                    </ReplayFigure>
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
          {replayed?.final_response && (
            <Section title="Final response">
              <div className="text-sm whitespace-pre-wrap">{replayed.final_response}</div>
            </Section>
          )}
          {open && open.status !== "active" && canGrade && (
            <Section title="Did it hold up?">
              <RunGrade key={open.run_id} identity={identity} run={open} />
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
