import type { WhoAmI } from "../api";
import { canCall, OP_MIN_ROLE, RANK, type GatedOp } from "./canCall";
import { needsRole, type NamespaceRole, type NamespaceRoleRead } from "./namespaceRole";

// Apart from canCall.ts and namespaceRole.ts, which the boot chunk holds: only in-page
// controls and the assistant's cards read it.

export const ROLE_LOADING = "Your role on this namespace is still loading.";

const serverDecides = (namespace: string) =>
  `The server decides this token's access to ${namespace}; it does not show here.`;

/** The token claims `*` without naming `namespace`. */
function starOnly(whoami: WhoAmI, namespace: string): boolean {
  const named = whoami.namespaces ?? [];
  return named.includes("*") && !named.includes(namespace);
}

/** The token claims `*` without naming `namespace`, with a flat role reaching `min`.
 *  `claim_cap` caps only a named namespace, so a grant row never lowers this claim;
 *  `ns_write` and a delegated creator grant may, unseen here. */
function starReaches(whoami: WhoAmI, namespace: string, min: NamespaceRole): boolean {
  if (!starOnly(whoami, namespace)) return false;
  const roles = (whoami.roles ?? []).map((r) => r.toLowerCase());
  return roles.some((r) => r in RANK && RANK[r as NamespaceRole] >= RANK[min]);
}

/**
 * Why the caller's role on `namespace` is not known, or null when it is: list_mine is still
 * being read (or whoami is), the caller's request to join waits for an admin, list_mine
 * could not be read, or a `*` claim that does not name it, which list_mine answers no row
 * for, leaves it to the server. A null role with list_mine read and none of these is no grant at all.
 */
export function roleGap(read: NamespaceRoleRead, whoami: WhoAmI | null, namespace: string): string | null {
  if (read.role !== null) return null;
  if (read.loading || !whoami) return ROLE_LOADING;
  if (read.rows?.some((m) => m.namespace === namespace && m.status === "pending"))
    return `Your request to join ${namespace} is waiting for an admin.`;
  if (read.rows === null) return `Couldn't check your access to ${namespace}.`;
  if (starOnly(whoami, namespace)) return serverDecides(namespace);
  return null;
}

/** `needsRole("admin")` for a repository, whose admins are its maintainers, by its name. */
export function needsMaintainer(repository: string): string {
  return `Needs maintainer on ${repository}. A maintainer of it can grant that.`;
}

/**
 * Why the caller cannot call a namespace-gated `op` on `namespace`, or null when `canCall`
 * allows it: the reason an in-page control shows through `ui/Unavailable` instead of
 * disappearing. A role not known yet says why (`roleGap`); a known role, or none at all once
 * list_mine is read, says the role the op needs (`needsRole`), except under a `*` claim whose
 * flat role reaches it, where the server decides.
 */
export function callReason(
  op: GatedOp,
  read: NamespaceRoleRead,
  whoami: WhoAmI | null,
  namespace: string,
): string | null {
  if (canCall(op, read.role, whoami)) return null;
  const min = OP_MIN_ROLE[op].min;
  const gap = roleGap(read, whoami, namespace);
  if (gap) return gap;
  return whoami && starReaches(whoami, namespace, min) ? serverDecides(namespace) : needsRole(min, namespace);
}
