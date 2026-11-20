#!/usr/bin/env node
// The real client must preserve endpoint, token and checkout scope together.
// A loopback fake answers every model request; no provider is called.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { baseEnv, run, scratch, startFake } from "./agent-session.mjs";

const cli = process.argv[2];
if (!cli) throw new Error("usage: claude-endpoint.mjs <claude binary>");
const root = fileURLToPath(new URL("../../../..", import.meta.url));
const box = scratch();
const prefix = "/proxy";
const repos = ["one", "two"].map((name) => ({
  repo: `example/${name}`,
  org: "e2e",
  namespace: `code-${name}`,
  branch: "main",
}));
const fake = await startFake(0, { indexedRepos: repos, prefix });
const env = {
  ...baseEnv(box.home),
  ANTHROPIC_API_KEY: "dummy-endpoint-model-key",
  ANTHROPIC_BASE_URL: fake.url,
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
  DISABLE_AUTOUPDATER: "1",
};
const invoke = async (args, cwd = box.work, useEnv = env) => {
  const result = await run(cli, args, { env: useEnv, cwd, timeoutMs: 45000 });
  assert.equal(result.status, 0, `${args.join(" ")}: ${result.stderr}${result.stdout}`);
  return result;
};
const turn = (cwd, useEnv = env) =>
  invoke(
    ["-p", "Reply briefly.", "--tools", "", "--output-format", "stream-json", "--verbose", "--include-hook-events"],
    cwd,
    useEnv,
  );
