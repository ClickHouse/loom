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

export async function listRuns(
  id: { org: string; namespace: string },
  opts: {
    status?: RunStatus | "";
    agent?: string;
    limit?: number;
    fields?: string[];
    memoryActivity?: "" | "with" | "without";
  } = {},
): Promise<RunRow[]> {
  const out = await post<{ runs: RunRow[] }>("/v1/run.list", {
    org: id.org,
    namespace: id.namespace,
    status: opts.status ?? "",
    agent: opts.agent ?? "",
    limit: opts.limit ?? 100,
    // The fold is a server filter so `limit` counts visible runs.
    memory_activity: opts.memoryActivity ?? "",
    ...(opts.fields ? { fields: opts.fields } : {}),
  });
  return out.runs || [];
}

export async function getRun(id: { org: string; namespace: string }, runId: string): Promise<RunGetResponse> {
  return post<RunGetResponse>("/v1/run.get", { org: id.org, namespace: id.namespace, run_id: runId });
}
