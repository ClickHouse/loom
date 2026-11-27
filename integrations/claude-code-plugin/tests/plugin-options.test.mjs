// The token the hooks are given (LOOM_API_KEY, the install option, the file
// LOOM_API_KEY_FILE names) is written once to ~/.loom/config.json for the
// header helper, which Claude Code gives none of them.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  applyPluginOptions,
  helperEnvHidden,
  installServerUrl,
  installTokenNotice,
  mcpServerUrl,
  planMaterialize,
  ranAsPlugin,
  sameServer,
  saveInstall,
  statusServerUrl,
} from "../lib/plugin-options.mjs";
import { assertMode } from "./host-env.mjs";

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "loom-opts-"));
}

test("helperEnvHidden: only where Claude Code ran the hook as its plugin's, and not for an agent", () => {
  assert.equal(helperEnvHidden({ CLAUDE_PLUGIN_ROOT: "/plugin" }), true);
  // a hand-wired client (hooks.generic.json) and Codex get no CLAUDE_PLUGIN_ROOT
  assert.equal(helperEnvHidden({}), false);
  assert.equal(helperEnvHidden({ LOOM_CLIENT: "claude-code" }), false);
  const agent = { LOOM_CLIENT_ID: "ci", LOOM_WORKLOAD_TOKEN: "proof" };
  assert.equal(helperEnvHidden({ CLAUDE_PLUGIN_ROOT: "/plugin", ...agent }), false);
  // the status script's shell lacks the variable: what the hook saved stands in
  assert.equal(helperEnvHidden({}, true), true);
  assert.equal(helperEnvHidden(agent, true), false);
});

