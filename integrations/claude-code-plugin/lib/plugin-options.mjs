// The session's token → the one file every reader knows.
//
// `claude plugin install loom@loom --config server_url=… --config token=…`
// stores the values with Claude Code and exports them to hook processes as
// CLAUDE_PLUGIN_OPTION_SERVER_URL / CLAUDE_PLUGIN_OPTION_TOKEN. The MCP entry
// takes the URL by `${user_config.server_url}` substitution, but its header
// helper is not a hook: Claude Code gives a plugin's helper no option
// (measured on 2.1.273) and removes LOOM_API_KEY and LOOM_API_KEY_FILE from
// its environment (2.1.332), so there it reads ~/.loom/config.json, then
// ~/.loom/token. So the first hook of a session writes the token the hooks
// were given — LOOM_API_KEY, else the option, else the file LOOM_API_KEY_FILE
// names — and the URL beside it into ~/.loom/config.json when no token is
// saved, and the helper waits for it (bin/loom-mcp-headers.mjs). A saved
// token is never replaced (`installTokenNotice` is what the primer says
// then). Idempotent, 0600, never throws.

import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  agentMode,
  loomConfigPath,
  loomTokenPath,
  normalizeUrl,
  pluginDefaults,
  readConfigFile,
  writeConfigMerged,
} from "./loom-client.mjs";
import { readJsonFile, removeFile, tmpStatePath, writeJsonFile } from "./tmp-state.mjs";

/** The server Claude Code's MCP entry was told to call: the install option,
 *  else `LOOM_BASE_URL`, which the `server_url` default reads. "" when
 *  neither names one and the entry falls back to localhost. */
function namedServerUrl(env) {
  return normalizeUrl(env.CLAUDE_PLUGIN_OPTION_SERVER_URL || env.LOOM_BASE_URL);
}

/** The URL Claude Code's MCP entry calls. */
export function mcpServerUrl(env = process.env) {
  return namedServerUrl(env) || "http://localhost:7777";
}

/** The Codex plugin ships a fixed MCP entry (`plugin-defaults.json`), which
 *  the person may replace whole in Codex's own config; these scripts cannot
 *  read that, so under Codex nothing here reasons about the entry's URL. */
export function entryIsFixed() {
  return Boolean(pluginDefaults().server_url);
}

/** Whether the MCP header helper is given neither LOOM_API_KEY nor
 *  LOOM_API_KEY_FILE. Measured only for the entry of Claude Code's plugin,
 *  whose hooks run with CLAUDE_PLUGIN_ROOT; the status script's shell lacks
 *  it and passes what the hook saved as `plugin`. False for a hand-wired
 *  client, whose user-scope entry's helper keeps them, for Codex, which was
 *  not measured, and for an agent, whose tools send its own token. */
export function helperEnvHidden(env = process.env, plugin = Boolean(env.CLAUDE_PLUGIN_ROOT)) {
  return plugin && !entryIsFixed() && !agentMode(env);
}

/** Whether two base URLs name one server: origins compared, with the
 *  loopback spellings folded. Unparsable values compare as strings. */
export function sameServer(a, b) {
  const origin = (u) => {
    const parsed = new URL(normalizeUrl(u));
    const host = parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]" ? "localhost" : parsed.hostname;
    return `${parsed.protocol}//${host}${parsed.port ? `:${parsed.port}` : ""}`;
  };
  try {
    return origin(a) === origin(b);
  } catch {
    return normalizeUrl(a) === normalizeUrl(b);
  }
}

/** One state file per installed plugin: the Codex copy of these scripts
 *  keeps its own, and never writes it (Codex passes no option). */
const PLUGIN_ROOT = fileURLToPath(new URL("..", import.meta.url));
const installStatePath = (tmp) => tmpStatePath("install", PLUGIN_ROOT, { tmp });

/** The SessionStart hook saves, for the status script, what the agent's
 *  shell does not carry: the install option's URL, and whether Claude Code
 *  ran the hook as its plugin's. A session with neither removes a stale file. */
export function saveInstall(env = process.env, { tmp } = {}) {
  const state = { url: normalizeUrl(env.CLAUDE_PLUGIN_OPTION_SERVER_URL), plugin: Boolean(env.CLAUDE_PLUGIN_ROOT) };
  if (state.url || state.plugin) writeJsonFile(installStatePath(tmp), state);
  else removeFile(installStatePath(tmp));
}

/** Whether Claude Code ran the last SessionStart hook as its plugin's. */
export function ranAsPlugin({ tmp } = {}) {
  return readJsonFile(installStatePath(tmp), {}).plugin === true;
}

/** The saved install URL, "" when no session saved one. */
export function installServerUrl({ tmp } = {}) {
  return String(readJsonFile(installStatePath(tmp), {}).url || "");
}

/** The server the status script checks under Claude Code: the one the MCP
 *  entry calls, with the saved install URL standing in for the option the
 *  agent's shell lacks. (Under Codex the script checks the hooks' server,
 *  `resolveServerUrl`, and decides that itself.) */
export function statusServerUrl({ tmp } = {}) {
  return installServerUrl({ tmp }) || mcpServerUrl();
}

/** The token the hooks were given and the helper may not see: LOOM_API_KEY,
 *  else the install option, else `keyFileToken`. */
function givenToken(env, keyFileToken = "") {
  return String(env.LOOM_API_KEY || "").trim() || String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim() || keyFileToken;
}

