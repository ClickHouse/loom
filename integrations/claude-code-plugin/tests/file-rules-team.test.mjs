// The file-context hook reads the repository's primary code
// namespace (team file rules) beside the person's own, in one request.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { forFileBody, lookupFileContext, teamRulesLine, teamRulesNamespace } from "../lib/file-context.mjs";
import { cachePathFor, primaryNamespaceOf, readCache, resolveIndexedRepo } from "../lib/repo-scope.mjs";

const scope = { org: "clickhouse", namespace: "jdoe-ns", tags: ["project:ClickHouse", "repo:gh-1"] };
const entry = {
  indexed: true,
  org: "clickhouse",
  namespace: "code-clickhouse-26-8",
  primaryNamespace: "code-clickhouse",
};

test("teamRulesNamespace: the cached entry's primary namespace, same org, not the caller's own", () => {
  assert.equal(teamRulesNamespace(entry, scope), "code-clickhouse");
  assert.equal(teamRulesNamespace(null, scope), "");
  assert.equal(teamRulesNamespace({ indexed: false, primaryNamespace: "code-clickhouse" }, scope), "");
  assert.equal(teamRulesNamespace({ ...entry, primaryNamespace: "" }, scope), ""); // no primary namespace
  assert.equal(teamRulesNamespace({ ...entry, org: "other" }, scope), "");
  assert.equal(teamRulesNamespace(entry, { ...scope, namespace: "code-clickhouse" }), "");
});

test("the hook asks for the team namespace when the cached repo entry has one", async () => {
  const calls = [];
  const post = async (op, body, bound) => {
    calls.push({ op, body, bound });
    return {
      total: 2,
      rendered:
        "Loom: 2 memories anchored to src/Interpreters/GraceHashJoin.cpp — read before editing:\n" +
        "- Team rule (by alice, 3 days ago) [constraint]: Don't change the default bucket count without the join benchmark. [id: rules/join/bucket_count]\n" +
        "- my/grace_notes: I traced the spill path last week.\n" +
        '- A team rule wrong or outdated? memory.delete(namespace="code-clickhouse", memory_key=<id>) removes it if you wrote it or are a maintainer; otherwise write a corrected rule for the file and tell its author.',
    };
  };
  const out = await lookupFileContext({
    scope,
    rel: "src/Interpreters/GraceHashJoin.cpp",
    tokenBudget: 300,
    entry,
    post,
    bound: { token: "t" },
  });
  assert.equal(calls.length, 1, "one request");
  assert.equal(calls[0].op, "memory.for_file");
  assert.deepEqual(calls[0].body, {
    org: "clickhouse",
    namespace: "jdoe-ns",
    path: "src/Interpreters/GraceHashJoin.cpp",
    tags: scope.tags,
    token_budget: 300,
    extra_namespaces: ["code-clickhouse"],
  });
  assert.deepEqual(calls[0].bound, { token: "t" });
  assert.match(out, /^- Team rule \(by alice, 3 days ago\) \[constraint\]: .* \[id: rules\/join\/bucket_count\]$/m);
  assert.match(out, /^- my\/grace_notes:/m);
});

test("no team namespace: the body names only the caller's namespace; no answer injects nothing", async () => {
  const bodies = [];
  const post = async (_op, body) => {
    bodies.push(body);
    return null;
  };
  const out = await lookupFileContext({ scope, rel: "a.cpp", tokenBudget: 300, entry: null, post });
  assert.equal(out, "");
  assert.equal("extra_namespaces" in bodies[0], false);
  assert.deepEqual(forFileBody(scope, "a.cpp", 300, null), {
    org: "clickhouse",
    namespace: "jdoe-ns",
    path: "a.cpp",
    tags: scope.tags,
    token_budget: 300,
  });
  const empty = await lookupFileContext({
    scope,
    rel: "a.cpp",
    tokenBudget: 300,
    entry,
    post: async () => ({ total: 0, rendered: "" }),
  });
  assert.equal(empty, "");
});

