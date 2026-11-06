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
 * - a claim-only row (`role: "jwt"`, no grant to join through): the token's
 *   flat VIEWER / WRITER claim — that is exactly the case the audit found,
 *   a viewer token on a jwt-only namespace being offered "Add memory";
 * - anything else (list_mine not loaded yet, failed, or no row): `null` —
 *   unknown, so nothing is shown and nothing is disabled.
 */
export type NamespaceRole = "admin" | "writer" | "viewer";

export const READ_ONLY_TIP = "You have read-only access here — ask a maintainer for writer";

export const ROLE_TIP: Record<NamespaceRole, string> = {
  admin: "You're an admin here — you can add memories, manage members and change settings",
  writer: "You're a writer here — you can add and edit memories",
  viewer: READ_ONLY_TIP,
};

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
  if (!row || row.status === "pending") return null;
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

/** The role, and whether list_mine is still being read for it: a null role while `loading`
 *  is not known yet, rather than known to be missing. */
export function useNamespaceRoleRead(namespace: string | undefined): { role: NamespaceRole | null; loading: boolean } {
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
  return {
    role: roleOnNamespace(whoami, stored ?? (answered ? fetched.rows : null), namespace ?? ""),
    loading: needsFetch && !answered,
  };
}

/** Whether the caller may write the namespace's guide: a known writer or admin, never a
 *  personal collection; an unknown role reads. */
export function canEditGuide(role: NamespaceRole | null, namespace: string): boolean {
  return (role === "writer" || role === "admin") && !isPersonalCollection(namespace);
}
