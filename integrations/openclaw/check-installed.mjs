import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as pause } from "node:timers/promises";

// Hosted-only install proof: a real host and SDK, with a synthetic HTTP backend.
const [hostArg, archiveArg] = process.argv.slice(2);
if (!hostArg || !archiveArg) throw new Error("Usage: node check-installed.mjs <openclaw.mjs> <sdk.tgz>");
const host = resolve(hostArg);
const archive = resolve(archiveArg);
const directory = await mkdtemp(join(tmpdir(), "loom-openclaw-"));
const state = join(directory, "state");
const configPath = join(directory, "openclaw.json");
await mkdir(state);
const environment = {
  PATH: process.env.PATH,
  HOME: directory,
  OPENCLAW_STATE_DIR: state,
  OPENCLAW_CONFIG_PATH: configPath,
  OPENCLAW_GATEWAY_TOKEN: "synthetic-gateway-token",
  LOOM_OPENCLAW_TOKEN: "synthetic-loom-token",
};
const requests = [];
let saved;
const backend = createServer(async (request, response) => {
  try {
    assert.equal(request.headers.authorization, "Bearer synthetic-loom-token");
    let body = "";
    for await (const chunk of request) {
      body += chunk;
      assert.ok(body.length < 16384);
    }
    const data = JSON.parse(body);
    assert.equal(data.org, "fixture-org");
    assert.equal(data.namespace, "fixture-notes");
    assert.equal(data.agent, "main");
    assert.equal(data.user_id, "fixture-user");
    assert.equal(data.session_id, "agent:main:main");
    requests.push({ path: request.url, data });
    if (request.url === "/v1/memory.set") {
      assert.equal(data.source_class, "derived");
      saved = data;
      response.writeHead(200, { "content-type": "application/json" });
      response.end(
        JSON.stringify({
          status: "active",
          memory_key: data.memory_key,
          memory_id: "synthetic-id",
          noop: false,
          dry_run: false,
        }),
      );
    } else {
      assert.equal(request.url, "/v1/memory.search");
      assert.equal(data.read_scope, "own_session");
      assert.equal(data.namespace_scope, "this");
      assert.equal(data.scope, "private");
      assert.ok(saved);
      response.writeHead(200, { "content-type": "application/json" });
      response.end(
        JSON.stringify({ abstain: false, results: [{ memory_key: saved.memory_key, content_excerpt: saved.value }] }),
      );
    }
  } catch {
    response.writeHead(500);
    response.end("Synthetic fixture contract failed");
  }
});
backend.listen(0, "127.0.0.1");
await once(backend, "listening");
const backendPort = backend.address().port;
const reservation = createServer();
reservation.listen(0, "127.0.0.1");
await once(reservation, "listening");
const gatewayPort = reservation.address().port;
await new Promise((resolveClose) => reservation.close(resolveClose));
let gateway;
let gatewayLog = "";

