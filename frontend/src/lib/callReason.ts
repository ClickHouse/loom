import type { WhoAmI } from "../api";
import { canCall, OP_MIN_ROLE, RANK, type GatedOp } from "./canCall";
import { needsRole, type NamespaceRole, type NamespaceRoleRead } from "./namespaceRole";

// Apart from canCall.ts and namespaceRole.ts, which the boot chunk holds: only in-page
// controls and the assistant's cards read it.

export const ROLE_LOADING = "Your role on this namespace is still loading.";

/** Why grants that are not read yet say nothing: still loading, or list_mine failed. */
export function unreadLine(loading: boolean, namespace: string): string {
  return loading ? ROLE_LOADING : `Couldn't check your access to ${namespace}.`;
}

export const serverDecides = (namespace: string) =>
  `The server decides this token's access to ${namespace}; it does not show here.`;

/** A signed-in token with no flat roles: one scoped per namespace (`ns_roles`), which whoami
 *  does not carry, so its role on a namespace is the server's to read (`_scoped_role`). */
export function rolesUnseen(whoami: WhoAmI | null): boolean {
  return !!whoami?.authenticated && (whoami.roles ?? []).length === 0;
}

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
 * could not be read, or the server decides: a `*` claim that does not name it (list_mine
 * answers no row for one) or a token scoped per namespace (`rolesUnseen`). A null role with
 * list_mine read, flat roles on the token and none of these is no grant at all.
 */
export function roleGap(read: NamespaceRoleRead, whoami: WhoAmI | null, namespace: string): string | null {
  if (read.role !== null) return null;
  if (read.loading || !whoami) return ROLE_LOADING;
  if (read.rows?.some((m) => m.namespace === namespace && m.status === "pending"))
    return `Your request to join ${namespace} is waiting for an admin.`;
  if (read.rows === null) return unreadLine(false, namespace);
  if (starOnly(whoami, namespace) || rolesUnseen(whoami)) return serverDecides(namespace);
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
 * list_mine is read, says the role the op needs (`needsRole`), except where the server
 * decides: a `*` claim whose flat role reaches it, or a token scoped per namespace, whose
 * role there may be above its grant row.
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
  if (whoami && (starReaches(whoami, namespace, min) || rolesUnseen(whoami))) return serverDecides(namespace);
  return needsRole(min, namespace);
}
