// What a token minted from the Setup page can read and write, computed from
// the caller's own membership list before the token exists.
//
// Two token shapes leave that page:
//  - "For an AI agent" (kind = mcp): ONE home namespace, where the client
//    writes. A token claiming `*` inherits its creator's live grants, so it
//    reads every namespace and repository the person belongs to, now and after
//    they join more; one minted for a named namespace reads only that one (and
//    any restricted namespaces it names).
//  - "For a script or bot" (scoped): reads exactly the namespaces ticked, in
//    that order, and nothing else.
//
// One rule narrows both: a restricted repository is read only by a token that names it
// AND whose own subject holds a direct grant. A token minted here runs under a generated
// `sa:` subject with no grants, so it never reads a restricted repository, however the
// person's own access looks. The page says so before the click instead of after the 403.
import type { CodeRepoSummary, MyNamespaceEntry, ServiceAccountEntry } from "../api";

export interface ScopeRepo {
  repo: string;
  /** One entry per branch namespace of this repository, in list order. */
  branches: { namespace: string; branch: string }[];
}

export interface TokenReadScope {
  /** Where an agent token writes: memories, captures, working notes. */
  home: string;
  /** Memory / tracker namespaces the token reads (active membership). */
  namespaces: string[];
  /** Repositories the token reads, grouped from their branch namespaces. */
  repositories: ScopeRepo[];
  /** Restricted repositories the person belongs to that this token still
   * cannot read (the token's own subject holds no grant). */
  excludedRestricted: ScopeRepo[];
  /** Memberships still awaiting approval: readable once approved, and the
   *  agent token follows them in with no re-mint. */
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

/** The read/write scope of an agent token minted with `home` as its
 *  namespace. `mine` is the caller's `namespace.list_mine` rows (null while
 *  loading); `repos` the `code.repos` rows, which carry each repository's
 *  classification (null when the caller may not list them). */
export function agentTokenScope(
  mine: MyNamespaceEntry[] | null,
  repos: RepoFacts[] | null,
  home: string,
): TokenReadScope {
  const rows = mine ?? [];
  const facts: Record<string, RepoFacts> = Object.fromEntries((repos ?? []).map((r) => [r.namespace, r]));
  const active = rows.filter((r) => r.status !== "pending");
  const pending = rows.filter((r) => r.status === "pending").map((r) => r.namespace);
  const codeRows = active.filter((r) => r.kind === "code");
  const restricted = codeRows.filter((r) => facts[r.namespace]?.classification === "restricted");
  const readableCode = codeRows.filter((r) => facts[r.namespace]?.classification !== "restricted");
  const namespaces = active.filter((r) => r.kind !== "code").map((r) => r.namespace);
  if (home && !namespaces.includes(home)) namespaces.unshift(home);
  return {
    home,
    namespaces,
    repositories: groupRepos(readableCode, facts),
    excludedRestricted: groupRepos(restricted, facts),
    pending,
  };
}

/** One line for a token row in the Tokens list: what it writes and what it
 *  reads, in words, instead of the raw `roles · namespaces` claim dump that
 *  reads as "this token sees one namespace". */
export function describeTokenScope(t: Pick<ServiceAccountEntry, "kind" | "namespaces" | "roles">): string {
  const ns = t.namespaces || [];
  const roles = t.roles || [];
  if (t.kind === "mcp") {
    // The first claim is the home namespace; any further ones are restricted
    // namespaces the token reads by name with a grant of its own. A `*` claim
    // reads wherever the person holds a grant (the server's `inherited_grants`);
    // without one the token reads only what it names (`named_namespaces`).
    const home = ns.length >= 1 && ns[0] !== "*" ? ns[0] : null;
    const extras = ns.slice(1).filter((n) => n !== "*");
    const writes = home ? `writes ${home}` : "writes any namespace you can write";
    const restricted = extras.length ? ` · plus restricted: ${extras.join(", ")}` : "";
    if (home && !ns.includes("*")) return `${writes} · reads only ${home}${restricted}`;
    return `${writes} · reads every namespace and repository you belong to${restricted}`;
  }
  if (ns.length === 0) return roles.join(", ");
  const role = roles.length ? `${roles.join(", ")} on ` : "";
  return `reads only ${role}${ns.join(", ")}`;
}
