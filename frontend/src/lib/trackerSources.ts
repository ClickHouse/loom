import { ApiError } from "./errors";
import { joinState, type JoinPolicy, type JoinState } from "./joinActions";

/**
 * tracker sources on the Repositories page.
 *
 * A tracker source is a database — issues, PRs, checks,
 * incidents read live — and its namespaces are joined exactly like a
 * repository's branches: the same `namespace.join`, the same standing words. This module
 * is the pure part: the response shape of `tracker.source.list`
 * (`server/tracker/app/sources.py`), the standing of the caller on each namespace,
 * the card's one primary verb, and the sort. Nothing here reads.
 */

export type BreakerState = "CLOSED" | "OPEN" | "HALF_OPEN" | "unconfigured";
export type TrackerClassification = "standard" | "restricted";
export type TrackerMemberRole = "" | "viewer" | "writer" | "admin";
export type TrackerPendingRole = "" | "viewer" | "writer" | "admin";

export interface TrackerSourceNamespace {
  namespace: string;
  kind: "tracker";
  visibility: string;
  repos: string[];
  classification: TrackerClassification;
  /** The directory row's effective rule (`inherit` + private → approval). */
  join_policy: JoinPolicy;
  /** False until the leader wrote the directory row; `join_policy` is then
   *  the rule the map will give it, and nothing can be joined yet. */
  registered: boolean;
  member_role: TrackerMemberRole;
  pending_role: TrackerPendingRole;
}

export interface TrackerContract {
  /** null = no verdict yet ("unchecked"). */
  ok: boolean | null;
  summary: string;
  checked_at: string;
  optional_present: string[];
  features: Record<string, unknown>;
}

export interface TrackerFreshness {
  issues: number;
  prs: number;
  last_refresh_at: string;
}

/** Where a source's entry came from: the operator's config, or the self-service
 *  registry a team wrote to. */
export type TrackerSourceOrigin = "config" | "registry";

export interface TrackerSource {
  name: string;
  host: string;
  port: number;
  database: string;
  /** Endpoint and credential present. */
  configured: boolean;
  /** Only `registry` sources are administered from the dashboard. */
  origin: TrackerSourceOrigin;
  contract: TrackerContract;
  breaker: { state: BreakerState; consecutive_failures: number };
  /** One standard namespace's counts; `{}` when the source could not
   *  answer in time or has only restricted namespaces. */
  freshness: TrackerFreshness | Record<string, never>;
  namespaces: TrackerSourceNamespace[];
  /** Read through a mapping of the team's own tables, or loom's views. */
  mode?: "mapping" | "views";
  mapping?: SourceMapping | null;
}

export interface TrackerSourceListResponse {
  sources: TrackerSource[];
  /** Whether `tracker.source.add/rotate/remove` answer on this deployment. */
  self_service: boolean;
}

/** The freshness object when it carries counts, else null. */
export function freshnessOf(s: Pick<TrackerSource, "freshness">): TrackerFreshness | null {
  const { issues, prs, last_refresh_at } = s.freshness;
  return typeof issues === "number" && typeof prs === "number" ? { issues, prs, last_refresh_at } : null;
}

/** A namespace the leader has not registered yet cannot be joined; every
 *  other standing is the BranchChip rule with `member_role` /
 *  `pending_role` in place of `my_status`. */
export type TrackerNamespaceState = JoinState | "registering";

export function namespaceState(
  ns: Pick<TrackerSourceNamespace, "member_role" | "pending_role" | "join_policy"> &
    Partial<Pick<TrackerSourceNamespace, "registered">>,
): TrackerNamespaceState {
  if (ns.registered === false) return "registering";
  return joinState({ member: ns.member_role !== "", pending: ns.pending_role !== "", join_policy: ns.join_policy });
}

/** Sources you read first, then the ones you can join at once, then by name
 *  — the Repositories order. */
