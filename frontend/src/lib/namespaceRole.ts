import { useEffect, useState } from "react";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { invalidateShared, sharedMine } from "./sharedReads";
import { useStore } from "./state";
import { hasAdminClaim } from "./role";
import { isPersonalCollection } from "./namespace";

/**
 * The caller's role on one namespace — what the role chip next to ScopeBadge shows and
 * what gates the write CTAs.
 *
 * Sources, in order:
 * - auth off (`authenticated: false` for the synthesised local identity):
 *   the operator is the user — admin everywhere;
 * - an ADMIN / ORG_ADMIN / PLATFORM_ADMIN claim: admin everywhere (the
 *   server honours the claim before any grant row);
 * - an active grant row from `namespace.list_mine`: its role;
 * - a claim-only row (`role: "jwt"`, no grant to join through): the token's flat
 *   VIEWER / WRITER claim, which the server honours there
 *   (`server/auth/authz.py::check_authorized`). list_mine answers a `jwt` row for each
 *   namespace the token names whose claim still reaches it (`live_claims`), never for `*`;
 * - no row on a namespace a `*` token covers without naming it: viewer, whatever the flat
 *   claim. The client cannot see what else holds such a token to less (`ns_write`, a
 *   delegated token's creator grant), so it offers reads only and no write;
 * - anything else (list_mine not loaded yet, failed, a pending request, or no row on a
 *   named namespace, whose claim a revoked grant caps): `null` — no request is sent. A
 *   pending row is unknown on purpose, whatever the token claims.
 */
export type NamespaceRole = "admin" | "writer" | "viewer";

/** Why a control is unavailable to the caller's role: the role it needs on the namespace,
 *  and who can grant it. Every in-page control a role cannot use shows this as its reason
 *  (`ui/Unavailable`) rather than disappearing. */
export function needsRole(role: NamespaceRole, namespace: string): string {
  return `Needs ${role} on ${namespace}. An admin of it can grant that.`;
}

/** The token's flat VIEWER / WRITER claim; admin claims go through
 * `hasAdminClaim` so both gates read the same list. */
function flatClaim(whoami: WhoAmI): NamespaceRole | null {
  const roles = (whoami.roles || []).map((r) => r.toLowerCase());
  if (roles.includes("writer")) return "writer";
  if (roles.includes("viewer")) return "viewer";
  return null;
}

export function roleOnNamespace(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  namespace: string,
): NamespaceRole | null {
  if (!whoami || !namespace) return null;
  if (hasAdminClaim(whoami)) return "admin";
  const claim = flatClaim(whoami);
  if (mine === null) return null;
  const row = mine.find((m) => m.namespace === namespace);
  if (row?.status === "pending") return null;
  if (!row) {
    const named = whoami.namespaces ?? [];
    return claim && named.includes("*") && !named.includes(namespace) ? "viewer" : null;
  }
  if (row.role === "jwt") return claim;
  return row.role;
}

/** True unless the role is known to be viewer: an unknown role never locks a button. */
export function canWrite(role: NamespaceRole | null): boolean {
  return role !== "viewer";
}

// App.tsx stores the bootstrap list_mine in `myNamespaces`; the hook reads that first.
// When it is absent (bootstrap still running, a test, or the read failed) one list_mine
// per org per 30 s is shared by every chip and CTA on the page rather than a request per
// ScopeBadge: one cache, not three. App's boot read, this hook's fallback and the shared
// read would otherwise each issue namespace.list_mine with its own memo, so the hook
// joins the shared read.
function fetchMine(org: string): Promise<MyNamespaceEntry[] | null> {
  return sharedMine(org).catch(() => null);
}

/** Tests and the picker's join/leave flows: forget cached rows. */
export function resetNamespaceRoleCache(): void {
  invalidateShared("mine:");
}

export function useNamespaceRole(namespace: string | undefined): NamespaceRole | null {
  return useNamespaceRoleRead(namespace).role;
}

/** The role, whether list_mine is still being read for it (a null role while `loading` is
 *  not known yet, rather than known to be missing), and the rows it was read from: null
 *  while unread or when the read failed. A caller that says why a role is null reads them
 *  (`views/assistant/cards/cardStore.ts::useCardGate`). */
export interface NamespaceRoleRead {
  role: NamespaceRole | null;
  loading: boolean;
  rows: MyNamespaceEntry[] | null;
}

export function useNamespaceRoleRead(namespace: string | undefined): NamespaceRoleRead {
  const whoami = useStore((s) => s.whoami);
  const org = useStore((s) => s.identity?.org);
  const stored = useStore((s) => s.myNamespaces);
  // The rows list_mine answered for `org`; null rows when the read failed.
  const [fetched, setFetched] = useState<{ org: string; rows: MyNamespaceEntry[] | null } | null>(null);
  // list_mine is only needed when the claim alone doesn't settle the role
  // and the store doesn't already hold the rows.
  const needsFetch = !!whoami && !hasAdminClaim(whoami) && stored === null;
  useEffect(() => {
    if (!org || !needsFetch) return;
    let cancelled = false;
    fetchMine(org).then((rows) => {
      if (!cancelled) setFetched({ org, rows });
    });
    return () => {
      cancelled = true;
    };
  }, [org, needsFetch, namespace]);
  const answered = fetched !== null && fetched.org === org;
  const rows = stored ?? (answered ? fetched.rows : null);
  return {
    role: roleOnNamespace(whoami, rows, namespace ?? ""),
    loading: needsFetch && !!org && !answered,
    rows,
  };
}

/** Whether the caller may write the namespace's guide: a known writer or admin, never a
 *  personal collection; an unknown role reads. */
export function canEditGuide(role: NamespaceRole | null, namespace: string): boolean {
  return (role === "writer" || role === "admin") && !isPersonalCollection(namespace);
}
