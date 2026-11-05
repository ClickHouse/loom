import {
  isAuthDisabled,
  isProxyMode,
  storedToken,
  type CodeRepoSummary,
  type MyNamespaceEntry,
  type WhoAmI,
} from "../api";
import { hasAdminClaim } from "./role";
import type { NamespaceRole } from "./namespaceRole";

/**
 * "The dashboard never issues a request the token cannot make; when the
 * role is unknown it does not poll."
 *
 * Every background read the dashboard fires on mount or on a timer goes
 * through `canCall` first. It answers from what the client already holds —
 * the whoami claims and the caller's role on the current namespace
 * (`roleOnNamespace`) — and never from a probe request.
 *
 * Two kinds of gate, because the server has two:
 * - `namespace`: a registry op (`server/operations/*.py`, `required_role`).
 *   The transport refuses it with AUTH_INSUFFICIENT unless the principal
 *   reaches the role AND may access the namespace (a grant, or a token
 *   naming it — `server/auth/authz.py::check_authorized`). The role on the
 *   namespace is the only client-side fact that covers both; `null` means
 *   the client does not know, and an unknown role makes no request.
 * - `claim`: a `/v1/admin/*` handler (`server/insights/app/admin_routes.py::require_admin`)
 *   that checks the JWT's flat roles claim and nothing else, so only
 *   `hasAdminClaim` counts — an admin grant row does not open it.
 *
 * `OP_MIN_ROLE` mirrors the registry; `canCall.test.ts` checks every
 * `namespace` entry against `sdk/typescript/src/generated/ops.ts`, which
 * `scripts/codegen/sdk_ts_gen.py` writes from the same registry.
 */
export type MinRole = "viewer" | "writer" | "admin";

export interface OpGate {
  min: MinRole;
  by: "namespace" | "claim";
}

