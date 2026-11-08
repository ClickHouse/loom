// The plugin's MCP block authenticates from the same places
// its hooks do. The helper is run the way Claude Code runs it — as a process,
// stdout is one JSON object — under each token source and under none.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
process.env.LOOM_HOOK_NO_MAIN = "1";
import { checkoutScope, headersFor, hookToken } from "../bin/loom-mcp-headers.mjs";
import { cachePathFor, scopeHeader } from "../lib/repo-scope.mjs";
import { execSync } from "node:child_process";
import { hostEnv, withTmpdir } from "./host-env.mjs";

const HELPER = fileURLToPath(new URL("../bin/loom-mcp-headers.mjs", import.meta.url));

// Hermetic: every test gets its own TMPDIR (which holds the repository
// cache), HOME and working directory, never the machine's or another
// test's, where a live session may have cached a code scope for the
// checkout the tests run from. The helper runs outside any checkout unless
// a test hands it one.
function sandbox() {
  const mk = (p) => fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), p)));
  return { tmp: mk("loom-headers-tmp-"), home: mk("loom-headers-home-"), cwd: mk("loom-headers-cwd-") };
}

function run(env, box = sandbox(), args = []) {
  const out = execFileSync(process.execPath, [HELPER, ...args], {
    encoding: "utf8",
    env: hostEnv({ PATH: process.env.PATH, HOME: box.home, TMPDIR: box.tmp, LOOM_MCP_HOOK_WAIT_MS: "0", ...env }),
    cwd: box.cwd,
    timeout: 5000,
  });
  return { ...box, out, parsed: JSON.parse(out) };
}

test("headersFor: a bearer for a token, an empty object for none", () => {
  assert.deepEqual(headersFor("abc"), { Authorization: "Bearer abc" });
  assert.deepEqual(headersFor("  abc "), { Authorization: "Bearer abc" });
  assert.deepEqual(headersFor(""), {});
  assert.deepEqual(headersFor("   "), {});
  assert.deepEqual(headersFor(undefined), {});
});

test("LOOM_API_KEY wins", () => {
  const { parsed } = run({ LOOM_API_KEY: "env.tok" });
  assert.deepEqual(parsed, { Authorization: "Bearer env.tok" });
});

test("~/.loom/config.json token when the env var is unset", () => {
  const box = sandbox();
  fs.mkdirSync(path.join(box.home, ".loom"));
  fs.writeFileSync(path.join(box.home, ".loom", "config.json"), JSON.stringify({ token: "cfg.tok", url: "https://x" }));
  assert.deepEqual(run({}, box).parsed, { Authorization: "Bearer cfg.tok" });
});

test("~/.loom/token file when neither env nor config names one", () => {
  const box = sandbox();
  fs.mkdirSync(path.join(box.home, ".loom"));
  fs.writeFileSync(path.join(box.home, ".loom", "token"), "file.tok\n");
  assert.deepEqual(run({}, box).parsed, { Authorization: "Bearer file.tok" });
});

test("no token anywhere and no server to sign up with → {} (the server's 401 challenge is the symptom, not an empty bearer)", () => {
  const { parsed, out } = run({ LOOM_BASE_URL: "http://127.0.0.1:9" });
  assert.deepEqual(parsed, {});
  assert.equal(out.trim().split("\n").length, 1, "exactly one JSON line on stdout");
});

test("the plugin's .mcp.json uses the helper and a localhost default URL", () => {
  const block = JSON.parse(fs.readFileSync(fileURLToPath(new URL("../.mcp.json", import.meta.url)), "utf8"));
  const loom = block.mcpServers.loom;
  assert.equal(loom.type, "http");
  assert.equal(loom.url, "${user_config.server_url}/mcp?toolset=${user_config.toolset}"); // the toolset is a setting
  assert.match(loom.headersHelper, /process\.env\.CLAUDE_PLUGIN_ROOT,'bin','loom-mcp-headers\.mjs'/);
  assert.match(loom.headersHelper, /\$CLAUDE_PLUGIN_ROOT\/bin\/loom-mcp-headers\.mjs/);
  assert.equal(loom.headers, undefined, "no static header: the helper supplies it");
});

// ---- the connection names the checkout's code namespace ----

function gitRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-scope-repo-"));
  execSync("git init -q && git remote add origin git@github.com:ClickHouse/example-private.git", { cwd: dir });
  return fs.realpathSync(dir);
}

