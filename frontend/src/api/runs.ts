// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { Identity } from "../api";

// ---------------------------------------------------------------------------
// Run ledger: one row per agent execution — goal → recalled → used → written → cost → outcome. Wire shapes mirror server/tasks/api/models_run.py.
// ---------------------------------------------------------------------------

export type RunStatus = "active" | "completed" | "failed" | "cancelled";

export interface RunRow {
  run_id: string;
  agent: string;
  user_id: string;
  session_id: string;
  goal: string;
  user_request: string;
  expected_outcome: string;
  environment: string;
  model: string;
  consumer: string;
  status: RunStatus;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  final_response: string;
  confidence: number;
  memories_used: string[];
  memories_written: string[];
  metadata: Record<string, unknown>;
  budget_usd: number; // 0 = none
  budget_seconds: number;
  /** `pass`, `fail`, or "" when not graded; the source, reason and time come with it. */
  grade: string;
  grade_source: string;
  grade_reason: string;
  graded_at: string;
}

export interface RunGetResponse {
  found: boolean;
  run: RunRow | null;
  recalled: number; // -1 = recall telemetry off
  recalled_keys: string[];
  memories_written: string[];
  observations: number;
  turns: number;
  cost_usd: number;
  unpriced_turns: number;
  elapsed_seconds: number;
  over_budget: ("cost" | "time")[];
}

export type RunOrder =
  | "started_desc"
  | "started_asc"
  | "duration_desc"
  | "duration_asc"
  | "relied_on_desc"
  | "relied_on_asc";

export interface RunListOptions {
  status?: RunStatus | "";
  agent?: string;
  limit?: number;
  // Runs to skip before the page (0 = first page).
  offset?: number;
  // Every order breaks ties on started_at then run_id, so pages never overlap.
  orderBy?: RunOrder;
  // Case-insensitive text in the goal, run id or agent; blank = no filter.
  q?: string;
  fields?: string[];
  memoryActivity?: "" | "with" | "without";
}

export interface RunListPage {
  runs: RunRow[];
  // Runs matching every filter, whatever limit and offset cut. The server sends it on the
  // first page, the last page and a page past the end; null on a full page after the first.
  total: number | null;
  // False when the server does not page (a replica a release behind): it ignores `offset`
  // and answers its newest runs with no total.
  paged: boolean;
}

// The largest `offset` run.list serves (its `le`); a larger one is a 422.
export const RUN_LIST_MAX_OFFSET = 100_000;
// The longest search run.list takes (its `q` max_length): a longer one is cut, not a 422.
export const RUN_LIST_MAX_Q = 200;

// A server a release behind ignores offset / order_by / q (named in its ignored_args)
// and answers the newest first page without a total.
export async function listRunsPage(
  id: { org: string; namespace: string },
  opts: RunListOptions = {},
): Promise<RunListPage> {
  // Cut by characters (code points, as the server counts), never inside one.
  const q = opts.q ? Array.from(opts.q.trim()).slice(0, RUN_LIST_MAX_Q).join("") : undefined;
  const out = await post<{ runs: RunRow[]; total?: number; ignored_args?: string[] }>("/v1/run.list", {
    org: id.org,
    namespace: id.namespace,
    status: opts.status ?? "",
    agent: opts.agent ?? "",
    limit: opts.limit ?? 100,
    // The fold is a server filter so `limit` counts visible runs.
    memory_activity: opts.memoryActivity ?? "",
    ...(opts.offset ? { offset: opts.offset } : {}),
    ...(opts.orderBy ? { order_by: opts.orderBy } : {}),
    ...(q ? { q } : {}),
    ...(opts.fields ? { fields: opts.fields } : {}),
  });
  const total = typeof out.total === "number" ? out.total : null;
  const paged = opts.offset ? !(out.ignored_args ?? []).includes("offset") : total !== null;
  return { runs: out.runs || [], total, paged };
}

// The page's rows alone, for callers that do not page.
export async function listRuns(id: { org: string; namespace: string }, opts: RunListOptions = {}): Promise<RunRow[]> {
  return (await listRunsPage(id, opts)).runs;
}

export async function getRun(id: { org: string; namespace: string }, runId: string): Promise<RunGetResponse> {
  return post<RunGetResponse>("/v1/run.get", { org: id.org, namespace: id.namespace, run_id: runId });
}

/** POST /v1/run.grade: whether a closed run's result held up, from the person who knows. */
export async function gradeRun(
  id: Pick<Identity, "org" | "namespace">,
  runId: string,
  grade: "pass" | "fail",
  reason: string,
): Promise<{ graded: number; skipped_active: number; unchanged: number }> {
  return post("/v1/run.grade", {
    org: id.org,
    namespace: id.namespace,
    run_id: runId,
    grade,
    reason,
    source: "dashboard",
  });
}
