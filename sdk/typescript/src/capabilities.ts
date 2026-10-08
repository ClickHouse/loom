/**
 * Probe the deployment before calling it (the onboarding kit's "probe" step, for TS).
 *
 * `ops.capabilities` lists every op the server serves with its required role
 * and request fields. A client that needs specific ops and fields asks once
 * instead of discovering a 404 or a 422 mid-session.
 */

import type { LoomClient } from "./client.js";
import { OP_BY_NAME, type OpCapability } from "./generated/ops.js";
import { roleOn, roleSatisfies, type TokenClaims } from "./token.js";

export interface ProbeResult {
  ok: boolean;
  buildCommit: string;
  schemaVersion: string;
  /** Ops the caller asked for that the deployment does not serve. */
  missingOps: string[];
  /** `op: [fields]` the caller asked for that the deployment's request model lacks. */
  missingFields: Record<string, string[]>;
  /** Ops present on the server but absent from this SDK's generated table (newer server). */
  unknownToSdk: string[];
}

export async function probe(
  client: LoomClient,
  need: { ops?: string[]; fields?: Record<string, string[]> } = {},
): Promise<ProbeResult> {
  const caps = await capabilities(client);
  const missingOps = (need.ops ?? []).filter((o) => !caps.has(o));
  const missingFields: Record<string, string[]> = {};
  for (const [op, fields] of Object.entries(need.fields ?? {})) {
    const have = new Set(caps.ops.get(op)?.request_fields ?? []);
    const miss = fields.filter((f) => !have.has(f));
    if (miss.length) missingFields[op] = miss;
  }
  const known = new Set(Object.keys(OP_BY_NAME));
  const unknownToSdk = [...caps.ops.keys()].filter((n) => !known.has(n)).sort();
  return {
    ok: missingOps.length === 0 && Object.keys(missingFields).length === 0,
    buildCommit: caps.buildCommit,
    schemaVersion: caps.schemaVersion,
    missingOps,
    missingFields,
    unknownToSdk,
  };
}

/**
 * What the deployment serves and what this token may call.
 *
 *     const caps = await loom.capabilities();
 *     if (caps.canCall("conflicts.list") !== false) poll();   // undefined = cannot tell, let the call decide
 *
 * `canCall` compares the op's `required_role` from `ops.capabilities` with
 * the role the bearer token carries (decoded, not verified — the server
 * verifies on every call). It answers `false` only when both sides are
 * known and the token falls short; `undefined` when the op is not served,
 * the token is not a JWT, or a role name is unknown.
 */
export class Capabilities {
  readonly ops: ReadonlyMap<string, OpCapability>;
  constructor(
    ops: Iterable<OpCapability>,
    readonly buildCommit: string,
    readonly schemaVersion: string,
    readonly claims: TokenClaims | undefined,
    readonly fetchedAt: number = Date.now(),
  ) {
    this.ops = new Map(Array.from(ops, (o) => [o.name, o]));
  }

  /** The deployment serves this op. */
  has(op: string): boolean {
    return this.ops.has(op);
  }

  /** The op's minimum role (lowercase: viewer | writer | admin | org_admin | platform_admin), if served. */
  requiredRole(op: string): string | undefined {
    return this.ops.get(op)?.required_role?.toLowerCase();
  }

  /** The token's role on `namespace` (scoped map first, else its highest flat role); undefined when unknown. */
  role(namespace?: string): string | undefined {
    return roleOn(this.claims, namespace);
  }

  /** true / false when both sides are known; undefined means "cannot tell — make the call". */
  canCall(op: string, namespace?: string): boolean | undefined {
    if (!this.has(op)) return undefined;
    return roleSatisfies(this.role(namespace), this.requiredRole(op));
  }

  /** Served ops the token is known to be allowed to call. */
  callable(namespace?: string): string[] {
    return Array.from(this.ops.keys())
      .filter((op) => this.canCall(op, namespace) === true)
      .sort();
  }
}

/** Fetch `ops.capabilities` and pair it with the token's claims. `LoomClient.capabilities()` caches this. */
export async function capabilities(client: LoomClient): Promise<Capabilities> {
  const caps = await client.call("ops.capabilities", {});
  return new Capabilities(
    caps.ops ?? [],
    caps.build_commit ?? "",
    caps.schema_version ?? "",
    client.transport.tokenClaims(),
  );
}
