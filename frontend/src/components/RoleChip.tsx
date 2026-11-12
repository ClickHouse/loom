import { Chip } from "./ui";
import { type NamespaceRole, useNamespaceRole } from "../lib/namespaceRole";

// Here, not in lib/namespaceRole.ts, which the boot chunk holds: only the chip reads it.
const ROLE_TIP: Record<NamespaceRole, string> = {
  admin: "You're an admin here — you can add memories, manage members and change settings",
  writer: "You're a writer here — you can add and edit memories",
  viewer: "You're a viewer here — you can read; an admin of it can grant writer",
};

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
