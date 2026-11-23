import { post, type Identity, type RequestOpts } from "../api";

type Scope = Pick<Identity, "org" | "namespace">;
export interface QueryHistorySource {
  name: string;
  source_id: string;
  kind: "clickhouse_queries";
  host: string;
  port: number;
  secure: boolean;
  user: string;
  databases: string[];
  cluster: string;
  created_at: string;
}
export type QueryHistoryRegistration = Omit<QueryHistorySource, "source_id" | "kind" | "created_at"> & {
  password: string;
};
export interface QueryHistorySources {
  enabled: boolean;
  sources: QueryHistorySource[];
}
export function listQueryHistory(id: Scope, opts: RequestOpts = {}): Promise<QueryHistorySources> {
  return post("/v1/knowledge.query_history.list", { ...id, limit: 1000 }, opts.signal);
}
export function addQueryHistory(id: Scope, source: QueryHistoryRegistration, intent: "automatic" | "explicit" = "automatic"): Promise<QueryHistorySource> {
  return post("/v1/knowledge.query_history.add", { ...id, ...source }, undefined, intent);
}
export function removeQueryHistory(id: Scope, name: string, intent: "automatic" | "explicit" = "automatic"): Promise<{ removed: boolean }> {
  return post("/v1/knowledge.query_history.remove", { ...id, name }, undefined, intent);
}
