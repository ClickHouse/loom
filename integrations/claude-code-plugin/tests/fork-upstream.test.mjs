// A fork resolves to its upstream's index. The checkout's
// remotes become one ordered candidate list (origin, upstream, the rest; or
// the repository LOOM_CODE_REPO names, alone), every hook resolves through the one lookup in
// lib/repo-scope.mjs, and the primer says how the checkout matched.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  cachePathFor,
  candidateLookupOutcome,
  checkoutRemotes,
  checkoutRepoSlugs,
  lookupIndexedRepo,
  readCache,
  resolveIndexedRepo,
  scopeKey,
} from "../lib/repo-scope.mjs";
import { matchNoteLine, notIndexedLine } from "../lib/session-start.mjs";
import { repoIdentity, resolveRemote, resolveScope } from "../lib/loom-client.mjs";

/** A temp checkout with `remotes` ([name, url] in add order) on `branch`. */
function checkout(remotes, branch = "master") {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-fork-repo-"));
  execSync(`git init -q -b ${branch}`, { cwd: dir });
  for (const [name, url] of remotes) execSync(`git remote add ${name} ${url}`, { cwd: dir });
  return fs.realpathSync(dir);
}

const NO_OVERRIDE = {};

test("candidates: origin, then upstream, then the rest in git remote order", () => {
  const dir = checkout([
    ["alpha", "https://github.com/alpha/tool"],
    ["upstream", "git@github.com:ClickHouse/ClickHouse.git"],
    ["origin", "git@github.com:someone/ClickHouse.git"],
    ["zeta", "https://github.com/zeta/other.git"],
  ]);
  assert.deepEqual(checkoutRepoSlugs(dir, NO_OVERRIDE), [
    "someone/clickhouse",
    "clickhouse/clickhouse",
    "alpha/tool",
    "zeta/other",
  ]);
  assert.deepEqual(
    checkoutRemotes(dir, NO_OVERRIDE).map((r) => r.via),
    ["origin", "upstream", "remote:alpha", "remote:zeta"],
  );
  // the display name keeps the remote's own spelling
  assert.equal(checkoutRemotes(dir, NO_OVERRIDE)[0].name, "someone/ClickHouse");
});

test("candidates: de-duplicated across URL forms", () => {
  const dir = checkout([
    ["origin", "git@github.com:ClickHouse/loom.git"],
    ["mirror", "https://github.com/clickhouse/loom"],
    ["upstream", "https://github.com/ClickHouse/loom.git"],
  ]);
  assert.deepEqual(checkoutRepoSlugs(dir, NO_OVERRIDE), ["clickhouse/loom"]);
});

test("candidates: the checkout's remotes first, LOOM_CODE_REPO after them; none outside a repository", () => {
  const dir = checkout([["origin", "git@github.com:someone/ClickHouse.git"]]);
  assert.deepEqual(checkoutRepoSlugs(dir, { LOOM_CODE_REPO: "ClickHouse/ClickHouse" }), [
    "someone/clickhouse",
    "clickhouse/clickhouse",
  ]);
  assert.deepEqual(checkoutRepoSlugs(dir, { LOOM_CODE_REPO: "https://github.com/o/n.git" }), [
    "someone/clickhouse",
    "o/n",
  ]);
  assert.deepEqual(checkoutRemotes(dir, { LOOM_CODE_REPO: "o/n" })[1].via, "configured");
  // a configured repository the checkout already names is not a second candidate
  assert.deepEqual(checkoutRepoSlugs(dir, { LOOM_CODE_REPO: "someone/ClickHouse" }), ["someone/clickhouse"]);
  // a bare name stays a bare name
  assert.deepEqual(checkoutRepoSlugs(dir, { LOOM_CODE_REPO: "loom" }), ["someone/clickhouse", "loom"]);
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), "not-a-repo-"));
  assert.deepEqual(checkoutRepoSlugs(bare, NO_OVERRIDE), []);
});

/** A code.repos / index_status stand-in: `rows` answer code.repos. */
function server(rows) {
  const calls = [];
  const post = async (op, body) => {
    calls.push([op, body]);
    if (op === "code.repos") return { repos: rows };
    return { path_prefixes: [], git_head: "abc", fast_tier_lag: 0 };
  };
  return { calls, post };
}

