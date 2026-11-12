import type { MyNamespaceEntry, WhoAmI } from "../api";
import { OP_MIN_ROLE, RANK, type MinRole, type NamespaceOp } from "./canCall";
import { hasAdminClaim } from "./role";
import { needsRole, type NamespaceRole, type NamespaceRoleRead } from "./namespaceRole";

// Apart from canCall.ts and namespaceRole.ts, which the boot chunk holds: only in-page
// controls and the assistant's cards read it.

export const ROLE_LOADING = "Your role on this namespace is still loading.";

/** Before whoami is read, for an op that turns on the token's claim, not a namespace role. */
export const ACCESS_LOADING = "Your access is still loading.";

/** The namespace-gated writes only in-page controls gate, with the registry's role for each
 *  (`callReason.test.ts` checks them against sdk/typescript/src/generated/ops.ts). Here, not in
 *  canCall.ts's table, which the boot chunk holds and no background read needs these from. */
export const PAGE_OP_MIN_ROLE = {
  "conflicts.resolve": "admin",
  "conflicts.allowlist_remove": "admin",
  "learnings.review": "admin",
  "memory.set": "writer",
  "memory.delete": "writer",
  "memory.verify": "writer",
  "memory.import_curated": "writer",
  "code.refresh": "writer",
  "policy.set": "admin",
  "policy.delete": "admin",
  "profile.set": "writer",
} as const satisfies Record<string, MinRole>;

/** A namespace-gated op a control can be gated on: canCall.ts's table, or the page writes. */
export type GatedNamespaceOp = NamespaceOp | keyof typeof PAGE_OP_MIN_ROLE;

function minOf(op: GatedNamespaceOp): MinRole {
  return op in PAGE_OP_MIN_ROLE
    ? PAGE_OP_MIN_ROLE[op as keyof typeof PAGE_OP_MIN_ROLE]
    : OP_MIN_ROLE[op as NamespaceOp].min;
}

/** `canCall` for a namespace-gated op: an admin claim, or a known role reaching the op's. */
function allows(op: GatedNamespaceOp, role: NamespaceRole | null, whoami: WhoAmI | null): boolean {
  if (hasAdminClaim(whoami)) return true;
  return role !== null && RANK[role] >= RANK[minOf(op)];
}

/** The loading line, named by `name` where it is not the namespace (a repository's label). */
export function loadingLine(name: string, namespace: string): string {
  return name === namespace ? ROLE_LOADING : `Your role on ${name} is still loading.`;
}

/** Why grants that are not read yet say nothing: still loading, or list_mine failed. */
export function unreadLine(loading: boolean, namespace: string): string {
  return loading ? ROLE_LOADING : `Couldn't check your access to ${namespace}.`;
}

export const serverDecides = (namespace: string) =>
  `The server decides this token's access to ${namespace}; it does not show here.`;

/** A signed-in token with no flat roles: one scoped per namespace (`ns_roles`), which whoami
 *  does not carry. */
export function rolesUnseen(whoami: WhoAmI | null): boolean {
  return !!whoami?.authenticated && (whoami.roles ?? []).length === 0;
}

/** Whether a scoped token's role on `namespace` is the server's to read: the subject holds no
 *  direct grant row there (none, a claim-only `jwt` row, or one inherited through a creator).
 *  Where it holds one, the server caps the token's own role at it (`claim_cap`,
 *  `_scoped_role`), so that row's role is the most the token has. `direct` says whether the
 *  caller holds one, for a list other than list_mine's. */
export function scopedUnseen(whoami: WhoAmI | null, direct: boolean): boolean {
  return rolesUnseen(whoami) && !direct;
}

/** The caller holds a direct grant row on `namespace` in list_mine's rows. */
export function holdsDirectRow(rows: MyNamespaceEntry[] | null, namespace: string): boolean {
  const row = rows?.find((m) => m.namespace === namespace);
  return !!row && row.role !== "jwt" && !row.inherited;
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
 * answers no row for one) or a token scoped per namespace with no direct grant row there
 * (`scopedUnseen`). A null role with list_mine read, flat roles on the token and none of
 * these is no grant at all. `name` is what the lines call it (a repository's label), and
 * `approver` who approves a request to join it (a repository's maintainer).
 */
export function roleGap(
  read: NamespaceRoleRead,
  whoami: WhoAmI | null,
  namespace: string,
  name: string = namespace,
  approver = "an admin",
): string | null {
  if (read.role !== null) return null;
  if (read.loading || !whoami) return loadingLine(name, namespace);
  if (read.rows?.some((m) => m.namespace === namespace && m.status === "pending"))
    return `Your request to join ${name} is waiting for ${approver}.`;
  if (read.rows === null) return unreadLine(false, name);
  if (starOnly(whoami, namespace) || scopedUnseen(whoami, holdsDirectRow(read.rows, namespace)))
    return serverDecides(name);
  return null;
}

/** `needsRole("admin")` for a repository, by its name: its admins are its maintainers, and a
 *  repository has no writer role, so maintainer is also what its WRITER ops need. */
export function needsMaintainer(repository: string): string {
  return `Needs maintainer on ${repository}. A maintainer of it can grant that.`;
}

/**
 * Why the caller cannot call a namespace-gated `op` on `namespace`, or null when `canCall`
 * allows it: the reason an in-page control shows through `ui/Unavailable` instead of
 * disappearing. A role not known yet says why (`roleGap`); a known role, or none at all once
 * list_mine is read, says the role the op needs (`needsRole`), except where the server
 * decides: a `*` claim whose flat role reaches it over a lower grant row (a grant row never
 * caps a `*` claim), or a token scoped per namespace with no direct grant row there. A direct
 * row caps a scoped token, so its role keeps "Needs". `repository`, a repository's label,
 * names it in the lines, whose "Needs" and join approver are then its maintainer.
 */
export function callReason(
  op: GatedNamespaceOp,
  read: NamespaceRoleRead,
  whoami: WhoAmI | null,
  namespace: string,
  repository?: string,
): string | null {
  if (allows(op, read.role, whoami)) return null;
  const name = repository ?? namespace;
  const gap = roleGap(read, whoami, namespace, name, repository ? "a maintainer" : undefined);
  if (gap) return gap;
  const needs = needsLine(op, read, whoami, namespace);
  if (needs) return repository ? needsMaintainer(repository) : needs;
  return serverDecides(name);
}

/**
 * `callReason`'s "Needs" line alone: the reason when the caller's role is known to fall short
 * of `op`, and null when `canCall` allows it, the role is not known yet, or the server decides.
 * For a control that stays usable unless the dashboard is sure the server would refuse it.
 */
export function needsLine(
  op: GatedNamespaceOp,
  read: NamespaceRoleRead,
  whoami: WhoAmI | null,
  namespace: string,
): string | null {
  if (allows(op, read.role, whoami) || roleGap(read, whoami, namespace)) return null;
  const min = minOf(op);
  if (whoami && (starReaches(whoami, namespace, min) || scopedUnseen(whoami, holdsDirectRow(read.rows, namespace))))
    return null;
  return needsRole(min, namespace);
}

/** A member of a repository's namespace (a live grant on a `code` row): the server admits its
 *  memory.set and its memory.delete of its own rows there (`server/code/app/member_writes.py`),
 *  whatever the role, so those are left to the server. */
export function codeMember(read: NamespaceRoleRead, namespace: string): boolean {
  return !!read.rows?.some((m) => m.namespace === namespace && m.kind === "code" && m.status !== "pending");
}