export function sortSources(sources: readonly TrackerSource[]): TrackerSource[] {
  const reads = (s: TrackerSource) => (s.namespaces.some((n) => namespaceState(n) === "member") ? 1 : 0);
  const instant = (s: TrackerSource) => (s.namespaces.some((n) => namespaceState(n) === "open") ? 1 : 0);
  return [...sources].sort((a, b) => reads(b) - reads(a) || instant(b) - instant(a) || a.name.localeCompare(b.name));
}

/** An absent op is a stated result, never an error banner. The transport answers an op
 *  this deployment does not serve with 404, or with an unknown-op code. */
export function isOpAbsent(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  if (err.status === 404 || err.status === 405) return true;
  return /^(UNKNOWN_OP|OP_NOT_FOUND|NOT_FOUND|UNKNOWN_OPERATION)$/.test(err.code);
}

/** The words a person knows a namespace by: its name, and its repos when
 *  the source binds it to some. */
export function namespaceLabel(ns: Pick<TrackerSourceNamespace, "namespace" | "repos">): string {
  return ns.repos.length ? `${ns.namespace} (${ns.repos.join(", ")})` : ns.namespace;
}

/** An operator-owned source (config): no rotate/remove from the
 *  dashboard, a caption instead. */
export function operatorOwned(source: Pick<TrackerSource, "origin">): boolean {
  return source.origin !== "registry";
}

/** Whether the caller may see the source's admin verbs (a request the token cannot make
 *  is never offered): a registry source the caller maintains, once the write ops
 *  (`tracker.source.add/check/rotate/remove`) are listed by `ops.capabilities`. Out of
 *  scope here; this is the gate the menu will hang from. */
export function canAdminister(source: Pick<TrackerSource, "namespaces" | "origin">, opsPresent: boolean): boolean {
  return opsPresent && source.origin === "registry" && source.namespaces.some((n) => n.member_role === "admin");
}

// ---------------------------------------------------------------------------
// Registering a source from the page, and binding one.
// ---------------------------------------------------------------------------

/** The full contract report `tracker.source.add` / `check` return
 *  (`ContractReport.to_dict`, server/tracker/app/contract.py). */
export interface TrackerContractReport {
  ok: boolean | null;
  summary: string;
  /** Views (loom's tables exposed directly) or a mapping. */
  mode?: "views" | "mapping";
  /** What the tracker's data lets loom answer. */
  capabilities?: Record<string, boolean>;
  mapping_defaulted?: Record<string, string[]>;
  mapping_errors?: string[];
  database?: string;
  missing_tables?: string[];
  missing_columns?: Record<string, string[]>;
  type_mismatches?: Record<string, Record<string, [string, string]>>;
  optional_present?: string[];
  optional_incomplete?: Record<string, string[]>;
  features?: Record<string, boolean>;
  aliased?: Record<string, Record<string, string>>;
  defaulted?: Record<string, Record<string, string>>;
}

export interface TrackerSourceForm {
  name: string;
  host: string;
  port: number;
  secure: boolean;
  database: string;
  user: string;
  password: string;
  namespace: string;
  repos: string[];
  restricted: boolean;
  join_policy: "open" | "approval";
}

export interface TrackerSourceAddResult {
  name: string;
  host: string;
  database: string;
  namespaces: { namespace: string; repos: string[]; classification: TrackerClassification; join_policy: JoinPolicy }[];
  contract: TrackerContractReport;
  roles: string[];
  my_role: string;
  dry_run: boolean;
  written?: boolean;
  /** table → a CREATE OR REPLACE VIEW draft for each required table the schema fails on. */
  view_templates?: Record<string, string>;
}

export interface TrackerSourceCheckResult {
  name: string;
  database: string;
  reachable: boolean;
  contract: TrackerContractReport;
  view_templates?: Record<string, string>;
}

/** The server's rules (`server/tracker/app/sources.py`), said before the round trip. */
const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
const NS_RE = /^(?:tracker|qa)-[a-z0-9][a-z0-9-]{1,62}$/;
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

/** `tracker-<name>`, the namespace a source's form proposes (`_` is not a namespace character). */
export function defaultNamespaceFor(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug ? `tracker-${slug}` : "";
}

