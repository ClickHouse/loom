// One read per question per page.
//
// Several components on one page ask the same org- or namespace-scoped
// question within milliseconds of each other — the bell, the token form
// and the Sharing page each asked `code.repos`; six components refetched
// `namespace.list_mine` the store already held; the memory inspector and
// the add-memory dialog each scanned 500 memories to build a tag list; a
// repository page asked `code.index_status` twice. This module memoises
// the promise per (question, key) for a short time so the second asker
// joins the first request instead of issuing another. A rejected read is
// forgotten at once so the next asker retries. Writers call
// `invalidateShared` after they change what a read returns.
import {
  codeIndexStatus,
  listCodeRepos,
  listMine,
  type CodeIndexStatus,
  type CodeRepoSummary,
  type Identity,
  type MyNamespaceEntry,
  getTaxonomy,
  getBudgetStatus,
  type BudgetStatus,
  boundedSignal,
  authHeader,
  ownsToken,
  storedToken,
} from "../api";
import { hasAutoResolved } from "../api/audit";
import { isRecord } from "./guards";

const cache = new Map<string, { at: number; p: Promise<unknown> }>();

export function shared<T>(key: string, ttlMs: number, fetcher: () => Promise<T>): Promise<T> {
  const hit = cache.get(key);
  const now = Date.now();
  // Cast: one cache holds every caller's type; the key identifies the fetcher that stored it.
  if (hit && now - hit.at < ttlMs) return hit.p as Promise<T>;
  const p = fetcher();
  cache.set(key, { at: now, p });
  p.catch(() => {
    if (cache.get(key)?.p === p) cache.delete(key);
  });
  return p;
}

/** Forget cached reads whose key starts with `prefix` (all when omitted). */
export function invalidateShared(prefix = ""): void {
  for (const k of Array.from(cache.keys())) if (k.startsWith(prefix)) cache.delete(k);
}

/** The caller's memberships: the store's copy when present, else one
 *  list_mine per org per 30 s shared by every asker. */
export function sharedMine(org: string): Promise<MyNamespaceEntry[]> {
  return shared(`mine:${org}`, 30_000, () => listMine(org));
}

/** The org's repositories (name, branch, classification, standing). */
export function sharedCodeRepos(org: string): Promise<CodeRepoSummary[]> {
  return shared(`repos:${org}`, 120_000, () => listCodeRepos(org));
}

/** One repository's index status, shared between the Health and Access tabs. */
export function sharedIndexStatus(id: Pick<Identity, "org" | "namespace">): Promise<CodeIndexStatus> {
  return shared(`index_status:${id.org}/${id.namespace}`, 60_000, () => codeIndexStatus(id));
}

/** The namespace's tag vocabulary for autocomplete, once per namespace per
 * five minutes: the taxonomy's tag list, which the server remembers per namespace version
 *  — not a memory.list of 500 rows (previews and all, ~1 MB) read to collect their tags.
 *  */
export function sharedTagVocabulary(id: Identity): Promise<string[]> {
  return shared(`tags:${id.org}/${id.namespace}`, 300_000, async () => {
    const t = await getTaxonomy(id, { top_n_tags: 500, top_n_entities: 1, top_n_communities: 0 });
    return (t.tags || []).map((e) => e.value).sort();
  });
}

/** `/v1/health` once per minute for every asker — the problems banner and
 *  the build watcher both read it on boot; the answer costs the server a
 *  ClickHouse round trip (590 ms on a cold replica in the live measurement). */
export function sharedHealth(): Promise<Record<string, unknown> | null> {
  return shared("health", 60_000, async () => {
    // A stalled server times out like any other request: the rejection is not
    // cached and every consumer catches it.
    // The page's own person's token rides along: a platform admin's answer names the
    // entry of each failing code-index tier (another tab may have stored another's).
    const headers = ownsToken(storedToken()) ? authHeader() : {};
    const r = await fetch("/v1/health", { headers, signal: boundedSignal() });
    // `ok` is absent on some hand-rolled responses (tests); only an explicit
    // failure status is a miss.
    if (typeof r.ok === "boolean" && !r.ok) return null;
    const body: unknown = await r.json();
    return isRecord(body) ? body : null;
  });
}

/** The namespace's budget status, once per 25 s for every asker: the sidebar's over-cap
 *  badge polls it every 30 s and the Usage page reads it on load — one answer to one
 *  question. */
export function sharedBudgetStatus(id: Pick<Identity, "org" | "namespace">): Promise<BudgetStatus> {
  return shared(`budget:${id.org}/${id.namespace}`, 25_000, () =>
    getBudgetStatus({ org: id.org, namespace: id.namespace }),
  );
}

/** Whether the namespace has auto-resolved memories, once per 25 s for every asker: the
 *  sidebar's Review queue entry and the Review queue page ask the same yes/no. A static
 *  import: an on-demand chunk that 404s after a redeploy would reload the page from this
 *  timer (staleChunk.ts), with nobody clicking. */
export function sharedHasAutoResolved(id: Pick<Identity, "org" | "namespace">): Promise<boolean> {
  return shared(`auto_resolved:${id.org}/${id.namespace}`, 25_000, () =>
    hasAutoResolved({ org: id.org, namespace: id.namespace }),
  );
}
