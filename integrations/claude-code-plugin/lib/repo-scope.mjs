// repo-scope — which indexed repository is this checkout? One resolver for
// the code shunt (PreToolUse), the session-start primer and the MCP headers
// helper, so the three agree on the namespace and share one
// cache file: $TMPDIR/loom-shunt-<sha1(root)[0:12]>.json.
//
// Entry shape: {ts, indexed:false} | {ts, indexed:false, unresolved:true} |
// {ts, indexed:true, org, namespace, repo, branch, prefixes, asOf, fetchedAt}.
// A definite answer is trusted CACHE_TTL_MS; an unresolved one (the server
// did not answer) UNRESOLVED_TTL_MS, then the next caller retries.
import fs from "node:fs";
import os from "node:os";
import { git, postBounded, resolveCodeRepo, resolveToken, shuntCachePath } from "./loom-client.mjs";
import { repoSlug } from "./code-shunt.mjs";

export const CACHE_TTL_MS = 10 * 60 * 1000;
/** The repository lookup (`code.repos` + `code.index_status`)
 *  runs once per cache window, so it gets its own bound — long enough for a
 *  listing the server takes 1–2 s to build plus a remote round trip — where
 *  the per-tool-call hooks keep the 3 s default. `LOOM_SHUNT_RESOLVE_TIMEOUT_MS`. */
export const DEFAULT_RESOLVE_TIMEOUT_MS = 8000;
/** How long a cache entry that records "the lookup did not answer" is trusted
 *  before the next tool call retries. Seconds, not the ten minutes a definite
 *  answer gets: a lookup that ran out of time must not be cached as "not
 *  indexed" for ten minutes, or every read passes on a false premise. */
export const UNRESOLVED_TTL_MS = 60 * 1000;

/** Is a cached repository entry still current at `now`? An `unresolved`
 *  entry ages out after `unresolvedTtlMs`; a definite one after `ttlMs`. */
export function cacheIsFresh(entry, now, ttlMs, unresolvedTtlMs = UNRESOLVED_TTL_MS) {
  if (!entry || typeof entry.ts !== "number") return false;
  return now - entry.ts < (entry.unresolved ? unresolvedTtlMs : ttlMs);
}

/** What a `code.repos` answer says about `slug` (lowercase owner/name):
 *  `{ unresolved: true }` when the call did not answer (null, no `repos`
 *  array) — the caller must not treat that as "not indexed";
 *  `{ hit }` for the first readable entry whose repo is the slug;
 *  `{ indexed: false }` when the listing answered and holds no such entry. */
export function repoLookupOutcome(repos, slug, branch = "") {
  if (repos == null || !Array.isArray(repos.repos)) return { unresolved: true };
  const readable = repos.repos.filter((r) => r && r.readable !== false);
  let mine = readable.filter((r) => String(r.repo || "").toLowerCase() === slug);
  if (!mine.length && !slug.includes("/")) {
    // A bare name (`loom`) matches the one readable repository of that name;
    // two owners with the name match neither — say owner/name then.
    mine = readable.filter(
      (r) =>
        String(r.repo || "")
          .toLowerCase()
          .split("/")
          .pop() === slug,
    );
    if (new Set(mine.map((r) => String(r.repo).toLowerCase())).size !== 1) mine = [];
  }
  if (mine.length === 0) return { indexed: false };
  // The checkout's branch picks the branch's own index; else
  // the base branch, else the first readable row.
  const b = String(branch || "").toLowerCase();
  const byBranch = (name) => mine.find((r) => String(r.branch || "").toLowerCase() === name);
  return { hit: (b && byBranch(b)) || byBranch("master") || byBranch("main") || mine[0] };
}

/** What a `code.repos` answer to the checkout's candidate
 *  list says. `remotes` is `checkoutRemotes()`'s [{slug, via}]. The first
 *  candidate with a readable entry wins (`via` = its remote: origin |
 *  upstream | remote:<name> | override); else a row the server matched as
 *  `fork_of` a candidate names the upstream (`via: "fork_of"`). The hit's
 *  branch is picked exactly as `repoLookupOutcome` does, so
 *  a fork's feature branch reads the upstream's default-branch index.
 *  Returns `{ unresolved }`, `{ indexed: false }`, or
 *  `{ hit, via, checkoutSlug, matchedSlug }`. */
