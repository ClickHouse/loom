import type { WhoAmI } from "../api";
import { canCall, OP_MIN_ROLE, type GatedOp } from "./canCall";
import { needsRole, type NamespaceRoleRead } from "./namespaceRole";

// Apart from canCall.ts and namespaceRole.ts, which the boot chunk holds: only in-page
// controls and the assistant's cards read it.

export const ROLE_LOADING = "Your role on this namespace is still loading.";

/**
 * Why the caller's role on `namespace` is not known, or null when it is: list_mine is still
 * being read (or whoami is), the caller's request to join waits for an admin, or list_mine
 * could not be read. A null role with list_mine read and none of these is no grant at all.
 */
export function roleGap(read: NamespaceRoleRead, whoami: WhoAmI | null, namespace: string): string | null {
  if (read.role !== null) return null;
  if (read.loading || !whoami) return ROLE_LOADING;
  if (read.rows?.some((m) => m.namespace === namespace && m.status === "pending"))
    return `Your request to join ${namespace} is waiting for an admin.`;
  if (read.rows === null) return `Couldn't check your access to ${namespace}.`;
  return null;
}

/**
 * Why the caller cannot call a namespace-gated `op` on `namespace`, or null when `canCall`
 * allows it: the reason an in-page control shows through `ui/Unavailable` instead of
 * disappearing. A role not known yet says why (`roleGap`); a known role, or none at all once
 * list_mine is read, says the role the op needs (`needsRole`).
 */
export function callReason(
  op: GatedOp,
  read: NamespaceRoleRead,
  whoami: WhoAmI | null,
  namespace: string,
): string | null {
  if (canCall(op, read.role, whoami)) return null;
  return roleGap(read, whoami, namespace) ?? needsRole(OP_MIN_ROLE[op].min, namespace);
}
