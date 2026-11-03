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

/** The install option's URL, else `LOOM_BASE_URL`: the order of the MCP
 *  entry's `${user_config.server_url}`, whose default is
 *  `${LOOM_BASE_URL:-http://localhost:7777}` (`.mcp.json`). "" when neither. */
function optionServerUrl(env) {
  return normalizeUrl(env.CLAUDE_PLUGIN_OPTION_SERVER_URL || env.LOOM_BASE_URL);
}

/** The URL the plugin's MCP entry calls: the option, then `LOOM_BASE_URL`,
 *  then the plugin's shipped default (the Codex plugin's fixed entry), then
 *  localhost. */
export function mcpServerUrl(env = process.env) {
  return optionServerUrl(env) || normalizeUrl(pluginDefaults().server_url) || "http://localhost:7777";
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

/** The server the status script checks: the one the MCP entry calls, with
 *  the saved install URL standing in for the option the agent's shell lacks. */
export function statusServerUrl({ tmp } = {}) {
  return installServerUrl({ tmp }) || mcpServerUrl();
}

/** What a run would do, without doing it: {write, url, reason}. Pure. */
export function planMaterialize(env, { configExists, configToken, tokenFileExists }) {
  const option = String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  if (!option) return { write: false, reason: "no option" };
  if (String(env.LOOM_API_KEY || "").trim()) return { write: false, reason: "LOOM_API_KEY set" };
  if (String(configToken || "").trim()) return { write: false, reason: "config has a token" };
  if (tokenFileExists) return { write: false, reason: "token file exists" };
  // The token is saved beside the server its MCP entry sends it to.
  const url = mcpServerUrl(env);
  return { write: true, url, reason: configExists ? "config lacks a token" : "no config" };
}

/** The primer line when the token the MCP header helper will send is the
 *  config's, saved for another server than `mcpUrl`, the one the MCP entry
 *  calls, which refuses it. The helper reads LOOM_API_KEY, then the config:
 *  no notice when the env var is set, when this hook has just written the
 *  config's token itself, or when the config names no server. Pure. */
export function installTokenNotice(plan, mcpUrl, env = process.env) {
  if (String(env.LOOM_API_KEY || "").trim() || plan.write || !plan.configHasToken) return "";
  const saved = normalizeUrl(plan.configUrl);
  const inUse = normalizeUrl(mcpUrl);
  if (!saved || !inUse || saved === inUse) return "";
  return (
    `Loom: ${loomConfigPath(env)} holds a token saved for ${saved}, which the loom MCP tools send to ` +
    `${inUse}. If the loom tools fail to connect, put a ${inUse} token in that file or in ` +
    `LOOM_API_KEY and restart.`
  );
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
    // The token and the url it is for go in as a pair, over a url another
    // install left: the notice and the status check read them together.
    writeConfigMerged(configPath, { token: String(env.CLAUDE_PLUGIN_OPTION_TOKEN).trim(), url: plan.url });
    return { ...plan, path: configPath };
  } catch (e) {
    return { write: false, reason: `write failed: ${e && e.code ? e.code : "error"}` };
  }
}