export function candidateLookupOutcome(repos, remotes, branch = "") {
  if (repos == null || !Array.isArray(repos.repos)) return { unresolved: true };
  for (const r of remotes || []) {
    const o = repoLookupOutcome(repos, r.slug, branch);
    if (o.hit) return { hit: o.hit, via: r.via, checkoutSlug: r.slug, matchedSlug: r.slug };
  }
  const fork = repos.repos.find((r) => r && r.matched_via === "fork_of" && r.readable !== false);
  const up = fork ? String(fork.repo || "").toLowerCase() : "";
  const o = up ? repoLookupOutcome(repos, up, branch) : { indexed: false };
  if (!o.hit) return { indexed: false };
  return { hit: o.hit, via: "fork_of", checkoutSlug: String(fork.checkout_slug || ""), matchedSlug: up };
}

/** The least time `code.index_status` gets when `code.repos` used most of
 *  the lookup's budget; a hit without a status keeps the listing's commit. */
const MIN_STATUS_MS = 500;
/** The header the MCP connection carries: `org/namespace`. */
export const SCOPE_HEADER = "X-Loom-Code-Scope";

export function resolveTimeoutMs(env = process.env) {
  const n = Number(env.LOOM_SHUNT_RESOLVE_TIMEOUT_MS || DEFAULT_RESOLVE_TIMEOUT_MS);
  return Math.max(1000, Number.isFinite(n) && n > 0 ? n : DEFAULT_RESOLVE_TIMEOUT_MS);
}

/** The cache key for a session: the checkout's root, or — outside a checkout
 *  — the named repository (`resolveCodeRepo`), so the hooks and the MCP
 *  headers helper still share one entry. null when neither exists. */
export function scopeKey(root, env = process.env) {
  if (root) return root;
  const named = resolveCodeRepo(env);
  return named ? `code-repo:${named.toLowerCase()}` : null;
}

export function cachePathFor(root, tmp = os.tmpdir()) {
  return shuntCachePath(root, tmp);
}

/** The cached entry for `root` when it is still fresh at `now`, else null. */
export function readCache(root, now = Date.now(), tmp = os.tmpdir()) {
  try {
    const c = JSON.parse(fs.readFileSync(cachePathFor(root, tmp), "utf8"));
    return cacheIsFresh(c, now, CACHE_TTL_MS) ? c : null;
  } catch {
    return null;
  }
}

export function writeCache(root, entry, tmp = os.tmpdir()) {
  try {
    fs.writeFileSync(cachePathFor(root, tmp), JSON.stringify(entry));
  } catch {
    /* tmp not writable: fine */
  }
}

/** The MCP header for an indexed entry, {} otherwise. */
export function scopeHeader(entry) {
  if (!entry || !entry.indexed || !entry.org || !entry.namespace) return {};
  return { [SCOPE_HEADER]: `${entry.org}/${entry.namespace}` };
}

/** One `git remote -v` line's name and fetch URL. */
function remoteLines(root) {
  const out = [];
  for (const line of git(["remote", "-v"], root).split("\n")) {
    const m = line.match(/^(\S+)\s+(\S+)\s+\(fetch\)$/);
    if (m && !out.some((r) => r.name === m[1])) out.push({ name: m[1], url: m[2] });
  }
  return out;
}

/** owner/name as the remote spells it (the primer prints it); "" if none. */
function displaySlug(remoteUrl) {
  const m = String(remoteUrl || "")
    .trim()
    .replace(/\.git$/, "")
    .match(/[:/]([^/:]+)\/([^/]+)$/);
  return m ? `${m[1]}/${m[2]}` : "";
}

/**
 * The checkout's repository candidates, in match order, as
 * [{slug, via, name}]: `origin`, then `upstream`, then every other remote in
 * `git remote` order (via "remote:<name>"), then the configured repository
 * (`resolveCodeRepo`: LOOM_CODE_REPO, the `code_repo` option or config key;
 * via "configured"). The first readable candidate wins, so a checkout's own
 * index comes first and the configured repository serves a session outside
 * any checkout, or one whose checkout has no index. Slugs are lower-case
 * owner/name, or a bare name (`loom`) as configured; de-duplicated; one
 * bounded git call, none outside a checkout.
 */
export function checkoutRemotes(root, env = process.env) {
  const out = root ? gitRemotes(root) : [];
  const configured = resolveCodeRepo(env);
  if (configured) {
    const slug = repoSlug(configured) || configured.toLowerCase().replace(/\.git$/, "");
    if (!out.some((c) => c.slug === slug)) {
      out.push({ slug, via: "configured", name: displaySlug(configured) || configured });
    }
  }
  return out;
}

