import type { CodeRepoSummary } from "../api";
import { joinState, type JoinState } from "./joinActions";
import {
  freshnessOf,
  namespaceState,
  type TrackerFreshness,
  type TrackerNamespaceState,
  type TrackerSource,
  type TrackerSourceNamespace,
} from "./trackerSources";

/**
 * A repository is one card with two facets: its code and its record, so a person looking
 * for "Acme/widgets" meets the repository once, in one vocabulary. This module
 * is the pure part: it joins `code.repos` rows and `tracker.source.list` namespaces on
 * the repository name — a tracker namespace's `repos[]` names the repositories its record
 * covers — into one card per repository, and picks the card's one primary verb. Nothing
 * here reads or renders.
 *
 * Five words, both facets: Open · Join · Request access · Withdraw
 * request · Leave. A chip or a button never says a namespace slug.
 */

export type Verb = "Open" | "Join" | "Request access" | "Withdraw request" | "Leave";
type FacetKind = "code" | "record";

/** The facet's contents, as a person names them. */
// One noun for the linked database — a tracker (issues, PRs, incidents). "issues & PRs"
// says what one holds, never what it is called.
export const RECORD_NOUN = "tracker";

/** ONE verb per state, the same five words on both facets. `closed` and `registering`
 *  have no verb: nothing can be done from here. */
export function verbForState(st: TrackerNamespaceState): Verb | null {
  switch (st) {
    case "member":
      return "Open";
    case "open":
      return "Join";
    case "approval":
      return "Request access";
    case "pending":
      return "Withdraw request";
    default:
      return null;
  }
}

/** Where the caller stands on a branch — the one rule tracker namespaces share
 * (`lib/joinActions.ts`). */
export function branchState(r: Pick<CodeRepoSummary, "my_status" | "join_policy">): JoinState {
  return joinState({
    member: r.my_status === "member",
    pending: r.my_status === "pending",
    join_policy: r.join_policy,
  });
}

/** master first, then release branches newest first, then the rest. */
// One collator per order — `localeCompare` with options builds an Intl.Collator on every
// comparison (26 ms at 4x sorting 23 repositories' branches).
const NUMERIC = new Intl.Collator(undefined, { numeric: true });
const PLAIN = new Intl.Collator();
export function branchOrder(a: Pick<CodeRepoSummary, "branch">, b: Pick<CodeRepoSummary, "branch">): number {
  const rank = (x: string) => (x === "master" || x === "main" ? 0 : /^\d/.test(x) ? 1 : 2);
  const ra = rank(a.branch),
    rb = rank(b.branch);
  if (ra !== rb) return ra - rb;
  if (ra === 1) return NUMERIC.compare(b.branch, a.branch);
  return PLAIN.compare(a.branch, b.branch);
}

/** The Code facet: one repository's branches (the `RepoGroup`, kept field-for-field so
 *  the Choose-branches and Add-branch dialogs read it). */
export interface CodeFacet {
  repo: string;
  branches: CodeRepoSummary[];
  /** The branch a first join lands on: master, else the newest. */
  primary: CodeRepoSummary;
  /** Where counts and freshness are read: the first branch you read, else primary. */
  head: CodeRepoSummary;
  restricted: boolean;
  joined: CodeRepoSummary[];
  pending: CodeRepoSummary[];
  /** open + approval: what a join or a request could still add. */
  joinable: CodeRepoSummary[];
  joinedCount: number;
  pendingCount: number;
  /** The caller maintains at least one branch, so may add one. */
  maintained: boolean;
}

export function buildCodeFacet(repo: string, list: readonly CodeRepoSummary[]): CodeFacet {
  const branches = [...list].sort(branchOrder);
  const joined = branches.filter((b) => branchState(b) === "member");
  const pending = branches.filter((b) => branchState(b) === "pending");
  return {
    repo,
    branches,
    primary: branches[0],
    head: joined[0] ?? branches[0],
    // The class, when the server states it: a member of a restricted repository still sees
    // it as restricted, whatever their own standing.
    restricted: branches.every((b) =>
      b.classification ? b.classification === "restricted" : branchState(b) === "approval" || b.readable === false,
    ),
    joined,
    pending,
    joinable: branches.filter((b) => branchState(b) === "open" || branchState(b) === "approval"),
    joinedCount: joined.length,
    pendingCount: pending.length,
    maintained: branches.some((b) => b.my_role === "admin"),
  };
}

