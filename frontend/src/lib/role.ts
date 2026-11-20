import type { MyNamespaceEntry, WhoAmI } from "../api";

/**
 * Who may see admin surfaces.
 *
 * Three different questions, because the server answers them differently. Each
 * admin surface uses the one its op is gated on, not a local copy:
 *
 * - `hasAdminClaim`: the JWT's flat `roles` claim reaches ADMIN. This is the
 *   exact gate on `/v1/admin/*` (`server/insights/app/admin_routes.py::require_admin`
 *   checks `info.has_role(Role.ADMIN)` and nothing else), so it is the only
 *   honest predicate for `admin/workers.status`.
 * - `hasOrgAdminClaim`: the flat claim reaches ORG_ADMIN. The org-wide ops gate
 *   here: `admin.users`, `ops.health` and `claim_release` require ORG_ADMIN
 *   (hierarchical, so PLATFORM_ADMIN too), and `resolve_org_scope` widens
 *   `audit.list` / `cost.summary` to the whole org only at ORG_ADMIN. An ADMIN
 *   claim does not reach it.
 * - `isNamespaceAdmin`: the caller is admin on the current namespace — an
 *   admin claim, or an active `admin` grant row from `namespace.list_mine`.
 *   Registry ops gate on the grant, so a password-login user whose token
 *   carries WRITER but who owns the namespace is still its admin.
 *   `holdsAdminGrant` is the same rule for a screen that read the namespace's
 *   own grant list (`grants.list`) rather than `namespace.list_mine`.
 * - `canUpdateNamespace` and the restricted gate's misses live in `lib/namespaceUpdate.ts`.
 *
 * `LOOM_AUTH_REQUIRED=false` (whoami says `authenticated: false` for the
 * synthesised local identity) means the operator is the user: admin.
 * `mine === null` means the grants read failed or hasn't happened; that is
 * "unknown", not "no access", so the drawer stays as it was rather than
 * vanishing on a transient failure.
 */

const WRITER_CLAIMS = new Set(["writer", "admin", "org_admin", "platform_admin"]);
const ADMIN_CLAIMS = new Set(["admin", "org_admin", "platform_admin"]);
const ORG_ADMIN_CLAIMS = new Set(["org_admin", "platform_admin"]);
const PLATFORM_ADMIN_CLAIMS = new Set(["platform_admin"]);

function claimIn(whoami: WhoAmI | null, claims: ReadonlySet<string>): boolean {
  if (!whoami) return false;
  if (!whoami.authenticated) return true;
  return (whoami.roles || []).some((r) => claims.has(r.toLowerCase()));
}

/** The flat claim reaches WRITER: what an op authorized for the org (`skip_namespace_authz`)
 *  at WRITER checks before its handler reads any grant. */
export function hasWriterClaim(whoami: WhoAmI | null): boolean {
  return claimIn(whoami, WRITER_CLAIMS);
}

export function hasAdminClaim(whoami: WhoAmI | null): boolean {
  return claimIn(whoami, ADMIN_CLAIMS);
}

export function hasOrgAdminClaim(whoami: WhoAmI | null): boolean {
  return claimIn(whoami, ORG_ADMIN_CLAIMS);
}

export function hasPlatformAdminClaim(whoami: WhoAmI | null): boolean {
  return claimIn(whoami, PLATFORM_ADMIN_CLAIMS);
}

export function holdsAdminGrant(
  whoami: WhoAmI | null,
  grant: { role: string; status?: "active" | "pending" } | undefined,
): boolean {
  if (hasAdminClaim(whoami)) return true;
  return grant?.role === "admin" && grant.status !== "pending";
}

export function isNamespaceAdmin(whoami: WhoAmI | null, mine: MyNamespaceEntry[] | null, namespace: string): boolean {
  if (hasAdminClaim(whoami)) return true;
  if (!whoami) return false;
  if (mine === null) return true;
  return holdsAdminGrant(
    whoami,
    mine.find((m) => m.namespace === namespace),
  );
}
