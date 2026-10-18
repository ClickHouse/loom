// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";

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
  budget_usd?: number; // 0 = none; absent on a server without budgets
  budget_seconds?: number;
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
  unpriced_turns?: number;
  elapsed_seconds?: number;
  over_budget?: ("cost" | "time")[];
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
  // Runs matching every filter, whatever limit and offset cut. null when the server
  // does not send it (a replica a release behind): the count is unknown.
  total: number | null;
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
  const q = opts.q?.trim().slice(0, RUN_LIST_MAX_Q);
  const out = await post<{ runs: RunRow[]; total?: number }>("/v1/run.list", {
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
  return { runs: out.runs || [], total: typeof out.total === "number" ? out.total : null };
}

// The page's rows alone, for callers that do not page.
export async function listRuns(id: { org: string; namespace: string }, opts: RunListOptions = {}): Promise<RunRow[]> {
  return (await listRunsPage(id, opts)).runs;
}

export async function getRun(id: { org: string; namespace: string }, runId: string): Promise<RunGetResponse> {
  return post<RunGetResponse>("/v1/run.get", {
    org: id.org,
    namespace: id.namespace,
    run_id: runId,
  });
}
