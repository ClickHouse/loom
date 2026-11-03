// Boot in one round trip.
//
// Waiting for whoami, then list_mine, then the view mounting and issuing its reads would
// put three serial network stages before the request that fills the page. whoami only
// verifies the token the browser already holds, and the token's own payload names the org
// and the claimed namespaces. So boot reads the claims here, starts the membership read
// at once, and paints a deep link's shell as soon as whoami confirms, on the namespace
// the claims settle. list_mine still decides the final namespace (a joined namespace is
// not in the claims) and the kinds; its answer corrects the shell only when it differs.
import type { Identity, MyNamespaceEntry } from "../api";
import { deepLinkNamespace, isReferenceKind, needsMemoryNamespace } from "./namespace";
import type { View } from "./state";
import { isRecord } from "./guards";

export interface TokenClaims {
  org: string | null;
  namespaces: string[];
  subject: string | null;
  /** The sign-in session (`sid`) the token belongs to; null for a minted token. */
  session: string | null;
}

/** A token's payload, decoded without verification; null for a missing or unparseable
 *  token (proxy mode, garbage, no payload). */
export function tokenPayload(jwt: string | null | undefined): Record<string, unknown> | null {
  if (!jwt) return null;
  const parts = jwt.split(".");
  if (parts.length < 2 || !parts[1]) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
    const json: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return isRecord(json) ? json : null;
  } catch {
    return null;
  }
}

/** The token's own org / namespaces / subject, read without verification —
 *  they steer which reads start early, never what the person may see; the
 *  server answers every read against the verified token. Null for a missing
 *  or unparseable token (proxy mode, garbage, no payload). */
export function claimsFromToken(jwt: string | null | undefined): TokenClaims | null {
  const json = tokenPayload(jwt);
  if (!json) return null;
  const org = typeof json.org === "string" && json.org ? json.org : null;
  const namespaces = Array.isArray(json.namespaces)
    ? (json.namespaces as unknown[]).filter((n): n is string => typeof n === "string")
    : [];
  const subject = typeof json.sub === "string" ? json.sub : null;
  const session = typeof json.sid === "string" && json.sid ? json.sid : null;
  return { org, namespaces, subject, session };
}

/** The session key for where a single sign-on started, `{ path, at }`: the sign-in gate
 *  writes it, and the return to it (`lib/linkToken.ts`) reads it. */
export const SIGN_IN_FROM = "loom_sign_in_from_v1";

/** Accept fragment tokens only; scrub both token forms before boot can fail.
 * Query tokens reach server logs and are never accepted. */
export function takeLinkToken(): { token: string | null; atRoot: boolean } | null {
  const { hash, search, pathname } = window.location;
  const fromHash = hash.startsWith("#token=");
  const query = new URLSearchParams(search);
  if (!fromHash && !query.has("token")) return null;
  query.delete("token");
  const qs = query.toString();
  history.replaceState(history.state, "", pathname + (qs ? `?${qs}` : "") + (fromHash ? "" : hash));
  let token: string | null = null;
  try {
    token = fromHash ? decodeURIComponent(hash.slice("#token=".length)) || null : null;
  } catch {
    /* a fragment that does not decode carries no token */
  }
  return { token, atRoot: fromHash && !qs && (pathname === "/" || pathname === "/me") };
}

/** Namespace kinds from the last membership answer this browser saw, so a
 *  deep link can tell a memory namespace from a code or QA one before
 *  list_mine answers again (a memory view never mounts on a code or tracker namespace;
 *  the kind is only in list_mine rows). A first visit remembers nothing and waits. */
const KEY_KINDS = "loom_kinds_v1";
export type KindMap = Record<string, string>;

export function rememberKinds(mine: readonly MyNamespaceEntry[]): void {
  try {
    const kinds: KindMap = {};
    for (const m of mine) if (m.namespace && m.kind) kinds[m.namespace] = String(m.kind);
    localStorage.setItem(KEY_KINDS, JSON.stringify(kinds));
  } catch {
    /* storage may be unavailable; boot then waits for list_mine */
  }
}

export function rememberedKinds(): KindMap {
  try {
    const raw = localStorage.getItem(KEY_KINDS);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (!isRecord(parsed)) return {};
    const out: KindMap = {};
    for (const [k, v] of Object.entries(parsed)) if (typeof v === "string") out[k] = v;
    return out;
  } catch {
    return {};
  }
}

/** The namespace a deep link can open on before list_mine answers: for a
 *  memory view, the default-namespace chain over the claims alone (saved pick when
 *  claimed, else the first claim) — the same answer list_mine gives unless the pick is a
 *  joined namespace the claims do not carry — and only when this browser remembers that
 *  namespace as a memory one. Null means "wait for list_mine": a view that reads the org,
 *  the code plane or the QA plane, a namespace whose kind is unknown or is code / QA, or
 *  a token with no claims at all. */
export function provisionalNamespace(
  view: View,
  saved: string | null,
  claims: readonly string[],
  kinds: KindMap = rememberedKinds(),
): string | null {
  if (!needsMemoryNamespace(view)) return null;
  const ns = deepLinkNamespace(view, saved, claims, []);
  if (!ns) return null;
  const kind = kinds[ns];
  if (!kind || isReferenceKind(kind)) return null;
  return ns;
}

/** Views key their reads on the identity object; replacing it with an equal
 *  one replays every read on the page. */
export function sameIdentity(a: Identity | null | undefined, b: Identity): boolean {
  return !!a && a.org === b.org && a.namespace === b.namespace && a.agent === b.agent;
}
