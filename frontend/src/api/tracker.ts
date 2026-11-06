// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type {
  SourceMapping,
  TrackerContractCatalogue,
  TrackerInspectResult,
  TrackerSourceAddResult,
  TrackerSourceCheckResult,
  TrackerSourceListResponse,
} from "../lib/trackerSources";
import type { RequestOpts } from "../api";

// ---------------------------------------------------------------------------
// qa.* — the tracker plane behind the Explorer's Patterns mode. Three thin wrappers: what
// a namespace may ask, an answer from the catalogue's templates, and the tracker record
// of up to ten numbers.

export interface TrackerBinding {
  /** The tracker namespace that answers for the addressed one; "" when none. */
  tracker_namespace: string;
  restricted: boolean;
  /** qa: a tracker namespace itself · code: a code namespace bound to one ·
   *  none: nothing to ask (unbound, refused or unknown — one answer). */
  kind: "tracker" | "code" | "none";
  authorized: boolean;
  /** Catalogue template names the caller may run there. */
  templates: string[];
}

export interface TrackerSection {
  template: string;
  title: string;
  access?: string;
  params: Record<string, unknown>;
  why?: string;
  /** served | empty | restricted_required | tracker_unavailable | planned */
  status: string;
  cites?: string;
  rows: Record<string, unknown>[];
}

export interface TrackerAnswer {
  intent: string;
  anchors: Record<string, unknown>;
  sections: TrackerSection[];
  searched: string[];
  lines: string[];
  restricted: boolean;
  /** planned | answered | empty | unknown */
  verdict: string;
}

/** The compact QA fact of one issue or PR (`server/tracker/app/facts.py`):
 *  only keys with a value are present. */
export interface TrackerFact {
  kind?: "issue" | "pr";
  component?: string;
  severity?: string;
  labels?: string[];
  affected_versions?: string[];
  bisect_outcome?: string;
  introduced_commit?: string;
  is_flaky_test?: boolean;
  is_crash_ci?: boolean;
  has_test?: boolean;
  /** Issue: the PRs whose `fixes` edge points at it. */
  fixed_by?: number[];
  /** PR: the issues it fixes. */
  fixes?: number[];
  /** The PR(s) recorded as having introduced this issue. */
  caused_by?: number[];
  /** PR: the issues the graph says it caused. */
  caused?: number[];
  backports?: number[];
  backported_by?: number[];
  minor_version?: string;
  url?: string;
  /** Present when a bisect answer rode along. */
  named_by_graph?: number[];
}

export interface TrackerItem {
  number: number;
  kind: "issue" | "pr" | string;
  state: string;
  is_merged: boolean;
  title: string;
  author: string;
  opened_at: string;
  closed_at: string;
  url: string;
  tracker: TrackerFact;
  /** The one-line `[qa]` rendering of the fact. */
  line: string;
}

export interface TrackerItemResponse {
  items: TrackerItem[];
  /** Numbers the tracker has no row for — unknown, not absent. */
  unknown: number[];
  tracker_unavailable: boolean;
}

export async function trackerBinding(
  id: { org: string; namespace: string },
  opts: RequestOpts = {},
): Promise<TrackerBinding> {
  return post<TrackerBinding>("/v1/tracker.binding", { org: id.org, namespace: id.namespace }, opts.signal);
}

export async function trackerAnswer(
  id: { org: string; namespace: string },
  opts: { question?: string; anchors?: Record<string, unknown>; limit?: number } & RequestOpts = {},
): Promise<TrackerAnswer> {
  return post<TrackerAnswer>(
    "/v1/tracker.answer",
    {
      org: id.org,
      namespace: id.namespace,
      question: opts.question ?? "",
      anchors: opts.anchors ?? {},
      limit: opts.limit ?? 10,
    },
    opts.signal,
  );
}

export async function trackerItem(
  id: { org: string; namespace: string },
  numbers: number[],
  opts: RequestOpts = {},
): Promise<TrackerItemResponse> {
  return post<TrackerItemResponse>(
    "/v1/tracker.item",
    { org: id.org, namespace: id.namespace, numbers: numbers.slice(0, 10) },
    opts.signal,
  );
}

/** The tracker sources of the org and where the caller stands on each of their namespaces
 *  — the `code.repos` of the tracker plane. `tracker.source.list` is org-level, viewer
 *  role, experimental; this literal is its dashboard reach in the manifest
 *  (`server/operations/reach_manifest.py`, `dashboard`). A deployment that does not
 *  serve it answers 404, which the section renders as a stated result. */
export async function listTrackerSources(
  org: string,
  opts: RequestOpts & { fresh?: boolean } = {},
): Promise<TrackerSourceListResponse> {
  // `fresh` right after the caller's own add — the server re-reads its registry
  return post<TrackerSourceListResponse>(
    "/v1/tracker.source.list",
    { org, ...(opts.fresh ? { fresh: true } : {}) },
    opts.signal,
  );
}

// The tracker-source write ops, from the page. The body of `addTrackerSource` is
// `sourceAddBody` (lib/trackerSources.ts); a dry run runs every gate and writes nothing.
export async function addTrackerSource(
  body: Record<string, unknown>,
  opts: RequestOpts = {},
): Promise<TrackerSourceAddResult> {
  return post<TrackerSourceAddResult>("/v1/tracker.source.add", body, opts.signal);
}

export async function checkTrackerSource(
  org: string,
  name: string,
  opts: RequestOpts = {},
): Promise<TrackerSourceCheckResult> {
  return post<TrackerSourceCheckResult>("/v1/tracker.source.check", { org, name }, opts.signal);
}

export async function rotateTrackerSource(
  org: string,
  name: string,
  user: string,
  password: string,
): Promise<{ name: string; contract: TrackerSourceCheckResult["contract"]; rotated_at: string }> {
  return post("/v1/tracker.source.rotate", { org, name, user, password });
}

export async function removeTrackerSource(
  org: string,
  name: string,
  confirm: string,
): Promise<{ name: string; deactivated: boolean; namespaces_retired: string[] }> {
  return post("/v1/tracker.source.remove", { org, name, confirm });
}

/** The contract before a connection — every table and column Loom reads, what each holds, and a blank view per table (no I/O). */
export async function getTrackerContract(
  org: string,
  database: string,
  opts: RequestOpts = {},
): Promise<TrackerContractCatalogue> {
  return post<TrackerContractCatalogue>(
    "/v1/tracker.contract",
    { org, database: database || "<database>" },
    opts.signal,
  );
}

/** Connect with the read-only user and read what the database holds, plus a first mapping of it. Writes nothing. */
export async function inspectTrackerSource(
  body: {
    org: string;
    repos: string[];
  } & (
    | { name: string }
    | { host: string; port: number; secure: boolean; database: string; user: string; password: string }
  ),
  opts: RequestOpts = {},
): Promise<TrackerInspectResult> {
  return post<TrackerInspectResult>("/v1/tracker.source.inspect", body, opts.signal);
}

/** Change a registered tracker's mapping (null = views). */
export async function setTrackerMapping(
  org: string,
  name: string,
  mapping: SourceMapping | null,
  dryRun = false,
): Promise<{ name: string; contract: TrackerSourceCheckResult["contract"]; dry_run: boolean; written: boolean }> {
  return post("/v1/tracker.source.set_mapping", { org, name, mapping, dry_run: dryRun });
}