try {
  await invoke(["plugin", "marketplace", "add", root]);
  const endpoint = fake.url + prefix + "/mcp?toolset=coding+tracker";
  await invoke(["plugin", "install", "loom@loom", "--config", `server_url=${endpoint}`]);
  const works = [
    path.join(box.dir, process.platform === "win32" ? "one $literal" : 'one "quotes" $literal'),
    path.join(box.dir, "two 'quotes' $literal"),
  ];
  for (const [i, cwd] of works.entries()) {
    fs.mkdirSync(cwd);
    execFileSync("git", ["init", "-q"], { cwd });
    execFileSync("git", ["remote", "add", "origin", `https://github.com/example/${i ? "two" : "one"}.git`], { cwd });
  }
  const start = fake.seen.length;
  const tokens = ["endpoint-one", "endpoint-two"];
  const credentials = tokens.map((token, i) => {
    const file = path.join(box.home, `credential-${i}.json`);
    fs.writeFileSync(file, JSON.stringify({ token, url: fake.url + prefix }), { mode: 0o600 });
    return file;
  });
  const results = await Promise.all(works.map((cwd, i) => turn(cwd, { ...env, LOOM_CONFIG_FILE: credentials[i] })));
  assert.ok(results.every((result) => result.stdout.includes('"subtype":"init"')));
  const seen = fake.seen.slice(start);
  const mcp = seen.filter((call) => call.path === prefix + "/mcp");
  for (const [i, repo] of repos.entries())
    assert.ok(
      mcp.some((call) => call.scope === `${repo.org}/${repo.namespace}` && call.auth === `Bearer ${tokens[i]}`),
      `no scoped bearer for ${repo.repo}: ${JSON.stringify(mcp)}`,
    );
  const headerClass = (value, expected) => {
    const index = expected.indexOf(value);
    return index >= 0 ? `expected-${index + 1}` : value ? "other" : "missing";
  };
  const pairCounts = {};
  for (const call of mcp) {
    // Fixed category labels keep captured headers out of the failure message.
    const auth = headerClass(
      call.auth,
      tokens.map((token) => `Bearer ${token}`),
    );
    const scope = headerClass(
      call.scope,
      repos.map((repo) => `${repo.org}/${repo.namespace}`),
    );
    const pair = `${auth}/${scope}`;
    pairCounts[pair] = (pairCounts[pair] ?? 0) + 1;
  }
  assert.ok(
    mcp.every((call) =>
      repos.some((repo, i) => call.scope === `${repo.org}/${repo.namespace}` && call.auth === `Bearer ${tokens[i]}`),
    ),
    `checkout scope crossed client credentials (auth/scope counts): ${JSON.stringify(pairCounts)}`,
  );
  assert.ok(
    mcp.every((call) => call.url === prefix + "/mcp?toolset=coding+tracker"),
    "MCP endpoint changed",
  );
  const hookCalls = seen.filter((call) => !call.path.endsWith("/mcp") && !call.path.includes("/messages"));
  assert.ok(hookCalls.length);
  assert.ok(
    hookCalls.every((call) => call.path.startsWith(prefix + "/v1/") || call.path.startsWith(prefix + "/oauth/")),
    "hook escaped proxy base",
  );

  // A fresh install takes the endpoint default from the environment's base.
  for (const [defaultPrefix, seeded] of [
    [prefix, true],
    [prefix + "/", true],
    [prefix + "/", false],
  ]) {
    const defaults = scratch();
    let defaultFake;
    try {
      if (!seeded) fs.unlinkSync(path.join(defaults.home, ".loom", "token"));
      defaultFake = await startFake(0, { prefix: defaultPrefix, signup: !seeded });
      const defaultEnv = {
        ...baseEnv(defaults.home),
        ANTHROPIC_API_KEY: "dummy-endpoint-model-key",
        ANTHROPIC_BASE_URL: defaultFake.url,
        LOOM_BASE_URL: defaultFake.url + defaultPrefix,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
        DISABLE_AUTOUPDATER: "1",
      };
      for (const args of [
        ["plugin", "marketplace", "add", root],
        ["plugin", "install", "loom@loom"],
      ])
        await invoke(args, defaults.work, defaultEnv);
      const before = defaultFake.seen.length;
      const defaultResult = await invoke(
        ["-p", "Reply briefly.", "--tools", "", "--output-format", "stream-json", "--verbose"],
        defaults.work,
        defaultEnv,
      );
      assert.ok(!defaultResult.stdout.includes("configure server_url as the full MCP URL"));
      const calls = defaultFake.seen.slice(before);
      assert.ok(
        calls.some((call) => call.path === defaultPrefix + "/v1/context.pack" && call.auth === "Bearer loom-e2e-token"),
        "default hooks missed the proxy base or bearer",
      );
      assert.ok(
        calls.some(
          (call) => call.url === defaultPrefix + "/mcp?toolset=coding" && call.auth === "Bearer loom-e2e-token",
        ),
        "environment default missed its MCP endpoint",
      );
      if (!seeded) {
        const signups = calls.filter((call) => call.url === defaultPrefix + "/v1/auth/agent-signup");
        assert.equal(signups.length, 1, "first session must sign up once at the configured proxy path");
        assert.equal(signups[0].auth, "");
        const config = JSON.parse(fs.readFileSync(path.join(defaults.home, ".loom", "config.json"), "utf8"));
        assert.equal(config.url, defaultFake.url + defaultPrefix);
        assert.equal(config.token, "loom-e2e-token");
        assert.equal(config.signed_up_by, "claude-code");
        assert.ok(
          calls
            .slice(calls.indexOf(signups[0]) + 1)
            .some((call) => call.path === defaultPrefix + "/v1/context.pack" && call.auth === "Bearer loom-e2e-token"),
          "signed-up token did not reach the primer",
        );
      }
    } finally {
      if (defaultFake) await defaultFake.close();
      fs.rmSync(defaults.dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  }

  // The old dashboard's exact origin-only option is a visible migration error.
  for (const value of [fake.url, fake.url + "/mcp?toolset=coding&toolset=readonly"]) {
    await invoke(["plugin", "install", "loom@loom", "--config", `server_url=${value}`]);
    const before = fake.seen.length;
    const result = await turn(works[0]);
    assert.ok(result.stdout.includes("configure server_url as the full MCP URL"), "migration notice missing");
    const calls = fake.seen.slice(before).filter((call) => !call.path.includes("/messages"));
    assert.ok(
      calls.every((call) => call.path === "/" || call.path === "/mcp" || call.path.startsWith("/.well-known/oauth-")),
      `invalid config made a hook request: ${JSON.stringify(calls)}`,
    );
    assert.ok(
      calls.every((call) => !call.auth),
      "invalid endpoint received the Loom token",
    );
  }
  console.log(
    `PASS Claude endpoint on ${process.platform}-${process.arch}: concurrent scopes, proxy path, union and invalid-option migration`,
  );
} finally {
  await fake.close();
  fs.rmSync(box.dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
