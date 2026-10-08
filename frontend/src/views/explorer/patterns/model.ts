import type { TrackerBinding, TrackerItem, TrackerSection } from "../../../api";

/**
 * Patterns mode's model: pure derivations from the QA plane's answers, so the board, the
 * facets and the tracker graph can be tested without a DOM.
 *
 * - `buildBoard` turns `severity_board` rows (one per component × severity
 *   cell) and `test_gaps` rows (one per component snapshot) into a matrix:
 *   components sorted by open count, the severities present in the order
 *   the source ranks them, each row carrying its gap ratio and trend.
 * - `filterBoard` applies the facets client-side: components, severities,
 *   flags (a cell must carry at least one issue with every flag chosen).
 * - `patternsAvailable` is the one rule that shows or hides the mode.
 * - `trackerGraphFrom` lays the selected issue's neighbourhood out as typed,
 *   directed edges: the PR the graph names as the cause to the left, the
 *   fixing PRs to the right, duplicates below.
 */

const SEVERITY_ORDER = ["critical", "major", "medium", "low", "minor", "unrated"] as const;
export type Flag = "untested" | "flaky" | "bisected";
export const FLAGS: Flag[] = ["untested", "flaky", "bisected"];
export type Window = 90 | 365 | 0;
export const WINDOWS: Array<{ days: Window; label: string }> = [
  { days: 90, label: "90 days" },
  { days: 365, label: "365 days" },
  { days: 0, label: "all open" },
];

/** A cell of the board: one component at one severity. */
export interface Cell {
  component: string;
  severity: string;
  open: number;
  untested: number;
  flaky: number;
  bisected: number;
}

/** The component's risk snapshot when `test_gaps` carried one. */
interface GapRow {
  gap_ratio: number;
  trend_delta: number;
  risk_score: number;
  total_bugs: number;
  bugs_without_test: number;
  open_bugs: number;
  critical_bugs: number;
  snapshot_date: string;
}

export interface BoardRow {
  component: string;
  /** Shown name: "(no component)" for the source's empty component. */
  label: string;
  open: number;
  untested: number;
  flaky: number;
  bisected: number;
  cells: Map<string, Cell>;
  gap: GapRow | null;
}

export interface Board {
  rows: BoardRow[];
  /** Severities present, in rank order. */
  severities: string[];
  /** The largest cell, for the heat scale. */
  maxOpen: number;
  totalOpen: number;
}

export interface Facets {
  components: Set<string>;
  severities: Set<string>;
  flags: Set<Flag>;
  days: Window;
}

export function emptyFacets(): Facets {
  return { components: new Set(), severities: new Set(), flags: new Set(), days: 0 };
}

export function hasFacets(f: Facets): boolean {
  return f.components.size > 0 || f.severities.size > 0 || f.flags.size > 0;
}

const NO_COMPONENT = "(no component)";