test("headersFor: the scope header rides beside the bearer, only for an indexed entry", () => {
  const entry = { indexed: true, org: "clickhouse", namespace: "code-example-private" };
  assert.deepEqual(headersFor("abc", entry), {
    Authorization: "Bearer abc",
    "X-Loom-Code-Scope": "clickhouse/code-example-private",
  });
  assert.deepEqual(headersFor("abc", { indexed: false }), { Authorization: "Bearer abc" });
  assert.deepEqual(headersFor("abc", { indexed: false, unresolved: true }), { Authorization: "Bearer abc" });
  assert.deepEqual(headersFor("abc", null), { Authorization: "Bearer abc" });
  assert.deepEqual(scopeHeader({ indexed: true, org: "o" }), {}, "no namespace, no header");
});

test("the helper reads the shunt's cache for the checkout it runs in", () => {
  const repo = gitRepo();
  const box = { ...sandbox(), cwd: repo };
  fs.writeFileSync(
    cachePathFor(repo, box.tmp),
    JSON.stringify({
      ts: Date.now(),
      indexed: true,
      org: "clickhouse",
      namespace: "code-example-private-26-5",
    }),
  );
  const { parsed } = run({ LOOM_API_KEY: "env.tok" }, box);
  assert.deepEqual(parsed, {
    Authorization: "Bearer env.tok",
    "X-Loom-Code-Scope": "clickhouse/code-example-private-26-5",
  });
  // a stale entry is not trusted; the helper's own lookup has no server here → bearer only
  fs.writeFileSync(
    cachePathFor(repo, box.tmp),
    JSON.stringify({
      ts: Date.now() - 11 * 60 * 1000,
      indexed: true,
      org: "clickhouse",
      namespace: "code-x",
    }),
  );
  assert.deepEqual(run({ LOOM_API_KEY: "env.tok", LOOM_BASE_URL: "http://127.0.0.1:9" }, box).parsed, {
    Authorization: "Bearer env.tok",
  });
  // off switch, and outside a repository: bearer only
  fs.writeFileSync(
    cachePathFor(repo, box.tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "n" }),
  );
  assert.deepEqual(run({ LOOM_API_KEY: "env.tok", LOOM_MCP_CODE_SCOPE: "off" }, box).parsed, {
    Authorization: "Bearer env.tok",
  });
  assert.deepEqual(run({ LOOM_API_KEY: "env.tok" }, { ...box, cwd: sandbox().cwd }).parsed, {
    Authorization: "Bearer env.tok",
  });
});

test("the helper names the project directory it is given, not the checkout it runs in", () => {
  // Claude Code starts the helper in the plugin root, which may sit inside
  // another checkout, and passes the session's project directory.
  const project = gitRepo();
  const pluginCheckout = gitRepo();
  const box = { ...sandbox(), cwd: pluginCheckout };
  const entry = (namespace) => JSON.stringify({ ts: Date.now(), indexed: true, org: "clickhouse", namespace });
  fs.writeFileSync(cachePathFor(project, box.tmp), entry("code-project"));
  fs.writeFileSync(cachePathFor(pluginCheckout, box.tmp), entry("code-plugin"));
  assert.deepEqual(run({ LOOM_API_KEY: "env.tok" }, box, [project]).parsed, {
    Authorization: "Bearer env.tok",
    "X-Loom-Code-Scope": "clickhouse/code-project",
  });
  // an empty or unsubstituted project directory names no checkout, never the cwd's
  for (const arg of ["", "${CLAUDE_PROJECT_DIR}"])
    assert.deepEqual(run({ LOOM_API_KEY: "env.tok" }, box, [arg]).parsed, { Authorization: "Bearer env.tok" }, arg);
  // no argument (the Codex entry): the checkout the helper runs in
  assert.deepEqual(run({ LOOM_API_KEY: "env.tok" }, box).parsed["X-Loom-Code-Scope"], "clickhouse/code-plugin");
});

test("checkoutScope: cache miss → one bounded lookup that also fills the cache", async () => {
  const { tmp } = sandbox();
  const repo = gitRepo();
  const calls = [];
  const post = async (op, body) => {
    calls.push([op, body]);
    if (op === "code.repos")
      return {
        repos: [
          {
            repo: "ClickHouse/example-private",
            org: "clickhouse",
            namespace: "code-example-private",
            branch: "master",
          },
        ],
      };
    return { path_prefixes: ["src/"], git_head: "abc", fast_tier_lag: 0 };
  };
  const entry = await withTmpdir(tmp, () => checkoutScope(repo, { token: "t", post }));
  assert.equal(entry.indexed, true);
  assert.equal(entry.namespace, "code-example-private");
  assert.equal(calls[0][0], "code.repos");
  assert.deepEqual(calls[0][1].repos, ["clickhouse/example-private"]);
  assert.equal(calls[0][1].consumer, "claude-code-mcp-headers");
  assert.equal(JSON.parse(fs.readFileSync(cachePathFor(repo, tmp), "utf8")).namespace, "code-example-private");
  // a lookup that does not answer: unresolved, no header, retried later
  const silent = gitRepo();
  const none = await withTmpdir(tmp, () => checkoutScope(silent, { token: "t", post: async () => null }));
  assert.deepEqual({ indexed: none.indexed, unresolved: none.unresolved }, { indexed: false, unresolved: true });
  assert.deepEqual(scopeHeader(none), {});
  // no token: nothing is asked
  assert.equal(
    await withTmpdir(tmp, () =>
      checkoutScope(silent, {
        token: "",
        post: async () => {
          throw new Error("must not call");
        },
      }),
    ),
    null,
  );
});