/** The checkout's own remotes as candidates (see `checkoutRemotes`). */
function gitRemotes(root) {
  const lines = remoteLines(root);
  const rank = (n) => (n === "origin" ? 0 : n === "upstream" ? 1 : 2);
  const ordered = lines.map((r, i) => ({ ...r, i })).sort((a, b) => rank(a.name) - rank(b.name) || a.i - b.i);
  const out = [];
  for (const r of ordered) {
    const slug = repoSlug(r.url);
    if (!slug || out.some((c) => c.slug === slug)) continue;
    const via = r.name === "origin" || r.name === "upstream" ? r.name : `remote:${r.name}`;
    out.push({ slug, via, name: displaySlug(r.url) });
  }
  return out;
}

/** The candidate slugs alone (see `checkoutRemotes`). */
export function checkoutRepoSlugs(root, env = process.env) {
  return checkoutRemotes(root, env).map((r) => r.slug);
}

/** The cache entry for a resolved hit, with how it matched. */
function hitEntry(ts, match, st) {
  const hit = match.hit;
  return {
    ts,
    indexed: true,
    org: hit.org,
    namespace: hit.namespace,
    repo: hit.repo || "",
    branch: hit.branch || "",
    prefixes: st?.path_prefixes || [],
    asOf: st?.git_head || hit.git_head || "",
    fetchedAt: st?.clone_fetched_at || "",
    matchedVia: match.via || "",
    checkoutSlug: match.checkoutSlug || "",
    primaryNamespace: primaryNamespaceOf(hit),
  };
}

/**
 * One lookup for the checkout at `root`, no cache read: one `code.repos`
 * naming the checkout's candidates, then one `code.index_status` for the
 * hit. Writes the shared cache. Returns {entry, status, remotes}.
 */
export async function lookupIndexedRepo(
  root,
  {
    env = process.env,
    now = Date.now,
    tmp = os.tmpdir(),
    consumer = "claude-code-shunt",
    post = postBounded,
    token = undefined,
    timeoutMs = undefined,
  } = {},
) {
  const remotes = checkoutRemotes(root, env);
  const slugs = remotes.map((r) => r.slug);
  const branch = root ? git(["rev-parse", "--abbrev-ref", "HEAD"], root) : "";
  const bearer = token === undefined ? resolveToken() : token;
  let entry = { ts: now(), indexed: false };
  let status = null;
  if (slugs.length && bearer) {
    // Name the repository so the server answers with that
    // entry alone, give the once-per-window lookup its own bound, and never
    // record a lookup that did not answer as "not indexed" — that entry is
    // `unresolved` and retried on the next call, not in ten minutes.
    // Both calls share one deadline, so a server that stalls costs the
    // caller one bound, not two.
    const budget = timeoutMs ?? resolveTimeoutMs(env);
    const deadline = Date.now() + budget;
    const full = slugs.filter((s) => s.includes("/"));
    let repos = await post("code.repos", { consumer, repos: full }, { token: bearer, timeoutMs: budget });
    let match = candidateLookupOutcome(repos, remotes, branch);
    if (!match.hit && !match.unresolved && full.length < slugs.length && full.length) {
      // A bare configured name needs the whole readable listing to match on.
      const left = Math.max(MIN_STATUS_MS, deadline - Date.now());
      repos = await post("code.repos", { consumer, repos: [] }, { token: bearer, timeoutMs: left });
      match = candidateLookupOutcome(repos, remotes, branch);
    }
    if (match.unresolved) {
      entry = { ts: now(), indexed: false, unresolved: true };
    } else if (match.hit) {
      const left = Math.max(MIN_STATUS_MS, deadline - Date.now());
      status = await post(
        "code.index_status",
        { org: match.hit.org, namespace: match.hit.namespace, consumer },
        { token: bearer, timeoutMs: left },
      );
      entry = hitEntry(now(), match, status);
    }
  }
  const key = scopeKey(root, env);
  if (key) writeCache(key, entry, tmp);
  return { entry, status, remotes };
}

/**
 * Resolve (and cache) the indexed repository for the checkout at `root`:
 * the shared cache, else `lookupIndexedRepo`. Every hook that needs the
 * checkout's index (shunt, MCP headers, primer, blast radius, prompt pack,
 * test gate) goes through here, so they agree.
 * `post` and `token` are injectable for tests.
 */
export async function resolveIndexedRepo(root, opts = {}) {
  const key = scopeKey(root, opts.env || process.env);
  if (!key) return { ts: Date.now(), indexed: false };
  const cached = readCache(key, (opts.now || Date.now)(), opts.tmp || os.tmpdir());
  if (cached) return cached;
  return (await lookupIndexedRepo(root, opts)).entry;
}

/** The repository's primary code namespace, where its team file rules live. */
export function primaryNamespaceOf(hit) {
  return String((hit && hit.primary_namespace) || "");
}