test("planMaterialize: only when no token is saved, and LOOM_API_KEY only where the helper lacks it", () => {
  assert.equal(planMaterialize({}, { configExists: false, fileToken: "" }).write, false);
  assert.equal(
    planMaterialize({ CLAUDE_PLUGIN_OPTION_TOKEN: "t", LOOM_API_KEY: "e" }, { configExists: false, fileToken: "" })
      .reason,
    "LOOM_API_KEY set",
  );
  assert.equal(
    planMaterialize({ LOOM_API_KEY: "e" }, { configExists: false, fileToken: "" }).reason,
    "LOOM_API_KEY set",
  );
  const hidden = { configExists: false, fileToken: "", envHidden: true };
  assert.deepEqual(planMaterialize({ LOOM_API_KEY: "e", LOOM_BASE_URL: "https://h/" }, hidden), {
    write: true,
    url: "https://h",
    reason: "no config",
  });
  assert.equal(planMaterialize({ LOOM_API_KEY: "  " }, hidden).reason, "no token to save");
  assert.equal(planMaterialize({ LOOM_API_KEY: "e" }, { ...hidden, configToken: "c" }).reason, "config has a token");
  assert.equal(planMaterialize({ LOOM_API_KEY: "e" }, { ...hidden, fileToken: "f" }).reason, "token file has a token");
  // the file LOOM_API_KEY_FILE names is a token to save, after the variable and the option
  assert.equal(planMaterialize({}, { ...hidden, keyFileToken: "k" }).write, true);
  assert.equal(planMaterialize({}, { ...hidden, keyFileToken: "k", fileToken: "f" }).reason, "token file has a token");
  assert.equal(
    planMaterialize({ CLAUDE_PLUGIN_OPTION_TOKEN: "t" }, { configExists: true, configToken: "c", fileToken: "" })
      .reason,
    "config has a token",
  );
  assert.equal(
    planMaterialize({ CLAUDE_PLUGIN_OPTION_TOKEN: "t" }, { configExists: false, fileToken: "f" }).reason,
    "token file has a token",
  );
  const p = planMaterialize(
    { CLAUDE_PLUGIN_OPTION_TOKEN: "t", CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://h/" },
    { configExists: false, fileToken: "" },
  );
  assert.deepEqual(p, { write: true, url: "https://h", reason: "no config" });
  // the url saved beside the token is the MCP entry's: the option outranks LOOM_BASE_URL
  assert.equal(
    planMaterialize(
      { CLAUDE_PLUGIN_OPTION_TOKEN: "t", LOOM_BASE_URL: "https://env", CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://opt" },
      { configExists: false, fileToken: "" },
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
  assertMode(configPath, 0o600);
  // second run: the config now has a token → nothing written; the saved url rides along
  const r2 = applyPluginOptions(env, { configPath, tokenPath });
  assert.equal(r2.reason, "config has a token");
  assert.equal(r2.configUrl, "https://loom.test");
  assert.equal(r2.configHasToken, true);
  // a config with other keys but no token is merged; the url follows the token it is for
  fs.writeFileSync(configPath, JSON.stringify({ namespace: "keep", url: "https://mine" }));
  const r3 = applyPluginOptions(env, { configPath, tokenPath });
  assert.equal(r3.write, true);
  assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), {
    namespace: "keep",
    url: "https://loom.test",
    token: "opt.tok",
  });
  // nothing names a server: the token goes in and the url the file holds stays
  fs.writeFileSync(configPath, JSON.stringify({ url: "https://mine" }));
  const r4 = applyPluginOptions({ CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok" }, { configPath, tokenPath });
  assert.equal(r4.write, true);
  assert.equal(r4.url, "");
  assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), { url: "https://mine", token: "opt.tok" });
  // a token file present → not written
  fs.unlinkSync(configPath);
  fs.writeFileSync(tokenPath, "file.tok\n");
  assert.equal(applyPluginOptions(env, { configPath, tokenPath }).reason, "token file has a token");
  assert.equal(fs.existsSync(configPath), false);
});

test("applyPluginOptions saves LOOM_API_KEY where the helper lacks it, and never over a saved token", () => {
  const dir = tmp();
  const configPath = path.join(dir, ".loom", "config.json");
  const tokenPath = path.join(dir, ".loom", "token");
  const paths = { configPath, tokenPath };
  const env = { LOOM_API_KEY: "env.tok", CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok", LOOM_BASE_URL: "https://loom.test" };
  // the helper reads the variable itself (Codex, an agent): nothing is written or said
  const seen = applyPluginOptions(env, paths);
  assert.equal(seen.reason, "LOOM_API_KEY set");
  assert.equal(seen.helperFinds, "");
  assert.equal(fs.existsSync(configPath), false);
  // Claude Code's helper lacks it: the hooks' token (the variable outranks the option) is saved
  const saved = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(saved.write, true);
  assert.equal(JSON.stringify(saved).includes("env.tok"), false, "the plan holds no token");
  assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), { token: "env.tok", url: "https://loom.test" });
  assertMode(configPath, 0o600);
  assertMode(path.dirname(configPath), 0o700);
  // the next session finds that token saved: nothing to write or say
  const again = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(again.reason, "config has a token");
  assert.equal(again.helperFinds, "");
  // a rotated variable never replaces the saved token: the helper sends another one
  const rotated = applyPluginOptions({ ...env, LOOM_API_KEY: "new.tok" }, paths, { envHidden: true });
  assert.equal(rotated.helperFinds, "another");
  assert.equal(JSON.parse(fs.readFileSync(configPath, "utf8")).token, "env.tok");
  // a token file stands: the same token there is what the helper sends, another one is named
  fs.unlinkSync(configPath);
  fs.writeFileSync(tokenPath, "env.tok\n");
  assert.equal(applyPluginOptions(env, paths, { envHidden: true }).helperFinds, "");
  fs.writeFileSync(tokenPath, "file.tok\n");
  const beside = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(beside.reason, "token file has a token");
  assert.equal(beside.helperFinds, "another");
  assert.equal(fs.existsSync(configPath), false);
  // an empty token file holds nothing for the helper: the token is saved
  fs.writeFileSync(tokenPath, "\n");
  assert.equal(applyPluginOptions(env, paths, { envHidden: true }).write, true);
  assert.equal(JSON.parse(fs.readFileSync(configPath, "utf8")).token, "env.tok");
  fs.unlinkSync(configPath);
  // a write that fails leaves the helper without a token, and says so
  fs.unlinkSync(tokenPath);
  fs.mkdirSync(configPath);
  const failed = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(failed.write, false);
  assert.match(failed.reason, /^write failed/);
  assert.equal(failed.helperFinds, "no");
});

test("applyPluginOptions reads the token file the helper reads, not the one LOOM_API_KEY_FILE names", () => {
  const dir = tmp();
  const configPath = path.join(dir, ".loom", "config.json");
  const named = path.join(dir, "secret");
  fs.writeFileSync(named, "env.tok\n");
  const env = { LOOM_API_KEY: "env.tok", LOOM_API_KEY_FILE: named, LOOM_CONFIG_FILE: configPath };
  // Claude Code keeps LOOM_API_KEY_FILE from the helper too: that file saves the helper nothing
  const saved = applyPluginOptions(env, {}, { envHidden: true });
  assert.equal(saved.write, true);
  assert.equal(JSON.parse(fs.readFileSync(configPath, "utf8")).token, "env.tok");
  // where the helper is given the variables, the named file is a saved token
  fs.unlinkSync(configPath);
  const option = { CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok", LOOM_API_KEY_FILE: named, LOOM_CONFIG_FILE: configPath };
  assert.equal(applyPluginOptions(option).reason, "token file has a token");
  assert.equal(fs.existsSync(configPath), false);
});

test("applyPluginOptions saves the token of the file LOOM_API_KEY_FILE names where the helper lacks it", () => {
  const dir = tmp();
  const configPath = path.join(dir, ".loom", "config.json");
  const tokenPath = path.join(dir, ".loom", "token");
  const paths = { configPath, tokenPath };
  const named = path.join(dir, "secret");
  fs.writeFileSync(named, "key.tok\n");
  const env = { LOOM_API_KEY_FILE: named, LOOM_BASE_URL: "https://loom.test" };
  // the helper reads the named file itself: nothing to save
  assert.equal(applyPluginOptions(env, paths).reason, "no token to save");
  assert.equal(fs.existsSync(configPath), false);
  // a token file the helper reads stands, and the primer names the other token it sends
  fs.mkdirSync(path.dirname(tokenPath));
  fs.writeFileSync(tokenPath, "file.tok\n");
  const beside = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(beside.reason, "token file has a token");
  assert.equal(beside.helperFinds, "another");
  assert.ok(installTokenNotice(beside, env).includes("the hooks use LOOM_API_KEY_FILE, which Claude Code"));
  assert.ok(installTokenNotice(beside, env).includes("save the LOOM_API_KEY_FILE token as `token` in"));
  // the option outranks the named file in the hooks' lookup: the file's token is not theirs
  const option = applyPluginOptions({ ...env, CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok" }, paths, { envHidden: true });
  assert.equal(option.helperFinds, "");
  fs.unlinkSync(tokenPath);
  // nothing saved: the named file's token goes where the helper reads
  const saved = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(saved.write, true);
  assert.equal(JSON.stringify(saved).includes("key.tok"), false, "the plan holds no token");
  assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), { token: "key.tok", url: "https://loom.test" });
  assertMode(configPath, 0o600);
  // the saved copy is never replaced: once the file changes, the primer says the helper sends another token
  fs.writeFileSync(named, "new.tok\n");
  const rotated = applyPluginOptions(env, paths, { envHidden: true });
  assert.equal(rotated.reason, "config has a token");
  assert.equal(rotated.helperFinds, "another");
  assert.equal(JSON.parse(fs.readFileSync(configPath, "utf8")).token, "key.tok");
  const stale = installTokenNotice(rotated, { ...env, LOOM_CONFIG_FILE: configPath });
  assert.ok(stale.includes("the hooks use LOOM_API_KEY_FILE, which Claude Code"), stale);
  assert.ok(stale.includes(`save the LOOM_API_KEY_FILE token as \`token\` in ${configPath}`), stale);
  assert.equal(/key\.tok|new\.tok/.test(stale), false);
  // the option outranks both for the hooks: the named file is not compared
  const optioned = { ...env, CLAUDE_PLUGIN_OPTION_TOKEN: "opt.tok" };
  assert.equal(applyPluginOptions(optioned, paths, { envHidden: true }).helperFinds, "");
  // the same token in both: nothing to say
  fs.writeFileSync(named, "key.tok\n");
  assert.equal(applyPluginOptions(env, paths, { envHidden: true }).helperFinds, "");
  // an unreadable or empty named file holds no token
  assert.equal(
    applyPluginOptions({ LOOM_API_KEY_FILE: path.join(dir, "absent") }, {}, { envHidden: true }).reason,
    "no token to save",
  );
});

test("installTokenNotice: LOOM_API_KEY the helper will not send", () => {
  const env = { LOOM_API_KEY: "env.tok", LOOM_CONFIG_FILE: "/h/.loom/config.json" };
  const other = installTokenNotice({ write: false, reason: "config has a token", helperFinds: "another" }, env);
  assert.ok(other.includes("which Claude Code does not pass to the loom MCP tools"), other);
  assert.ok(other.includes("read /h/.loom/config.json, then "), other);
  assert.ok(other.includes("and send another token saved there."), other);
  assert.ok(other.includes("save the LOOM_API_KEY token as `token` in /h/.loom/config.json and restart"), other);
  const none = installTokenNotice({ write: false, reason: "write failed: EACCES", helperFinds: "no" }, env);
  assert.ok(none.includes("and find no token there."), none);
  assert.equal((other + none).includes("env.tok"), false);
});

test("installTokenNotice: when the helper will send a config token saved for another server", () => {
  const stale = { write: false, reason: "no option", configUrl: "https://old", configHasToken: true };
  const named = { CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://new/" };
  const line = installTokenNotice(stale, named);
  assert.ok(line.includes("saved for https://old, which the loom MCP tools send to https://new."), line);
  assert.ok(line.includes("put a https://new token in that file"), line);
  // LOOM_BASE_URL names the entry's server too
  assert.ok(installTokenNotice(stale, { LOOM_BASE_URL: "https://new" }).includes("send to https://new."));
  // with a token option the skip reason differs; the same config still misleads the helper
  assert.ok(installTokenNotice({ ...stale, reason: "config has a token" }, named).includes("https://old"));
  // nothing names the entry's server: it fell back to localhost, and the fix is to name the config's server
  const fallback = installTokenNotice(stale, {});
  assert.ok(fallback.includes("send to http://localhost:7777, the MCP entry's default"), fallback);
  assert.ok(fallback.includes("--config server_url=https://old, or export LOOM_BASE_URL=https://old"), fallback);
  assert.equal(fallback.includes("localhost:7777 token"), false);
  assert.equal(installTokenNotice({ ...stale, configUrl: "https://new" }, named), "");
  assert.equal(installTokenNotice({ ...stale, configUrl: "" }, named), "");
  assert.equal(installTokenNotice({ ...stale, configHasToken: false }, named), "");
  assert.equal(line.includes("LOOM_API_KEY"), false, "the helper is not given the variable");
  // the helper sends the config's token whatever LOOM_API_KEY holds
  assert.ok(installTokenNotice(stale, { ...named, LOOM_API_KEY: "e" }).includes("send to https://new."));
  // the hook has just written the install token into the config: that token is the right one
  assert.equal(installTokenNotice({ ...stale, write: true, reason: "config lacks a token" }, named), "");
});

test("mcpServerUrl follows the MCP entry: the option, then LOOM_BASE_URL, then localhost", () => {
  assert.equal(
    mcpServerUrl({ CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://opt/", LOOM_BASE_URL: "https://env" }),
    "https://opt",
  );
  assert.equal(mcpServerUrl({ LOOM_BASE_URL: "https://env/" }), "https://env");
  assert.equal(mcpServerUrl({}), "http://localhost:7777");
});

test("sameServer compares origins and folds the loopback spellings", () => {
  assert.equal(sameServer("http://127.0.0.1:7777", "http://localhost:7777/"), true);
  assert.equal(sameServer("https://a.example/", "https://A.example"), true);
  assert.equal(sameServer("https://a.example", "https://a.example:8443"), false);
  assert.equal(sameServer("https://a.example", "http://a.example"), false);
  assert.equal(sameServer("not a url", "not a url/"), true); // unparsable: string compare
  assert.equal(sameServer("", "http://localhost:7777"), false);
});

test("the hook saves the install URL and whether it ran as the plugin's for the status script", () => {
  const state = { tmp: tmp() };
  const baseUrl = process.env.LOOM_BASE_URL;
  try {
    assert.equal(installServerUrl(state), "");
    assert.equal(ranAsPlugin(state), false);
    saveInstall({ CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://b/" }, state);
    assert.equal(installServerUrl(state), "https://b");
    assert.equal(ranAsPlugin(state), false);
    // the MCP entry's order: the saved option first, then LOOM_BASE_URL
    process.env.LOOM_BASE_URL = "https://env/";
    assert.equal(statusServerUrl(state), "https://b");
    // the plugin's hook without the option: no URL, and the record that it ran
    saveInstall({ CLAUDE_PLUGIN_ROOT: "/plugin" }, state);
    assert.equal(installServerUrl(state), "");
    assert.equal(ranAsPlugin(state), true);
    // a hand-wired hook with neither removes the file
    saveInstall({}, state);
    assert.deepEqual(fs.readdirSync(state.tmp), []);
    assert.equal(ranAsPlugin(state), false);
    assert.equal(installServerUrl(state), "");
    assert.equal(statusServerUrl(state), "https://env");
    delete process.env.LOOM_BASE_URL;
    // nothing saved or set: the MCP entry's default, never the config's url
    assert.equal(statusServerUrl(state), mcpServerUrl(process.env));
    assert.equal(statusServerUrl(state), "http://localhost:7777");
  } finally {
    if (baseUrl === undefined) delete process.env.LOOM_BASE_URL;
    else process.env.LOOM_BASE_URL = baseUrl;
    fs.rmSync(state.tmp, { recursive: true, force: true });
  }
});

test("resolvers: the option ranks after the env var and before the files, the named file before the config", async () => {
  const dir = tmp();
  fs.mkdirSync(path.join(dir, ".loom"));
  fs.writeFileSync(path.join(dir, ".loom", "config.json"), JSON.stringify({ token: "cfg.tok", url: "https://cfg" }));
  fs.writeFileSync(path.join(dir, "secret"), "key.tok\n");
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
    process.env.LOOM_API_KEY_FILE = path.join(dir, "secret");
    assert.equal(m.resolveToken(), "opt.tok");
    delete process.env.CLAUDE_PLUGIN_OPTION_TOKEN;
    assert.deepEqual(m.tokenLookup(), { token: "key.tok", kind: "file" });
    // a named file that holds no token leaves the config's
    process.env.LOOM_API_KEY_FILE = path.join(dir, "absent");
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
  assert.equal(
    mcp.mcpServers.loom.url,
    "${user_config.server_url}/mcp?toolset=${user_config.toolset}#${CLAUDE_PROJECT_DIR}",
  );
  assert.match(mcp.mcpServers.loom.headersHelper, /loom-mcp-headers\.mjs/);
});
