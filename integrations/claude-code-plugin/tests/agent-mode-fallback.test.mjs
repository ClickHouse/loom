// An agent whose workload-token exchange fails signs in as no one: the
// SessionStart hook and the MCP headers helper never fall back to the token a
// person saved on this machine, nor sign a new identity up. Both run as the
// host runs them, against a server whose token endpoint is away.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const entry = (rel) => fileURLToPath(new URL(`../${rel}`, import.meta.url));
const PERSONAL = "personal.tok";

async function awayTokenEndpoint() {
  const seen = [];
  const srv = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      seen.push({ url: req.url, auth: req.headers.authorization || "" });
      res.writeHead(req.url.startsWith("/oauth/token") ? 503 : 200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return { srv, seen, url: `http://127.0.0.1:${srv.address().port}` };
}

function run(rel, url, stdin = "") {
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-agent-fallback-")));
  fs.mkdirSync(path.join(home, ".loom"), { recursive: true });
  fs.writeFileSync(path.join(home, ".loom", "config.json"), JSON.stringify({ token: PERSONAL, url }), {
    mode: 0o600,
  });
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [entry(rel)], {
      cwd: home,
      env: {
        PATH: process.env.PATH,
        HOME: home,
        TMPDIR: home,
        LOOM_BASE_URL: url,
        LOOM_CLIENT_ID: "ci",
        LOOM_WORKLOAD_TOKEN: "workload-proof",
        LOOM_MCP_HOOK_WAIT_MS: "0",
        LOOM_MCP_CODE_SCOPE: "off",
      },
    });
    let stdout = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.on("close", () => resolve({ stdout, home }));
    child.stdin.end(stdin);
  });
}

for (const [name, rel, stdin] of [
  ["the SessionStart hook", "hooks/loom-session-start.mjs", "{}"],
  ["the MCP headers helper", "bin/loom-mcp-headers.mjs", ""],
]) {
  test(`${name} sends neither the person's saved token nor a sign-up when the exchange fails`, async () => {
    const { srv, seen, url } = await awayTokenEndpoint();
    let home = "";
    try {
      let stdout;
      ({ stdout, home } = await run(rel, url, stdin));
      assert.ok(
        seen.some((r) => r.url.startsWith("/oauth/token")),
        "the exchange was tried",
      );
      assert.deepEqual(
        seen.filter((r) => r.auth.includes(PERSONAL) || r.url.startsWith("/v1/auth/agent-signup")),
        [],
      );
      assert.ok(!stdout.includes(PERSONAL));
      if (rel.startsWith("bin/")) assert.deepEqual(JSON.parse(stdout), {});
      else assert.match(stdout, /could not be exchanged/);
    } finally {
      srv.close();
      if (home) fs.rmSync(home, { recursive: true, force: true });
    }
  });
}

test("an agent's scope is never the org and namespace a person saved", async () => {
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-agent-scope-")));
  try {
    fs.mkdirSync(path.join(home, ".loom"));
    const saved = { token: PERSONAL, org: "personal-org", namespace: "personal-ns", agent: "me" };
    fs.writeFileSync(path.join(home, ".loom", "config.json"), JSON.stringify(saved));
    const lib = new URL("../lib/loom-client.mjs", import.meta.url).href;
    const script = `import { resolveScope } from ${JSON.stringify(lib)};
      process.stdout.write(JSON.stringify(resolveScope(process.cwd())));`;
    const out = await new Promise((resolve) => {
      const child = spawn(process.execPath, ["--input-type=module", "-e", script], {
        cwd: home,
        env: {
          PATH: process.env.PATH,
          HOME: home,
          TMPDIR: home,
          LOOM_HOOK_NO_MAIN: "1",
          LOOM_BASE_URL: "http://127.0.0.1:9", // nothing listens: the exchange fails
          LOOM_CLIENT_ID: "ci",
          LOOM_WORKLOAD_TOKEN: "workload-proof",
        },
      });
      let stdout = "";
      child.stdout.on("data", (c) => (stdout += c));
      child.on("close", () => resolve(stdout));
    });
    const scope = JSON.parse(out);
    assert.notEqual(scope.org, "personal-org");
    assert.notEqual(scope.namespace, "personal-ns");
    assert.notEqual(scope.agent, "me");
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("the status check names no source for an exchange that supplied nothing", async () => {
  const { srv, url } = await awayTokenEndpoint();
  try {
    const { stdout } = await run("bin/loom-client-status.mjs", url);
    const result = JSON.parse(stdout);
    assert.equal(result.checks.find((c) => c.name === "token").grade, "FAIL");
    assert.equal(
      result.checks.some((c) => c.name === "source"),
      false,
    );
    assert.equal(stdout.includes(PERSONAL), false);
  } finally {
    srv.close();
  }
});