const CH = { repo: "ClickHouse/ClickHouse", org: "clickhouse", namespace: "code-clickhouse", branch: "master" };

test("a checkout whose origin is a fork and whose upstream is indexed resolves to upstream", async () => {
  const dir = checkout(
    [
      ["origin", "git@github.com:someone/ClickHouse.git"],
      ["upstream", "https://github.com/ClickHouse/ClickHouse"],
    ],
    "my-feature",
  );
  const { calls, post } = server([{ ...CH, matched_via: "candidate", checkout_slug: "clickhouse/clickhouse" }]);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-fork-cache-"));
  const entry = await resolveIndexedRepo(dir, { env: NO_OVERRIDE, token: "t", post, tmp });
  assert.equal(entry.indexed, true);
  assert.equal(entry.namespace, "code-clickhouse");
  assert.equal(entry.branch, "master", "a fork's feature branch reads the default-branch index");
  assert.equal(entry.matchedVia, "upstream");
  // one code.repos naming every candidate
  assert.deepEqual(calls[0][1].repos, ["someone/clickhouse", "clickhouse/clickhouse"]);
  assert.equal("repo" in calls[0][1], false);
  // the shared cache answers the next hook without a call
  const again = await resolveIndexedRepo(dir, {
    env: NO_OVERRIDE,
    token: "t",
    tmp,
    post: async () => {
      throw new Error("must not call");
    },
  });
  assert.equal(again.namespace, "code-clickhouse");
  assert.ok(fs.existsSync(cachePathFor(dir, tmp)));
});

test("a fork with no upstream remote resolves through the server's fork_of row", async () => {
  const dir = checkout([["origin", "git@github.com:someone/ClickHouse.git"]], "fix-1");
  const { post } = server([{ ...CH, matched_via: "fork_of", checkout_slug: "someone/clickhouse" }]);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-fork-cache-"));
  const { entry, remotes } = await lookupIndexedRepo(dir, { env: NO_OVERRIDE, token: "t", post, tmp });
  assert.equal(entry.namespace, "code-clickhouse");
  assert.equal(entry.matchedVia, "fork_of");
  assert.equal(entry.checkoutSlug, "someone/clickhouse");
  assert.equal(
    matchNoteLine(entry, remotes),
    "# Loom code index: someone/ClickHouse is a fork of ClickHouse/ClickHouse → code-clickhouse",
  );
});

test("origin matches first; unresolved and not-indexed stay distinct", () => {
  const remotes = [
    { slug: "clickhouse/loom", via: "origin", name: "ClickHouse/loom" },
    { slug: "clickhouse/clickhouse", via: "upstream", name: "ClickHouse/ClickHouse" },
  ];
  const loom = { repo: "ClickHouse/loom", org: "clickhouse", namespace: "code-loom", branch: "main" };
  const both = candidateLookupOutcome({ repos: [CH, loom] }, remotes);
  assert.equal(both.hit.namespace, "code-loom");
  assert.equal(both.via, "origin");
  // an unreadable origin row yields to the upstream
  const up = candidateLookupOutcome({ repos: [{ ...loom, readable: false }, CH] }, remotes);
  assert.equal(up.via, "upstream");
  assert.deepEqual(candidateLookupOutcome(null, remotes), { unresolved: true });
  assert.deepEqual(candidateLookupOutcome({ repos: [] }, remotes), { indexed: false });
});

