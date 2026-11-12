import type { MyNamespaceEntry, NamespaceInfo, WhoAmI } from "../api";
import { hasAdminClaim, isNamespaceAdmin } from "./role";

// Apart from role.ts, which the boot chunk holds: only the settings dialog and Spending ask.

/** What a restricted namespace's admin gate (`server/auth/authz.py::_check_restricted`)
 *  finds missing for the caller: a token of the namespace's own org (`*` is no org), a token
 *  that names the namespace (`*` does not), an admin claim, and a direct, active admin grant
 *  (not one inherited through a creator). Empty when the gate admits; auth off admits all.
 *  whoami does not carry a token's `ns_roles`, `ns_write` or its creator's grant: a token with
 *  no flat roles (one scoped per namespace) has a role here the client cannot see, so it is
 *  `unseen`, never `claim`, and the gate stays shut on the client. */
export type RestrictedMiss = "org" | "name" | "claim" | "unseen" | "grant";

export function restrictedMisses(
  whoami: WhoAmI | null,
  mine: MyNamespaceEntry[] | null,
  { org, namespace }: { org: string; namespace: string },
): RestrictedMiss[] {
  if (whoami && !whoami.authenticated) return [];
  const grant = mine?.find((m) => m.namespace === namespace);
  const misses: RestrictedMiss[] = [];
  if (whoami?.org !== org) misses.push("org");
  if (!whoami?.namespaces?.includes(namespace)) misses.push("name");
  if (whoami && (whoami.roles ?? []).length === 0) misses.push("unseen");
  else if (!hasAdminClaim(whoami)) misses.push("claim");
  if (!(grant?.role === "admin" && grant.status !== "pending" && !grant.inherited)) misses.push("grant");
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