test("resolveIndexedRepo caches the repository's primary namespace", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-13840-"));
  const root = path.join(tmp, "checkout");
  fs.mkdirSync(root);
  const post = async (op) => {
    if (op === "code.repos") {
      return {
        repos: [
          {
            org: "clickhouse",
            namespace: "code-clickhouse",
            repo: "ClickHouse/ClickHouse",
            branch: "master",
            primary_namespace: "code-clickhouse",
          },
        ],
      };
    }
    return { path_prefixes: [], git_head: "abc" };
  };
  const out = await resolveIndexedRepo(root, {
    env: { LOOM_CODE_REPO: "ClickHouse/ClickHouse" },
    tmp,
    post,
    token: "t",
  });
  assert.equal(out.primaryNamespace, "code-clickhouse");
  assert.equal(readCache(root, Date.now(), tmp).primaryNamespace, "code-clickhouse");
  assert.equal(primaryNamespaceOf({}), "");
});

test("teamRulesLine: one primer line naming the namespace, empty when none", () => {
  const line = teamRulesLine({ primary_namespace: "code-example-private" });
  assert.match(line, /"code-example-private"/);
  assert.equal(line.includes("\n"), false);
  assert.equal(teamRulesLine({}), "");
  assert.equal(teamRulesLine(null), "");
});

test("the hook passes the file's path and the team namespace through to the guidance after the block", async () => {
  const hook = fileURLToPath(new URL("../hooks/loom-file-context.mjs", import.meta.url));
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-fc-hook-")));
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-fc-tmp-")));
  fs.mkdirSync(path.join(dir, "src"));
  fs.writeFileSync(path.join(dir, "src/a.cpp"), "int x;\n");
  const g = (...a) => execFileSync("git", ["-c", "user.email=t@t", "-c", "user.name=t", ...a], { cwd: dir });
  g("init", "-q");
  g("add", ".");
  g("commit", "-qm", "c");
  fs.writeFileSync(
    cachePathFor(dir, tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x-1", primaryNamespace: "code-x" }),
  );
  const seen = [];
  const srv = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      seen.push({ url: req.url, body: raw ? JSON.parse(raw) : {} });
      res.setHeader("content-type", "application/json");
      res.end(
        JSON.stringify({
          total: 2,
          rendered:
            "Loom: 2 memories anchored to src/a.cpp — read before editing:\n" +
            "- Team rule (by alice, 2 days ago): keep the lock order [id: rules/a/lock]\n" +
            '- … 1 more; memory.for_file(path="src/a.cpp") lists them.\n' +
            '- A team rule wrong or outdated? memory.delete(namespace="code-x", memory_key=<id>) removes it if you wrote it or are a maintainer; otherwise write a corrected rule for the file and tell its author.',
        }),
      );
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  try {
    const out = await new Promise((resolve) => {
      const child = spawn(process.execPath, [hook], {
        cwd: dir,
        env: {
          PATH: process.env.PATH,
          HOME: tmp,
          TMPDIR: tmp,
          LOOM_BASE_URL: `http://127.0.0.1:${srv.address().port}`,
          LOOM_API_KEY: "t",
          LOOM_ORG: "o",
          LOOM_NAMESPACE: "me",
        },
      });
      let stdout = "";
      child.stdout.on("data", (c) => (stdout += c));
      child.on("close", () => resolve(stdout));
      child.stdin.end(
        JSON.stringify({
          tool_name: "Edit",
          tool_input: { file_path: path.join(dir, "src/a.cpp") },
          cwd: dir,
          session_id: "s",
        }),
      );
    });
    const asked = seen.find((s) => s.url === "/v1/memory.for_file");
    assert.deepEqual(asked.body.extra_namespaces, ["code-x"]);
    const ctx = JSON.parse(out).hookSpecificOutput.additionalContext;
    const after = ctx.slice(ctx.lastIndexOf("</loom-recalled-data"));
    assert.match(after, /memory\.delete\(namespace="code-x", memory_key=<its id>\)/);
    assert.match(after, /memory\.for_file\(path="src\/a\.cpp"\) with a larger token_budget/);
  } finally {
    srv.close();
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