test("primer lines: fork, upstream, origin, not indexed", () => {
  const remotes = [
    { slug: "someone/clickhouse", via: "origin", name: "someone/ClickHouse" },
    { slug: "clickhouse/clickhouse", via: "upstream", name: "ClickHouse/ClickHouse" },
  ];
  const entry = { indexed: true, repo: "ClickHouse/ClickHouse", namespace: "code-clickhouse" };
  assert.equal(
    matchNoteLine({ ...entry, matchedVia: "upstream", checkoutSlug: "clickhouse/clickhouse" }, remotes),
    "# Loom code index: matched the upstream remote ClickHouse/ClickHouse → code-clickhouse",
  );
  assert.equal(matchNoteLine({ ...entry, matchedVia: "origin" }, remotes), "");
  assert.equal(
    matchNoteLine({ ...entry, matchedVia: "configured" }, remotes),
    "# Loom code index: the configured code_repo ClickHouse/ClickHouse → code-clickhouse",
  );
  assert.equal(matchNoteLine({ indexed: false }, remotes), "");
  assert.equal(
    notIndexedLine(remotes),
    "# Loom code index: someone/ClickHouse (nor ClickHouse/ClickHouse) is not an indexed repository.\n\n",
  );
  assert.equal(notIndexedLine([remotes[0]]), "# Loom code index: someone/ClickHouse is not an indexed repository.\n\n");
});

// ---- the memory side: one repo:<id> tag for a fork and its upstream ----

function withoutRemoteOverride(fn) {
  const saved = process.env.LOOM_REPO_REMOTE;
  delete process.env.LOOM_REPO_REMOTE;
  try {
    return fn();
  } finally {
    if (saved !== undefined) process.env.LOOM_REPO_REMOTE = saved;
  }
}

test("repo identity: a fork origin with an upstream remote tags memories as the upstream", () => {
  withoutRemoteOverride(() => {
    const fork = checkout([
      ["origin", "git@github.com:someone/ClickHouse.git"],
      ["upstream", "https://github.com/ClickHouse/ClickHouse.git"],
    ]);
    const upstreamCheckout = checkout([["origin", "git@github.com:ClickHouse/ClickHouse.git"]]);
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-fork-id-"));
    const want = repoIdentity(resolveRemote(upstreamCheckout, { tmp })).id;
    // no cache yet: `upstream` is preferred over `origin`
    assert.equal(repoIdentity(resolveRemote(fork, { tmp })).id, want);
    assert.ok(resolveScope(fork).tags.includes(`repo:${want}`));
  });
});

test("repo identity: the shared cache's fork_of match names the upstream without a remote", () => {
  withoutRemoteOverride(() => {
    const fork = checkout([["origin", "git@github.com:someone/ClickHouse.git"]]);
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-fork-id-"));
    assert.equal(repoIdentity(resolveRemote(fork, { tmp })).slug, "someone/clickhouse");
    fs.writeFileSync(
      cachePathFor(fork, tmp),
      JSON.stringify({
        ts: Date.now(),
        indexed: true,
        org: "clickhouse",
        namespace: "code-clickhouse",
        repo: "ClickHouse/ClickHouse",
        matchedVia: "fork_of",
        checkoutSlug: "someone/clickhouse",
      }),
    );
    const id = repoIdentity(resolveRemote(fork, { tmp }));
    assert.equal(id.id, repoIdentity("git@github.com:ClickHouse/ClickHouse.git").id);
    // an origin match (no matchedVia) keeps the origin identity
    fs.writeFileSync(
      cachePathFor(fork, tmp),
      JSON.stringify({
        ts: Date.now(),
        indexed: true,
        org: "o",
        namespace: "n",
        repo: "someone/ClickHouse",
      }),
    );
    assert.equal(repoIdentity(resolveRemote(fork, { tmp })).slug, "someone/clickhouse");
  });
});