/** What a run would do, without doing it: {write, url, reason}. `url` is
 *  the server the token is for, "" when nothing names one. `fileToken` is
 *  the content of the token file the helper reads. `envHidden`
 *  (`helperEnvHidden`): LOOM_API_KEY and `keyFileToken`, the content of the
 *  file LOOM_API_KEY_FILE names, are saved like the option; where the helper
 *  reads LOOM_API_KEY itself, it rules the option out. Pure. */
export function planMaterialize(env, { configExists, configToken, fileToken, keyFileToken = "", envHidden = false }) {
  if (!givenToken(env, keyFileToken)) return { write: false, reason: "no token to save" };
  if (!envHidden && String(env.LOOM_API_KEY || "").trim()) return { write: false, reason: "LOOM_API_KEY set" };
  if (String(configToken || "").trim()) return { write: false, reason: "config has a token" };
  if (String(fileToken || "").trim()) return { write: false, reason: "token file has a token" };
  const url = namedServerUrl(env);
  return { write: true, url, reason: configExists ? "config lacks a token" : "no config" };
}

/** The token in the token file at `path`; "" when it is missing or empty. */
function readTokenFile(path) {
  try {
    return readFileSync(path, "utf8").trim();
  } catch {
    return "";
  }
}

/** The token Claude Code's helper sends: the config's, else the one in
 *  ~/.loom/token (LOOM_API_KEY_FILE is kept from it too); "" with neither. */
export function helperToken(configToken, fileToken = readTokenFile(loomTokenPath({}))) {
  return String(configToken || "").trim() || fileToken;
}

/** The primer line when Claude Code's MCP header helper will not send the
 *  token the session was given. With that token in LOOM_API_KEY, or in the
 *  file LOOM_API_KEY_FILE names, and not saved (`plan.helperFinds`), the
 *  helper sends another saved token or none, and the fix is to save that
 *  token. Otherwise, when the config's token was saved for another server
 *  than the MCP entry calls, which refuses it: no notice when this hook has
 *  just written the config's token itself, or when the config names no
 *  server. That remedy depends on whether anything named the entry's server:
 *  when nothing did, the entry fell back to localhost and the fix is to name
 *  the config's server. Pure; never holds a token. */
export function installTokenNotice(plan, env = process.env) {
  if (plan.helperFinds) {
    const config = loomConfigPath(env);
    const name = String(env.LOOM_API_KEY || "").trim() ? "LOOM_API_KEY" : "LOOM_API_KEY_FILE";
    const found = plan.helperFinds === "another" ? "send another token saved there" : "find no token there";
    return (
      `Loom: the hooks use ${name}, which Claude Code does not pass to the loom MCP tools. The tools ` +
      `read ${config}, then ${loomTokenPath({})}, and ${found}. Tell the user once: save the ` +
      `${name} token as \`token\` in ${config} and restart Claude Code.`
    );
  }
  if (plan.write || !plan.configHasToken) return "";
  const saved = normalizeUrl(plan.configUrl);
  const inUse = mcpServerUrl(env);
  if (!saved || sameServer(saved, inUse)) return "";
  const where = `Loom: ${loomConfigPath(env)} holds a token saved for ${saved}, which the loom MCP tools send to `;
  if (!namedServerUrl(env)) {
    return (
      `${where}${inUse}, the MCP entry's default, since nothing names its server. Point the entry at ` +
      `${saved}: install with --config server_url=${saved}, or export LOOM_BASE_URL=${saved}, and restart.`
    );
  }
  return `${where}${inUse}. If the loom tools fail to connect, put a ${inUse} token in that file and restart.`;
}

/** Save the session's token for the helper when the plan says so. `envHidden`
 *  is `helperEnvHidden`'s answer: with it the token file is the one the
 *  helper reads, and the one LOOM_API_KEY_FILE names holds a token to save. */
export function applyPluginOptions(env = process.env, paths = {}, { envHidden = false } = {}) {
  const configPath = paths.configPath || loomConfigPath(env);
  const fileToken = readTokenFile(paths.tokenPath || loomTokenPath(envHidden ? {} : env));
  const keyFileToken = envHidden && env.LOOM_API_KEY_FILE ? readTokenFile(env.LOOM_API_KEY_FILE) : "";
  const configExists = existsSync(configPath);
  const existing = readConfigFile(configPath);
  const plan = planMaterialize(env, {
    configExists,
    configToken: existing.token,
    fileToken,
    keyFileToken,
    envHidden,
  });
  plan.configUrl = normalizeUrl(existing.url);
  plan.configHasToken = Boolean(String(existing.token || "").trim());
  // For the notice, the token the helper is not given: LOOM_API_KEY, else the
  // named file's unless the option outranks it for the hooks. "another" token
  // is what the helper sends in its place, or "no" token.
  const key = String(env.LOOM_API_KEY || "").trim();
  const option = String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  const hidden = envHidden ? key || (option ? "" : keyFileToken) : "";
  const sends = helperToken(existing.token, fileToken);
  let finds = "";
  if (hidden && sends !== hidden) finds = sends ? "another" : "no";
  if (!plan.write) return { ...plan, helperFinds: finds };
  try {
    // The token goes in beside the server named for it, over a url another
    // install left: the notice and the status check read them as a pair. A
    // url the file already holds stays when nothing names a server.
    const patch = { token: givenToken(env, keyFileToken) };
    if (plan.url) patch.url = plan.url;
    writeConfigMerged(configPath, patch);
    return { ...plan, path: configPath };
  } catch (e) {
    return { write: false, reason: `write failed: ${e && e.code ? e.code : "error"}`, helperFinds: finds };
  }
}