/** What keeps the form from a Check, one sentence per field; [] = ready. */
export function sourceFormProblems(f: TrackerSourceForm): string[] {
  const out: string[] = [];
  if (!NAME_RE.test(f.name.trim())) out.push("Name: 2–32 lower-case letters, digits, - or _, starting with a letter.");
  if (!f.host.trim()) out.push("Host is required.");
  if (!Number.isInteger(f.port) || f.port < 1 || f.port > 65535) out.push("Port must be 1–65535.");
  if (!f.database.trim()) out.push("Database is required — the one holding the views.");
  if (!f.user.trim()) out.push("A read-only user is required.");
  if (!f.password) out.push("Password is required.");
  if (!NS_RE.test(f.namespace.trim())) out.push("Namespace: tracker-<team>, lower-case letters, digits and -.");
  const bad = f.repos.find((r) => !REPO_RE.test(r));
  if (bad) out.push(`Repositories are Org/repo, not “${bad}”.`);
  if (f.repos.length === 0) out.push("Name at least one repository: a tracker reads only the repositories it lists.");
  return out;
}

/** The request body; the same for the Check (dry run) and the Register. */
export function sourceAddBody(
  org: string,
  f: TrackerSourceForm,
  dryRun: boolean,
  mapping: unknown = null,
): Record<string, unknown> {
  return {
    // How the team's tables map onto loom's model (null = views)
    ...(mapping ? { mapping } : {}),
    org,
    name: f.name.trim(),
    host: f.host.trim(),
    port: f.port,
    secure: f.secure,
    database: f.database.trim(),
    user: f.user.trim(),
    password: f.password,
    namespaces: [
      {
        namespace: f.namespace.trim(),
        repos: f.repos,
        restricted: f.restricted,
        join_policy: f.restricted ? null : f.join_policy,
      },
    ],
    dry_run: dryRun,
  };
}

/** A fingerprint of everything the Check read: Register stays off until the
 *  form it would send is the one that passed. */
export function formFingerprint(f: TrackerSourceForm): string {
  return JSON.stringify({ ...f, repos: [...f.repos].sort() });
}

export type ContractTableState = "ok" | "aliased" | "missing" | "incomplete" | "absent";

export interface ContractTableRow {
  table: string;
  required: boolean;
  state: ContractTableState;
  /** One line: what is missing, mistyped, aliased or defaulted. */
  detail: string;
}

const LOGICAL_TABLES = ["github_issues", "github_prs", "edges"] as const;

/** The report, table by table: the required three always, then the optional
 *  tables the source holds (complete or not). */
export function contractRows(r: TrackerContractReport): ContractTableRow[] {
  const rows: ContractTableRow[] = [];
  const joined = (m?: Record<string, string>) => (m ? Object.keys(m).join(", ") : "");
  for (const table of LOGICAL_TABLES) {
    const missingCols = r.missing_columns?.[table] ?? [];
    const mism = r.type_mismatches?.[table] ?? {};
    const aliased = joined(r.aliased?.[table]);
    const defaulted = joined(r.defaulted?.[table]);
    if ((r.missing_tables ?? []).includes(table)) {
      rows.push({ table, required: true, state: "missing", detail: "no such table or view in the database" });
    } else if (missingCols.length || Object.keys(mism).length) {
      const parts = [
        missingCols.length ? `missing ${missingCols.join(", ")}` : "",
        ...Object.entries(mism).map(([c, [want, have]]) => `${c} is ${have}, the contract reads ${want}`),
      ].filter(Boolean);
      rows.push({ table, required: true, state: "incomplete", detail: parts.join("; ") });
    } else {
      const parts = [aliased && `read another way: ${aliased}`, defaulted && `not provided: ${defaulted}`].filter(
        Boolean,
      );
      rows.push({
        table,
        required: true,
        state: parts.length ? "aliased" : "ok",
        detail: parts.join("; ") || "every column present",
      });
    }
  }
  for (const table of r.optional_present ?? [])
    rows.push({ table, required: false, state: "ok", detail: "optional, present" });
  for (const [table, what] of Object.entries(r.optional_incomplete ?? {})) {
    rows.push({ table, required: false, state: "incomplete", detail: `optional, not read: ${what.join("; ")}` });
  }
  return rows;
}

