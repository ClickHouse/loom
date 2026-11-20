// What a new token can read and where it saves, computed from the person's own
// membership list before the token exists, and the same in words for each token in the
// list.
//
// A token reads either everything its owner belongs to (a `*` claim: every namespace
// and repository, now and after they join more, at their own role in each) or exactly
// the namespaces it lists, in that order. A `*` token saves to the namespace named
// before `*`; a list saves where its mint recorded (`saves_to`,
// `server/identity/app/token_saves_to.py`).
//
// A restricted repository is read only by a token that names it AND whose own subject
// holds a direct grant; the mint writes that grant for each one named. The page lists
// the restricted repositories a token would not read before the click instead of after
// the 403.
import type { CodeRepoSummary, MyNamespaceEntry, TokenEntry } from "../api";
import { isCollectionName } from "./namespace";

export interface ScopeRepo {
  repo: string;
  /** One entry per branch namespace of this repository, in list order. */
  branches: { namespace: string; branch: string }[];
}

export interface TokenReadScope {
  /** Memory / tracker namespaces the token reads (active membership). */
  namespaces: string[];
  /** Whether the form offers a collection to list (`offersCollection`): a `*` claim names
   *  none, so the token reads none of them, and the panel says how a token reads one. */
  collectionsOffered: boolean;
  /** Repositories the token reads, grouped from their branch namespaces. */
  repositories: ScopeRepo[];
  /** Restricted repositories the person belongs to that the token reads only when
   *  added. */
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

/** Whether the token form offers `r` under Collections: a collection the person holds a
 *  grant on directly, the one the mint accepts (not a claim-only row, nor one a client token
 *  reads through its creator). */
export function offersCollection(r: MyNamespaceEntry): boolean {
  return (
    isCollectionName(r.namespace) && (r.role === "viewer" || r.role === "writer" || r.role === "admin") && !r.inherited
  );
}

/** Whether a namespace its owner writes counts as written by a token with `claim`: a `*`
 *  claim names no collection, so a collection counts only where the claim names it. */
export function claimReaches(claim: readonly string[], ns: string): boolean {
  return !claim.includes("*") || !isCollectionName(ns) || claim.includes(ns);
}

/** What a token that reads everything its owner belongs to reads. `mine` is the
 *  caller's `namespace.list_mine` rows (null while loading); `repos` the `code.repos`
 *  rows, which carry each repository's classification (null when the caller may not
 *  list them). */
export function everythingScope(mine: MyNamespaceEntry[] | null, repos: RepoFacts[] | null): TokenReadScope {
  const rows = mine ?? [];
  const facts: Record<string, RepoFacts> = Object.fromEntries((repos ?? []).map((r) => [r.namespace, r]));
  const active = rows.filter((r) => r.status !== "pending");
  const pending = rows.filter((r) => r.status === "pending").map((r) => r.namespace);
  const codeRows = active.filter((r) => r.kind === "code");
  const restricted = codeRows.filter((r) => facts[r.namespace]?.classification === "restricted");
  const readableCode = codeRows.filter((r) => facts[r.namespace]?.classification !== "restricted");
  const namespaces = active.filter((r) => r.kind !== "code" && !isCollectionName(r.namespace)).map((r) => r.namespace);
  return {
    namespaces,
    collectionsOffered: active.some(offersCollection),
    repositories: groupRepos(readableCode, facts),
    excludedRestricted: groupRepos(restricted, facts),
    pending,
  };
}

type TokenClaim = Pick<TokenEntry, "namespaces" | "roles" | "ns_roles" | "saves_to">;

const WRITES = new Set(["writer", "admin"]);

/** A `*` claim's home (the name before the `*`, never a collection: the mint refuses one as
 *  a home, so one named first is read like the rest) and the names it also reads. */
export function starClaim(namespaces: readonly string[]): { home: string; also: string[] } {
  const home = namespaces[0] !== "*" && !isCollectionName(namespaces[0]) ? (namespaces[0] ?? "") : "";
  return { home, also: namespaces.filter((n) => n !== "*" && n !== home) };
}

/** What a `*` token's line adds: it reads no collection but the ones its claim also names,
 *  however its owner reaches one (`sharing._claim_limits`); nothing for a viewer who reads no
 *  collection (`readsCollections`), whom "everything" already describes. */
export function noCollection(also: readonly string[], readsCollections: boolean): string {
  if (also.some(isCollectionName)) return ", but no other collection";
  return readsCollections ? ", but no collection" : "";
}

/** One line for a token row in the Tokens list: what it reads and where it saves, in
 *  words, instead of the raw claim. */
export function describeTokenScope(
  t: TokenClaim,
  {
    yours = true,
    writable,
    writesAny = writable,
    label = (n) => n,
    readsCollections = true,
  }: {
    yours?: boolean;
    /** The memory namespaces the viewer may write that a token saves to (no collection);
     *  unknown when absent. */
    writable?: readonly string[];
    /** Every namespace the viewer may write, a collection included (a list token's call may
     *  name one; a `*` token reads no collection, so none counts for it); `writable` when
     *  absent. */
    writesAny?: readonly string[];
    /** A namespace in words, e.g. a repository's as "repo · branch". */
    label?: (ns: string) => string;
    /** Whether the viewer may read a collection (`readsCollections`); another person's
     *  token says it reads none either way, its owner's reach unknown here. */
    readsCollections?: boolean;
  } = {},
): string {
  const ns = t.namespaces || [];
  if (ns.length === 0) return "reads nothing";
  if (ns.includes("*")) {
    const { home, also } = starClaim(ns);
    // Another person's token (an admin of one of its namespaces sees it) reads as theirs.
    const who = yours ? "you belong" : "its owner belongs";
    // The claim alone decides which collections it reads, whoever's token it is.
    const reads = `reads everything ${who} to${also.length ? `, plus ${also.map(label).join(", ")}` : ""}${noCollection(also, readsCollections || !yours)}`;
    // It writes through its owner's grants and its own one role: either one short of
    // writer, and it only reads.
    const tokenWrites = (t.roles || []).some((r) => WRITES.has(r));
    const writesNamed = writesAny?.filter((n) => claimReaches(ns, n));
    if (!tokenWrites || (yours && writesNamed?.length === 0)) return `${reads} · read-only`;
    // Its owner writes only namespaces a token never saves to unasked: a call may name one.
    if (yours && writable?.length === 0) return `${reads} · saves nowhere by default`;
    // Its home counts only while its owner may write there; else it follows the pick (the
    // server skips a picked collection, which this line does not name).
    if (home && (!yours || !writable || writable.includes(home))) return `${reads} · saves to ${home}`;
    return `${reads} · ${whereYouWork(yours)}`;
  }
  const roles = t.ns_roles && Object.keys(t.ns_roles).length > 0 ? t.ns_roles : null;
  const distinct = new Set(ns.map((n) => roles?.[n] ?? ""));
  const listed =
    roles && distinct.size > 1
      ? ns.map((n) => `${label(n)} (${roles[n]})`).join(", ")
      : `${ns.map(label).join(", ")}${roles ? ` (${roles[ns[0]]})` : ""}`;
  const writesIn = (n: string) => (roles ? WRITES.has(roles[n] ?? "") : (t.roles || []).some((r) => WRITES.has(r)));
  const writes = ns.some(writesIn);
  if (t.saves_to) {
    // Recorded at mint, where the token could write. It writes through its owner's grants:
    // once the owner may not write there, its calls land in the first memory namespace of
    // its list the owner may write, never a collection (`caller_home._pick_default`), or
    // nowhere.
    if (!yours || !writable || writable.includes(t.saves_to))
      return `reads only ${listed} · saves to ${label(t.saves_to)}`;
    const next = ns.find((n) => writesIn(n) && writable.includes(n));
    const lost = `can't write to ${label(t.saves_to)}`;
    if (next) return `reads only ${listed} · saves to ${label(next)} now; ${lost}`;
    // Still writes a listed namespace a call may name (a collection), or nothing at all.
    const still = ns.some((n) => writesIn(n) && writesAny?.includes(n));
    return `reads only ${listed} · ${still ? "saves nowhere by default now" : "read-only now"}; ${lost}`;
  }
  // No recorded target: minted before tokens recorded one (where it saves can follow its
  // owner's pick), or one whose only writable entries are collections (a call names where it
  // saves). The line names no namespace for either.
  return `reads only ${listed}${writes ? "" : " · read-only"}`;
}

// A token that records no target saves where its owner works on the dashboard.
const whereYouWork = (yours: boolean) =>
  `saves to ${yours ? "the namespace you pick in Loom" : "the namespace its owner picks in Loom"}`;