/** A record namespace is named by its scope, not its slug. */
type RecordScope = "public" | "private";

export interface RecordNamespace {
  ns: TrackerSourceNamespace;
  source: TrackerSource;
  state: TrackerNamespaceState;
  scope: RecordScope;
  /** "Public" or "Private"; the namespace name when two share a scope. */
  label: string;
}

/** The Issues & PRs facet: the tracker namespace(s) whose `repos` name the repository. */
export interface RecordFacet {
  namespaces: RecordNamespace[];
  /** Distinct sources behind the namespaces, in first-seen order. */
  sources: TrackerSource[];
  /** The first source that carries counts, else null. */
  freshness: TrackerFreshness | null;
  /** Every source lacks a credential: nothing can be read yet. */
  unconfigured: boolean;
  restricted: boolean;
  joined: RecordNamespace[];
  pending: RecordNamespace[];
  /** `member_role: admin` on one of the namespaces. */
  maintained: boolean;
}

function scopeOf(ns: Pick<TrackerSourceNamespace, "classification">): RecordScope {
  return ns.classification === "restricted" ? "private" : "public";
}

const SCOPE_LABEL: Record<RecordScope, string> = { public: "Public", private: "Private" };

export function buildRecordFacet(
  entries: ReadonlyArray<{ ns: TrackerSourceNamespace; source: TrackerSource }>,
): RecordFacet {
  const byScope = new Map<RecordScope, number>();
  for (const e of entries) byScope.set(scopeOf(e.ns), (byScope.get(scopeOf(e.ns)) ?? 0) + 1);
  const namespaces: RecordNamespace[] = entries
    .map(({ ns, source }) => {
      const scope = scopeOf(ns);
      return {
        ns,
        source,
        state: namespaceState(ns),
        scope,
        label: (byScope.get(scope) ?? 0) > 1 ? ns.namespace : SCOPE_LABEL[scope],
      };
    })
    // Public before private; then by name so the order is stable.
    .sort((a, b) =>
      a.scope === b.scope ? a.ns.namespace.localeCompare(b.ns.namespace) : a.scope === "public" ? -1 : 1,
    );
  const sources: TrackerSource[] = [];
  for (const e of entries) if (!sources.includes(e.source)) sources.push(e.source);
  const configured = sources.filter((s) => s.configured && s.breaker.state !== "unconfigured");
  return {
    namespaces,
    sources,
    freshness: sources.map(freshnessOf).find((f) => f !== null) ?? null,
    unconfigured: sources.length > 0 && configured.length === 0,
    restricted: namespaces.length > 0 && namespaces.every((n) => n.scope === "private"),
    joined: namespaces.filter((n) => n.state === "member"),
    pending: namespaces.filter((n) => n.state === "pending"),
    maintained: namespaces.some((n) => n.ns.member_role === "admin"),
  };
}

export interface RepositoryCard {
  /** The header: `Org/repo` as configured, or as the record declares it. */
  repo: string;
  code: CodeFacet | null;
  record: RecordFacet | null;
  /** Every facet namespace needs a maintainer's approval (or is closed). */
  restricted: boolean;
  /** The caller maintains a facet namespace: the Details disclosure renders. */
  maintained: boolean;
  joinedCount: number;
  pendingCount: number;
}

const norm = (repo: string) => repo.trim().toLowerCase();

/**
 * One card per repository. Code rows group by `repo`; a tracker namespace joins
 * every card whose repository its `repos[]` names; a namespace naming no
 * configured repository gets a record-only card headed by the name it
 * declares (or by its source's name when it declares none).
 */