export const OP_MIN_ROLE = {
  // Gated at a call site: Sidebar badge polls, Recent / MemoryInspector
  // conflict chips, Health's workers panel.
  "conflicts.list": { min: "viewer", by: "namespace" },
  "cost.budget_status": { min: "viewer", by: "namespace" },
  "cost.summary": { min: "viewer", by: "namespace" },
  "agents.connected": { min: "viewer", by: "namespace" },
  "audit.list": { min: "viewer", by: "namespace" },
  "erasure.list": { min: "viewer", by: "namespace" },
  "admin/workers.status": { min: "admin", by: "claim" },
  // Setup's "Don't save these" panel (`views/admin/Blacklist.tsx::PrivacyFilters`) must
  // not list the blacklist for every visitor — that is one AUTH_INSUFFICIENT per page
  // load from a viewer token with no memory namespace. The three blacklist ops are
  // namespace-scoped (`server/memory/api/ops_blacklist.py`: list VIEWER, add WRITER, remove
  // ADMIN); the panel reads only when the role is known, and shows the add form / remove
  // buttons only to a role that reaches them. Setup's `memory.list` total for the
  // checklist is gated the same way.
  "blacklist.list": { min: "viewer", by: "namespace" },
  "blacklist.add": { min: "writer", by: "namespace" },
  "blacklist.remove": { min: "admin", by: "namespace" },
  "memory.auto_retirements": { min: "viewer", by: "namespace" },
  "memory.list": { min: "viewer", by: "namespace" },
  // The Memories view's "Usage audit" panel offers the button only to a role that reaches
  // `namespace.audit` (VIEWER); the inspector's "Rename (keep redirect)" only to one that
  // reaches `memory.move` (WRITER).
  "namespace.audit": { min: "viewer", by: "namespace" },
  // The mentor brief reads the ledger for one agent; a viewer of the namespace may ask.
  "mentor.brief": { min: "viewer", by: "namespace" },
  // The Assistant page's four reads.
  "namespace.inventory": { min: "viewer", by: "namespace" },
  "memory.inspect": { min: "viewer", by: "namespace" },
  "knowledge.inspect": { min: "viewer", by: "namespace" },
  "namespace.tune": { min: "viewer", by: "namespace" },
  "memory.move": { min: "writer", by: "namespace" },
  "memory.pin": { min: "writer", by: "namespace" },
  // The inspector's Restore on a superseded row and the Review queue's Auto-resolved Undo.
  "memory.restore": { min: "writer", by: "namespace" },
  "memory.unpin": { min: "admin", by: "namespace" },
  // Conversations' recording toggle: a namespace setting, ADMIN on the namespace.
  "conversation.set_logging": { min: "admin", by: "namespace" },
  // A repository card's Docs and Sources facets (`canCallOn`): Add a document and Remove,
  // and every verb on a document source.
  "knowledge.add": { min: "writer", by: "namespace" },
  "knowledge.delete": { min: "writer", by: "namespace" },
  "knowledge.source.add": { min: "admin", by: "namespace" },
  "knowledge.source.check": { min: "admin", by: "namespace" },
  "knowledge.source.rotate": { min: "admin", by: "namespace" },
  "knowledge.source.remove": { min: "admin", by: "namespace" },
  "knowledge.source.sync_now": { min: "admin", by: "namespace" },
  // Listed for the registry mirror, not yet consulted at a call site: the
  // other namespace-scoped ops a view reads on mount. Each is the page's
  // own content (the view renders the humanized 403) or a secondary read
  // of a memory the caller has already listed. Org-level ops whose gate the
  // client cannot mirror (`namespace.list_mine`, `namespace.browse`) are
  // deliberately absent; `code.repos` is not a role gate either — see
  // `canCallCodeRepos` below.
  "memory.get": { min: "viewer", by: "namespace" },
  "memory.bulk_get": { min: "viewer", by: "namespace" },
  "memory.history": { min: "viewer", by: "namespace" },
  "memory.related": { min: "viewer", by: "namespace" },
  "memory.lineage": { min: "viewer", by: "namespace" },
  "memory.provenance": { min: "viewer", by: "namespace" },
  "memory.search_trace_log": { min: "viewer", by: "namespace" },
  "conversation.threads": { min: "viewer", by: "namespace" },
  "conversation.list": { min: "viewer", by: "namespace" },
  "namespace.get": { min: "viewer", by: "namespace" },
  "namespace.taxonomy": { min: "viewer", by: "namespace" },
  "knowledge.list": { min: "viewer", by: "namespace" },
  "facts.list": { min: "viewer", by: "namespace" },
  "run.list": { min: "viewer", by: "namespace" },
  "run.get": { min: "viewer", by: "namespace" },
  "grants.list": { min: "viewer", by: "namespace" },
  "learnings.list": { min: "viewer", by: "namespace" },
  "policy.decisions": { min: "viewer", by: "namespace" },
  "consolidations.list": { min: "viewer", by: "namespace" },
  "conflicts.allowlist_list": { min: "viewer", by: "namespace" },
  // The task inbox plane. The header bell polls `task.counts` only for a known role; the
  // Tasks view offers each action to a role that reaches it (the handler still authorizes
  // against the inbox's owner namespace).
  "task.get": { min: "viewer", by: "namespace" },
  "task.list": { min: "viewer", by: "namespace" },
  "task.counts": { min: "viewer", by: "namespace" },
  "task.history": { min: "viewer", by: "namespace" },
  "task.updates": { min: "viewer", by: "namespace" },
  "inbox.list": { min: "viewer", by: "namespace" },
  "inbox.policy_list": { min: "viewer", by: "namespace" },
  "task.create": { min: "writer", by: "namespace" },
  "task.accept": { min: "writer", by: "namespace" },
  "task.defer": { min: "writer", by: "namespace" },
  "task.block": { min: "writer", by: "namespace" },
  "task.unblock": { min: "writer", by: "namespace" },
  "task.resume": { min: "writer", by: "namespace" },
  "task.wont_do": { min: "writer", by: "namespace" },
  "task.cancel": { min: "writer", by: "namespace" },
  "task.reopen": { min: "writer", by: "namespace" },
  "task.update": { min: "writer", by: "namespace" },
  "task.reassign": { min: "writer", by: "namespace" },
  "inbox.register": { min: "admin", by: "namespace" },
  "inbox.policy_set": { min: "admin", by: "namespace" },
  "inbox.principals": { min: "admin", by: "namespace" },
} as const satisfies Record<string, OpGate>;

export type GatedOp = keyof typeof OP_MIN_ROLE;

const RANK: Record<MinRole, number> = { viewer: 1, writer: 2, admin: 3 };

/**
 * True when the client can show that the current principal is allowed to
 * call `op` on the current namespace. An admin claim (or auth off) is
 * enough for anything; otherwise `role` must be known and reach the op's
 * minimum. `null` — list_mine not loaded, failed, or holding no row for the
 * namespace — is "unknown", and unknown never sends a request.
 */