test("outside a git checkout, a named repository stands in for the remotes", async () => {
  assert.deepEqual(checkoutRemotes(null, NO_OVERRIDE), []);
  assert.equal(scopeKey(null, NO_OVERRIDE), null);
  const named = { LOOM_CODE_REPO: "ClickHouse/ClickHouse" };
  assert.deepEqual(checkoutRemotes(null, named), [
    { slug: "clickhouse/clickhouse", via: "configured", name: "ClickHouse/ClickHouse" },
  ]);
  assert.equal(scopeKey(null, named), "code-repo:clickhouse/clickhouse");
  assert.equal(scopeKey("/some/checkout", named), "/some/checkout");
  // the plugin option works the same way
  assert.deepEqual(checkoutRepoSlugs(null, { CLAUDE_PLUGIN_OPTION_CODE_REPO: "o/n" }), ["o/n"]);
  const { calls, post } = server([{ ...CH, matched_via: "candidate", checkout_slug: "clickhouse/clickhouse" }]);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-named-cache-"));
  const entry = await resolveIndexedRepo(null, { env: named, token: "t", post, tmp });
  assert.equal(entry.indexed, true);
  assert.equal(entry.namespace, "code-clickhouse");
  assert.equal(entry.matchedVia, "configured");
  assert.deepEqual(calls[0][1].repos, ["clickhouse/clickhouse"]);
  // the hooks and the MCP headers helper share the entry under the named key
  assert.equal(readCache(scopeKey(null, named), Date.now(), tmp).namespace, "code-clickhouse");
  // no checkout and nothing named: no lookup at all
  const none = server([]);
  assert.equal((await resolveIndexedRepo(null, { env: NO_OVERRIDE, token: "t", post: none.post, tmp })).indexed, false);
  assert.equal(none.calls.length, 0);
});

test("inside an indexed checkout, the checkout's own index wins over LOOM_CODE_REPO", async () => {
  const dir = checkout([["origin", "https://github.com/ClickHouse/loom.git"]]);
  const LOOM = { repo: "ClickHouse/loom", org: "clickhouse", namespace: "code-loom", branch: "main" };
  const { post } = server([LOOM, CH]);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-own-first-"));
  const entry = await resolveIndexedRepo(dir, {
    env: { LOOM_CODE_REPO: "ClickHouse/ClickHouse" },
    token: "t",
    post,
    tmp,
  });
  assert.equal(entry.namespace, "code-loom");
  assert.equal(entry.matchedVia, "origin");
});

test("a checkout with no index falls back to LOOM_CODE_REPO", async () => {
  const dir = checkout([["origin", "https://github.com/someone/unindexed.git"]]);
  const { post } = server([CH]);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-fallback-"));
  const entry = await resolveIndexedRepo(dir, {
    env: { LOOM_CODE_REPO: "ClickHouse/ClickHouse" },
    token: "t",
    post,
    tmp,
  });
  assert.equal(entry.namespace, "code-clickhouse");
  assert.equal(entry.matchedVia, "configured");
});

test("a bare LOOM_CODE_REPO name matches the one readable repository of that name", async () => {
  const LOOM = { repo: "ClickHouse/loom", org: "clickhouse", namespace: "code-loom", branch: "main" };
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-bare-"));
  // outside a checkout: one full listing, matched on the name part
  const one = server([CH, LOOM]);
  const entry = await resolveIndexedRepo(null, { env: { LOOM_CODE_REPO: "loom" }, token: "t", post: one.post, tmp });
  assert.equal(entry.namespace, "code-loom");
  assert.deepEqual(one.calls[0][1].repos, []);
  // inside an unindexed checkout: the named lookup misses, then the listing matches
  const dir = checkout([["origin", "https://github.com/someone/unindexed.git"]]);
  // as the server does: a `repos` filter answers only the rows it names
  const two = { calls: [] };
  two.post = async (op, body) => {
    two.calls.push([op, body]);
    if (op !== "code.repos") return { path_prefixes: [], git_head: "abc" };
    const want = new Set(body.repos || []);
    return { repos: [CH, LOOM].filter((r) => !want.size || want.has(r.repo.toLowerCase())) };
  };
  const inCheckout = await resolveIndexedRepo(dir, {
    env: { LOOM_CODE_REPO: "Loom" },
    token: "t",
    post: two.post,
    tmp,
  });
  assert.equal(inCheckout.namespace, "code-loom");
  assert.deepEqual(
    two.calls.filter(([op]) => op === "code.repos").map(([, b]) => b.repos),
    [["someone/unindexed"], []],
  );
  // two owners with the name: neither is picked
  const fork = { ...LOOM, repo: "someone/loom", namespace: "code-loom-fork" };
  const amb = await resolveIndexedRepo(null, {
    env: { LOOM_CODE_REPO: "loom" },
    token: "t",
    post: server([LOOM, fork]).post,
    tmp: fs.mkdtempSync(path.join(os.tmpdir(), "loom-bare-amb-")),
  });
  assert.equal(amb.indexed, false);
});
