import { Chip } from "./ui";
import { ROLE_TIP, useNamespaceRole } from "../lib/namespaceRole";

/**
 * RoleChip — "admin" / "writer" / "viewer" for the caller on one namespace. Sits beside
 * ScopeBadge so the page says what the role lets you do; renders nothing while the role
 * is unknown. The tooltip spells the role out, and the chip is focusable so keyboard
 * users get it.
 */
export function RoleChip({ namespace }: { namespace: string }) {
  const role = useNamespaceRole(namespace);
  if (!role) return null;
  return <Chip label={role} ariaLabel={`Your role here: ${role}`} tip={ROLE_TIP[role]} />;
}