test("checkoutScope outside a checkout: LOOM_CODE_REPO names the repository and the scope header rides", async () => {
  const { tmp } = sandbox();
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "loom-no-checkout-"));
  const calls = [];
  const post = async (op, body) => {
    calls.push([op, body]);
    if (op === "code.repos")
      return { repos: [{ repo: "ClickHouse/loom", org: "clickhouse", namespace: "code-loom", branch: "main" }] };
    return { path_prefixes: [], git_head: "abc", fast_tier_lag: 0 };
  };
  const env = { LOOM_CODE_REPO: "ClickHouse/loom" };
  const entry = await withTmpdir(tmp, () => checkoutScope(outside, { env, token: "t", post }));
  assert.equal(entry.namespace, "code-loom");
  assert.deepEqual(calls[0][1].repos, ["clickhouse/loom"]);
  assert.deepEqual(scopeHeader(entry), { "X-Loom-Code-Scope": "clickhouse/code-loom" });
  // nothing named and no checkout: no header, no lookup
  const none = await withTmpdir(tmp, () =>
    checkoutScope(outside, {
      env: {},
      token: "t",
      post: async () => {
        throw new Error("must not call");
      },
    }),
  );
  assert.equal(none, null);
});

// Claude Code connects the MCP entry while the SessionStart
// hook is still signing up; the helper signs up (or waits for the hook) and
// sends the bearer instead of `{}` → 401 → OAuth discovery → 404.
test("no token, sign-up reachable → the helper signs up and sends the bearer", async () => {
  const http = await import("node:http");
  const srv = http.createServer((req, res) => {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ token: "fresh.tok", namespace: "agent-x", org: "dev", claim_code: "C" }));
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${srv.address().port}`;
  try {
    const box = sandbox();
    const { execFile } = await import("node:child_process");
    const out = await new Promise((resolve, reject) =>
      execFile(
        process.execPath,
        [HELPER],
        {
          encoding: "utf8",
          timeout: 10000,
          cwd: box.cwd,
          env: hostEnv({
            PATH: process.env.PATH,
            HOME: box.home,
            TMPDIR: box.tmp,
            LOOM_BASE_URL: url,
            LOOM_MCP_CODE_SCOPE: "off",
          }),
        },
        (e, so) => (e ? reject(e) : resolve(so)),
      ),
    );
    assert.deepEqual(JSON.parse(out), { Authorization: "Bearer fresh.tok" });
    const cfg = JSON.parse(fs.readFileSync(path.join(box.home, ".loom", "config.json"), "utf8"));
    assert.equal(cfg.token, "fresh.tok");
  } finally {
    srv.close();
  }
});

test("LOOM_AUTO_SIGNUP=off keeps the helper from signing up", () => {
  const { parsed } = run({ LOOM_AUTO_SIGNUP: "off" });
  assert.deepEqual(parsed, {});
});

test("no token yet: the helper waits for the SessionStart hook's credential and adopts its server", async () => {
  const env = {};
  let reads = 0;
  const read = () => (++reads < 3 ? {} : { token: "tok.en", url: "https://loom.team.example" });
  assert.equal(await hookToken({ env, read, pollMs: 1, waitMs: 1000 }), "tok.en");
  assert.equal(env.LOOM_BASE_URL, "https://loom.team.example");
  const pinned = { LOOM_BASE_URL: "http://localhost:7777" };
  await hookToken({ env: pinned, read: () => ({ token: "t", url: "https://other" }), pollMs: 1 });
  assert.equal(pinned.LOOM_BASE_URL, "http://localhost:7777");
  assert.equal(await hookToken({ env: {}, read: () => ({}), pollMs: 1, waitMs: 5 }), "");
});

test("an agent never takes the token the SessionStart hook or a person saved", async () => {
  // The branch an MCP entry with no LOOM_BASE_URL takes (Codex's): the
  // helper would otherwise wait for config.json and adopt its token.
  const agent = { LOOM_CLIENT_ID: "ci", LOOM_WORKLOAD_TOKEN: "w" };
  assert.equal(await hookToken({ env: agent, read: () => ({ token: "personal.tok" }), waitMs: 0 }), "");
});
