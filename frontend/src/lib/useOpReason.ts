import { type GatedNamespaceOp, callReason } from "./callReason";
import { useNamespaceRoleRead } from "./namespaceRole";
import { useStore } from "./state";

/** Why the caller cannot call `op` on `namespace` (callReason), or null: what an in-page
 *  control shows through `ui/Unavailable` while it stays in view. */
export function useOpReason(op: GatedNamespaceOp, namespace: string): string | null {
  const read = useNamespaceRoleRead(namespace);
  const whoami = useStore((s) => s.whoami);
  return callReason(op, read, whoami, namespace);
}
