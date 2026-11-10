/**
 * The signed-in person's own tokens, outside the op registry: what the
 * dashboard's Tokens list does, the token calls of the Python SDK's
 * `agent.account`.
 *
 *     const t = await loom.account.createToken();  // reads everything you belong to
 *     const ci = await loom.account.createToken(["support", { namespace: "docs", role: "viewer" }], { name: "ci" });
 *     // ci.token is shown once; loom.account.revokeToken("ci") ends it
 */

import type { CallOptions, Transport } from "./transport.js";

/** The role a token is granted on a namespace. */
export type GrantRole = "viewer" | "writer" | "admin";

/** One namespace of a new token: a bare name, `*` (every namespace you belong to, now
 *  and later), or a name with its own role (only in a list without `*`). */
export type TokenNamespace = string | { namespace: string; role?: GrantRole };

export interface CreateTokenOptions {
  /** Its name in your token list; default `token-<day>-<hex>`. */
  name?: string;
  /** The role of each bare namespace of a list; default: the most your sign-in can give there. */
  role?: GrantRole;
  /** Days until it expires, 1..3650; 0 never expires; `null` is the server's default (90). Default 90. */
  ttlDays?: number | null;
  /** A list's save target: one of the list's memory namespaces the token may write; default
   *  the parent token's when it may write there, else the first.
   *  With `"*"`, name the namespace before `"*"` instead. */
  savesTo?: string;
}

/** One MCP client's setup for a new token. */
export interface TokenClientSetup {
  config_key: string;
  config: Record<string, unknown>;
  /** The file's own text when it is not JSON (Codex's TOML); `""` otherwise. */
  config_toml: string;
  /** A one-line registration command; `""` when the client has none. */
  command: string;
  /** Variables to export before the client starts (Codex: `LOOM_API_KEY`); `{}` otherwise. */
  env: Record<string, string>;
  config_path: { darwin: string; linux: string; win32: string };
}

/** A token just made: `token` is in this answer once and never again. */
export interface TokenCreated {
  token: string;
  name: string;
  sa_subject: string;
  org: string;
  /** The claim as minted: `*` and the names beside it, or the list it reads, in read order. */
  namespaces: string[];
  /** The role per namespace of a list token; `{}` for a `*` token. */
  ns_roles: Record<string, GrantRole>;
  /** The one role of a token without `ns_roles`; `[]` for a list with a role each. */
  roles: GrantRole[];
  reads_everything: boolean;
  /** Where calls that name no namespace save; `""` none; `null` not readable now. */
  saves_to: string | null;
  created_at: string;
  /** `null`: never expires. */
  expires_at: string | null;
  /** The restricted namespaces the token was granted directly. */
  restricted_namespaces: string[];
  mcp_url: string;
  /** Each supported MCP client's setup, by client id. */
  clients: Record<string, TokenClientSetup>;
}

/** One row of your token list. */
export interface TokenRow {
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
  ns_roles: Record<string, GrantRole>;
  /** Where a list token saves, as minted; `""` for a `*` token, one with no default target,
   *  or one minted before tokens recorded it; absent from the previous build's answer. */
  saves_to?: string;
  /** Made by you, under any spelling of yours; absent from the previous build's answer. */
  yours?: boolean;
}

/** The answer to a revoke. */
export interface TokenRevoked {
  revoked: boolean;
  name: string;
  revoked_at: string;
  /** The token's own restricted-namespace grants revoked with it. */
  grants_revoked: number;
}

export class AccountClient {
  constructor(private readonly transport: Transport) {}

  /** The tokens you made, and other people's that name a namespace you administer, newest
   *  first; each row's `yours` marks your own. */
  async tokens(opts?: CallOptions): Promise<TokenRow[]> {
    return (await this.transport.get<{ rows: TokenRow[] }>("me/service_accounts", opts)).rows;
  }

  /**
   * A token for your tools, agents, scripts or CI, never wider than your own grants.
   * `namespaces`: `"*"` (the default) reads every namespace you belong to; name one
   * before `"*"` to save there and restricted ones after it to read them too. A list
   * without `"*"` reads exactly those, in read order. A live token holding the name is
   * a `PreconditionFailed` with code `TOKEN_NAME_TAKEN`.
   */
  createToken(
    namespaces: "*" | TokenNamespace[] = "*",
    options: CreateTokenOptions = {},
    opts?: CallOptions,
  ): Promise<TokenCreated> {
    const body: Record<string, unknown> = {
      namespaces,
      ttl_days: options.ttlDays === undefined ? 90 : options.ttlDays,
    };
    if (options.name !== undefined) body.name = options.name;
    if (options.role !== undefined) body.role = options.role;
    if (options.savesTo !== undefined) body.saves_to = options.savesTo;
    return this.transport.post<TokenCreated>("me/service_accounts.create", body, opts);
  }

  /** Revoke a token by name: it stops authenticating, and its own grants go with it. */
  revokeToken(name: string, opts?: CallOptions): Promise<TokenRevoked> {
    return this.transport.post<TokenRevoked>("me/service_accounts.revoke", { name }, opts);
  }
}
