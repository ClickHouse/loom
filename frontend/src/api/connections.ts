import { post, type RequestOpts } from "../api";

export const CONNECTION_KINDS = ["gdrive", "notion", "linear"] as const;
export type ConnectionKind = (typeof CONNECTION_KINDS)[number];

/** The public connection.list entry; credentials never cross this boundary. */
export interface ConnectionEntry {
  kind: ConnectionKind;
  connected: boolean;
  connect_available?: boolean;
  account_email: string | null;
  scopes: string[];
  connect_url: string;
}

export function listConnections(org: string, opts: RequestOpts = {}): Promise<{ connections: ConnectionEntry[] }> {
  return post("/v1/connection.list", { org }, opts.signal);
}

export function disconnectConnection(
  org: string,
  kind: ConnectionKind,
  opts: RequestOpts = {},
): Promise<{ disconnected: boolean }> {
  return post("/v1/connection.disconnect", { org, kind }, opts.signal);
}

export function startConnection(kind: ConnectionKind, opts: RequestOpts = {}): Promise<{ authorization_url: string }> {
  return post(`/v1/connect/${kind}/start`, {}, opts.signal);
}
