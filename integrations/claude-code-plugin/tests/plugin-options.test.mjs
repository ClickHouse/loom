// Install-time options reach the hooks as env and are written
// once to ~/.loom/config.json for the header helper.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  applyPluginOptions,
  installServerUrl,
  installTokenNotice,
  mcpServerUrl,
  planMaterialize,
  saveInstallServerUrl,
  statusServerUrl,
} from "../lib/plugin-options.mjs";
import { resolveServerUrl } from "../lib/loom-client.mjs";

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "loom-opts-"));
}

test("planMaterialize: only when the option is the sole token source", () => {
  assert.equal(planMaterialize({}, { configExists: false, tokenFileExists: false }).write, false);
  assert.equal(
    planMaterialize(
      { CLAUDE_PLUGIN_OPTION_TOKEN: "t", LOOM_API_KEY: "e" },
      { configExists: false, tokenFileExists: false },
    ).reason,
    "LOOM_API_KEY set",
  );
  assert.equal(
    planMaterialize(
      { CLAUDE_PLUGIN_OPTION_TOKEN: "t" },
      { configExists: true, configToken: "c", tokenFileExists: false },
    ).reason,
    "config has a token",
  );
  assert.equal(
    planMaterialize({ CLAUDE_PLUGIN_OPTION_TOKEN: "t" }, { configExists: false, tokenFileExists: true }).reason,
    "token file exists",
  );
  const p = planMaterialize(
    { CLAUDE_PLUGIN_OPTION_TOKEN: "t", CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://h/" },
    { configExists: false, tokenFileExists: false },
  );
  assert.deepEqual(p, { write: true, url: "https://h", reason: "no config" });
  // the url saved beside the token is the MCP entry's: the option outranks LOOM_BASE_URL
  assert.equal(
    planMaterialize(
      { CLAUDE_PLUGIN_OPTION_TOKEN: "t", LOOM_BASE_URL: "https://env", CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://opt" },
      { configExists: false, tokenFileExists: false },
    ).url,
    "https://opt",
  );
});

test("applyPluginOptions writes token + url once, 0600, merging other keys", () => {
  const dir = tmp();
  const configPath = path.join(dir, ".loom", "config.json");
  const tokenPath = path.join(dir, ".loom", "token");
  const env = { CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok", CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.test/" };
  const r1 = applyPluginOptions(env, { configPath, tokenPath });
  assert.equal(r1.write, true);
  const written = JSON.parse(fs.readFileSync(configPath, "utf8"));
  assert.deepEqual(written, { token: "opt.tok", url: "https://loom.test" });
  assert.equal(fs.statSync(configPath).mode & 0o777, 0o600);
  // second run: the config now has a token → nothing written; the saved url rides along
  const r2 = applyPluginOptions(env, { configPath, tokenPath });
  assert.equal(r2.reason, "config has a token");
  assert.equal(r2.configUrl, "https://loom.test");
  // a config with other keys but no token is merged, its own url kept
  fs.writeFileSync(configPath, JSON.stringify({ namespace: "keep", url: "https://mine" }));
  const r3 = applyPluginOptions(env, { configPath, tokenPath });
  assert.equal(r3.write, true);
  assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), {
    namespace: "keep",
    url: "https://mine",
    token: "opt.tok",
  });
  // a token file present → not written
  fs.unlinkSync(configPath);
  fs.writeFileSync(tokenPath, "file.tok\n");
  assert.equal(applyPluginOptions(env, { configPath, tokenPath }).reason, "token file exists");
  assert.equal(fs.existsSync(configPath), false);
});

test("installTokenNotice: only when the config token was saved for another server", () => {
  const skipped = { write: false, reason: "config has a token", configUrl: "https://old" };
  assert.match(installTokenNotice(skipped, "https://new/"), /saved for https:\/\/old.*send to https:\/\/new;/);
  assert.equal(installTokenNotice({ ...skipped, configUrl: "https://new" }, "https://new"), "");
  assert.equal(installTokenNotice({ ...skipped, configUrl: "" }, "https://new"), "");
  assert.equal(installTokenNotice({ write: false, reason: "token file exists" }, "https://new"), "");
  assert.equal(installTokenNotice({ write: true, url: "https://new", reason: "no config" }, "https://new"), "");
});