function num(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

function severityRank(s: string): number {
  const i = (SEVERITY_ORDER as readonly string[]).indexOf(s || "unrated");
  return i === -1 ? SEVERITY_ORDER.length : i;
}

/** The matrix from the two answers. Rows the gaps snapshot names but the
 *  board does not (no open issue) are not rows: the board is open issues. */
export function buildBoard(boardRows: Record<string, unknown>[], gapRows: Record<string, unknown>[] = []): Board {
  const gaps = new Map<string, GapRow>();
  for (const g of gapRows) {
    const c = str(g.component);
    gaps.set(c, {
      gap_ratio: num(g.gap_ratio),
      trend_delta: num(g.trend_delta),
      risk_score: num(g.risk_score),
      total_bugs: num(g.total_bugs),
      bugs_without_test: num(g.bugs_without_test),
      open_bugs: num(g.open_bugs),
      critical_bugs: num(g.critical_bugs),
      snapshot_date: str(g.snapshot_date),
    });
  }
  const rows = new Map<string, BoardRow>();
  const sevs = new Set<string>();
  let maxOpen = 0;
  let totalOpen = 0;
  for (const r of boardRows) {
    const component = str(r.component);
    const severity = str(r.severity) || "unrated";
    const cell: Cell = {
      component,
      severity,
      open: num(r.open),
      untested: num(r.untested),
      flaky: num(r.flaky),
      bisected: num(r.bisected),
    };
    if (cell.open <= 0) continue;
    sevs.add(severity);
    maxOpen = Math.max(maxOpen, cell.open);
    totalOpen += cell.open;
    const row = rows.get(component) ?? {
      component,
      label: component || NO_COMPONENT,
      open: 0,
      untested: 0,
      flaky: 0,
      bisected: 0,
      cells: new Map(),
      gap: gaps.get(component) ?? null,
    };
    const prev = row.cells.get(severity);
    if (prev) {
      prev.open += cell.open;
      prev.untested += cell.untested;
      prev.flaky += cell.flaky;
      prev.bisected += cell.bisected;
    } else {
      row.cells.set(severity, cell);
    }
    row.open += cell.open;
    row.untested += cell.untested;
    row.flaky += cell.flaky;
    row.bisected += cell.bisected;
    rows.set(component, row);
  }
  return {
    rows: [...rows.values()].sort((a, b) => b.open - a.open || a.label.localeCompare(b.label)),
    severities: [...sevs].sort((a, b) => severityRank(a) - severityRank(b) || a.localeCompare(b)),
    maxOpen,
    totalOpen,
  };
}

function cellPasses(c: Cell, flags: Set<Flag>): boolean {
  for (const f of flags) if (c[f] <= 0) return false;
  return true;
}

/** The facets applied client-side: rows by component, columns by severity,
 *  and a cell survives a flag only when at least one of its issues carries
 *  it. A row with no surviving cell is dropped; the scale is recomputed. */
export function filterBoard(b: Board, f: Facets): Board {
  const rows: BoardRow[] = [];
  let maxOpen = 0;
  let totalOpen = 0;
  const sevs = new Set<string>();
  for (const r of b.rows) {
    if (f.components.size && !f.components.has(r.component)) continue;
    const cells = new Map<string, Cell>();
    let open = 0,
      untested = 0,
      flaky = 0,
      bisected = 0;
    for (const [s, c] of r.cells) {
      if (f.severities.size && !f.severities.has(s)) continue;
      if (!cellPasses(c, f.flags)) continue;
      cells.set(s, c);
      sevs.add(s);
      open += c.open;
      untested += c.untested;
      flaky += c.flaky;
      bisected += c.bisected;
      maxOpen = Math.max(maxOpen, c.open);
    }
    if (cells.size === 0) continue;
    totalOpen += open;
    rows.push({ ...r, cells, open, untested, flaky, bisected });
  }
  return {
    rows: rows.sort((a, b) => b.open - a.open || a.label.localeCompare(b.label)),
    severities: [...sevs].sort((a, b) => severityRank(a) - severityRank(b) || a.localeCompare(b)),
    maxOpen,
    totalOpen,
  };
}

/** Components with their open counts, for the facet chips (every row of the
 *  unfiltered board, most open first). */
export function componentFacets(b: Board): Array<{ component: string; label: string; open: number }> {
  return b.rows.map((r) => ({ component: r.component, label: r.label, open: r.open }));
}

/** Severities with their open counts across the unfiltered board. */
export function severityFacets(b: Board): Array<{ severity: string; open: number }> {
  const m = new Map<string, number>();
  for (const r of b.rows) for (const [s, c] of r.cells) m.set(s, (m.get(s) || 0) + c.open);
  return b.severities.map((s) => ({ severity: s, open: m.get(s) || 0 }));
}

/** The heat of a cell as a share of the largest, on a square-root scale so
 *  the long tail of ones and twos is still visible against a 236. */
export function heat(open: number, maxOpen: number): number {
  if (open <= 0 || maxOpen <= 0) return 0;
  return Math.min(1, Math.sqrt(open / maxOpen));
}

/** The one rule: Patterns mode exists when the server names a tracker namespace
 *  the caller may read. An unbound, refused or unknown namespace is one
 * answer and shows nothing. A missing or malformed answer is "none". */
export function patternsAvailable(b: Partial<TrackerBinding> | null | undefined): boolean {
  return Boolean(
    b &&
      typeof b.tracker_namespace === "string" &&
      b.tracker_namespace !== "" &&
      b.authorized === true &&
      b.kind !== "none",
  );
}

// ---- Issues (area_open_issues rows).

export interface Issue {
  number: number;
  title: string;
  severity: string;
  labels: string[];
  component: string;
  untested: boolean;
  flaky: boolean;
  bisected: boolean;
  bisect_outcome: string;
  url: string;
  state: string;
  opened_at: string;
}

/** An `area_open_issues` row as the list's issue. `has_test` is -1 or NULL
 * on most open issues: untested means no linked test known. */
function issueFrom(r: Record<string, unknown>): Issue {
  const hasTest = r.has_test;
  return {
    number: num(r.number),
    title: str(r.title),
    severity: str(r.severity_level) || "unrated",
    labels: Array.isArray(r.labels) ? r.labels.map(str) : [],
    component: str(r.primary_component),
    untested: !(hasTest === 1 || hasTest === true),
    flaky: Boolean(num(r.is_flaky_test)),
    bisected: str(r.bisect_outcome) !== "",
    bisect_outcome: str(r.bisect_outcome),
    url: str(r.url),
    state: str(r.state),
    opened_at: str(r.opened_at),
  };
}

/** The list a cell drills into: the component's issues at that severity,
 *  narrowed by the flag facets. */
export function issuesForCell(rows: Record<string, unknown>[], severity: string | null, flags: Set<Flag>): Issue[] {
  return rows
    .map(issueFrom)
    .filter((i) => (severity === null || i.severity === severity) && [...flags].every((f) => i[f]));
}

/** What a cell's `area_open_issues` section yields. */
interface CellRead {
  issues: Issue[];
  /** The server filtered the read to this severity (`params.severities`
   *  names it), so the rows are the cell and are not narrowed again here. */
  serverFiltered: boolean;
  /** The read's row ceiling (`params.limit`). */
  read: number;
  /** The ceiling was reached: the list may be shorter than the cell. */
  capped: boolean;
}

/** A cell's issues from its section. A server that filtered by severity
 * (`anchors.severities` → `params.severities`, up to 300 rows) returns the cell itself;
 *  an answer without the parameter (a severity the server does not rank) is the
 *  component's most severe 50 and is narrowed to the severity here. The flag chips
 *  narrow client-side either way. */
export function cellIssues(
  section: Pick<TrackerSection, "params" | "rows"> | null | undefined,
  severity: string,
  flags: Set<Flag>,
): CellRead {
  const rows = section?.rows ?? [];
  const severities = section?.params?.severities;
  const sevs = Array.isArray(severities) ? severities.map(str) : [];
  const serverFiltered = sevs.includes(severity);
  const read = Math.max(1, num(section?.params?.limit) || 50);
  return {
    issues: issuesForCell(rows, serverFiltered ? null : severity, flags),
    serverFiltered,
    read,
    capped: rows.length >= read,
  };
}

// ---- The restricted class (area_incidents / area_escalations rows, `_doc_row`).

interface AreaDoc {
  /** The upstream id, e.g. "acme/incidents#9". */
  source_id: string;
  /** The part after `#`, for the line. */
  ref: string;
  title: string;
  severity: string;
  opened_at: string;
  open: boolean;
  url: string;
  /** What the row knows beyond the title: an incident's error type or
   *  signal, an escalation's customer and instance. */
  detail: string;
}

export function areaDocFrom(r: Record<string, unknown>): AreaDoc {
  const sid = str(r.source_id);
  const bits: string[] = [];
  if (r.error_type) bits.push(str(r.error_type));
  else if (r.error_name) bits.push(str(r.error_name));
  if (r.signal) bits.push(str(r.signal));
  if (r.org_name) bits.push(`customer ${str(r.org_name)}`);
  if (r.spoken_name) bits.push(`instance ${str(r.spoken_name)}`);
  if (r.version_affected) bits.push(`on ${str(r.version_affected)}`);
  return {
    source_id: sid,
    ref: sid.includes("#") ? `#${sid.split("#").pop()}` : sid,
    title: str(r.title),
    severity: str(r.severity) || "unrated",
    opened_at: str(r.created_at),
    open: r.open === undefined ? !r.closed_at : Boolean(r.open),
    url: str(r.url),
    detail: bits.join(" · "),
  };
}

type AreaClassStatus = "served" | "empty" | "restricted_required" | "tracker_unavailable" | "unplanned";

export interface AreaClass {
  template: string;
  status: AreaClassStatus;
  docs: AreaDoc[];
  /** The window the read covered (`params.days`, 90 by default). */
  days: number;
  /** The read's row ceiling (`params.limit`) was reached: there may be more. */
  capped: boolean;
}

/** One restricted-class section of a cell's area plan as the inspector
 *  shows it: rows when served, an honest empty for the window, the
 *  restricted line on a standard namespace, never a blank. */
export function areaClass(sections: TrackerSection[], template: string): AreaClass {
  const s = sections.find((x) => x.template === template);
  if (!s) return { template, status: "unplanned", docs: [], days: 90, capped: false };
  const days = num(s.params?.days) || 90;
  const status: AreaClassStatus =
    s.status === "served"
      ? "served"
      : s.status === "restricted_required"
        ? "restricted_required"
        : s.status === "tracker_unavailable"
          ? "tracker_unavailable"
          : "empty";
  const docs = status === "served" ? (s.rows || []).map(areaDocFrom) : [];
  const limit = num(s.params?.limit);
  return { template, status, docs, days, capped: limit > 0 && docs.length >= limit };
}

/** The status line of a drill-down's restricted sections: the ones the
 *  token cannot run, named once, never as an error. */
export function restrictedSections(sections: TrackerSection[]): string[] {
  return sections.filter((s) => s.status === "restricted_required").map((s) => s.template);
}

// ---- The tracker graph.

type TrackerRel = "caused_by" | "fixed_by" | "duplicate_of" | "related_to" | "fixes" | "caused";

export interface TrackerNode {
  id: string;
  number: number;
  kind: "issue" | "pr";
  title: string;
  severity: string;
  url: string;
  /** seed | cause | fix | duplicate | related (a PR seed's issues) | linked
   *  (an issue the graph relates to the seed without calling it a duplicate). */
  role: "seed" | "cause" | "fix" | "duplicate" | "related" | "linked";
  /** The graph named this PR as the culprit — an edge, not a scan. */
  namedByGraph: boolean;
  known: boolean;
}

export interface TrackerEdge {
  id: string;
  from: string;
  to: string;
  rel: TrackerRel;
  /** Spoken as `from — label → to`. */
  label: string;
}

export interface TrackerGraph {
  seed: TrackerNode;
  nodes: TrackerNode[];
  edges: TrackerEdge[];
}

export interface DuplicateRow {
  number: number;
  title: string;
  severity: string;
  url: string;
  rel_type: string;
  direction: string;
}

/** A `duplicates` row (`_doc_row` + rel_type/direction): the other item's
 *  number is the part of `source_id` after `#`. */
export function duplicateFrom(r: Record<string, unknown>): DuplicateRow {
  const sid = str(r.source_id);
  const n = Number(sid.split("#").pop());
  return {
    number: Number.isFinite(n) ? n : 0,
    title: str(r.title),
    severity: str(r.severity),
    url: str(r.url),
    rel_type: str(r.rel_type),
    direction: str(r.direction),
  };
}

function nodeOf(
  n: number,
  items: Map<number, TrackerItem>,
  role: TrackerNode["role"],
  kindHint: "issue" | "pr",
  namedByGraph = false,
): TrackerNode {
  const it = items.get(n);
  const kind: "issue" | "pr" = it?.kind === "pr" || it?.kind === "issue" ? it.kind : kindHint;
  return {
    id: String(n),
    number: n,
    kind,
    title: it?.title ?? "",
    severity: it?.tracker.severity ?? "",
    url: it?.url ?? it?.tracker.url ?? "",
    role,
    namedByGraph,
    known: Boolean(it),
  };
}

/**
 * The selected issue's neighbourhood as a graph. `items` holds the QA
 * records read so far (the seed, and its neighbours when a second read
 * fetched their titles); a neighbour without a record is still a node,
 * marked unknown. Roles: `caused_by` PRs are causes (the named culprits), `fixed_by` PRs
 * are fixes, duplicates come from the `duplicates` template — its `duplicate_of` rows as
 * duplicates and its `related_to` rows as linked issues, both below the seed and each
 * labelled for what the graph says (live, 119601 → 116490 is `related_to`, not a
 * duplicate, and the picture must not promote it). A PR seed reads its `fixes` and
 * `caused` the other way round.
 */
export function trackerGraphFrom(
  seedNumber: number,
  items: Map<number, TrackerItem>,
  duplicates: DuplicateRow[] = [],
): TrackerGraph | null {
  const seedItem = items.get(seedNumber);
  if (!seedItem) return null;
  const seed = nodeOf(seedNumber, items, "seed", seedItem.kind === "pr" ? "pr" : "issue");
  const nodes: TrackerNode[] = [];
  const edges: TrackerEdge[] = [];
  const seen = new Set<number>([seedNumber]);
  const add = (
    n: number,
    role: TrackerNode["role"],
    kind: "issue" | "pr",
    rel: TrackerRel,
    from: "n" | "seed",
    label: string,
    named = false,
  ) => {
    if (!n || seen.has(n)) return;
    seen.add(n);
    const node = nodeOf(n, items, role, kind, named);
    nodes.push(node);
    edges.push(
      from === "n"
        ? { id: `${node.id}-${rel}`, from: node.id, to: seed.id, rel, label }
        : { id: `${rel}-${node.id}`, from: seed.id, to: node.id, rel, label },
    );
  };
  const q = seedItem.tracker;
  if (seed.kind === "issue") {
    // A `caused_by` edge IS the graph naming the culprit; `named_by_graph`, when a bisect
    // answer rode along, names the same PRs.
    for (const n of q.caused_by ?? []) add(n, "cause", "pr", "caused_by", "n", "caused", true);
    for (const n of q.fixed_by ?? []) add(n, "fix", "pr", "fixed_by", "n", "fixes");
  } else {
    for (const n of q.caused ?? []) add(n, "related", "issue", "caused", "seed", "caused");
    for (const n of q.fixes ?? []) add(n, "related", "issue", "fixes", "seed", "fixes");
  }
  for (const d of duplicates) {
    if (!d.number) continue;
    if (d.rel_type === "duplicate_of") {
      // direction out: the seed is a duplicate of the other; in: the other duplicates the seed
      if (d.direction === "out") add(d.number, "duplicate", "issue", "duplicate_of", "seed", "duplicate of");
      else add(d.number, "duplicate", "issue", "duplicate_of", "n", "duplicate of");
    } else if (d.rel_type === "related_to") {
      if (d.direction === "out") add(d.number, "linked", "issue", "related_to", "seed", "related to");
      else add(d.number, "linked", "issue", "related_to", "n", "related to");
    }
  }
  return { seed, nodes, edges };
}

/** The numbers a graph read needs beyond the seed, at most nine more so the
 *  batch stays within `tracker.item`'s ten. */
export function neighbourNumbers(item: TrackerItem, duplicates: DuplicateRow[] = []): number[] {
  const q = item.tracker;
  const out: number[] = [];
  for (const n of [
    ...(q.caused_by ?? []),
    ...(q.fixed_by ?? []),
    ...(q.caused ?? []),
    ...(q.fixes ?? []),
    ...duplicates.map((d) => d.number),
  ]) {
    if (n > 0 && n !== item.number && !out.includes(n)) out.push(n);
  }
  return out.slice(0, 9);
}

/** The trend arrow's text: ▲ when the gap ratio worsened, ▼ when it improved. */
export function trendGlyph(delta: number): { glyph: string; word: string } {
  if (delta > 0.005) return { glyph: "▲", word: "worse" };
  if (delta < -0.005) return { glyph: "▼", word: "better" };
  return { glyph: "▬", word: "flat" };
}

export function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}
