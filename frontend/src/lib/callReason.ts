import type { WhoAmI } from "../api";
import { canCall, OP_MIN_ROLE, type GatedOp } from "./canCall";
import { needsRole, type NamespaceRole } from "./namespaceRole";

// Apart from canCall.ts, which the boot chunk holds: only in-page controls read it.
/** Why the caller cannot call a namespace-gated `op` on `namespace` (the role it needs,
 *  `needsRole`), or null when `canCall` allows it: the reason an in-page control shows
 *  through `ui/Unavailable` instead of disappearing. */
export function callReason(
  op: GatedOp,
  role: NamespaceRole | null,
  whoami: WhoAmI | null,
  namespace: string,
): string | null {
  return canCall(op, role, whoami) ? null : needsRole(OP_MIN_ROLE[op].min, namespace);
}
