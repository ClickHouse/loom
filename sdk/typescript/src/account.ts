/**
 * The signed-in person's own tokens, outside the op registry: what the
 * dashboard's Tokens list does, mirroring the Python SDK's `agent.account`.
 *
 *     const sa = await loom.account.createServiceAccount("ci", ["support", { namespace: "docs", role: "viewer" }]);
 *     // sa.token is shown once; loom.account.revokeServiceAccount("ci") ends it
 */

import type { CallOptions, Transport } from "./transport.js";

/** The role a token is granted on a namespace. */
export type GrantRole = "viewer" | "writer" | "admin";

/** One namespace of a new token: a bare name, or a name with its own role. */
export type ServiceAccountNamespace = string | { namespace: string; role?: GrantRole };

export interface CreateServiceAccountOptions {
  /** The role of each bare namespace; default: the most your sign-in can give there. */
  role?: GrantRole;
  /** Days until it expires, 1..3650; 0 never expires; `null` is the server's default (90). Default 90. */
  ttlDays?: number | null;
}

/** A token just made: `token` is in this answer once and never again. */
export interface ServiceAccountCreated {
  token: string;
  name: string;
  sa_subject: string;
  org: string;
  /** In read-priority order. */
  namespaces: string[];
  ns_roles: Record<string, GrantRole>;
  created_at: string;
  /** `null`: never expires. */
  expires_at: string | null;
  /** The restricted namespaces the token was granted directly. */
  restricted_namespaces: string[];
}

/** One row of your token list: a service account or an MCP client token. */
export interface ServiceAccountRow {
  name: string;
  sa_subject: string;
  namespaces: string[];
  roles: GrantRole[];
  ttl_days: number;
  created_by: string;
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  /** `""` while live. */
  revoked_by: string;
  last_used_at: string | null;
  /** `service_account`, or `mcp` for a token the Setup page saved. */
  kind: string;
  /** The MCP client a Setup-page token is for; `""` otherwise. */
  client: string;
  ns_roles: Record<string, GrantRole>;
}

/** The answer to a revoke. */
export interface ServiceAccountRevoked {
  revoked: boolean;
  name: string;
  revoked_at: string;
  /** The token's own restricted-namespace grants revoked with it. */
  grants_revoked: number;
}

export class AccountClient {
  constructor(private readonly transport: Transport) {}

  /** Your service accounts and MCP client tokens, newest first. */
  async serviceAccounts(opts?: CallOptions): Promise<ServiceAccountRow[]> {
    return (await this.transport.get<{ rows: ServiceAccountRow[] }>("me/service_accounts", opts)).rows;
  }

  /**
   * A scoped token for a machine, never wider than your own grants.
   * `namespaces` in read order. A live token holding `name` is a
   * `PreconditionFailed` with code `TOKEN_NAME_TAKEN`.
   */
  createServiceAccount(
    name: string,
    namespaces: ServiceAccountNamespace[],
    options: CreateServiceAccountOptions = {},
    opts?: CallOptions,
  ): Promise<ServiceAccountCreated> {
    const body: Record<string, unknown> = {
      name,
      namespaces,
      ttl_days: options.ttlDays === undefined ? 90 : options.ttlDays,
    };
    if (options.role !== undefined) body.role = options.role;
    return this.transport.post<ServiceAccountCreated>("me/service_accounts.create", body, opts);
  }

  /** Revoke a token by name: it stops authenticating, and its own grants go with it. */
  revokeServiceAccount(name: string, opts?: CallOptions): Promise<ServiceAccountRevoked> {
    return this.transport.post<ServiceAccountRevoked>("me/service_accounts.revoke", { name }, opts);
  }
}
