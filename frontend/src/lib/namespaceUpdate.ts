import type { MyNamespaceEntry, NamespaceInfo, WhoAmI } from "../api";
import { RANK } from "./canCall";
import { hasAdminClaim, hasWriterClaim, isNamespaceAdmin } from "./role";
import { loadingLine, rolesUnseen, serverDecides, unreadLine } from "./callReason";

// Apart from role.ts, which the boot chunk holds: only lazy pages ask (the settings dialog,
// Spending, a repository's Access tab and Refresh now).

/** What a restricted namespace's gate (`server/auth/authz.py::_check_restricted`) finds
 *  missing for the caller at `role` (admin unless said): a token of the namespace's own org
 *  (`*` is no org), a token that names the namespace (`*` does not), a flat claim reaching
 *  `role`, and a direct, active grant reaching it (not one inherited through a creator).
 *  Empty when the gate admits; auth off admits all. whoami does not carry a token's
 *  `ns_roles`, `ns_write` or its creator's grant: a token with no flat roles (one scoped
 *  per namespace) has a role here the client cannot see, so it is `unseen`, never `claim`,
 *  and the gate stays shut on the client. */
export type RestrictedMiss = "org" | "name" | "claim" | "unseen" | "grant";

export function restrictedMisses(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  { org, namespace }: { org: string; namespace: string },
  role: "writer" | "admin" = "admin",
): RestrictedMiss[] {
  if (whoami && !whoami.authenticated) return [];
  const grant = mine?.find((m) => m.namespace === namespace);
  const misses: RestrictedMiss[] = [];
  if (whoami?.org !== org) misses.push("org");
  if (!whoami?.namespaces?.includes(namespace)) misses.push("name");
  if (rolesUnseen(whoami)) misses.push("unseen");
  else if (!(role === "admin" ? hasAdminClaim(whoami) : hasWriterClaim(whoami))) misses.push("claim");
  const reaches = !!grant && grant.role in RANK && RANK[grant.role as keyof typeof RANK] >= RANK[role];
  if (!(reaches && grant.status !== "pending" && !grant.inherited)) misses.push("grant");
  return misses;
}

// Matches server.identity.app.namespace_autoregister.AUTO_REGISTER_TAG.
const AUTO_REGISTER_TAG = "auto-registered";

export function canUpdateNamespace(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  scope: { org: string; namespace: string },
  row: Pick<NamespaceInfo, "owner_email" | "tags" | "classification"> | null,
): boolean {
  const { namespace } = scope;
  if (row?.classification === "restricted") return restrictedMisses(whoami, mine, scope).length === 0;
  if (isNamespaceAdmin(whoami, mine, namespace)) return true;
  const subject = whoami?.subject;
  return !!subject && !!row && row.owner_email === subject && row.tags.includes(AUTO_REGISTER_TAG);
}

/**
 * Why a restricted namespace's gate refuses the caller, from what `restrictedMisses` finds
 * missing: the token's parts, the direct grant, or both; with neither, the token is scoped
 * per namespace and the server decides. `what` names the thing changed ("its settings");
 * `claimRole` is the flat claim the gate asked for (admin unless said).
 */
export function restrictedLine(
  misses: RestrictedMiss[],
  {
    org,
    namespace,
    name,
    adminWord,
    what,
    claimRole = "admin",
  }: { org: string; namespace: string; name: string; adminWord: string; what: string; claimRole?: "writer" | "admin" },
): string {
  const tokenMisses = [
    misses.includes("org") ? `be of ${org}` : null,
    misses.includes("name") ? `name ${namespace}` : null,
    misses.includes("claim") ? `carry ${claimRole === "admin" ? "an admin" : "a writer"} claim` : null,
  ].filter((p): p is string => p !== null);
  if (misses.includes("grant"))
    return tokenMisses.length
      ? `To change ${what}, your token must ${andList(tokenMisses)}, and you need a direct ${adminWord} grant on ${name}.`
      : `Changing ${what} needs a direct ${adminWord} grant on ${name}.`;
  return tokenMisses.length ? `To change ${what}, your token must ${andList(tokenMisses)}.` : serverDecides(name);
}

/** "a", "a and b", "a, b and c". */
function andList(parts: string[]): string {
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0];
}

/**
 * Why a restricted namespace's gate refuses the caller at `role`, or null when it admits: the
 * loading or unread line while its grant rows are out (`useGrantRows`), else `restrictedLine`
 * from what `restrictedMisses` finds missing. `name` is what the lines call the place.
 */
export function restrictedReason(
  grants: { rows: MyNamespaceEntry[] | null; loading: boolean },
  whoami: WhoAmI | null,
  scope: { org: string; namespace: string },
  wording: { name: string; adminWord: string; what: string; role?: "writer" | "admin" },
): string | null {
  // Auth off admits all, with no grant rows to wait for.
  if (whoami?.authenticated === false) return null;
  if (grants.loading || !whoami) return loadingLine(wording.name, scope.namespace);
  if (grants.rows === null) return unreadLine(false, wording.name);
  const role = wording.role ?? "admin";
  const misses = restrictedMisses(whoami, grants.rows, scope, role);
  if (misses.length === 0) return null;
  return restrictedLine(misses, { ...scope, ...wording, claimRole: role });
}