export interface BindableRecord {
  namespace: string;
  /** How a person knows it: the tracker, then its scope ("qa · public"). */
  label: string;
  source: string;
  restricted: boolean;
  /** The tracker lists this repository: its reads can return the repository's items. */
  covers: boolean;
  /** The repositories the tracker lists (for the "answers nothing" sentence). */
  repos: string[];
  /** The caller holds an admin grant on the tracker. A hint only: the server decides a new
   *  link, and a token that administers the tracker another way (org-wide) links without one. */
  admin: boolean;
}

/** The tracker namespaces a repository may be bound to, from the one
 *  `tracker.source.list` read: every registered namespace, those naming the
 *  repository first (a record reads only the repositories it names — the
 *  others answer nothing for it). */
export function bindableRecords(sources: readonly TrackerSource[], repo: string): BindableRecord[] {
  const out: BindableRecord[] = [];
  for (const s of sources) {
    for (const ns of s.namespaces) {
      if (!ns.registered) continue;
      out.push({
        namespace: ns.namespace,
        label: scopeLabel(s, ns),
        source: s.name,
        restricted: ns.classification === "restricted",
        covers: ns.repos.includes(repo),
        repos: [...ns.repos],
        admin: ns.member_role === "admin",
      });
    }
  }
  return out.sort((a, b) => Number(b.covers) - Number(a.covers) || a.namespace.localeCompare(b.namespace));
}

// ---------------------------------------------------------------------------
// The tracker as a person reads it.
// ---------------------------------------------------------------------------

/** "qa · public" / "qa · private": the tracker's name, then which scope of it. */
function scopeLabel(
  s: Pick<TrackerSource, "name" | "namespaces">,
  ns: Pick<TrackerSourceNamespace, "classification">,
): string {
  const scoped = s.namespaces.length > 1;
  const scope = ns.classification === "restricted" ? "private" : "public";
  return scoped || ns.classification === "restricted" ? `${s.name} · ${scope}` : s.name;
}

export type TrackerTone = "ok" | "warn" | "danger" | "muted";

export interface TrackerStatus {
  tone: TrackerTone;
  /** One or two words for the pill. */
  label: string;
  /** One sentence: what it means and what to do. */
  detail: string;
}

/** A tracker's state in the words the Trackers list and the card use. */
export function trackerStatus(
  s: Pick<TrackerSource, "configured" | "contract" | "breaker" | "namespaces">,
): TrackerStatus {
  if (!s.configured || s.breaker.state === "unconfigured") {
    return {
      tone: "muted",
      label: "no credential",
      detail: "Declared, but nothing can be read until a read-only credential is added.",
    };
  }
  if (s.namespaces.length > 0 && s.namespaces.every((n) => n.registered === false)) {
    return {
      tone: "muted",
      label: "setting up",
      detail: "Its namespaces are being registered; this takes up to a minute.",
    };
  }
  if (s.contract.ok === false) {
    return {
      tone: "danger",
      label: "schema failing",
      detail: `Reads are paused until the views match: ${s.contract.summary || "a table or column is missing"}.`,
    };
  }
  if (s.breaker.state === "OPEN") {
    return {
      tone: "danger",
      label: "paused",
      detail: `Reads paused after ${s.breaker.consecutive_failures} failures in a row; they resume once the database answers.`,
    };
  }
  if (s.breaker.state === "HALF_OPEN")
    return { tone: "warn", label: "recovering", detail: "Reads are being retried; one success resumes them." };
  if (s.contract.ok === null)
    return { tone: "muted", label: "not checked", detail: "Not checked yet — Check runs the schema check now." };
  return { tone: "ok", label: "ready", detail: "Read live; every table and column Loom needs is there." };
}

export interface ParsedEndpoint {
  host: string;
  port?: number;
  secure?: boolean;
  database?: string;
  user?: string;
}

/**
 * A pasted ClickHouse address, whatever shape the console gave: an https /
 * clickhouse:// URL (with port, `?database=` or a path, `user@`), a
 * `host:port`, or a bare host. null when there is no host in it.
 */
