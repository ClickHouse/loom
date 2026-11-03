// Install-time options → the one file every reader knows.
//
// `claude plugin install loom@loom --config server_url=… --config token=…`
// stores the values with Claude Code and exports them to hook processes as
// CLAUDE_PLUGIN_OPTION_SERVER_URL / CLAUDE_PLUGIN_OPTION_TOKEN. The MCP entry
// takes the URL by `${user_config.server_url}` substitution, but its header
// helper is not a hook and receives no option (measured on 2.1.273): it reads
// LOOM_API_KEY, then ~/.loom/config.json, then ~/.loom/token. So the first hook
// of a session writes the option token — and the URL beside it — into
// ~/.loom/config.json when nothing else already provides a token. Claude
// Code re-runs the helper on a 401 and retries the call once, so the first
// tool call of the first session finds it. Idempotent, 0600, never throws.

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
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

/** The SessionStart hook saves the install option's URL for the status
 *  script, which the agent runs from a shell that carries no option; an
 *  install without the option removes a stale one. */
export function saveInstallServerUrl(env = process.env, { tmp } = {}) {
  const url = normalizeUrl(env.CLAUDE_PLUGIN_OPTION_SERVER_URL);
  if (url) writeJsonFile(installStatePath(tmp), { url });
  else removeFile(installStatePath(tmp));
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

/** What a run would do, without doing it: {write, url, reason}. `url` is
 *  the server the option token is for, "" when nothing names one. Pure. */
export function planMaterialize(env, { configExists, configToken, tokenFileExists }) {
  const option = String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  if (!option) return { write: false, reason: "no option" };
  if (String(env.LOOM_API_KEY || "").trim()) return { write: false, reason: "LOOM_API_KEY set" };
  if (String(configToken || "").trim()) return { write: false, reason: "config has a token" };
  if (tokenFileExists) return { write: false, reason: "token file exists" };
  const url = namedServerUrl(env);
  return { write: true, url, reason: configExists ? "config lacks a token" : "no config" };
}

/** The primer line when the token the MCP header helper will send is the
 *  config's, saved for another server than Claude Code's MCP entry calls,
 *  which refuses it. The helper reads LOOM_API_KEY, then the config: no
 *  notice when the env var is set, when this hook has just written the
 *  config's token itself, or when the config names no server. The remedy
 *  depends on whether anything named the entry's server: when nothing did,
 *  the entry fell back to localhost and the fix is to name the config's
 *  server. Pure. */
export function installTokenNotice(plan, env = process.env) {
  if (String(env.LOOM_API_KEY || "").trim() || plan.write || !plan.configHasToken) return "";
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
  return `${where}${inUse}. If the loom tools fail to connect, put a ${inUse} token in that file or in LOOM_API_KEY and restart.`;
}

export function applyPluginOptions(env = process.env, paths = {}) {
  const configPath = paths.configPath || loomConfigPath(env);
  const tokenPath = paths.tokenPath || loomTokenPath(env);
  const configExists = existsSync(configPath);
  const existing = readConfigFile(configPath);
  const plan = planMaterialize(env, {
    configExists,
    configToken: existing.token,
    tokenFileExists: existsSync(tokenPath),
  });
  plan.configUrl = normalizeUrl(existing.url);
  plan.configHasToken = Boolean(String(existing.token || "").trim());
  if (!plan.write) return plan;
  try {
    // The token goes in beside the server named for it, over a url another
    // install left: the notice and the status check read them as a pair. A
    // url the file already holds stays when nothing names a server.
    const patch = { token: String(env.CLAUDE_PLUGIN_OPTION_TOKEN).trim() };
    if (plan.url) patch.url = plan.url;
    writeConfigMerged(configPath, patch);
    return { ...plan, path: configPath };
  } catch (e) {
    return { write: false, reason: `write failed: ${e && e.code ? e.code : "error"}` };
  }
}