export function buildCards(repos: readonly CodeRepoSummary[], sources: readonly TrackerSource[]): RepositoryCard[] {
  const codeBy = new Map<string, { repo: string; rows: CodeRepoSummary[] }>();
  for (const r of repos) {
    const k = norm(r.repo);
    const e = codeBy.get(k) ?? { repo: r.repo, rows: [] };
    e.rows.push(r);
    codeBy.set(k, e);
  }
  const recordBy = new Map<
    string,
    { repo: string; entries: Array<{ ns: TrackerSourceNamespace; source: TrackerSource }> }
  >();
  const attach = (repo: string, ns: TrackerSourceNamespace, source: TrackerSource) => {
    const k = norm(repo);
    const head = codeBy.get(k)?.repo ?? repo;
    const e = recordBy.get(k) ?? { repo: head, entries: [] };
    if (!e.entries.some((x) => x.ns.namespace === ns.namespace)) e.entries.push({ ns, source });
    recordBy.set(k, e);
  };
  for (const s of sources) {
    for (const ns of s.namespaces) {
      if (ns.repos.length === 0) attach(s.name, ns, s);
      for (const repo of ns.repos) attach(repo, ns, s);
    }
  }
  const keys = new Set([...codeBy.keys(), ...recordBy.keys()]);
  const cards: RepositoryCard[] = [];
  for (const k of keys) {
    const c = codeBy.get(k);
    const r = recordBy.get(k);
    const repo = c?.repo ?? r?.repo;
    if (repo === undefined) continue;
    const code = c ? buildCodeFacet(c.repo, c.rows) : null;
    const record = r ? buildRecordFacet(r.entries) : null;
    const joinedCount = (code?.joinedCount ?? 0) + (record?.joined.length ?? 0);
    const pendingCount = (code?.pendingCount ?? 0) + (record?.pending.length ?? 0);
    const facets = [code ? code.restricted : null, record ? record.restricted : null].filter(
      (x): x is boolean => x !== null,
    );
    const card: RepositoryCard = {
      repo,
      code,
      record,
      restricted: facets.length > 0 && facets.every(Boolean),
      maintained: (code?.maintained ?? false) || (record?.maintained ?? false),
      joinedCount,
      pendingCount,
    };
    cards.push(card);
  }
  // Repositories you read first, then the ones you can join at once, then by name.
  return cards.sort(
    (a, b) =>
      (b.joinedCount > 0 ? 1 : 0) - (a.joinedCount > 0 ? 1 : 0) ||
      (a.restricted ? 1 : 0) - (b.restricted ? 1 : 0) ||
      a.repo.localeCompare(b.repo),
  );
}

/** The header's one primary button. */
interface PrimaryAction {
  verb: Exclude<Verb, "Leave">;
  facet: FacetKind;
  branch?: CodeRepoSummary;
  record?: RecordNamespace;
}

function codeTarget(code: CodeFacet, st: JoinState): CodeRepoSummary | undefined {
  if (branchState(code.primary) === st) return code.primary;
  return code.branches.find((b) => branchState(b) === st);
}

/**
 * Open when they read something (code first, then the record); else the
 * first facet the person cannot yet read — Join beats Request access, code
 * beats record; else Withdraw request on a pending one; else nothing.
 */
export function primaryAction(card: Pick<RepositoryCard, "code" | "record">): PrimaryAction | null {
  const { code, record } = card;
  const rec = (st: TrackerNamespaceState) => record?.namespaces.find((n) => n.state === st);
  const readBranch = code?.joined[0];
  if (readBranch) return { verb: "Open", facet: "code", branch: readBranch };
  const readRecord = rec("member");
  if (readRecord) return { verb: "Open", facet: "record", record: readRecord };
  for (const [st, verb] of [
    ["open", "Join"],
    ["approval", "Request access"],
    ["pending", "Withdraw request"],
  ] as const) {
    const b = code ? codeTarget(code, st) : undefined;
    if (b) return { verb, facet: "code", branch: b };
    const r = rec(st);
    if (r) return { verb, facet: "record", record: r };
  }
  return null;
}

/**
 * Whether a restricted namespace can be approved at all. Neither
 * `tracker.source.list` nor `code.repos` says who maintains a namespace before
 * the click — `pending_admins` arrives only on the join response and on
 * `namespace.list_mine` rows already pending. Until the list carries the live-admin
 * count, the answer is "unknown" and Request stays enabled.
 */
export function canBeApproved(_ns: Pick<TrackerSourceNamespace, "namespace">): boolean | null {
  return null;
}

export const NO_APPROVER = "no maintainer can approve yet";

/** The caller maintains the repository but this token does not name it:
 *  say so (`unreadable_reason`). The UI never offers to adopt a repository: whoever adds
 * one is its maintainer, and an indexer-registered one
 *  gets its first maintainer from an operator. */
export function maintainsOutOfScope(b: Pick<CodeRepoSummary, "unreadable_reason" | "my_role">): boolean {
  return b.unreadable_reason === "token_scope" && b.my_role === "admin";
}

export const OUT_OF_SCOPE_MAINTAINER = "Sign in again — a fresh login names the repositories you maintain.";