export function parseClickHouseUrl(text: string): ParsedEndpoint | null {
  const raw = text.trim();
  if (!raw) return null;
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : `https://${raw}`;
  let u: URL;
  try {
    u = new URL(withScheme);
  } catch {
    return null;
  }
  const host = u.hostname;
  if (!host || !/^[A-Za-z0-9.-]+$/.test(host)) return null;
  const scheme = u.protocol.replace(":", "").toLowerCase();
  const out: ParsedEndpoint = { host };
  if (u.port) out.port = Number(u.port);
  const tlsPort = out.port === 8443 || out.port === 9440;
  out.secure = scheme === "https" || scheme === "clickhouses" || tlsPort;
  if (out.port === undefined) out.port = out.secure ? 8443 : 8123;
  const db = u.searchParams.get("database") || decodeURIComponent(u.pathname.replace(/^\/+|\/+$/g, ""));
  if (db && /^[A-Za-z_][A-Za-z0-9_]*$/.test(db)) out.database = db;
  if (u.username) out.user = decodeURIComponent(u.username);
  return out;
}

/** The contract as `tracker.contract` returns it. */
export interface ContractColumn {
  name: string;
  family: string;
  type: string;
  must_map: boolean;
  note: string;
}
export interface ContractTable {
  table: string;
  required: boolean;
  note: string;
  columns: ContractColumn[];
}
export interface TrackerContractCatalogue {
  database: string;
  tables: ContractTable[];
  templates: Record<string, string>;
}

/** One `.sql` file: the header that says what to do, then the views asked for. */
export function templatesFile(cat: TrackerContractCatalogue, tables: readonly string[]): string {
  const head = [
    `-- Views Loom reads, in ${cat.database}. Replace <your_table> with the table each record lives in,`,
    "-- map every column marked MUST map, keep a default only where you have no data, then run this",
    "-- in your ClickHouse and press Check in Loom.",
    "-- Keep DEFINER (Loom's user reads the views, not your tables) and loom_uuid5 (the ids Loom computes).",
    "",
  ].join("\n");
  // The read-only user, as the last statement
  const user = [
    "",
    "-- The read-only user Loom connects as: readonly = 2 (SELECT only, timeouts allowed).",
    "CREATE USER IF NOT EXISTS loom_ro IDENTIFIED BY '<password>' SETTINGS readonly = 2;",
    `GRANT SELECT ON ${cat.database}.* TO loom_ro;`,
    "GRANT SELECT ON system.columns TO loom_ro;",
  ].join("\n");
  return (
    head +
    tables
      .map((t) => cat.templates[t])
      .filter(Boolean)
      .join("\n\n") +
    "\n" +
    user +
    "\n"
  );
}

/** The Trackers list's row id, which a card's Details links to. */
export const trackerAnchor = (name: string) => `tracker-${name}`;

// ---------------------------------------------------------------------------
// A tracker's own schema, mapped onto loom's logical model.
// ---------------------------------------------------------------------------

/** A mapping expression (server/tracker/domain/mapping.py): a column
 *  name, or one of a few safe forms. */
export type MapExpr =
  | string
  | { lit: string | number | boolean }
  | { concat: MapExpr[] }
  | { coalesce: MapExpr[] }
  | { eq: [MapExpr, unknown] }
  | { in: [MapExpr, unknown[]] }
  | { has: [MapExpr, unknown] }
  | { cast: [MapExpr, string] }
  | { lower: MapExpr }
  | { digits: MapExpr };

export type LogicalTable = (typeof LOGICAL_TABLES)[number];

export interface TableMapping {
  source: string;
  final: boolean;
  columns: Record<string, MapExpr>;
}
export interface SourceMapping {
  version: 1;
  tables: Partial<Record<LogicalTable, TableMapping>>;
}