async function command(args) {
  const child = spawn(process.execPath, [host, ...args], {
    env: environment,
    cwd: directory,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  const collect = (chunk) => {
    output = (output + chunk).slice(-65536);
  };
  child.stdout.on("data", collect);
  child.stderr.on("data", collect);
  const timeout = setTimeout(() => child.kill("SIGTERM"), 120000);
  try {
    const [code] = await once(child, "exit");
    assert.equal(code, 0, `openclaw ${args[0]} failed: ${output}`);
    return output;
  } finally {
    clearTimeout(timeout);
  }
}

const config = {
  gateway: {
    mode: "local",
    bind: "loopback",
    port: gatewayPort,
    auth: { mode: "token", token: "synthetic-gateway-token" },
  },
};
try {
  await writeFile(configPath, JSON.stringify(config));
  await command(["plugins", "install", `npm-pack:${archive}`, "--force", "--accept-capabilities"]);
  const installedConfig = JSON.parse(await readFile(configPath, "utf8"));
  installedConfig.plugins ??= {};
  installedConfig.plugins.allow = ["clickhouse-loom"];
  installedConfig.plugins.entries ??= {};
  installedConfig.plugins.entries["clickhouse-loom"] = {
    enabled: true,
    config: {
      baseUrl: `http://127.0.0.1:${backendPort}`,
      tokenEnv: "LOOM_OPENCLAW_TOKEN",
      org: "fixture-org",
      namespace: "fixture-notes",
      agentId: "main",
      userId: "fixture-user",
      sessionKey: "agent:main:main",
    },
  };
  installedConfig.tools = { allow: ["clickhouse_loom_save", "clickhouse_loom_search"] };
  installedConfig.agents = {
    ownership: "explicit",
    defaults: { systemAgent: { agentId: "main" } },
    entries: { main: {}, other: {} },
  };
  await writeFile(configPath, JSON.stringify(installedConfig));
  const inspected = await command(["plugins", "inspect", "clickhouse-loom", "--runtime", "--json"]);
  assert.match(inspected, /clickhouse_loom_save/);
  assert.match(inspected, /clickhouse_loom_search/);
  gateway = spawn(process.execPath, [host, "gateway", "run", "--port", String(gatewayPort)], {
    env: environment,
    cwd: directory,
    stdio: ["ignore", "pipe", "pipe"],
  });
  gateway.stdout.on("data", (chunk) => {
    gatewayLog = (gatewayLog + chunk).slice(-65536);
  });
  gateway.stderr.on("data", (chunk) => {
    gatewayLog = (gatewayLog + chunk).slice(-65536);
  });
  const endpoint = `http://127.0.0.1:${gatewayPort}`;
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (gateway.exitCode !== null) throw new Error(`Gateway exited: ${gatewayLog}`);
    try {
      const health = await fetch(`${endpoint}/health`, { signal: AbortSignal.timeout(1000) });
      if (health.ok) {
        ready = true;
        break;
      }
    } catch {
      /* Only this owned Gateway's readiness is being awaited. */
    }
    await pause(500);
  }
  assert.ok(ready, `Gateway readiness failed: ${gatewayLog}`);
  async function invoke(tool, args, sessionKey = "main") {
    const response = await fetch(`${endpoint}/tools/invoke`, {
      method: "POST",
      signal: AbortSignal.timeout(15000),
      headers: { "content-type": "application/json", authorization: "Bearer synthetic-gateway-token" },
      body: JSON.stringify({ tool, args, sessionKey }),
    });
    return { status: response.status, body: await response.json() };
  }
  const stored = await invoke("clickhouse_loom_save", { key: "editor", text: "Prefer compact output." });
  assert.equal(stored.status, 200, JSON.stringify(stored));
  assert.equal(stored.body.ok, true);
  assert.equal(stored.body.result.details.saved, true);
  const recalled = await invoke("clickhouse_loom_search", { query: "editor" });
  assert.equal(recalled.status, 200, JSON.stringify(recalled));
  assert.deepEqual(recalled.body.result.details.memories, [{ key: "editor", text: "Prefer compact output." }]);
  assert.match(saved.memory_key, /^openclaw\/[a-f0-9]{32}\/editor$/);
  assert.equal(requests.length, 2);
  const other = await invoke("clickhouse_loom_search", { query: "editor" }, "agent:other:main");
  assert.equal(other.status, 404, JSON.stringify(other));
  assert.equal(requests.length, 2, "another native agent must send no backend request");
  console.log(
    JSON.stringify({
      passed: true,
      host: "2026.11.6",
      real_package_install: true,
      real_gateway_tool_invocation: true,
      synthetic_backend_requests: 2,
      cross_agent_no_request: true,
      provider_calls: 0,
      actual_Loom_server_tested: false,
    }),
  );
} finally {
  if (gateway && gateway.exitCode === null) {
    const stopped = once(gateway, "exit");
    gateway.kill("SIGTERM");
    const timeout = setTimeout(() => gateway.kill("SIGKILL"), 10000);
    try {
      await stopped;
    } finally {
      clearTimeout(timeout);
    }
  }
  backend.closeAllConnections();
  await new Promise((resolveClose) => backend.close(resolveClose));
  await rm(directory, { recursive: true });
}