test("mcpServerUrl follows the MCP entry: the option, then LOOM_BASE_URL, then localhost", () => {
  assert.equal(
    mcpServerUrl({ CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://opt/", LOOM_BASE_URL: "https://env" }),
    "https://opt",
  );
  assert.equal(mcpServerUrl({ LOOM_BASE_URL: "https://env/" }), "https://env");
  assert.equal(mcpServerUrl({}), "http://localhost:7777");
});

test("the hook saves the install URL for the status script, removes it when the option is gone", () => {
  // The state path is fixed at import (keyed by the plugin root), so the
  // round trip runs in the real tmp and removes its own file.
  const before = installServerUrl();
  try {
    saveInstallServerUrl({ CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://b/" });
    assert.equal(installServerUrl(), "https://b");
    assert.equal(statusServerUrl({}), "https://b");
    assert.equal(statusServerUrl({ LOOM_BASE_URL: "https://env/" }), "https://env");
    saveInstallServerUrl({});
    assert.equal(installServerUrl(), "");
    assert.equal(statusServerUrl({}), resolveServerUrl()); // the hooks' server, whatever this machine's is
  } finally {
    if (before) saveInstallServerUrl({ CLAUDE_PLUGIN_OPTION_SERVER_URL: before });
  }
});

test("resolvers: the option ranks after the env var and before the files", async () => {
  const dir = tmp();
  fs.mkdirSync(path.join(dir, ".loom"));
  fs.writeFileSync(path.join(dir, ".loom", "config.json"), JSON.stringify({ token: "cfg.tok", url: "https://cfg" }));
  const saved = { ...process.env };
  try {
    process.env.LOOM_CONFIG_FILE = path.join(dir, ".loom", "config.json");
    delete process.env.LOOM_API_KEY;
    delete process.env.LOOM_BASE_URL;
    process.env.CLAUDE_PLUGIN_OPTION_TOKEN = "opt.tok";
    process.env.CLAUDE_PLUGIN_OPTION_SERVER_URL = "https://opt/";
    const m = await import(`../lib/loom-client.mjs?t=${Date.now()}`);
    assert.equal(m.resolveToken(), "opt.tok");
    assert.equal(m.tokenLookup().kind, "option");
    assert.equal(m.resolveServerUrl(), "https://opt");
    delete process.env.CLAUDE_PLUGIN_OPTION_TOKEN;
    assert.equal(m.resolveToken(), "cfg.tok");
    assert.equal(m.tokenLookup().kind, "config");
    process.env.LOOM_API_KEY = "env.tok";
    process.env.LOOM_BASE_URL = "https://env";
    assert.equal(m.resolveToken(), "env.tok");
    assert.equal(m.tokenLookup().kind, "env");
    assert.equal(m.resolveServerUrl(), "https://env");
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});

test("the manifest declares the options and the MCP url takes the server and toolset ones", () => {
  const m = JSON.parse(fs.readFileSync(new URL("../.claude-plugin/plugin.json", import.meta.url), "utf8"));
  assert.equal(m.userConfig.server_url.default, "${LOOM_BASE_URL:-http://localhost:7777}");
  assert.equal(m.userConfig.token.sensitive, true);
  assert.equal(m.userConfig.token.required, undefined);
  // The listing is a setting; `coding` unless the user asks for a union
  assert.equal(m.userConfig.toolset.default, "coding");
  assert.match(m.userConfig.toolset.description, /coding\+tracker/);
  const mcp = JSON.parse(fs.readFileSync(new URL("../.mcp.json", import.meta.url), "utf8"));
  assert.equal(mcp.mcpServers.loom.url, "${user_config.server_url}/mcp?toolset=${user_config.toolset}");
  assert.match(mcp.mcpServers.loom.headersHelper, /loom-mcp-headers\.mjs/);
});
