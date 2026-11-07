// What a new token can read and where it saves, computed from the person's own
// membership list before the token exists, and the same in words for each token in the
// list.
//
// A token reads either everything its owner belongs to (a `*` claim: every namespace
// and repository, now and after they join more, at their own role in each) or exactly
// the namespaces it lists, in that order. A `*` token saves to the namespace named
// before `*`; a list follows the order in `server/identity/app/caller_home.py`.
//
// A restricted repository is read only by a token that names it AND whose own subject
// holds a direct grant; the mint writes that grant for each one named. The page lists
// the restricted repositories a token would not read before the click instead of after
// the 403.
import type { CodeRepoSummary, MyNamespaceEntry, TokenEntry } from "../api";

export interface ScopeRepo {
  repo: string;
  /** One entry per branch namespace of this repository, in list order. */
  branches: { namespace: string; branch: string }[];
}

export interface TokenReadScope {
  /** Memory / tracker namespaces the token reads (active membership). */
  namespaces: string[];
  /** Repositories the token reads, grouped from their branch namespaces. */
  repositories: ScopeRepo[];
  /** Restricted repositories the person belongs to that this token still
   * cannot read (the token's own subject holds no grant). */
  excludedRestricted: ScopeRepo[];
  /** Memberships still awaiting approval: readable once approved, with no
   *  new token. */
  pending: string[];
}

type RepoFacts = Pick<CodeRepoSummary, "namespace" | "repo" | "branch"> & {
  classification?: string;
};

function groupRepos(rows: MyNamespaceEntry[], facts: Record<string, RepoFacts>): ScopeRepo[] {
  const by = new Map<string, ScopeRepo>();
  for (const n of rows) {
    const f = facts[n.namespace];
    const repo = f?.repo ?? n.namespace;
    const grp = by.get(repo) ?? { repo, branches: [] };
    grp.branches.push({ namespace: n.namespace, branch: f?.branch ?? n.namespace });
    by.set(repo, grp);
  }
  return Array.from(by.values()).sort((a, b) => a.repo.localeCompare(b.repo));
}

/** What a token that reads everything its owner belongs to reads. `mine` is the caller's `namespace.list_mine` rows (null while
 *  loading); `repos` the `code.repos` rows, which carry each repository's
 *  classification (null when the caller may not list them). */
export function everythingScope(mine: MyNamespaceEntry[] | null, repos: RepoFacts[] | null): TokenReadScope {
  const rows = mine ?? [];
  const facts: Record<string, RepoFacts> = Object.fromEntries((repos ?? []).map((r) => [r.namespace, r]));
  const active = rows.filter((r) => r.status !== "pending");
  const pending = rows.filter((r) => r.status === "pending").map((r) => r.namespace);
  const codeRows = active.filter((r) => r.kind === "code");
  const restricted = codeRows.filter((r) => facts[r.namespace]?.classification === "restricted");
  const readableCode = codeRows.filter((r) => facts[r.namespace]?.classification !== "restricted");
  const namespaces = active.filter((r) => r.kind !== "code").map((r) => r.namespace);
  return {
    namespaces,
    repositories: groupRepos(readableCode, facts),
    excludedRestricted: groupRepos(restricted, facts),
    pending,
  };
}

type TokenClaim = Pick<TokenEntry, "namespaces" | "roles" | "ns_roles">;

const WRITES = new Set(["writer", "admin"]);

/** One line for a token row in the Tokens list: what it reads and where it saves, in
 *  words, instead of the raw claim. */
export function describeTokenScope(
  t: TokenClaim,
  {
    yours = true,
    writable,
    label = (n) => n,
  }: {
    yours?: boolean;
    /** The memory namespaces the viewer may write; unknown when absent. */
    writable?: readonly string[];
    /** A namespace in words, e.g. a repository's as "repo · branch". */
    label?: (ns: string) => string;
  } = {},
): string {
  const ns = t.namespaces || [];
  if (ns.length === 0) return "reads nothing";
  if (ns.includes("*")) {
    const home = ns[0] !== "*" ? ns[0] : "";
    const also = ns.filter((n) => n !== "*" && n !== home);
    // Another person's token (an admin of one of its namespaces sees it) reads as theirs.
    const who = yours ? "you belong" : "its owner belongs";
    const reads = `reads everything ${who} to${also.length ? `, plus ${also.map(label).join(", ")}` : ""}`;
    // It writes through its owner's grants and its own one role: either one short of
    // writer, and it only reads.
    const tokenWrites = (t.roles || []).some((r) => WRITES.has(r));
    if (!tokenWrites || (yours && writable?.length === 0)) return `${reads} · read-only`;
    const pick = yours ? "the namespace you pick in Loom" : "the namespace its owner picks in Loom";
    // Its home counts only while its owner may write there; else it follows the pick.
    const saves = home && (!yours || !writable || writable.includes(home)) ? home : pick;
    return `${reads} · saves to ${saves}`;
  }
  const roles = t.ns_roles && Object.keys(t.ns_roles).length > 0 ? t.ns_roles : null;
  const distinct = new Set(ns.map((n) => roles?.[n] ?? ""));
  const listed =
    roles && distinct.size > 1
      ? ns.map((n) => `${label(n)} (${roles[n]})`).join(", ")
      : `${ns.map(label).join(", ")}${roles ? ` (${roles[ns[0]]})` : ""}`;
  // Where a list saves can follow its owner's pick, so the row names no namespace.
  const writes = roles ? ns.some((n) => WRITES.has(roles[n] ?? "")) : (t.roles || []).some((r) => WRITES.has(r));
  return `reads only ${listed}${writes ? "" : " · read-only"}`;
}