export function canCall(op: GatedOp, role: NamespaceRole | null, whoami: WhoAmI | null): boolean {
  if (hasAdminClaim(whoami)) return true;
  const gate: OpGate = OP_MIN_ROLE[op];
  if (gate.by === "claim") return false;
  if (role === null) return false;
  return RANK[role] >= RANK[gate.min];
}

// The flat claims, lowercased: org and platform admin reach no more than admin here.
const CLAIM_RANK: Record<string, number> = { ...RANK, org_admin: RANK.admin, platform_admin: RANK.admin };

/** The ops `canCallOn` answers: namespace-gated at writer or above, the card's verbs. */
export type RowOp = Extract<
  GatedOp,
  | "knowledge.add"
  | "knowledge.delete"
  | "knowledge.source.add"
  | "knowledge.source.check"
  | "knowledge.source.rotate"
  | "knowledge.source.remove"
  | "knowledge.source.sync_now"
>;

/** The fields of a `code.repos` row that `canCallOn` reads. */
export type RepoRow = Pick<CodeRepoSummary, "org" | "namespace" | "my_role" | "my_status" | "classification">;

/**
 * Whether the caller may call `op` on the namespace of one `code.repos` row, by the rule
 * `server/auth/authz.py::check_authorized` applies, from the token's claims and the
 * row's `my_role` (the caller's grant there) and `my_status`:
 * - auth off with no bearer: the server checks nothing. With a stored token or the
 *   proxy's header, or before whoami has loaded, the claims are unknown here, so only
 *   the grant counts and a restricted namespace offers nothing;
 * - a restricted namespace (`_check_restricted`): a token of its org naming it, a claim
 *   reaching the op's role, and a grant reaching it too. No claim widens it;
 * - otherwise a grant that reaches the role, or a claim that does on a token covering
 *   the namespace (named, or `*`); a token naming a namespace where its subject holds a
 *   grant or a request is held to that grant (`claim_cap`).
 * It cannot follow what whoami and the row do not carry: per-namespace token roles,
 * `ns_write`, delegation, service-account inheritance, a revoked grant row that still
 * caps a claim, the restricted kill switch and grants under other alias spellings. The
 * server stays the authority.
 */
export function canCallOn(op: RowOp, row: RepoRow, whoami: WhoAmI | null): boolean {
  const bearer = isProxyMode() || !!storedToken();
  if (isAuthDisabled() && !bearer) return true;
  const min = RANK[OP_MIN_ROLE[op].min];
  const grant = row.my_role ? RANK[row.my_role] : 0;
  if (!whoami || isAuthDisabled()) return row.classification !== "restricted" && grant >= min;
  const claim = Math.max(0, ...(whoami.roles ?? []).map((r) => CLAIM_RANK[r.toLowerCase()] ?? 0));
  const namespaces = whoami.namespaces ?? [];
  const inOrg = whoami.org === "*" || whoami.org === row.org;
  const named = inOrg && namespaces.includes(row.namespace);
  if (row.classification === "restricted") return whoami.org === row.org && named && claim >= min && grant >= min;
  const covered = inOrg && (named || namespaces.includes("*"));
  const capped = named && (row.my_status === "member" || row.my_status === "pending");
  return grant >= min || (covered && !capped && claim >= min);
}

/**
 * `code.repos` is not a role gate on the current namespace: the transport
 * authorizes it against the deployment's default code namespace and the
 * handler then omits entries the caller cannot read
 * (`server/code/api/models_repos.py::CodeReposInput`). A token with no code
 * namespace at all is refused outright — three silent 403s per page load
 * from the header bell, Setup's token form and Sharing. The honest client-side fact is
 * standing on at least one code-kind namespace: an active `list_mine` row with `kind ===
 * "code"` (the row model exposes it), or an admin claim / auth off. `null` rows are
 * "unknown" and, as everywhere in this file, send nothing.
 */
export function canCallCodeRepos(mine: readonly MyNamespaceEntry[] | null, whoami: WhoAmI | null): boolean {
  if (hasAdminClaim(whoami)) return true;
  if (mine === null) return false;
  return mine.some((m) => m.kind === "code" && (m.status ?? "active") === "active");
}