export interface InspectedTable {
  table: string;
  engine: string;
  rows: number;
  columns: { name: string; type: string }[];
}
export interface MappingProposal {
  mode: "views" | "mapping";
  roles: Partial<Record<LogicalTable, string>> & { edges_derived_from?: string };
  fields: Partial<Record<LogicalTable, Record<string, { from: MapExpr; confidence: "high" | "medium" | "low" }>>>;
  mapping: SourceMapping | null;
}
export interface TrackerInspectResult {
  host: string;
  database: string;
  tables: InspectedTable[];
  roles: string[];
  proposal: MappingProposal;
}

/** How the page names each logical table (never its internal name). */
export const ROLE_LABEL: Record<LogicalTable, { title: string; hint: string; required: boolean }> = {
  github_issues: { title: "Items", hint: "issues, tickets, bugs, incidents — the rows agents search", required: true },
  github_prs: {
    title: "Pull requests",
    hint: "changes that fix items; bisect and the fix graph use them",
    required: false,
  },
  edges: { title: "Links", hint: "which change fixes which item, duplicates, backports", required: false },
};

export interface FieldSpec {
  key: string;
  label: string;
  hint: string;
  core?: boolean;
  kind?: "ref" | "value" | "type";
}

/** The fields the page shows per table, most useful first; everything
 *  unlisted keeps its default (loom fills it). */
export const ROLE_FIELDS: Record<LogicalTable, FieldSpec[]> = {
  github_issues: [
    { key: "source_id", label: "Reference", hint: "'Org/repo#number' — built below", core: true, kind: "ref" },
    { key: "number", label: "Number", hint: "the item's number", core: true },
    { key: "title", label: "Title", hint: "every search reads it", core: true },
    { key: "created_at", label: "Opened", hint: "when it was created", core: true },
    { key: "body", label: "Text", hint: "the duplicate check reads it" },
    { key: "closed_at", label: "Closed", hint: "empty while open" },
    { key: "updated_at", label: "Updated", hint: "defaults to the latest of opened / closed" },
    { key: "author", label: "Author", hint: "who opened it" },
    { key: "labels", label: "Labels", hint: "an array of strings" },
    { key: "url", label: "Link", hint: "a URL a person can open" },
    { key: "primary_component", label: "Component", hint: "your area / module taxonomy" },
    { key: "severity_level", label: "Severity", hint: "your severity or priority" },
    { key: "state_reason", label: "Close reason", hint: "completed / not_planned / duplicate" },
    { key: "affected_versions", label: "Affected versions", hint: "an array of strings" },
    { key: "fix_versions", label: "Fixed in", hint: "an array of strings" },
  ],
  github_prs: [
    { key: "source_id", label: "Reference", hint: "'Org/repo#number' — built below", core: true, kind: "ref" },
    { key: "pr_number", label: "Number", hint: "the PR's number", core: true },
    { key: "title", label: "Title", hint: "", core: true },
    { key: "created_at", label: "Opened", hint: "", core: true },
    { key: "body", label: "Text", hint: "" },
    { key: "closed_at", label: "Closed", hint: "empty while open" },
    { key: "is_merged", label: "Merged?", hint: "1/0; state defaults from it" },
    { key: "merged_at", label: "Merged at", hint: "defaults to Closed when merged" },
    { key: "state", label: "State", hint: "open / closed / merged — defaults from Merged? and Closed" },
    { key: "author", label: "Author", hint: "" },
    { key: "base_branch", label: "Base branch", hint: "" },
    { key: "affected_file_paths", label: "Changed files", hint: "an array of paths — bisect needs it or the diff" },
    { key: "diff", label: "Diff", hint: "the unified diff — bisect needs it or the files" },
    { key: "additions", label: "Lines added", hint: "" },
    { key: "deletions", label: "Lines removed", hint: "" },
    { key: "url", label: "Link", hint: "" },
  ],
  edges: [
    { key: "from_ref", label: "From", hint: "the change's 'Org/repo#number'", core: true, kind: "ref" },
    { key: "to_ref", label: "To", hint: "the item's 'Org/repo#number'", core: true, kind: "ref" },
    { key: "rel_type", label: "Relation", hint: "fixes / duplicate_of / related_to …", core: true, kind: "type" },
    { key: "from_type", label: "From kind", hint: "github_pr / github_issue …", core: true, kind: "type" },
    { key: "to_type", label: "To kind", hint: "", core: true, kind: "type" },
    { key: "created_at", label: "When", hint: "" },
  ],
};

