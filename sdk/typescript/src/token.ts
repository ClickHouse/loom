/**
 * What the bearer token says about its holder. Loom tokens
 * are JWTs whose payload carries `org`, `namespaces`, `roles` and, for
 * scoped tokens, `ns_roles` (namespace → role). The SDK decodes the payload
 * WITHOUT verifying the signature — the server does that on every call —
 * and uses it only to avoid requests the token cannot make, never to grant.
 */

/** Ascending privilege, mirroring the server's `Role` enum. */
export const ROLE_RANK: Readonly<Record<string, number>> = Object.freeze({
  viewer: 10,
  writer: 20,
  admin: 30,
  org_admin: 40,
  platform_admin: 50,
});

export interface TokenClaims {
  subject?: string;
  org?: string;
  namespaces: string[];
  /** Flat roles (unscoped and wildcard tokens). */
  roles: string[];
  /** Per-namespace role for scoped tokens: authoritative where present. */
  nsRoles: Array<[namespace: string, role: string]>;
  /** Seconds since the epoch, if the token carries `exp`. */
  exp?: number;
}

/** The token's payload, or undefined when it is not a decodable JWT. */
export function decodeTokenClaims(token: string | undefined): TokenClaims | undefined {
  if (!token) return undefined;
  const parts = token.split(".");
  if (parts.length !== 3) return undefined;
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(base64UrlDecode(parts[1])) as Record<string, unknown>;
  } catch {
    return undefined;
  }
  if (!payload || typeof payload !== "object") return undefined;
  const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  const nsRoles: Array<[string, string]> = [];
  const raw = payload.ns_roles;
  if (Array.isArray(raw)) {
    for (const e of raw)
      if (Array.isArray(e) && typeof e[0] === "string" && typeof e[1] === "string") nsRoles.push([e[0], e[1]]);
  } else if (raw && typeof raw === "object") {
    for (const [ns, r] of Object.entries(raw as Record<string, unknown>))
      if (typeof r === "string") nsRoles.push([ns, r]);
  }
  return {
    subject: typeof payload.sub === "string" ? payload.sub : undefined,
    org: typeof payload.org === "string" ? payload.org : undefined,
    namespaces: strs(payload.namespaces),
    roles: strs(payload.roles).map((r) => r.toLowerCase()),
    nsRoles: nsRoles.map(([ns, r]) => [ns, r.toLowerCase()]),
    exp: typeof payload.exp === "number" ? payload.exp : undefined,
  };
}

/** The role a token holds on `namespace`: the scoped entry when the token has a map, else its highest flat role. */
export function roleOn(claims: TokenClaims | undefined, namespace?: string): string | undefined {
  if (!claims) return undefined;
  if (claims.nsRoles.length) {
    if (namespace) {
      const hit = claims.nsRoles.find(([ns]) => ns === namespace);
      if (hit) return hit[1];
    }
    // A deployment-scoped op is gated on the best role the map grants anywhere.
    return best(claims.nsRoles.map(([, r]) => r));
  }
  return best(claims.roles);
}

/** Whether `role` satisfies `required`; undefined when either name is unknown. */
export function roleSatisfies(role: string | undefined, required: string | undefined): boolean | undefined {
  if (!role || !required) return undefined;
  const have = ROLE_RANK[role.toLowerCase()];
  const need = ROLE_RANK[required.toLowerCase()];
  if (have === undefined || need === undefined) return undefined;
  return have >= need;
}

function best(roles: string[]): string | undefined {
  let top: string | undefined;
  for (const r of roles) {
    const rank = ROLE_RANK[r];
    if (rank !== undefined && (top === undefined || rank > ROLE_RANK[top])) top = r;
  }
  return top;
}

function base64UrlDecode(s: string): string {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  return new TextDecoder().decode(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
}