export const CAPABILITY_LABEL: Record<string, string> = {
  items: "Read and search items",
  duplicate_check: "Duplicate check",
  duplicate_calibration: "Calibrated duplicate verdicts",
  pull_requests: "Pull requests",
  links: "Fix graph (which change fixes what)",
  severity_board: "Severity and component board",
  bisect: "Bisect candidates from PRs",
  incidents: "Incidents",
  ci_checks: "CI test history",
};

/** A readable one-line rendering of an expression ("repo + '#' + issue_id"). */
export function describeExpr(e: MapExpr | undefined): string {
  if (e === undefined || e === "") return "";
  if (typeof e === "string") return e;
  if ("lit" in e) return JSON.stringify(e.lit);
  if ("concat" in e) return e.concat.map(describeExpr).join(" + ");
  if ("coalesce" in e) return `first of (${e.coalesce.map(describeExpr).join(", ")})`;
  if ("eq" in e) return `${describeExpr(e.eq[0])} = ${JSON.stringify(e.eq[1])}`;
  if ("in" in e) return `${describeExpr(e.in[0])} in ${JSON.stringify(e.in[1])}`;
  if ("has" in e) return `${describeExpr(e.has[0])} has ${JSON.stringify(e.has[1])}`;
  if ("cast" in e) return `${describeExpr(e.cast[0])} as ${e.cast[1]}`;
  if ("lower" in e) return `lower(${describeExpr(e.lower)})`;
  if ("digits" in e) return `number in ${describeExpr(e.digits)}`;
  return JSON.stringify(e);
}

/** A reference ('Org/repo#number') as its two parts, when it is one of the
 *  two shapes the page builds: [repo column | fixed repo] + '#' + number column. */
/** The number part: a column, or the trailing number of a key column ('HD-7' → 7). */
export type RefNumber = string | { digits: string };
export interface RefParts {
  repo: { column: string } | { fixed: string };
  number: RefNumber;
}

function asNumber(e: MapExpr): RefNumber | null {
  if (typeof e === "string") return e;
  if (typeof e === "object" && "digits" in e && typeof e.digits === "string") return { digits: e.digits };
  return null;
}

export function refParts(e: MapExpr | undefined): RefParts | null {
  if (!e || typeof e === "string" || !("concat" in e)) return null;
  const c = e.concat;
  if (c.length === 3 && typeof c[0] === "string" && typeof c[1] === "object" && "lit" in c[1] && c[1].lit === "#") {
    const n = asNumber(c[2]);
    return n === null ? null : { repo: { column: c[0] }, number: n };
  }
  if (
    c.length === 2 &&
    typeof c[0] === "object" &&
    "lit" in c[0] &&
    typeof c[0].lit === "string" &&
    c[0].lit.endsWith("#")
  ) {
    const n = asNumber(c[1]);
    return n === null ? null : { repo: { fixed: c[0].lit.slice(0, -1) }, number: n };
  }
  return null;
}

export function refExpr(p: RefParts): MapExpr {
  if ("column" in p.repo) return { concat: [p.repo.column, { lit: "#" }, p.number] };
  return { concat: [{ lit: `${p.repo.fixed}#` }, p.number] };
}

/** What still keeps a mapping from a Check: the items table and its core fields. */
export function mappingProblems(m: SourceMapping | null): string[] {
  if (!m) return [];
  const out: string[] = [];
  if (!m.tables.github_issues) out.push("Pick the table that holds your items.");
  for (const role of LOGICAL_TABLES) {
    const t = m.tables[role];
    if (!t) continue;
    for (const f of ROLE_FIELDS[role].filter((x) => x.core)) {
      if (t.columns[f.key] === undefined || t.columns[f.key] === "")
        out.push(`${ROLE_LABEL[role].title}: map ${f.label}.`);
    }
  }
  return out;
}
